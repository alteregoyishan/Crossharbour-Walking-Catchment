GIS consultant take-home exercise - data pack
=============================================

Prepared for Prior + Partners' GIS consultant take-home exercise. The plot is
fictional and does not represent any real proposal or client. The files were
put together quickly by a colleague and may contain errors, as real project
data often does. Checking them is part of the task.

Files
-----
1. site_boundary.geojson
   Boundary of a single building plot, sent by the design team. Traced from
   an aerial photo in a web mapping tool.

2. schools.csv
   Extract from the Department for Education "Get Information about Schools"
   (GIAS) public download, all establishments file dated 23 September 2026.
   Extract rule used by our colleague: establishments located inside a
   4 km x 4 km box around the plot, plus establishments in Tower Hamlets and
   Greenwich that have no location. Column names are as published by GIAS.
   Source: https://get-information-schools.service.gov.uk/Downloads

3. lsoa_2021_population.gpkg  (layer: lsoa_2021_population)
   Office for National Statistics (ONS) Lower layer Super Output Areas
   (December 2021) Boundaries EW BFE (V10), for the same 4 km x 4 km box, with
   Census 2021 table TS001 "Number of usual residents in households and
   communal establishments" (Census Day, 21 March 2021) joined on LSOA21CD.
     usual_residents_total      = TS001 "Total: All usual residents"
     usual_residents_household  = TS001 "Lives in a household"
     usual_residents_communal   = TS001 "Lives in a communal establishment"
   Boundaries: ONS Open Geography Portal, https://geoportal.statistics.gov.uk/
   Census table: Nomis, https://www.nomisweb.co.uk/datasets/c2021ts001

Not provided
------------
Street and pedestrian path network: download the OpenStreetMap data you need
yourself and record how you did it.

Licences and credits (please show them on your map(s))
------------------------------------------------------
- Schools: contains public sector information licensed under the Open
  Government Licence v3.0 (Department for Education, Get Information about
  Schools).
- LSOA boundaries and Census 2021: Source: Office for National Statistics
  licensed under the Open Government Licence v3.0. Contains OS data
  (c) Crown copyright and database right 2026.
- OpenStreetMap: (c) OpenStreetMap contributors, Open Database Licence (ODbL).
