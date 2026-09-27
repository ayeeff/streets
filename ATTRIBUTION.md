# Attribution

## Street data

All street names, road classifications and coordinates produced by this pipeline
are derived from **OpenStreetMap**.

> © OpenStreetMap contributors
> Licensed under the [Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/)

If you redistribute any output of this pipeline — the per-extract JSON files or
the merged per-city files — you must attribute OpenStreetMap contributors and
make the data available under the ODbL. The `raw` field on each street row
carries the `name:en` tag where OSM provides one, which is the value to display
alongside the localised `n` name.

## Source extracts

Input `.osm.pbf` files are [Geofabrik](https://download.geofabrik.com/) country
extracts, which are themselves derived from OpenStreetMap and carry the same
ODbL licence. Geofabrik also publishes per-continent extracts under
`sources/osm/<region>-latest.osm.pbf`.

## Tools

| Tool | Role | Licence |
|---|---|---|
| [osmium-tool](https://osmcode.org/osmium-tool/) (`tags-filter`) | prefilter to named highways | GPL-2.0 |
| [pyosmium](https://pyosmium.readthedocs.io/) + numpy | streaming PBF read, node index | GPL-2.0 / BSD-3 |

Both are invoked as external tools / imported libraries; neither is vendored
into this repository.
