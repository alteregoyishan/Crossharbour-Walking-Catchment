# Crossharbour walking catchment — methodology

The 10-minute walking catchment around Crossharbour contains an estimated 8,016 residents, with an average density of 117.8 residents per hectare across approximately 68 hectares of analytical land. 3 schools are identified within the catchment, including 2 primary and 1 secondary schools. The catchment follows the pedestrian network rather than extending evenly from the site. Docks and the River Thames constrain east–west movement, while connections extend primarily north–south through the Isle of Dogs. This highlights the importance of connecting the site to surrounding neighbourhoods, schools and community infrastructure. Five- and 15-minute catchments provide reference scenarios. Alternative population allocations test sensitivity to where residents are located within census areas. Population is estimated from Census 2021, not a present-day headcount. The model represents existing walking connections from two proxy access points, not confirmed future entrances. School locations indicate spatial proximity, not capacity or admissions; pedestrian gates remain unverified.

## Methods

Reviewed OpenStreetMap walking network; two boundary-crossing access proxies A/B; 4.8 km/h; multi-source Dijkstra and partial-edge clipping. The final workshop polygon is an ordinary-Delaunay travel-time contour with mapped water removed from analytical land. Cartographic regularisation and bridge display corridors do not enter the population-area denominator. The original 25 m network buffer remains a comparison baseline. The independent validation grid is a model-based consistency check, not observed ground truth.

Census 2021 residents are allocated using population × intersection area / original full LSOA area. Density uses 68.038243 ha analytical land. School locations are identified using polygon covers, including points on the boundary; no school gate-to-gate time is implied. Land, footprint and direct floor-space allocations are sensitivity scenarios, not confidence intervals. Analysis uses British National Grid (EPSG:27700).

## Limitations

Access proxies are not future entrances. Census 2021 is not a present-day headcount. The baseline assumes uniform distribution within LSOAs. School locations are not verified gates. Results depend on OSM completeness and fixed walking speed, excluding delays and mobility variation. The polygon is a derived representation rather than an exact physical boundary.

## Technical evidence

Cleaning logs, network QA, polygonisation comparisons and population sensitivity are preserved in [https://github.com/alteregoyishan/Crossharbour-Walking-Catchment](https://github.com/alteregoyishan/Crossharbour-Walking-Catchment).
