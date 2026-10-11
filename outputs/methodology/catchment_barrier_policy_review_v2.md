# Crossharbour catchment barrier-policy review (v2)

## Scope and authoritative result

The OSMnx pedestrian graph, reviewed proxy origins A/B, multi-source shortest-path calculation, 4.8 km/h walking speed, 600-second threshold and partial-edge handling are unchanged. The authoritative routing result remains the reachable pedestrian network. Every polygon in this review is a derived two-dimensional representation for population interpolation and workshop communication.

All metric operations use EPSG:27700. The cleaned analytical water source is `data/processed/OpenStreetMap/water_polygons.gpkg` and its geometry is validated before use. Existing polygonisation outputs and Figure 01 are not overwritten.

## Common water and bridge policy

`apply_common_barrier_policy(candidate_polygon, reachable_network evidence)` performs the same operation for A2, B2, C1 and C2:

1. validate and union mapped OSM water;
2. subtract mapped water from the candidate polygon to create `analytical_land_catchment`;
3. retain land on every side of the water where the candidate and routing support it;
4. restore only separately classified, likely valid reachable water crossings as narrow `bridge_connectivity_corridors`;
5. keep bridge corridors separate from population, land area and density calculations.

The graph contains **11** reachable physical edges intersecting mapped water. Classification counts are: A=7, B=4. **7** have explicit bridge/tunnel/layer or matching NGD evidence and are used by the policy. Untagged shoreline overlaps remain review items and are not silently restored.

The existing OS NGD Structure/Compound Structure data contains only ten-feature discovery samples. None is treated as complete coverage. Real NGD bridge geometry would be preferred where a reliable sample matches; otherwise the policy uses an OSM line buffer. Each display corridor extends 0.5 m beyond the mapped water edge as a land tie-in so numerical boundary differences cannot leave it isolated.

`buffer(1.0)` means 1 m on each side of the line, or about 2 m total width. The selected corridor geometry totals **150.79 m²** across all accepted crossings, including the 0.5 m land tie-ins, and is the smallest tested value that remains legible when symbolised at A3 scale. The per-method area-increase columns measure the actual new display area after overlap with land.

| Buffer each side (m) | Approx. width (m) | All corridor area (m²) | A2 increase (m²) | B2 increase (m²) | C1 increase (m²) | C2 increase (m²) |
|---|---:|---:|---:|---:|---:|---:|
| 0.50 | 1.00 | 75.39 | 70.89 | 70.89 | 70.89 | 70.89 |
| 1.00 | 2.00 | 150.79 | 141.79 | 141.79 | 141.79 | 141.79 |
| 2.00 | 4.00 | 301.62 | 283.61 | 283.61 | 283.64 | 283.64 |

These corridor areas are negligible relative to catchment hectares and are excluded from analytical land area. The display-area sensitivity is retained in the table rather than mixed into population results.

## A0, A2 and B2 hole diagnosis

The original A0 audit contains 37 holes. 34 were classified as evidence-supported line-buffer artefacts and 3 were retained as potentially inaccessible or unresolved.

After the common water mask, A2 contains **40** interior rings. B2 fills **34** rings covering **4.976 ha** and retains **6** rings covering **0.334 ha**. Each B2 decision records water overlap, nearby reachable-network density, limited NGD context, distance to the network, boundary support and independent grid accessibility. Mapped docks, rivers and reservoirs are masked again after filling, so B2 cannot reintroduce water.

## Method C audit

C1 uses the existing experimental method: approximately 15 m network samples; minimum A/B directed arrival time at each sample; SciPy ordinary Delaunay; linear travel-time interpolation; clipping at 600 seconds; rejection of triangles with a side over 150 m; then the common water/bridge policy. It removes narrow buffer holes because it interpolates a continuous time surface between network samples rather than buffering each one-dimensional line independently.

All **10,855** ordinary-Delaunay triangles are exported. Each records side lengths, vertex times, mapped-water overlap, limited building-sample overlap, a relevant vertex-pair Euclidean distance, the origin-time network-distance lower bound, and a pedestrian-network distance for pre-screened pairs. Pedestrian distances use the reviewed graph between the nearest endpoint attachments, so they are an audit approximation rather than exact point-on-edge distances.

C1 has **568** contour-contributing triangles constrained by C2: **153** include a water-spanning vertex connection outside an accepted crossing zone and **483** are Euclidean-close but network-far. Counts can overlap. Building overlap is a review flag and is never a blanket exclusion because buildings may contain residents.

C2 uses the same samples and interpolation. Where a contour triangle crosses mapped water away from an accepted crossing, connects different pedestrian components, or is demonstrably network-far, only the portion within the established 25 m reachable-network buffer is retained. This local constraint avoids deleting the accessible part of the triangle while preventing unsupported interpolation across the barrier. The 150 m rule continues to apply. C2 then receives the same water mask and bridge restoration as every other processed method. It is a barrier-aware ordinary-Delaunay proxy, not a constrained triangulation.

Method C metadata: `{"c1_surface_triangle_count": 4398, "c1_triangles_over_150m": 217, "c2_constrained_surface_triangle_count": 568, "c2_removed_surface_triangle_count": 0, "c2_unconstrained_surface_triangle_count": 3830, "implementation_status": "ordinary-Delaunay experimental proxies; not Lin et al. reproduction", "network_distance_method": "selected pre-screened vertex pairs; undirected reviewed graph between nearest edge-endpoint attachments", "network_far_triangle_count": 1058, "sample_count": 5443, "sample_spacing_m": 15.0, "triangle_count": 10855, "water_spanning_triangle_count": 792}`

## Constrained Delaunay feasibility (C3)

The Lin et al. reference implementation was inspected at commit `066c6d6796250fd186a382dc612678b779d671cc`. Its constraints are the segmented graph edges returned by `GetEdgesForConstrianedTriangulationLineSegmentation`, including inserted extended-tree break/delta nodes; `BuildConstrainedTrangulation*` inserts each resulting edge-node pair as a CGAL constraint. In the multiple-origin implementation the zero-cost virtual facility node is excluded, and its synthetic links are not inserted as constraints.

This environment has SciPy ordinary Delaunay but does not have CGAL Python bindings, Triangle or meshpy. A correct C3 would also require porting and testing the directed extended-shortest-path-tree and break-node logic, rather than merely passing current sample points to another triangulator. C3 is therefore **not implemented** because it would add substantial dependency and validation risk to the take-home.

## Fair comparison

| Method | Land area (ha) | Population | Density (/ha) | Holes | Hole area (ha) | Water overlap (ha) | Bridge corridor (ha) | Land components | Grid-model F1 | Suspicious interpolation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A0 | 69.44 | 8161 | 117.5 | 37 | 5.196 | 2.753115 | 0.0000 | 1 | 0.887 | 0 |
| A2 | 66.69 | 7683 | 115.2 | 40 | 5.310 | 0.000000 | 0.0151 | 2 | 0.905 | 0 |
| B2 | 71.66 | 8410 | 117.4 | 6 | 0.334 | 0.000000 | 0.0151 | 2 | 0.943 | 0 |
| C1 | 67.93 | 8006 | 117.9 | 3 | 0.101 | 0.000000 | 0.0151 | 2 | 0.962 | 568 |
| C2 | 67.54 | 7969 | 118.0 | 15 | 0.317 | 0.000000 | 0.0151 | 3 | 0.962 | 0 |

| Method | Symmetric difference from A2 (ha) | Method only (ha) | A2 only (ha) |
|---|---:|---:|---:|
| A0 | 2.75 | 2.75 | 0.00 |
| A2 | 0.00 | 0.00 | 0.00 |
| B2 | 4.98 | 4.98 | 0.00 |
| C1 | 12.63 | 6.94 | 5.69 |
| C2 | 12.24 | 6.54 | 5.69 |

Water overlap for A2/B2/C1/C2 should be numerical zero. A0 is intentionally retained as the unprocessed raw baseline. Suspicious interpolation counts apply only to the ordinary-Delaunay contour surface; zero for buffer methods means “not applicable,” not proof of physical permeability.

## Grid-model agreement audit

Reference points form a regular 10 m grid. Each label uses the closest reviewed physical edge, directed multi-source travel time to the projected point, straight off-network time at 4.8 km/h and a water/connector check. Candidate polygons are used only as predictions and never generate the labels. The reference does not use Delaunay interpolation.

There is no direct polygon circularity. There is still conceptual similarity that can favour C1/C2: both the grid model and contours extend network travel time into surrounding space. The metric is therefore named **Grid-model agreement F1**, is not real-world accuracy, and is not the sole selection criterion.

## Population sensitivity

Standard results use the complete original LSOA area as the denominator and the analytical land catchment as the numerator geometry:

| Method | Land area (ha) | Standard population | Density (/ha) |
|---|---:|---:|---:|
| A0 | 69.44 | 8161 | 117.5 |
| A2 | 66.69 | 7683 | 115.2 |
| B2 | 71.66 | 8410 | 117.4 |
| C1 | 67.93 | 8006 | 117.9 |
| C2 | 67.54 | 7969 | 118.0 |

The water-adjusted source-zone denominator remains a separate sensitivity test:

| Method | Water-adjusted sensitivity population | Sensitivity density (/ha) |
|---|---:|---:|
| A0 | 10129 | 151.9 |
| A2 | 10129 | 151.9 |
| B2 | 11059 | 154.3 |
| C1 | 10508 | 154.7 |
| C2 | 10459 | 154.9 |

Bridge display corridors are excluded from both tables.

## Explicit answers and recommendation

1. **Which A0 holes were artificial?** 34 holes had independent accessibility and boundary-support evidence consistent with line-buffer artefacts.
2. **Which represent genuine barriers?** Water-classified rings and low-accessibility/uncertain rings remain excluded. “Genuine” means supported by current mapped evidence; field access, parcel gates and complete land-use coverage remain unavailable.
3. **Does B2 avoid mapped water?** Yes. B2 is derived from A2, water-related rings are never filled, and the common water mask is reapplied after filling. The comparison table reports the residual overlap.
4. **Why is C1 more coherent?** It constructs a continuous 600-second time surface between dense network samples, eliminating the repeated 25 m corridor gaps produced by line buffering.
5. **Where is ordinary Delaunay questionable?** The suspicious-triangle layer identifies water-spanning, network-far, disconnected or very-long triangulation relationships with explicit evidence and geometry.
6. **Does C2 improve these locations?** It constrains 568 contour-contributing suspect triangles to the local 25 m reachable-network buffer before polygon union. Its area/population change relative to C1 is visible in the comparison, but it remains a heuristic proxy.
7. **Does C3 differ?** No defensible C3 result exists in this environment, so no difference is claimed.
8. **Are bridges preserved correctly?** Yes for 7 likely valid tagged/reviewed links. Narrow corridors preserve display continuity while surrounding water stays outside analytical land.
9. **How sensitive are area and population?** The method and population tables quantify the change under identical routing, water, population and bridge rules.
10. **Recommended workshop method:** use **B2 — water-aware controlled-hole treatment**. It is transparent, auditable, conservative around unresolved barriers and implemented entirely in the existing reproducible stack. Keep A2 beside it as the conservative baseline. Use C2 as a research sensitivity figure rather than the final workshop catchment until constrained triangulation and better parcel/barrier evidence can be validated.

## Remaining limitations

- OSM water and OSM bridge tags may contain omissions or positional differences.
- 4 unresolved water overlaps remain manual-review items and are not restored automatically.
- NGD discovery samples are not complete bridge, building or land-use coverage.
- The triangle network distance uses nearest endpoint attachments for pre-screened pairs, not exact temporary point insertion.
- The grid benchmark models off-network movement and does not provide observed walking traces or verified parcel entrances.
- Area-weighted population assumes uniform distribution within each LSOA.
