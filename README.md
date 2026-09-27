# city-streets

Named-street extraction for every city with population ≥ 2,000,000, from
[Geofabrik](https://download.geofabrik.com/) OpenStreetMap country extracts.

For each city this produces up to **320 named streets**, ranked by OSM highway
class, each carrying a real mapped position — enough to draw a "popular
streets" highlight layer and to answer length / direction / endpoint questions
in a map popup.

> Street data © OpenStreetMap contributors, [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/).
> See [ATTRIBUTION.md](ATTRIBUTION.md).

## How it works

OpenStreetMap PBF files store **all nodes before all ways**, so a single
streaming pass can resolve named-highway ways against an in-memory index of the
nodes that fall inside a target city box:

1. `node()` — one global bbox reject (~90–106k nodes/s). Surviving nodes go into
   chunked numpy buffers (id `int64` + packed coord `int64`). Only nodes inside
   some city box are retained, so memory stays bounded regardless of file size.
2. First `way()` — buffers are concatenated and sorted by id; `lookup(ref)` is a
   `searchsorted`.
3. `way()` — keeps only allow-listed `highway=` classes that carry a `name` or
   `name:en`. It samples up to 9 node refs, because a long arterial often has
   both endpoints outside the city box while running straight through it; the
   first sampled point inside a box seeds that city. Dedupe key is
   `canonical(name)@lat.toFixed(2),lon.toFixed(2)`, keeping the
   highest-priority class. Sorted by class priority then name, capped.

City box size scales with population:

```
half_lat = 0.115 + min(0.20, sqrt(pop_millions) / 320)   degrees
```

≈ 13 km for a 2 M city, ≈ 39 km for a 21 M city. The longitude half-extent is
`half_lat * 1.25`.

The extractor emits a key for **every** candidate city box, so a result file
always lists all in-scope cities — most with `"streets": []` when that country's
extract contains nothing inside the box. Check `streets.length`, not key
presence, to know whether a city was covered.

### Class priority

`motorway` → `trunk` → `primary` → `secondary` → `tertiary` → `unclassified` →
`residential` → `living_street` → `pedestrian` → `service`.

The display label (`t`) comes from the street-name suffix when there is one
(`Avenue`, `Boulevard`, `Lane`, `Walk`, …) and falls back to the highway class.

## Layout

| Path | What |
|---|---|
| `scripts/city-streets-pbf.py` | the extractor — single streaming pass, pyosmium + numpy |
| `scripts/merge-city-streets.mjs` | merges per-extract results into one file per city + a manifest |
| `data/cities-pop2m.json` | the 161 target cities (pop ≥ 2M) with lat/lng |
| `data/country-extracts.json` | the 74 Geofabrik slugs the matrix runs over |
| `.github/workflows/city-streets.yml` | the matrix workflow |

## Data sources

Street data comes **only** from the R2-staged Geofabrik extracts at
`sources/osm/<slug>-latest.osm.pbf`. There is no Overpass API dependency and
none may be added — public Overpass instances are shared, rate-limited public
services and are not an acceptable way to get a map.

If a city is not covered by a country extract you hold, stage that country's
extract into `sources/osm/` first and extract the city from it. Geofabrik
publishes no country extract for Cuba, the Dominican Republic, Puerto Rico or
Russia, so Havana, Santo Domingo, San Juan, Moscow and Saint Petersburg are not
reachable through this pipeline as written; the continent PBFs
(`sources/osm/europe-latest.osm.pbf`, `sources/osm/north-america-latest.osm.pbf`)
hold them but are 19–35 GiB and do not fit a CI runner's disk.

## Running the workflow

The workflow expects the Geofabrik PBFs to already be staged in object storage
at `sources/osm/<slug>-latest.osm.pbf` and writes each result to
`data/osm-city-streets/<slug>.json`.

Repository secrets:

| Secret | Value |
|---|---|
| `R2_ENDPOINT` | `https://<account>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | object-storage key with read + write |
| `R2_SECRET_ACCESS_KEY` | its secret |
| `R2_BUCKET` | optional, default `geo-datalake` |
| `R2_PREFIX` | optional, default `data/osm-city-streets/` |

```bash
# everything still missing (finished extracts self-skip on a head-object check)
gh workflow run city-streets.yml -f max-parallel=6

# just the countries you care about
gh workflow run city-streets.yml -f "extracts=china,us-west,india,gcc-states"

# re-extract everything, e.g. after changing the extractor
gh workflow run city-streets.yml -f max-parallel=6 -f force=true

gh run list --workflow city-streets.yml -L 5
gh run view <id> --log | grep 'cities with streets'
```

Each job costs roughly 30 s of setup plus the extraction itself, so a
re-run of a finished matrix is nearly free.

## Running locally

```bash
pip install osmium numpy

# 1. prefilter a country PBF to named highways (keeps referenced nodes — no -R)
osmium tags-filter -f pbf -o nhw.osm.pbf --overwrite src.osm.pbf w/name

# 2. extract
python scripts/city-streets-pbf.py nhw.osm.pbf data/cities-pop2m.json out.json 320
```

`out.json` is `{ "region": [minLon, minLat, maxLon, maxLat], "cities": { <slug>: { name, streets[] } } }`,
where each street is `{ n, raw, t, la, lo, p, cls }` — display name, `name:en`
(or the display name), label, latitude, longitude, rank, highway class.

### Geometry

Each street also carries `g`: the way's real polyline, as
`[[[lng,lat], …], …]` — one array per run of the way inside the city box.

Because the extractor already holds every node inside every city box in its
index, it can resolve a matched way's full node-ref list directly from the PBF
rather than emitting a single seed point. Ways that leave the box are clipped
at the boundary by interpolating the crossing, and each run is thinned to at
most 60 points (endpoints always preserved), so a 320-street city costs roughly
300 KB.

This matters because it removes the need for an Overpass round-trip per street
to draw the street or answer length / direction / endpoint questions: the
geometry is already in the data. Pass `0` as the 5th argument to turn it off and
get the original seed-point-only output:

```bash
python scripts/city-streets-pbf.py nhw.osm.pbf data/cities-pop2m.json out.json 320 0
```

Cost is negligible — `gcc-states` (241.8 MiB, 33.6 M nodes) emitted geometry for
41,277 ways with no measurable slowdown versus the seed-point-only run.

Then merge, from local extract files or straight from object storage:

```bash
node scripts/merge-city-streets.mjs --extracts ./extracts --out ./out
R2_ENDPOINT=… R2_ACCESS_KEY_ID=… R2_SECRET_ACCESS_KEY=… node scripts/merge-city-streets.mjs
```

Output is `out/<city-slug>.json` plus `out/city-streets-manifest.json`.

## Two bugs worth not reintroducing

Both cost a full matrix run to diagnose:

- **`osmium tags-filter -R` drops referenced nodes.** The extractor then sees
  `nodes=0` and returns nothing at all. The prefilter must keep referenced
  nodes — no `-R`.
- **Unsigned longitude unpacking.** The packed coord stores longitude in the low
  32 bits as unsigned, so it must be re-signed on read (`lng_i -= 2**32` when
  `> 1.8e9`) and latitude unpacked with an arithmetic shift (`v >> 32`). Without
  the re-sign, every western-hemisphere city matches zero boxes.
