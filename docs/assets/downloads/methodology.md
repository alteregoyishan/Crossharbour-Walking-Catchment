# Crossharbour walking catchment — methodology

A 10-minute walking catchment was modelled from two plausible pedestrian access points on the northern edge of the Crossharbour development plot. It covers approximately 68 hectares of analytical land and contains an estimated 8,016 residents, or 117.8 residents per hectare. 3 schools are identified within the catchment: Canary Wharf Primary, East Ferry; Canary Wharf Secondary; Cubitt Town Primary School. Population is based on Census 2021 totals allocated proportionally by the area of each census zone inside the catchment. Alternative residential land, building-footprint and floor-space allocations produce higher estimates, demonstrating sensitivity to where residents are located within census areas. The two access points are planning proxies rather than confirmed future entrances. School coordinates represent cleaned locations rather than verified gates, and the catchment represents modelled pedestrian accessibility. These findings provide a useful starting point for early-stage masterplanning: consider the surrounding residential population and school context alongside proposed pedestrian connections and confirm entrance arrangements as the design develops.

## Methods

Reviewed OpenStreetMap walking network; two boundary-crossing access proxies A/B; 4.8 km/h; multi-source Dijkstra and partial-edge clipping. The final workshop polygon is an ordinary-Delaunay travel-time contour with mapped water removed from analytical land. Cartographic regularisation and bridge display corridors do not enter the population-area denominator. The original 25 m network buffer remains a comparison baseline. The independent validation grid is a model-based consistency check, not observed ground truth.

Census 2021 residents are allocated using population × intersection area / original full LSOA area. Density uses 68.038243 ha analytical land. School locations are identified using polygon covers, including points on the boundary; no school gate-to-gate time is implied. Land, footprint and direct floor-space allocations are sensitivity scenarios, not confidence intervals. Analysis uses British National Grid (EPSG:27700).

## Limitations

Access proxies are not future entrances. Census 2021 is not a present-day headcount. The baseline assumes uniform distribution within LSOAs. School locations are not verified gates. Results depend on OSM completeness and fixed walking speed, excluding delays and mobility variation. The polygon is a derived representation rather than an exact physical boundary.

## Technical evidence

Cleaning logs, network QA, polygonisation comparisons and population sensitivity are preserved in [https://github.com/alteregoyishan/Crossharbour-Walking-Catchment](https://github.com/alteregoyishan/Crossharbour-Walking-Catchment).
