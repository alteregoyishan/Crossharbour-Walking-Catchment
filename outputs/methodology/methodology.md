# Methodology

## Inputs and provenance
Reuse the supplied site, cleaned DfE schools and 109 unique ONS Census 2021 population zones. This is a **Census 2021 population baseline**, not measured 2026 population. The input inventory and SHA-256 manifest identify the files used. The existing population correction log documents one exact duplicate removed and a self-intersection repaired; the latter changed area by approximately 0.000569 m². No second correction was applied. Original zone population totals, excluding the known duplicate, were preserved.

## Pedestrian network and time
OSMnx 2.0.1 acquired the network with `network_type="walk"`, retained all components and simplified edges. Analysis uses the reviewed EPSG:27700 MultiDiGraph and the saved routing graph with A/B inserted. Three user-confirmed connections are documented in `network_changes.csv`; the downloaded GraphML is preserved. Foot/layer attribute omissions in the initial export were an auditability limitation; the retained-tag graph is used here.

Each edge time is projected geometry length / (4.8 km/h), equivalent to 80 m/min. Multi-source Dijkstra gives the minimum cumulative edge time from A or B. These reviewed external proxy accesses are not confirmed future building entrances; travel starts at the accesses and omits internal-site travel. Thresholds are 300, 600 and 900 seconds, with 600 seconds primary. A partially reachable edge is clipped along its oriented geometry using remaining seconds multiplied by speed. Saved reachable geometries were checked against recalculated partial edges before reuse. No change to the shortest-path algorithm was needed. A full-graph check found no restricted-tag edge reachable within 15 minutes; conditional permissions and wider topology findings remain limitations.

## Two-dimensional service areas
Network reach is the authoritative time result. Buffer all reachable lines by 25, 50 and 75 m and dissolve, producing nine raw geometries. Retain all polygon parts, without smoothing, convex hulls or silently filling gaps. Also subtract the selected water-polygon union to create nine land-only geometries. Verify validity and 5/10/15 nesting; derive non-overlapping bands by differences. These polygons are representations, not exact travel-time boundaries. Buffering can cross unmodelled barriers or narrow gaps; water subtraction does not prove access to individual parcels and also removes water beneath genuine bridge routes from the polygon (the network routes remain).

Water source: **OpenStreetMap via Overpass / OSMnx features_from_polygon**. The selected file is `data/processed/OpenStreetMap/water_polygons.gpkg` and its adjacent JSON records query, download timestamp, bounds, CRS and hash. Full returned polygons are retained; the query covers complete supplied census zones, avoiding truncated land denominators. Invalid geometry repairs, if any, are logged. OSM Overpass remains the selected analysis water source. OS NGD features are investigated separately for building, site, land and barrier refinement. OS Maps API Light raster basemap is requested at export time and cached locally. The API key is not stored in the outputs.

## Population and density
For each complete zone i, standard weighting estimates P_i * Area(zone_i intersection raw catchment) / Area(zone_i). Retain zone ID, original population, denominator, intersection area, fraction and contribution. Denominators are full zone areas, never clipped zone areas. Population QA checks identifiers, values, geometries, overlap and preservation of unique source totals. All areas use EPSG:27700 metres, divided by 10,000 for hectares. No intermediate rounding is applied. Density uses the corresponding raw area.

Land-only sensitivity subtracts water from both each full zone and each catchment intersection, then applies the same formula. Its density uses land-only catchment area. This redistributes each full zone population over mapped land, which is still not a residential-area model. At 10 minutes / 25 m, this changes estimated population by +24.1% relative to standard weighting.

## Primary choice and sensitivity
Retain the pre-existing 25 m corridor as the primary 10-minute representation: it is transparent, reproducible and keeps the polygon close to the dense pedestrian network, reducing unsupported spread across docks, yards and enclosed parcels. It is a conservative corridor convention, not an empirically measured walking-distance allowance or an optimised buffer. Use the raw footprint for primary standard population/density metrics; display water explicitly and provide the land-only alternative. This decision retains the original parameter rather than selecting a preferred population result.

Standard population changes by +43.7% between 25 and 75 m. This is material: population and area results are strongly dependent on polygon construction and should be presented as scenario estimates, not precise catchment counts. Increasing width fills more space between dense network lines; narrow buffers may omit nearby residents, while wider buffers can include inaccessible land. The six 10-minute standard/land-only results and percentage comparisons must accompany the main map.

## School accessibility
Reuse documented Closed/Overseas/Duplicate exclusions and retain unusual attributes for review. Address geocoding does not establish a pedestrian entrance. The present inputs provide no reviewed school destination evidence, so 42 destinations are unverified. The number of network-accessible schools is pending, not zero. Candidate projections and travel times are diagnostic only and omit any unverified school-to-network connection. No connector is added through water, buildings or fences. Records within two minutes of the 10-minute threshold are flagged. Polygon inclusion is a secondary comparison only, using the water-subtracted primary representation.

`data/qa/school_destination_review.csv` accepts a reviewed existing graph node, destination method, evidence, reviewer and date. Approve only a verified entrance on the network or a defensible public-network proxy. Approved destination times use the existing Dijkstra results; unresolved destinations remain null. If a verified entrance lies inside an edge, a separate documented edge-insertion step is required before assigning its node. Rerun steps 10 and 11 after review. No candidate destination is silently accepted.

## Reproduction
Activate the project's GIS environment, then run:

```bash
python scripts/09_prepare_water.py
python scripts/10_analyse_service_areas.py
python scripts/11_export_analysis_maps.py
python scripts/12_google_isochrone_comparison.py
python -m unittest discover -s tests -v
```

Step 09 reuses OSM water by default; step 10 verifies and reuses saved network reach, writes 18 scenarios and school QA; step 11 exports A3 landscape PDF/PNG maps, charts and notes using the native EPSG:27700 OS Maps layer when the OS project key is available. Step 12 performs the six-request Google walking-isochrone comparison (A/B × 5/10/15 minutes). Step 13 is a read-only OS NGD collection and small-sample discovery. Existing steps 01-08 need not be rerun unless their inputs or assumptions change. Analysis outputs are under `outputs/gis`, `tables`, `qa`, `maps` and `methodology`.
