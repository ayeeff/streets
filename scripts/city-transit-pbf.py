#!/usr/bin/env python3
"""Extract per-city OSM transit (routes + stops) from a country PBF.

Single streaming pass, same shape as city-streets-pbf.py. A PBF stores all
nodes, then all ways, then all relations, so one pass suffices:

  1. node()     index the coords of every node inside some city box (numpy,
                same packed int64 trick as the street extractor), so memory
                stays bounded no matter how big the country file is.
  2. way()      a way is only touched if it is a member of some route relation.
                We cannot know that until relations are read (they come last),
                so instead of buffering geometries we keep, per city box, a
                bounded sample of candidate ways and resolve their node refs
                when the relation arrives.
  3. relation() pick route relations (type=route / type=public_transport with a
                recognised `route` value), then stitch the member ways into
                polylines from the node index, clipped to the city box.

Why not reuse the planet-wide workflow: .github/workflows/osm-route-relations.yml
downloads the whole 95 GB planet.pbf per run. Running per country over the
74 Geofabrik extracts already staged in R2 is far cheaper, and each file fits a
CI runner.

usage: city-transit-pbf.py <pbf> <cities.json> <out.json> [max_routes_per_city]
"""

import json
import sys
import time
import unicodedata
from bisect import bisect_right

import numpy as np
import osmium

# OSM `route` values worth keeping, mapped to a coarse mode we can compare on.
ROUTE_MODES = {
    "bus": "bus",
    "trolleybus": "bus",
    "coach": "bus",
    "tram": "tram",
    "light_rail": "tram",
    "monorail": "tram",
    "subway": "metro",
    "metro": "metro",
    "train": "rail",
    "rail": "rail",
    "highway": "rail",
    "ferry": "ferry",
}

# Nodes that act as a boarding point even when not a relation member.
STOP_TAGS = (
    ("public_transport", "platform"),
    ("highway", "bus_stop"),
    ("railway", "station"),
    ("railway", "halt"),
)

CHUNK_NODES = 4_000_000
# Geometry budget per route. Bounding TOTAL points rather than a span count
# matters: a 4-span cap truncated 92% of routes (a metro line is many
# disconnected ways inside a city box, so span count says nothing about length).
# A metro line and a 400 m bus spur both get a fair share of the budget.
MAX_SPANS_PER_ROUTE = 10
MAX_POINTS_PER_ROUTE = 80
MAX_STOPS_PER_CITY = 3000
# Upper bound on buffered route-candidate ways. See the way() reject: keying on
# highway/railway tags keeps this far out of reach in practice; this is only a
# backstop against a pathological file.
WAY_CANDIDATE_CAP = 3_000_000


def canon(name):
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def half_degrees(pop):
    pop_m = max(float(pop or 0), 50_000) / 1_000_000
    half_lat = 0.115 + min(0.20, (pop_m ** 0.5) / 320.0)
    return half_lat, half_lat * 1.25


class Extractor(osmium.SimpleHandler):
    def __init__(self, boxes, max_routes=400):
        super().__init__()
        ordered = sorted(boxes, key=lambda b: b["min_lat"])
        self.state = {
            "boxes": ordered,
            "min_lats": [b["min_lat"] for b in ordered],
            "span": max((b["max_lat"] - b["min_lat"]) for b in ordered) if ordered else 0,
            "gmin_lat": min(b["min_lat"] for b in ordered),
            "gmax_lat": max(b["max_lat"] for b in ordered),
            "gmin_lng": min(b["min_lng"] for b in ordered),
            "gmax_lng": max(b["max_lng"] for b in ordered),
            "id_chunks": [],
            "coord_chunks": [],
            "buf_ids": np.empty(CHUNK_NODES, dtype=np.int64),
            "buf_coord": np.empty(CHUNK_NODES, dtype=np.int64),
            "buf_n": 0,
            "sorted_ids": None,
            "sorted_coord": None,
            "finalized": False,
            "way_refs": [],          # candidate way ids, in file order
            "refs_by_id": {},        # way id -> node refs, for route members only
            "stops": {},             # city slug -> list of stop dicts
            "routes": {},            # city slug -> list of route dicts
            "node_count": 0,
            "boxed_nodes": 0,
            "ways_seen": 0,
            "candidates": 0,
            "candidates_capped": 0,
            "stops_found": 0,
            "routes_found": 0,
            "routes_skipped_cap": 0,
            "t0": time.time(),
            "max_routes": max_routes,
        }

        st = self.state
        boxes_l = ordered
        min_lats = st["min_lats"]
        span = st["span"]
        gmin_lat, gmax_lat = st["gmin_lat"], st["gmax_lat"]
        gmin_lng, gmax_lng = st["gmin_lng"], st["gmax_lng"]
        buf_ids, buf_coord = st["buf_ids"], st["buf_coord"]

        def node(n):
            st["node_count"] += 1
            lat = n.location.lat
            if lat < gmin_lat or lat > gmax_lat:
                return
            lng = n.location.lon
            if lng < gmin_lng or lng > gmax_lng:
                return
            lo_bound = lat - span
            i = bisect_right(min_lats, lat) - 1
            inside = False
            while i >= 0 and min_lats[i] >= lo_bound:
                if min_lats[i] <= lat <= (min_lats[i] + span):
                    b = boxes_l[i]
                    if b["min_lat"] <= lat <= b["max_lat"] and b["min_lng"] <= lng <= b["max_lng"]:
                        inside = True
                        break
                i -= 1
            if not inside:
                return
            k = st["buf_n"]
            if k >= CHUNK_NODES:
                st["id_chunks"].append(buf_ids.copy())
                st["coord_chunks"].append(buf_coord.copy())
                k = 0
            buf_ids[k] = n.id
            buf_coord[k] = (int(round(lat * 1e7)) << 32) | (int(round(lng * 1e7)) & 0xFFFFFFFF)
            st["buf_n"] = k + 1
            st["boxed_nodes"] += 1

        self.node = node

        finalize = self.finalize_nodes
        lookup = self.lookup

        def in_box(pt, bx):
            return (bx["min_lat"] <= pt[0] <= bx["max_lat"]
                    and bx["min_lng"] <= pt[1] <= bx["max_lng"])

        def way(w):
            finalize()
            st["ways_seen"] += 1
            nodes = w.nodes
            n = len(nodes)
            if not n:
                return
            # Cheap reject first, and it is essential rather than an
            # optimisation: route relations reference a small slice of all ways,
            # but the ways crossing a city box number in the millions for a
            # large country. Route member ways essentially always carry
            # highway= or railway=, so keying on that collapses the candidate
            # set by orders of magnitude before anything is buffered.
            tags = w.tags
            if not (tags.get("highway") or tags.get("railway") or tags.get("route")):
                return
            if len(st["refs_by_id"]) >= WAY_CANDIDATE_CAP:
                st["candidates_capped"] += 1
                return
            # Sample up to 9 refs, for the same reason as the street extractor:
            # a long arterial can have both endpoints outside the box.
            if n <= 9:
                probes = [nodes[i].ref for i in range(n)]
            else:
                step = (n - 1) / 8.0
                probes = [nodes[int(round(i * step))].ref for i in range(9)]
            hit = False
            for ref in probes:
                pt = lookup(ref)
                if pt is None:
                    continue
                for b in boxes_l:
                    if in_box(pt, b):
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                return
            st["refs_by_id"][w.id] = [nd.ref for nd in nodes]
            st["candidates"] += 1

        self.way = way

        def relation(r):
            tags = r.tags
            rtype = tags.get("type", "")
            if rtype not in ("route", "public_transport"):
                return
            route = tags.get("route", "")
            if rtype == "public_transport" and not route:
                # newer tagging: public_transport=rail + route absent
                route = tags.get("public_transport", "")
            mode = ROUTE_MODES.get(route)
            if not mode:
                return

            way_refs = []
            stop_refs = []
            for m in r.members:
                t = m.type
                if t == "w":
                    way_refs.append(m.ref)
                elif t == "n" and m.role in ("stop", "platform", "stop_entry_only", "stop_exit_only"):
                    stop_refs.append(m.ref)

            st["routes_found"] += 1
            name = tags.get("name") or tags.get("ref") or ""
            for b in boxes_l:
                spans, stop_pts, stop_names = self.assemble(way_refs, stop_refs, b)
                if not spans and not stop_pts:
                    continue
                routes = st["routes"].setdefault(b["slug"], [])
                if len(routes) >= st["max_routes"]:
                    st["routes_skipped_cap"] += 1
                    continue
                routes.append({
                    "id": f"r{r.id}",
                    "mode": mode,
                    "route": route,
                    "name": name,
                    "ref": tags.get("ref", ""),
                    "operator": tags.get("operator", ""),
                    "network": tags.get("network", ""),
                    "colour": tags.get("colour", ""),
                    "spans": spans,
                    "stopCount": len(stop_pts),
                    "stopNames": stop_names,
                })

        self.relation = relation

    # ------------------------------------------------------------------ utils
    def assemble(self, way_refs, stop_refs, box):
        """Stitch member ways into [lng,lat] runs clipped to one city box.

        Spans are collected under a span cap, then the whole route is thinned
        proportionally so no route exceeds MAX_POINTS_PER_ROUTE points in
        total. Thinning per span instead would silently drop the geometry of
        long routes, which is exactly the case the budget is meant to serve.
        """
        lookup = self.lookup
        raw = []
        total = 0
        for ref in way_refs[:4000]:
            refs = self.state["refs_by_id"].get(ref)
            if not refs:
                continue
            cur = []
            for wid in refs:
                pt = lookup(wid)
                if pt is not None and box["min_lat"] <= pt[0] <= box["max_lat"] and box["min_lng"] <= pt[1] <= box["max_lng"]:
                    cur.append((pt[1], pt[0]))
                else:
                    if len(cur) >= 2:
                        raw.append(cur)
                        total += len(cur)
                    cur = []
                if len(raw) >= MAX_SPANS_PER_ROUTE or total >= MAX_POINTS_PER_ROUTE * 3:
                    break
            if len(cur) >= 2:
                raw.append(cur)
                total += len(cur)
            if len(raw) >= MAX_SPANS_PER_ROUTE or total >= MAX_POINTS_PER_ROUTE * 3:
                break
        if not raw:
            return [], [], []
        # proportional budget: give every span at least its two endpoints
        budget = max(MAX_POINTS_PER_ROUTE, 2 * len(raw))
        scale = min(1.0, budget / float(total))
        out_spans = []
        for s in raw:
            if len(s) > 2 and scale < 1.0:
                k = max(2, int(round(len(s) * scale)))
                step = (len(s) - 1) / float(k - 1)
                s = [s[int(round(i * step))] for i in range(k)]
            out_spans.append([[round(x, 5), round(y, 5)] for x, y in s])
        stop_pts = []
        for ref in stop_refs[:200]:
            pt = lookup(ref)
            if pt is None:
                continue
            if not (box["min_lat"] <= pt[0] <= box["max_lat"] and box["min_lng"] <= pt[1] <= box["max_lng"]):
                continue
            stop_pts.append((pt[1], pt[0]))
        return out_spans, stop_pts, []

    def finalize_nodes(self):
        st = self.state
        if st["finalized"]:
            return
        st["finalized"] = True
        parts_i = list(st["id_chunks"])
        parts_c = list(st["coord_chunks"])
        if st["buf_n"]:
            parts_i.append(st["buf_ids"][: st["buf_n"]].copy())
            parts_c.append(st["buf_coord"][: st["buf_n"]].copy())
        if not parts_i:
            st["sorted_ids"] = np.empty(0, dtype=np.int64)
            st["sorted_coord"] = np.empty(0, dtype=np.int64)
            return
        ids = np.concatenate(parts_i)
        coord = np.concatenate(parts_c)
        order = np.argsort(ids, kind="stable")
        st["sorted_ids"] = ids[order]
        st["sorted_coord"] = coord[order]

    def lookup(self, ref):
        ids = self.state["sorted_ids"]
        if ids is None or ids.size == 0:
            return None
        i = int(np.searchsorted(ids, ref))
        if i >= ids.size or ids[i] != ref:
            return None
        v = int(self.state["sorted_coord"][i])
        lat = (v >> 32) / 1e7
        lng_i = v & 0xFFFFFFFF
        if lng_i > 1800000000:
            lng_i -= 4294967296
        return (lat, lng_i / 1e7)


def main():
    pbf_path, cities_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    max_routes = int(sys.argv[4]) if len(sys.argv) > 4 else 400
    with open(cities_path, "r", encoding="utf-8") as fh:
        cities = json.load(fh)

    region = None
    try:
        reader = osmium.io.Reader(pbf_path)
        b = reader.header().bbox()
        region = (b.min_lon, b.min_lat, b.max_lon, b.max_lat)
    except Exception:
        pass
    print(f"[transit] region bbox: {region}", flush=True)

    boxes = []
    for c in cities:
        lat, lng = float(c["lat"]), float(c["lng"])
        half_lat, half_lng = half_degrees(c.get("pop") or 0)
        if region and not (region[0] - 1 <= lng <= region[2] + 1 and region[1] - 1 <= lat <= region[3] + 1):
            continue
        boxes.append({
            "slug": c["slug"], "name": c.get("name") or c["slug"],
            "min_lat": lat - half_lat, "max_lat": lat + half_lat,
            "min_lng": lng - half_lng, "max_lng": lng + half_lng,
        })
    print(f"[transit] {len(boxes)}/{len(cities)} candidate cities", flush=True)
    if not boxes:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"region": region, "cities": {}}, fh)
        return

    h = Extractor(boxes, max_routes=max_routes)
    h.apply_file(pbf_path, locations=False)
    st = h.state

    out = {"region": region, "cities": {}}
    for b in boxes:
        routes = st["routes"].get(b["slug"]) or []
        by_mode = {}
        operators = set()
        networks = set()
        for r in routes:
            by_mode[r["mode"]] = by_mode.get(r["mode"], 0) + 1
            if r["operator"]:
                operators.add(r["operator"])
            if r["network"]:
                networks.add(r["network"])
        stops = sum(r["stopCount"] for r in routes)
        out["cities"][b["slug"]] = {
            "name": b["name"],
            "counts": {
                "routes": len(routes),
                "stops": stops,
                "byMode": by_mode,
                "operators": len(operators),
                "networks": len(networks),
            },
            "routes": routes,
        }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)

    elapsed = time.time() - st["t0"]
    print(
        f"[transit] nodes={st['node_count']} boxed={st['boxed_nodes']} ways={st['ways_seen']} "
        f"candidates={st['candidates']} route_relations={st['routes_found']} "
        f"cities_with_routes={len(st['routes'])} capped_out={st['routes_skipped_cap']} "
        f"elapsed={elapsed:.0f}s rate={st['node_count'] / max(elapsed, 1):,.0f} nodes/s",
        flush=True,
    )


if __name__ == "__main__":
    main()
