# OS NGD feature discovery for Crossharbour

## Scope

This was a read-only discovery exercise. It did not alter the OSM pedestrian graph, existing catchments, school results or population estimates.

- Live endpoint: OS NGD API – Features `collections`, `schema`, `queryables` and `items`.
- Query area: the supplied development site buffered by 350 m; EPSG:27700 bbox `537642.24,178797.29,538415.76,179565.71`.
- Sample limit: 10 features per collection. No London-wide request was made.
- Authentication: the existing OS Data Hub project key already used by OS Places. The key value is not recorded.
- Discovery date: 10 October 2026.
- The collections endpoint returned 94 collections. The 12 selected candidates below all returned HTTP 200 for schema, queryables and sample requests.

The live service declares OGC API Features core, GeoJSON, CRS, filter, simple CQL and CQL-text conformance. The sample requests verified `bbox`, `bbox-crs=EPSG:27700`, output `crs=EPSG:27700` and each collection's actual queryable fields.

## Candidate collections

| Collection | Geometry | Local sample | Useful observed/schema attributes | Assessment |
|---|---|---:|---|---|
| `bld-fts-building-4` — Building v4 | Polygon | 10 valid | `osid`, `buildinguse`, `buildinguse_oslandusetiera`, `buildinguse_addresscount_residential`, `buildinguse_addresscount_total`, `numberoffloors`, `geometry_area_m2`, `primarysiteid`, `uprnreference`, `physicalstate` | **Core.** Building accessibility and dasymetric population allocation. |
| `bld-fts-buildingpart-2` — Building Part v2 | Polygon | 10 valid | `osid`, `toid`, `geometry_area_m2`, `description`, `oslandusetiera`, `numberoffloors`, height fields, `address_primarydescription`, `physicallevel`, `isobscured` | Optional refinement where multipart buildings or vertical form matters. |
| `bld-fts-buildingaccesslocation-1` — Building Access Location v1 | Point | 0 | `osid`, `accessedbuildingid`, `access_mode`, `access_purpose`, `access_obstruction`, `access_level` | Accessible, but no local sample records. Coverage must be measured before use. |
| `lus-fts-site-2` — Site v2 | MultiPolygon | 10 valid | `osid`, `toid`, `description`, `oslandusetiera`, `oslandusetierb`, `addresscount_residential`, `addresscount_total`, `mainbuildingid`, `matcheduprn`, `geometry_area_m2` | **Core.** Best available OS grouping for land-use sites; useful for block-aware selection, but not a complete urban-block tessellation. |
| `lus-fts-siteaccesslocation-2` — Site Access Location v2 | Point | 10 valid | `osid`, `accessedsiteid`, `access_mode`, `access_purpose`, `accessednetworknodeid`, `accessednetworknodefeaturetype`, `distancetonetworknode_m` | **Core for access QA.** Samples included Pedestrian / Pedestrian And Vehicular and Public / Primary Public access. |
| `lus-fts-siteroutingpoint-1` — Site Routing Point v1 | Point | 0 | `osid`, `toid`, `description`, `roadlinkid` | Accessible, but no local records; do not depend on it. |
| `lnd-fts-land-3` — Land v3 | Polygon | 10 valid | `osid`, `toid`, `description`, `oslandcovertiera/b`, `oslandusetiera/b`, `nlud_groupdescription`, `physicallevel`, `isobscured`, `geometry_area_m2` | **Core.** Land-cover parcels, residential gardens, made surfaces and open-space context. |
| `str-fts-structure-3` — Structure v3 | Polygon | 10 valid | `osid`, `toid`, `description`, `physicallevel`, `isobscured`, land-cover/use and height fields | **Core for barrier QA.** Local sample included upper-level communication and other structures. |
| `str-fts-compoundstructure-3` — Compound Structure v3 | MultiPolygon | 10 valid | `osid`, `description`, `networkover`, `networkunder`, `status`, `geometry_area_m2` | **Core for grade separation.** Local sample included Bridge, Footbridge and Swing Bridge over road, path, rail or water. |
| `trn-fts-roadtrackorpath-3` — Road Track Or Path v3 | Polygon | 10 valid | `osid`, `toid`, `description`, land-cover/use, `physicallevel`, `isobscured`, `geometry_area_m2` | **Core.** Road/path/pavement surface polygons adjoining reachable network lines. |
| `trn-ntwk-pathlink-3` — Path Link v3 | LineString | 10 valid | `osid`, `toid`, `description`, `surfacetype`, node IDs, grade separation and length | Secondary comparison/QA against OSM; not a silent replacement network. |
| `trn-ntwk-pavementlink-1` — Pavement Link v1 | LineString | 10 valid | `osid`, `roadlinkid`, `routehierarchy`, pavement side and minimum/average width | Secondary frontage and pavement QA. Local minimum-width samples ranged from 0.40 m to 1.44 m. |

All returned sample geometries were valid. Detailed live schemas and limited GeoJSON samples are saved under `data/qa/os_ngd_discovery/`; visualisable sample layers are in `data/qa/os_ngd_discovery_samples.gpkg`. `outputs/qa/os_ngd_sample_schema.csv` lists every field, its type, whether it is queryable and up to three observed values.

## Conclusions by objective

### 1. Building-level pedestrian accessibility

Feasible as a reviewed enhancement. Building v4 supplies footprints and identifiers; Site Access Location v2 supplies the strongest local access evidence because its points link an `accessedsiteid` to an OS network node and describe access mode and purpose. Building Access Location has the desired schema but returned no records locally, so it cannot be assumed complete. Access points must still be checked against the pedestrian network used for routing; an OS point must not be connected across water, walls or grade-separated structures merely because it is close in 2D.

### 2. Urban block representation

There is no dedicated complete "urban block" collection in the inspected candidates. A block-aware planning catchment is nevertheless feasible by combining:

1. authoritative reachable pedestrian links;
2. adjoining Road Track Or Path surfaces;
3. linked Site polygons where a valid pedestrian Site Access Location connects the site to the reachable network;
4. Land and Building polygons associated with the selected site/frontage;
5. Water, Structure and Compound Structure checks for physical and grade-separated barriers.

This would be stronger than a uniform 25 m buffer for urban form, but it must be implemented as a new sensitivity method. Adjacency alone is insufficient, and a complete Site polygon must not be included merely because one edge touches the network.

### 3. School entrance identification

Potentially feasible through Site v2 plus Site Access Location v2. The schema contains the relationships required to identify public pedestrian site access. The current 350 m development-site sample contained residential and business sites, not a verified school site, while Building Access Location returned zero records. Reliable school entrances therefore **have not yet been demonstrated for the 42 schools**. A later, bounded per-school query should match each school to an OS Site/UPRN and retain only Pedestrian or Pedestrian And Vehicular access with Public or Primary Public purpose, followed by manual map review.

### 4. Residential population allocation

Building v4 is the strongest improvement over uniform LSOA area weighting. It provides residential address counts, total address counts, building use, footprint area and sometimes floors. Site v2 also provides residential address counts and land-use classification. A defensible dasymetric method would preserve each LSOA total and distribute it among residential buildings primarily by residential address count, using footprint × floors only as a documented fallback. These attributes are allocation weights, not observed population.

### 5. Physical barrier validation

Land v3, Structure v3, Compound Structure v3 and Road Track Or Path v3 are useful. `physicallevel`, `isobscured`, `networkover` and `networkunder` help distinguish genuine crossings from 2D overlap. They complement the existing OSM water mask and are particularly relevant around docks, bridges, upper-level routes and enclosed sites.

## Recommended download set for a later prototype

Use a bounded extent covering the 15-minute network reach plus a small QA margin, with pagination and actual schema versions discovered at run time.

1. **Primary:** Building v4, Site v2, Site Access Location v2, Land v3, Road Track Or Path v3, Structure v3 and Compound Structure v3.
2. **Conditional:** Building Part v2 and Building Access Location v1 after coverage testing.
3. **QA only:** Path Link v3 and Pavement Link v1 for comparison with the audited OSM network.

Do not download all London, replace OSM routing automatically, or use address/building/site points as entrances without relationship and access-purpose evidence.
