# Crossharbour 10-minute population calculation

## Scope and common denominator

All methods use the same 68.038 ha analytical 10-minute land catchment in EPSG:27700. Water is excluded from the catchment, cartographic bridge corridors are excluded from the denominator, and OS NGD is used only as ancillary spatial support. ONS Census 2021 remains the authoritative population source.

Figure 06 fills this analytical land geometry. Its exterior outline uses the connected display geometry so the reviewed bridge display corridor avoids a distracting small satellite polygon. The corridor is not included in population or density calculations, and internal holes are not outlined.

## Calculation methods

### P1 — Census area weighting (primary)

For each LSOA `i`: `P_i,catchment = P_i × Area(Catchment ∩ LSOA_i) / Area(LSOA_i)`. The final estimate is the sum across intersecting LSOAs. This assumes population is uniformly distributed within each LSOA.

### P2 — NGD residential-site weighting

For each LSOA, Census population is redistributed over the union of OS NGD residential Site polygons: `P_i,catchment = P_i × Area(Catchment ∩ ResidentialSite_i) / Area(ResidentialSite_i)`. Zones with no usable residential support fall back to P1, so Census population is never silently discarded. The current result has 0 fallback zones.

### P3 — Residential-building footprint weighting

Buildings are clipped to each LSOA before weighting. Residential footprints receive weight 1; mixed-use footprints use their observed residential-address share; clearly non-residential buildings receive weight 0. The method then allocates each LSOA total in proportion to those weights. The current result has 0 fallback zones. Footprint is a weak capacity proxy in a high-rise area.

### P4 — Residential floor-space proxy

The building weight is `clipped footprint area × direct numberoffloors × residential share`. Only the recorded OS NGD `numberoffloors` field is used; building height is not converted into invented storeys. Residential share is 1 for residential buildings and 0 for non-residential buildings. Mixed-use shares of 0.25, 0.50 and 0.75 form the low, central and high modelling scenarios. Direct-floor weighted coverage is 99.4%.

The scenario labels refer to the mixed-use share assumption. They do not guarantee that the final population estimate rises with the label. In this dataset, adding mixed-use weight slightly reduces the total because more of that weighted floor-space lies outside the catchment in the affected LSOAs.

### P5 — Building-access accessibility

P5 is not calculated because direct Building Access Location association covers only 0.03% of classified residential-capable buildings. Missing access information is classified as `ACCESS_UNKNOWN`, never automatically inaccessible.

## Results

| Method                                     | Role        |   Population |   Difference from P1 |   Difference from P1 (%) |   Residents per ha |   Allocation coverage (%) |
|:-------------------------------------------|:------------|-------------:|---------------------:|-------------------------:|-------------------:|--------------------------:|
| P1 Area weighted                           | primary     |       8016.2 |                  0.0 |                      0.0 |              117.8 |                     100.0 |
| P2 Residential-site weighted               | sensitivity |       9739.4 |               1723.2 |                     21.5 |              143.1 |                     100.0 |
| P3 Residential-building footprint weighted | sensitivity |       9065.5 |               1049.3 |                     13.1 |              133.2 |                     100.0 |
| P4 Floor-space proxy — low                 | sensitivity |       8997.1 |                980.9 |                     12.2 |              132.2 |                     100.0 |
| P4 Floor-space proxy — central             | sensitivity |       8962.3 |                946.1 |                     11.8 |              131.7 |                     100.0 |
| P4 Floor-space proxy — high                | sensitivity |       8938.3 |                922.1 |                     11.5 |              131.4 |                     100.0 |

## Recommended figures

Use **P1: 8,016 residents (117.8 residents/ha)** as the primary workshop estimate. It is transparent and directly tied to authoritative Census totals.

Use **P4 central: 8,962 residents (131.7 residents/ha)** as the principal NGD sensitivity, accompanied by the full mixed-use-share range of **8,938–8,997 residents**.

P2 and P3 remain supporting sensitivity tests. P5 should not be published as an accessible-population estimate until entrance coverage is materially improved.

## Why the estimates differ

The catchment intersects 12 LSOAs. P1 responds only to the share of each full LSOA inside the catchment. P2–P4 respond to where residential land, residential footprints and recorded floor-space sit within each LSOA. The largest differences are listed in `population_spatial_diagnostics_by_lsoa.csv`, including each method's allocation and the P4 scenario support percentages.

## Source roles

- **ONS Census 2021:** authoritative population counts.
- **OS NGD:** ancillary spatial information for testing within-zone allocation.
- **Walking network:** accessibility model.
- **Catchment polygon:** derived two-dimensional representation of network accessibility.

NGD-weighted estimates are sensitivity results, not ground truth.
