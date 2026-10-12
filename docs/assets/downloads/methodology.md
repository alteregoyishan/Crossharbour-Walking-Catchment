# Crossharbour walking catchment — methodology

The 10-minute walking catchment around Crossharbour contains an estimated 8,016 residents, with an average density of 117.8 residents per hectare across approximately 68 hectares of analytical catchment land. Three schools are identified within the catchment, including two primary and one secondary school.

The catchment follows the pedestrian network rather than extending evenly from the site. Docks and the River Thames constrain east–west movement, while connections extend more strongly north–south through the Isle of Dogs. This pattern highlights the role of pedestrian connectivity and crossing opportunities in linking the site to surrounding neighbourhoods, schools and community infrastructure.

Five- and 15-minute catchments are provided as reference scenarios, while alternative population allocation methods test the sensitivity of estimates to where residents are assumed to be located within census areas.

Population estimates use Census 2021 data rather than a present-day headcount. The model represents existing walking connections from two proxy site access points rather than confirmed future entrances. School locations indicate proximity only; capacity and pedestrian gate locations are outside the scope of this analysis.

## Methods

Reviewed OpenStreetMap walking network; two boundary-crossing access proxies A/B; 4.8 km/h; multi-source Dijkstra and partial-edge clipping. The final workshop polygon is an ordinary-Delaunay travel-time contour with mapped water removed from analytical land. Cartographic regularisation and bridge display corridors do not enter the population-area denominator. The original 25 m network buffer remains a comparison baseline. The independent validation grid is a model-based consistency check, not observed ground truth.

Census 2021 residents are allocated using population × intersection area / original full LSOA area. Density uses 68.038243 ha analytical land. School locations are identified using polygon covers, including points on the boundary; no school gate-to-gate time is implied. Land, footprint and direct floor-space allocations are sensitivity scenarios, not confidence intervals. Analysis uses British National Grid (EPSG:27700).

## Limitations

Access proxies are not future entrances. Census 2021 is not a present-day headcount. The baseline assumes uniform distribution within LSOAs. School locations are not verified gates. Results depend on OSM completeness and fixed walking speed, excluding delays and mobility variation. Separately mapped footways are retained, but pavements on both sides of every road and all crossing locations are not systematically modelled; crossing detours and boundary travel times may differ. The polygon is a derived representation rather than an exact physical boundary.

## Technical evidence

Cleaning logs, network QA, polygonisation comparisons and population sensitivity are preserved in [https://github.com/alteregoyishan/Crossharbour-Walking-Catchment](https://github.com/alteregoyishan/Crossharbour-Walking-Catchment).
