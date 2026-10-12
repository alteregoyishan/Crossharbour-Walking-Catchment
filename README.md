# Crossharbour Walking Catchment

## Overview

Walking accessibility, estimated resident density and school context around a fictional development plot at Crossharbour, Isle of Dogs, London, prepared for early-stage masterplanning.

[Open the report](https://alteregoyishan.github.io/Crossharbour-Walking-Catchment/).

## Key Findings

| Primary 10-minute scenario | Result |
|---|---:|
| Analytical land area | 68.038 ha (approximately 68 ha) |
| Estimated residents, Census 2021 baseline | 8,016 |
| Estimated resident density | 117.8 residents/ha |
| Schools within the analytical catchment | 3 |
| Walking speed | 4.8 km/h |

Schools identified from the cleaned school locations:

| School | URN | Phase | Type |
|---|---|---|---|
| Canary Wharf Primary, East Ferry | 137016 | Primary | Free schools |
| Canary Wharf Secondary | 142879 | Secondary | Free schools |
| Cubitt Town Primary School | 100898 | Primary | Community school |

These school locations represent cleaned address/location coordinates rather than verified pedestrian gates; exact gate-to-gate times may differ. Population estimates use Census 2021 usual residents and are not direct measurements of present-day population.

## Final Approach

Ten minutes is the primary local planning scenario for the workshop, balancing immediate 5-minute and wider 15-minute context. It is a stated planning choice rather than a prescribed standard or optimised threshold. Five- and 15-minute maps are shown alongside each other as reference scenarios.

Two reviewed northern site-boundary footway crossings are used as access proxies A and B, rather than confirmed future entrances. Multi-source Dijkstra calculates the shortest walking time from either origin, using 4.8 km/h and partial-edge clipping. The reachable pedestrian network is the authoritative travel-time result.

The final workshop-facing walking catchment uses the selected travel-time contour representation, with mapped water excluded from analytical land. The original 25 m network buffer remains a methodological comparison baseline. Display smoothing and evidenced bridge corridors do not enter the analytical land-area or population denominator. The independent validation grid is a model-based consistency check, not observed ground truth.

Census population is allocated proportionally using the intersection area of each original full LSOA with the analytical catchment. Density divides estimated residents by analytical land hectares. Residential land (9,739 residents), building footprints (9,065) and residential floor-space scenarios (8,938–8,997) test allocation assumptions; they are not confidence intervals. School points are identified with a boundary-inclusive point-in-polygon test against the saved final 10-minute analytical land catchment.

## Outputs

- [Primary 10-minute map PDF](docs/assets/downloads/catchment-10min.pdf)
- [5 / 10 / 15-minute combined map PDF](docs/assets/downloads/catchments-combined.pdf)
- [Schools within the catchment PDF](docs/assets/downloads/schools-within-10min.pdf) and [school list CSV](outputs/tables/schools_within_10min_catchment.csv)
- [Site/origin map PDF](docs/assets/downloads/origin-selection.pdf)
- [Population sensitivity PDF](docs/assets/downloads/population-sensitivity.pdf)
- [Methodology note](docs/assets/downloads/methodology.md)
- `outputs/maps/`: final PNG/PDF series; `outputs/tables/`: source results, cleaning logs and QA tables.
- `outputs/methodology/` and `outputs/qa/`: detailed technical evidence and sensitivity outputs.
- `outputs/gis/`: locally generated analytical GIS layers; large intermediates are excluded from Git.

Internal scenario identifiers in filenames and technical tables are retained for reproducibility; the public report uses descriptive method names.

## Reproduce the Analysis

```bash
conda env create -f environment.yml
conda activate prior-gis
cp .env.example .env
```

Run numbered scripts individually as documented in the [technical workflow](report/technical_workflow.md). Begin with data QA, clean population and schools, obtain/review the walking graph, select origins and calculate network travel time. Subsequent stages preserve the buffer baseline, compare contour representations and calculate population sensitivity. API-dependent stages require your own keys in `.env`; keys and map tile caches are ignored by Git.

```bash
python scripts/01_validate_data.py
python scripts/02_clean_population.py
python scripts/03_clean_schools.py
python scripts/04_download_pedestrian_network.py
# Continue through the documented stages.
python scripts/18_build_web_report.py --render-maps
python -m unittest discover -s tests -v
```

The delivery build reads existing final polygons, network outputs, cleaned schools and population tables. `--render-maps` redraws saved geometry; it does not rerun routing, polygonisation or allocation. School selection and report numbers are recomputed from saved final data. The page template is `report/web_report_template.html`; `docs/index.html` is generated.

GitHub Pages publishes `main` → `/docs`. Update local files, run the delivery build and push to `main` to update [the website](https://alteregoyishan.github.io/Crossharbour-Walking-Catchment/).

## Data and Licensing

- Schools: contains public sector information licensed under the Open Government Licence v3.0. Source: Department for Education, Get Information about Schools.
- LSOA boundaries and Census 2021: Source: Office for National Statistics, licensed under the Open Government Licence v3.0. Contains OS data © Crown copyright and database right 2026.
- OpenStreetMap: © OpenStreetMap contributors, Open Database Licence (ODbL).
- OS Maps API basemap, OS Places and OS NGD: Contains OS data © Crown copyright and database rights 2026. OS Maps is presentation context; OSM supplies analytical network/water geometry.

Credits appear on the relevant maps and in the website. Raw inputs are preserved separately from cleaned data.

## Detailed Methodology

- [Technical workflow and development comparisons](report/technical_workflow.md)
- [Final contour representation](outputs/methodology/final_c1_map_series.md)
- [Water/barrier and representation comparison](outputs/methodology/catchment_barrier_policy_review_v2.md)
- [Population calculation and sensitivity](outputs/methodology/population_calculation_summary.md)
- [Network QA tables](outputs/tables/network_qa_summary.csv)

Metric geometry calculations use British National Grid (EPSG:27700). Results depend on pedestrian-network completeness and fixed walking speed; slope, crossing delay and mobility variation are not modelled. Separately mapped footways are retained, but pavements on both sides of every road and all crossing locations are not systematically modelled; crossing detours and boundary travel times may therefore differ. The polygon is a derived spatial representation rather than an exact physical boundary. Uniform within-LSOA population distribution is an assumption tested by alternative allocations. Access proxies should be checked as development designs progress.
