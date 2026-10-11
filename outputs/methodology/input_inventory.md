# Inputs inspected

- Site: `data/raw/site_boundary.geojson`; transformed to EPSG:27700 for analysis.
- Reviewed origins A/B: `data/processed/site_origins.gpkg`.
- Downloaded graph: `data/processed/OpenStreetMap/pedestrian_network.graphml` (preserved).
- Reviewed graph: `data/processed/OpenStreetMap/pedestrian_network_reviewed.graphml`.
- Routing graph with A/B inserted: `data/processed/OpenStreetMap/pedestrian_network_reviewed_with_origins.graphml`.
- Existing 5/10/15-minute network reach: `outputs/spatial/reachable_network_*min.gpkg`; verify before reuse.
- Population: `data/cleaned/lsoa_2021_population_cleaned.gpkg`, 109 unique LSOAs, EPSG:27700; field `usual_residents_total`, ONS Census 2021 baseline.
- Population corrections already recorded: one exact duplicate removed; E01035698 repaired with a 0.000569 m² area change. Do not repeat repairs.
- Schools: `data/cleaned/schools_cleaned.gpkg`, 42 records, EPSG:27700. Address geocoding does not verify pedestrian entrances.
- School exclusions: `data/qa/schools_qa.csv`; preserve existing Closed/Overseas/Duplicate decisions.
- Network evidence: `outputs/tables/network_changes.csv`, `network_qa_summary.csv`, `origin_network_qa.csv`; three manual connections previously applied.
- Water: retain the existing OSM polygon layer as the selected analysis source. OS NGD Water remains an optional future comparison, not a required replacement.
- Map context: use OS Maps API `Light_27700` raster tiles through the same OS Data Hub project key used for OS Places. Tiles are visual context only and do not enter distance, population or water calculations.
- External comparison: Google Maps Platform Isochrones API, `WALK`, `FROM`, `TRAFFIC_UNAWARE`, high fidelity and no smoothing. Request A/B separately at 5/10/15 minutes and union equal-duration results. This is a comparison, not a replacement for the audited OSMnx network result.

Final deliverables will be written under `outputs/gis`, `outputs/tables`, `outputs/maps`, `outputs/qa` and `outputs/methodology`. Existing source and intermediate datasets are reused.
