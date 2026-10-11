# Final C1 walking-catchment map series

The final workshop map series uses C1, the ordinary-Delaunay travel-time contour proxy selected by the project author. Routing inputs remain unchanged. C1 is not described as a reproduction of Lin et al. because it does not use constrained Delaunay triangulation.

A single triangulation and time surface is used for 300, 600 and 900 seconds, ensuring nested 5/10/15-minute contours. Every analytical polygon subtracts the validated OSM water layer. Fully enclosed non-water gaps caused by the 150 m triangle filter are filled, then water is subtracted again. A 2 m/side land-only support strip is unioned around the authoritative reachable network so routed lines cannot protrude from the analytical catchment. Threshold-specific reachable bridge links remain governed by the separate bridge-corridor policy.

| Time | Analytical land | Display area | Network length | Holes | Land components | Display components |
|---:|---:|---:|---:|---:|---:|---:|
| 5 min | 16.24 ha | 16.24 ha | 8.18 km | 0 | 1 | 1 |
| 10 min | 68.04 ha | 68.05 ha | 29.00 km | 2 | 2 | 1 |
| 15 min | 144.89 ha | 144.95 ha | 57.26 km | 6 | 4 | 2 |

## Cartographic rules

- PNG files contain only the map, legend, scale bar, north arrow and required attribution.
- PDF files add a title and concise method note.
- Schools are shown as `School` point symbols without individual names or accessibility claims.
- The development site is an unfilled, thin black dashed outline without a white casing.
- Origins A and B are labelled beside the points without leader lines.
- School and origin symbols use the same point diameter in the map and legend.
- All three individual maps use the same 15-minute map extent and therefore the same scale.
- The plotted edge uses 10 m topology-preserving simplification followed by a 10 m close/open operation; a 6 m/side visible network support strip is restored after both operations.
- Reachable network lines use darker versions of their corresponding 5-, 10- and 15-minute catchment colours.
- LSOA boundaries are not drawn.
- Bridge links use the normal catchment/network symbology and are not highlighted in red.

## Straight-line reference circle

The combined reference map uses a 1200 m radius (2400 m diameter) circle centred on the midpoint of origins A and B at approximately E 538020.0, N 179213.6. The radius equals 4.8 km/h multiplied by 15 minutes and contains the complete 15-minute cartographic catchment. It is a theoretical unobstructed straight-line reference, not a walking catchment.

## Context maps

`figure_00a_site_origin_selection` enlarges the development site over a muted OS Maps Outdoor_27700 background and shows the reviewed OSM pedestrian network, the two selected boundary-crossing footways and origins A/B with their crossing angles. A finer WMTS tile matrix reduces the visual prominence of baked-in place labels. `figure_00b_all_cleaned_schools` shows every cleaned school with a usable geometry before catchment filtering.

C1 surface metadata: `{"sample_count": 8137, "sample_spacing_m": 15.0, "triangle_count": 16231, "triangles_over_150m": 245, "triangulation": "SciPy ordinary Delaunay; experimental C1 proxy"}`
