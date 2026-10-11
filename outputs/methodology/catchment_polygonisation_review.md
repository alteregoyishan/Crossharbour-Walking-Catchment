# Crossharbour 10-minute catchment polygonisation review

## Scope and preserved analysis

This review leaves the existing OSMnx graph, reviewed origins A/B, multi-source Dijkstra travel times, 4.8 km/h speed, partial-edge handling, ONS Census 2021 data, DfE schools, OSM water and Figure 01 unchanged. All metric geometry operations use EPSG:27700. New outputs are comparison and QA artefacts.

## Reference method

[Lin et al. (2020)](https://doi.org/10.3390/ijgi9080467) construct a local subgraph, extend the shortest-path tree with break points on non-tree edges, segment every network edge into constrained triangulation edges, build a CGAL constrained Delaunay triangulation and trace the cut-off contour. Directed networks require direction-specific extended-tree handling; multiple entrances can be linked to a zero-cost virtual root. Section 2.4 evaluates polygons against grid points classified through a network plus off-network distance model.

The [official reference implementation](https://gitlab.com/Drsulmp/tcageneration) was inspected at commit `066c6d6796250fd186a382dc612678b779d671cc`. It is a C++/Visual Studio project documented against GDAL 2.3.2, Boost 1.69, CGAL 4.13 and optional Qt 5.13.2. CGAL is unavailable in this project's Python environment, and the source is not a drop-in adapter for an OSMnx directed MultiDiGraph with time weights. Rebuilding that stack for this take-home would add material dependency and verification risk.

## Hole diagnosis

The existing polygon has **37** interior rings covering **5.20 ha**. The classifications are: A water-related 0, B likely buffer artefact 34, C potentially inaccessible land 1, D network uncertainty 0, and E manual review 2.

Each ring is assessed with its geometry, mapped-water overlap, minimum and centroid distance to the reachable network, reviewed/full-network links within it, reachable-boundary support, and a separate 10 m point-access classification. OS NGD Building, Land and Site attributes are included only where the existing ten-feature discovery samples intersect. Those samples are incomplete, so their absence is never treated as proof that a building or land use is absent.

Method B fills **34** rings covering **4.98 ha** and retains **3** rings. Its exterior boundary Hausdorff difference from Method A is **0.000000 m**.

## Methods

- **A — raw 25 m network buffer.** The existing unmodified baseline.
- **B — controlled hole treatment.** Fills only Category B rings. A ring needs water/network evidence plus either at least three 10 m reference points with at least 80% accessibility and 60% reachable-boundary support, or a separately accessible representative point, at least 75% boundary support and area no more than 500 m² when the grid is too coarse. Water, uncertain-network and weakly accessible rings are retained. The exterior boundary is preserved.
- **C — experimental research-inspired contour proxy.** Samples every physical directed network edge at 15 m, evaluates the minimum A/B arrival time at each sample, constructs a SciPy ordinary Delaunay triangulation, masks triangles with sides over 150 m, clips triangles at 600 seconds and subtracts mapped water. It includes all relevant edges and intermediate points, but it does **not** insert the paper's exact extended-tree break nodes and does **not** enforce network segments as constrained Delaunay edges. It is therefore an experimental comparator, not a reproduction of Lin et al.

Method C metadata: {"barrier_treatment": "selected OSM water subtracted after contour construction", "constraint_status": "network edges are not enforced as CDT constraints", "implementation_status": "experimental deviation; not a reproduction of Lin et al. (2020)", "max_triangle_edge_m": 150.0, "network_sample_spacing_m": 15.0, "sample_count": 5443, "threshold_s": 600.0, "triangle_count": 10855, "triangles_masked_over_max_edge": 217, "triangulation": "SciPy ordinary Delaunay"}

## Comparison

| Method | Area (ha) | Population | Density (/ha) | Holes | Hole area (ha) | Water overlap (ha) | Components | Network coverage | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A — Raw 25 m network buffer | 69.44 | 8,161 | 117.5 | 37 | 5.20 | 2.75 | 1 | 100.00% | 0.881 | 0.893 | 0.887 |
| B — Controlled hole treatment | 74.42 | 8,888 | 119.4 | 3 | 0.22 | 2.75 | 1 | 100.00% | 0.888 | 0.964 | 0.924 |
| C — Research-inspired contour proxy | 67.93 | 8,006 | 117.9 | 3 | 0.10 | 0.00 | 2 | 99.64% | 0.967 | 0.958 | 0.962 |

- A versus B: 4.98 ha symmetric difference (0.00 ha only in A; 4.98 ha only in B).
- A versus C: 15.38 ha symmetric difference (8.44 ha only in A; 6.94 ha only in C).
- B versus C: 10.41 ha symmetric difference (8.44 ha only in B; 1.96 ha only in C).

Method A's holes are mainly ordinary spaces between 25 m edge buffers. Method B fills the rings supported by the independent point-access model while retaining ambiguous land. Method C changes both internal and external geometry because it interpolates a travel-time surface rather than editing buffer rings; water subtraction also splits narrow water crossings while preserving reachable land on both sides of valid bridges.

## Barrier and crossing review

The comparison extent intersects **10** selected OSM water features. The reviewed routed graph contains **47** reachable physical edges in this extent with a non-empty `bridge`, `tunnel` or `layer` tag; **7** overlap mapped water geometry. These records are exported for review. Their connectivity comes from graph edges and tags, never from apparent 2D line intersection. Water subtraction removes mapped water surfaces but retains reachable land polygons on each side of graph-connected bridge crossings.

## Accessibility validation

The reference labels come from a regular 10 m grid independent of all candidate polygons. For each point, the model uses the nearest reviewed physical network edge, the minimum directed multi-source arrival time at the projected location, Euclidean off-network time at 4.8 km/h, and a water/connector barrier check. Candidate polygons are used only as predictions. Precision, recall and F1 are therefore available as **model-consistency metrics**, not empirical ground truth. Building interiors, gates, private parcels and vertical circulation remain incompletely modelled.

Method C achieves the highest model-consistency F1 (0.962), but the unavailable constrained triangulation means this score does not validate the paper's algorithm. Method B improves F1 from 0.887 to 0.924 without moving the baseline exterior boundary.

## Population sensitivity

| Method | Weighting | Area (ha) | Estimated population | Density (/ha) |
|---|---|---:|---:|---:|
| A | standard_area_weighting | 69.44 | 8,161 | 117.5 |
| A | water_adjusted_area_weighting | 66.69 | 10,129 | 151.9 |
| B | standard_area_weighting | 74.42 | 8,888 | 119.4 |
| B | water_adjusted_area_weighting | 71.66 | 11,059 | 154.3 |
| C | standard_area_weighting | 67.93 | 8,006 | 117.9 |
| C | water_adjusted_area_weighting | 67.93 | 10,508 | 154.7 |

Standard estimates use each full original LSOA area as the denominator. The separate water-adjusted scenario subtracts mapped water from both source-zone denominators and catchment intersections. Both remain area-weighted estimates using the ONS Census 2021 baseline.

## Recommendation

Use **Method B — controlled hole treatment** as the recommended planning/workshop sensitivity after review. It directly explains which baseline rings changed and why, preserves the established external reach, is reproducible in the existing Python stack, and materially improves consistency with the independent point-access model. Keep Method A beside it as the conservative audit baseline. Keep Method C as an experimental research comparator because its higher F1 is promising but its ordinary Delaunay surface cannot provide the geometric guarantees of the paper's CGAL constrained triangulation.

Do not overwrite Figure 01 yet. Review the hole inventory and Figure 04 first. If a later production implementation installs CGAL and ports the directed, multi-origin extended-tree workflow with tests against the reference program, Method C can be reassessed.

## Remaining limitations

- The 10 m reference grid is a model-based benchmark rather than observed pedestrian traces or parcel-entry evidence.
- Straight off-network movement can still simplify buildings, fences, private land and vertical access; water crossings are explicitly checked.
- OS NGD discovery samples are not complete building or land-use coverage.
- OSM topology, access tags and the three manually approved local connections remain inherited inputs.
- Area-weighted population assumes residents are uniformly distributed within each source zone; the water-adjusted version only changes the assumed allocatable area.
