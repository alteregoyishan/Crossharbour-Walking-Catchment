# Population allocation sensitivity

## Audited primary method

The code applies `P_i,c = P_i × Area(Catchment ∩ LSOA_i) / Area(LSOA_i)` and sums the allocated values. The source is `usual_residents_total` from ONS Census 2021 LSOAs. Areas are calculated in EPSG:27700. The cleaned source contains 109 unique, valid LSOAs; one exact duplicate was removed and one self-intersection was repaired with `make_valid`, both in the logged cleaning stage.

P1 uses each original full LSOA area as its denominator. Water is already absent from the final analytical C1 catchment, but water is not subtracted from the LSOA denominator. Cartographic bridge corridors are excluded. Density divides the estimated population by analytical catchment land hectares.

The primary 10-minute result is **8,016 residents** over the selected analytical C1 land catchment (117.8 residents/ha). It assumes population is uniform within each LSOA.

## OS NGD feasibility

| method              | available   | required_ngd_fields                                                                                             | limitations                                                                                                                                                                 |
|:--------------------|:------------|:----------------------------------------------------------------------------------------------------------------|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| P1                  | True        | none                                                                                                            | Uniform population distribution within each 2021 LSOA.                                                                                                                      |
| P2                  | True        | Site v2: oslandusetiera, nlud_orderdescription, address_primarydescription                                      | Site polygons are land-use support, not dwelling counts; missing-support zones use P1.                                                                                      |
| P3                  | True        | Building v4: buildinguse, address counts, geometry                                                              | Footprint does not represent floor space in high-rise areas; mixed use uses residential-address share.                                                                      |
| P4 low/central/high | True        | Building v4: direct numberoffloors plus P3 fields                                                               | Direct-floor weighted footprint coverage is 99.4%; requires 95%. Mixed-use shares 0.25/0.50/0.75 are explicit sensitivity assumptions; heights are not converted to floors. |
| P5                  | False       | Building Access Location v1: accessedbuildingid and pedestrian access; reviewed barrier-safe network connection | Direct entrance coverage of classified residential buildings is 0.03%; no automatic barrier-crossing snaps are accepted.                                                    |

## OS NGD inventory

The live collections endpoint exposed 94 collections under the current entitlement. Twelve relevant current-version collections were schema-checked; all were accessible. Five layers needed for population testing were then downloaded only for the bounded intersecting-LSOA extent. `schema/sample only` means that collection was inspected but not included in the full bounded population extract. The complete field-level inventory is in `outputs/methodology/os_ngd_feature_inventory.csv` and `outputs/qa/os_ngd_sample_schema.csv`.

| collection_id                    | collection_title            | geometry_type   | access_status   | feature_count      |
|:---------------------------------|:----------------------------|:----------------|:----------------|:-------------------|
| bld-fts-building-4               | Building v4                 | Polygon         | accessible      | 5874               |
| bld-fts-buildingaccesslocation-1 | Building Access Location v1 | Point           | accessible      | 85                 |
| bld-fts-buildingpart-2           | Building Part v2            | Polygon         | accessible      | 8036               |
| str-fts-compoundstructure-3      | Compound Structure v3       | MultiPolygon    | accessible      | schema/sample only |
| lnd-fts-land-3                   | Land v3                     | Polygon         | accessible      | schema/sample only |
| trn-ntwk-pathlink-3              | Path Link v3                | LineString      | accessible      | schema/sample only |
| trn-ntwk-pavementlink-1          | Pavement Link v1            | LineString      | accessible      | schema/sample only |
| trn-fts-roadtrackorpath-3        | Road Track Or Path v3       | Polygon         | accessible      | schema/sample only |
| lus-fts-site-2                   | Site v2                     | MultiPolygon    | accessible      | 4346               |
| lus-fts-siteaccesslocation-2     | Site Access Location v2     | Point           | accessible      | 284                |
| lus-fts-siteroutingpoint-1       | Site Routing Point v1       | Point           | accessible      | schema/sample only |
| str-fts-structure-3              | Structure v3                | Polygon         | accessible      | schema/sample only |

## 10-minute comparison

| method_name                                | result_role   |   estimated_residents |   residents_per_hectare |   absolute_difference_from_p1 |   percentage_difference_from_p1 |   census_population_successfully_allocated_pct |   fallback_zone_count |
|:-------------------------------------------|:--------------|----------------------:|------------------------:|------------------------------:|--------------------------------:|-----------------------------------------------:|----------------------:|
| P1 Area weighted                           | primary       |                8016.2 |                   117.8 |                           0.0 |                             0.0 |                                          100.0 |                     0 |
| P2 Residential-site weighted               | sensitivity   |                9739.4 |                   143.1 |                        1723.2 |                            21.5 |                                          100.0 |                     0 |
| P3 Residential-building footprint weighted | sensitivity   |                9065.5 |                   133.2 |                        1049.3 |                            13.1 |                                          100.0 |                     0 |
| P4 Floor-space proxy — low                 | sensitivity   |                8997.1 |                   132.2 |                         980.9 |                            12.2 |                                          100.0 |                     0 |
| P4 Floor-space proxy — central             | sensitivity   |                8962.3 |                   131.7 |                         946.1 |                            11.8 |                                          100.0 |                     0 |
| P4 Floor-space proxy — high                | sensitivity   |                8938.3 |                   131.4 |                         922.1 |                            11.5 |                                          100.0 |                     0 |

P2 uses explicit residential Site v2 classifications. P3 uses residential building footprints and the observed residential-address share for mixed-use buildings. Clearly non-residential buildings receive zero weight. A zone with no classified support falls back to P1 and remains visible in the QA outputs.

P4 uses direct `numberoffloors` because weighted-footprint coverage is 99.4%, above the declared 95% threshold. Residential buildings receive share 1.00; mixed-use shares are tested at 0.25, 0.50 and 0.75. This produces a 10-minute range of 8,938–8,997 residents, with 8,962 in the central scenario. Heights are inventoried but are never converted into invented storeys.
The scenario names describe the assumed mixed-use residential share, not a guaranteed low-to-high ordering of the final population estimate. Here, raising the mixed-use share from 0.25 to 0.75 reduces the aggregate estimate by 59 residents. Mixed-use floor-space is proportionally more concentrated outside the 10-minute catchment in the affected LSOAs, so its added weight lowers their within-zone allocation fractions; the per-LSOA support percentages and high-minus-low changes are reported in the spatial diagnostics table.

## Spatial diagnostics — why estimates change

The following LSOAs produce the largest absolute difference between P1 and at least one NGD-weighted method. Changes occur where the catchment intersects a different proportion of residential sites, residential footprints or floor-space proxy than of the full Census zone.

| LSOA21CD   |   p1_allocated_population |   p2_allocated_population |   p3_allocated_population |   p4_central_allocated_population |   largest_absolute_ngd_difference |
|:-----------|--------------------------:|--------------------------:|--------------------------:|----------------------------------:|----------------------------------:|
| E01004214  |                     873.4 |                    1620.0 |                    1620.0 |                            1620.0 |                             746.6 |
| E01004217  |                     940.6 |                     437.1 |                     285.3 |                             204.0 |                             736.7 |
| E01004221  |                     619.1 |                    1169.5 |                     943.4 |                             666.4 |                             550.4 |
| E01035703  |                     773.3 |                    1107.0 |                    1107.0 |                            1107.0 |                             333.7 |
| E01032780  |                     109.5 |                      78.1 |                     125.3 |                             434.4 |                             324.9 |

The complete per-zone table reports Census population, full-zone area, catchment overlap, residential-site support, residential and mixed-use footprint, high-rise proxy weight and every P1–P4 allocation.

## P5 entrance accessibility

P5 is not calculated. Direct Building Access Location association covers only 0.03% of classified residential-capable buildings. The remaining population is treated as access unknown, not inaccessible. No automatic connector is allowed to cross mapped water or other obvious barriers.

## Crossharbour limitation and recommendation

Crossharbour and the Isle of Dogs contain substantial high-rise and mixed-use development. The bounded extract contains 37 residential buildings with at least 10 directly recorded floors; the maximum is 75 floors. Similar footprints can contain very different residential floor area, so P3 is not inherently more accurate than P1. Use P1 as the primary workshop estimate because it is transparent and tied directly to authoritative ONS totals. Use P4 central as the principal NGD sensitivity, accompanied by the P4 low/high range. Do not publish P5 until entrance association and barrier-safe network coverage are materially improved.

## Reproducibility

ONS Census 2021 supplies authoritative population counts. OS NGD supplies ancillary spatial support for within-zone allocation. The walking network is the accessibility model, and the catchment polygon is its derived 2D representation. None of the NGD allocations is claimed as ground truth.

NGD query coverage: `[537042.43, 178049.047, 538917.125, 180655.257999999]`. API keys are not stored in data, logs or reports. Detailed method QA is in `outputs/qa/population_ngd_coverage_qa.csv`; feature/zone findings are in `outputs/qa/population_spatial_qa_issues.csv`; spatial diagnostics are in `outputs/tables/population_spatial_diagnostics_by_lsoa.csv`.
