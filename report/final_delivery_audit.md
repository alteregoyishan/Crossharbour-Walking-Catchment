# Final delivery refinement audit

## Implementation

Presentation-only refinement of the existing saved analysis. No routing, catchment polygonisation, population allocation, network repair or external data acquisition was rerun. School membership was calculated from the cleaned points and saved final analytical 10-minute land polygon, with boundary-inclusive `covers`.

Changed: `src/final_delivery.py`, `scripts/18_build_web_report.py`, `report/web_report_template.html`, `docs/index.html`, `docs/assets/style.css`, `src/c1_final_maps.py`, presentation footer in `src/population_sensitivity.py`, README, architect note, final rendered maps and web assets. The prior full README is retained in `report/technical_workflow.md`.

## Reader journey

Hero → Key findings → Overview note → Compact analysis approach, origins and cleaning table → Walking catchment and visible 5/15-minute references → Full school context and selected schools → Population sensitivity → Limitations. Data corrections and concise network/model validation are grouped under Analysis approach; the duplicate QA section is removed. Supporting maps are visible rather than collapsed; population figure sits alongside the allocation table on desktop.

## Findings

10-minute analytical land: 68.038243 ha. Census area-weighted baseline: 8,016.192542 residents and 117.818923 residents/ha. Public values: approximately 68 ha, 8,016 residents and 117.8 residents/ha. Walking speed: 4.8 km/h. Population source: Census 2021.

Three school locations are covered by the final analytical land polygon:

| Map | School | URN |
|---|---|---|
| 1 | Canary Wharf Primary, East Ferry | 137016 |
| 2 | Canary Wharf Secondary | 142879 |
| 3 | Cubitt Town Primary School | 100898 |

The table and numbered school map derive from the same selection. The primary 10-minute map displays these three locations. The full cleaned-school map is supporting context.

## Communication and maps

Removed assessment branding and internal scenario codes from the primary story. Descriptive names distinguish the Census baseline, residential land, building footprint and floor-space sensitivity. CRS is in Methods. README separates overview, findings, final approach, outputs, reproduction, licensing and detailed methodology; the original 25 m buffer is explicitly a comparison baseline.

Muted the basemap, reduced network stroke dominance, placed the development site first in the legend, retained black dashed site boundary and origins, and added the numbered school map. Individual 5/10/15-minute maps retain common bounds. The straight-line reference circle is supporting context, not a second accessibility model. Overview note retained without a displayed word count. The origin-selection map now has a thicker boundary and A/B labels without crossing angles. Ten minutes is explained as a planning scenario, not a prescribed or optimised threshold. Detailed QA, comparisons, logs and tests are preserved.

## Checks

- Source area, population and density reconciled; population / land hectares agrees with saved density.
- School URNs unique, count and names consistent across HTML, CSV, map, README and note.
- All local HTML image/download paths resolve; no unresolved template placeholders or stale primary assessment/scenario/Pending terminology.
- Desktop report, 390 px mobile first-screen layout, and rendered primary/school PDFs visually inspected. Full mobile workflow visual inspection was interrupted by the browser window becoming unavailable; responsive CSS was reviewed.
- Existing unittest: 32 passed. Existing pytest: 42 passed. Tests were not changed or weakened. Checks used `PROJ_NETWORK=OFF` with temporary plotting/cache directories; default environment initially had projection-network-dependent failures.
- Python compilation and `git diff --check` passed.

## Limitations

Origins A/B are planning access proxies, not confirmed future entrances. School coordinates are cleaned locations, not verified gates; membership does not assert exact gate-to-gate travel time. Census 2021 is not a current population measurement. Baseline allocation assumes uniform within-LSOA distribution. Alternative allocations are sensitivity scenarios, not confidence intervals. Accessibility depends on OSM completeness and fixed walking speed; the contour is a derived representation, and validation is model-based consistency rather than observed ground truth.
