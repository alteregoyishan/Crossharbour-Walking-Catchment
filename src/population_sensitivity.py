"""ONS Census population allocation with OS NGD sensitivity methods.

P1 remains the primary estimate.  P2–P4 redistribute the same Census total
within each LSOA over explicit residential support features.  Missing support
falls back to P1, so no zone population is silently discarded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
import pandas as pd
import requests
from shapely import box, make_valid
from shapely.geometry import LineString, shape
from shapely.ops import nearest_points, unary_union

from .api_credentials import os_project_key
from .os_data_services import os_maps_basemap, selected_water_path
from .os_ngd_discovery import BASE as NGD_BASE, CRS, CRS_URI
from .paths import CLEANED, OUTPUTS, PROCESSED, ROOT
from .service_area_analysis import area_weight, polygonal


OS_DIR = PROCESSED / "OrdnanceSurvey"
NGD_GPKG = OS_DIR / "os_ngd_population_support.gpkg"
NGD_META = OS_DIR / "os_ngd_population_support.json"
C1_GPKG = OUTPUTS / "gis/c1_walking_catchments_final.gpkg"
C1_MAP_METADATA = OUTPUTS / "methodology/final_c1_map_series_metadata.json"
REVIEWED_NETWORK_GPKG = PROCESSED / "OpenStreetMap/pedestrian_network_reviewed.gpkg"
INVENTORY = OUTPUTS / "methodology/os_ngd_feature_inventory.csv"

OUT_TABLE = OUTPUTS / "tables/population_sensitivity_c1.csv"
OUT_ZONE = OUTPUTS / "tables/population_spatial_diagnostics_by_lsoa.csv"
OUT_ZONE_METHODS = OUTPUTS / "tables/population_allocation_methods_by_lsoa.csv"
OUT_P5 = OUTPUTS / "tables/population_accessibility_p5.csv"
OUT_FEASIBILITY = OUTPUTS / "tables/population_method_feasibility.csv"
OUT_QA = OUTPUTS / "qa/population_ngd_coverage_qa.csv"
OUT_QA_ISSUES = OUTPUTS / "qa/population_spatial_qa_issues.csv"
OUT_GPKG = OUTPUTS / "gis/population_sensitivity_c1.gpkg"
OUT_MAP_PNG = OUTPUTS / "maps/figure_06_population_sensitivity_qa.png"
OUT_MAP_PDF = OUTPUTS / "maps/figure_06_population_sensitivity_qa.pdf"
OUT_REPORT = OUTPUTS / "methodology/population_sensitivity.md"
OUT_CALCULATION_SUMMARY = OUTPUTS / "methodology/population_calculation_summary.md"
OUT_METADATA = OUTPUTS / "methodology/population_sensitivity_metadata.json"
LEGACY_ZONE_TABLE = OUTPUTS / "tables/population_allocation_by_lsoa.csv"

LAYER_BY_FAMILY = {
    "building": "buildings",
    "building_part": "building_parts",
    "site": "sites",
    "site_access_location": "site_access_points",
    "building_access_location": "building_access_points",
}
DIRECT_FLOOR_COVERAGE_THRESHOLD = 0.95
MIXED_USE_SHARES = {"low": 0.25, "central": 0.50, "high": 0.75}
LONG_ACCESS_SNAP_M = 30.0
SPATIAL_QA_TYPES = [
    "census_zone_zero_residential_support",
    "census_zone_low_residential_coverage",
    "residential_building_outside_residential_site",
    "mixed_use_building",
    "highrise_residential_building",
    "highrise_mixed_use_building",
    "building_crosses_census_boundary",
    "building_without_access_point",
    "access_point_without_downloaded_building",
    "access_point_long_network_snap",
    "access_point_connector_crosses_water",
]


def _clean_url(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query)
             if key.lower() != "key"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def _serialise(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _download_collection(session, key, collection_id, bounds, limit=100):
    endpoint = f"{NGD_BASE}/collections/{collection_id}/items"
    params = {
        "bbox": ",".join(f"{value:.3f}" for value in bounds),
        "bbox-crs": CRS_URI,
        "crs": CRS_URI,
        "limit": limit,
    }
    url = endpoint
    features = []
    page_count = 0
    while url:
        response = session.get(url, headers={"key": key}, params=params, timeout=120)
        if response.status_code == 400 and page_count == 0:
            params = {"bbox": params["bbox"], "limit": limit}
            response = session.get(url, headers={"key": key}, params=params, timeout=120)
        if response.status_code == 403:
            raise RuntimeError(f"OS NGD entitlement rejected {collection_id} (HTTP 403)")
        response.raise_for_status()
        payload = response.json()
        features.extend(payload.get("features", []))
        page_count += 1
        next_link = next((item.get("href") for item in payload.get("links", [])
                          if item.get("rel") == "next"), None)
        url = _clean_url(urljoin(NGD_BASE + "/", next_link)) if next_link else None
        params = None
        if page_count > 10000:
            raise RuntimeError(f"Pagination guard exceeded for {collection_id}")
    rows = []
    repaired = 0
    for feature in features:
        geometry_json = feature.get("geometry")
        if not geometry_json:
            continue
        geometry = shape(geometry_json)
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            repaired += 1
        properties = {name: _serialise(value)
                      for name, value in feature.get("properties", {}).items()}
        properties["feature_id"] = str(feature.get("id", properties.get("osid", "")))
        properties["geometry"] = geometry
        rows.append(properties)
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)
    return frame, {"collection_id": collection_id, "endpoint": endpoint,
                   "feature_count": len(frame), "page_count": page_count,
                   "invalid_geometries_repaired": repaired}


def download_population_support(population, catchments, force=False):
    """Download full features for LSOAs intersecting any selected catchment."""
    if NGD_GPKG.exists() and NGD_META.exists() and not force:
        metadata = json.loads(NGD_META.read_text(encoding="utf-8"))
        existing_layers = set(__import__("pyogrio").list_layers(NGD_GPKG)[:, 0])
        frames = {
            family: (gpd.read_file(NGD_GPKG, layer=layer).to_crs(CRS)
                     if layer in existing_layers else
                     gpd.GeoDataFrame(geometry=[], crs=CRS))
            for family, layer in LAYER_BY_FAMILY.items()
        }
        return frames, metadata

    key = os_project_key()
    if not key:
        raise RuntimeError("Add the existing OS project key to .env before downloading NGD support data")
    inventory = pd.read_csv(INVENTORY).set_index("priority_family")
    missing = sorted(set(LAYER_BY_FAMILY) - set(inventory.index))
    if missing:
        raise RuntimeError(f"NGD discovery inventory is missing: {missing}")
    all_catchments = catchments.geometry.union_all()
    selected_zones = population[population.intersects(all_catchments)]
    if selected_zones.empty:
        raise RuntimeError("No Census zones intersect the C1 catchments")
    bounds = tuple(float(value) for value in selected_zones.total_bounds)
    session = requests.Session()
    frames = {}
    downloads = []
    NGD_GPKG.unlink(missing_ok=True)
    for family, layer in LAYER_BY_FAMILY.items():
        collection_id = inventory.loc[family, "collection_id"]
        frame, details = _download_collection(session, key, collection_id, bounds)
        frames[family] = frame
        if not frame.empty:
            frame.to_file(NGD_GPKG, layer=layer, driver="GPKG", engine="pyogrio",
                          append=NGD_GPKG.exists())
        details["family"] = family
        details["layer"] = layer
        downloads.append(details)
    metadata = {
        "source": "OS NGD API – Features",
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_bbox_epsg27700": list(bounds),
        "coverage_rule": "bounding box of every 2021 LSOA intersecting the 5/10/15-minute final C1 catchments",
        "api_key_recorded": False,
        "downloads": downloads,
        "sha256": sha256(NGD_GPKG.read_bytes()).hexdigest() if NGD_GPKG.exists() else None,
    }
    NGD_META.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return frames, metadata


def _text(frame, field):
    return frame.get(field, pd.Series("", index=frame.index)).fillna("").astype(str).str.lower()


def classify_sites(sites):
    tier = _text(sites, "oslandusetiera")
    nlud = _text(sites, "nlud_orderdescription")
    address = _text(sites, "address_primarydescription")
    description = _text(sites, "description")
    residential = (tier.eq("residential accommodation") |
                   nlud.eq("residential") | address.eq("residential"))
    explicit_exclusion = description.str.contains(
        r"school|education|dock|water|road|rail|transport", regex=True)
    out = sites.copy()
    out["population_support_class"] = np.where(
        residential & ~explicit_exclusion, "residential_land", "other_land")
    return out


def classify_buildings(buildings):
    use = _text(buildings, "buildinguse")
    description = _text(buildings, "description")
    residential_count = pd.to_numeric(
        buildings.get("buildinguse_addresscount_residential", 0), errors="coerce").fillna(0)
    commercial_count = pd.to_numeric(
        buildings.get("buildinguse_addresscount_commercial", 0), errors="coerce").fillna(0)
    other_count = pd.to_numeric(
        buildings.get("buildinguse_addresscount_other", 0), errors="coerce").fillna(0)
    total_count = residential_count + commercial_count + other_count
    direct_residential = use.str.contains("residential") | description.str.contains("residential")
    has_residential = direct_residential | residential_count.gt(0)
    mixed = has_residential & (commercial_count.gt(0) | use.str.contains("mixed"))
    explicit_nonres = (~has_residential & ~use.isin(["", "unknown", "none", "nan"]))
    classification = np.select(
        [mixed, has_residential, explicit_nonres],
        ["mixed_use", "residential", "non_residential"], default="unclassified")
    share = np.where(total_count.gt(0), residential_count / total_count, np.nan)
    share = np.where(has_residential & ~mixed & np.isnan(share), 1.0, share)
    share = np.where(has_residential & np.isnan(share), 1.0, share)
    share = np.where(has_residential, share, 0.0)
    out = buildings.copy()
    out["population_support_class"] = classification
    out["residential_share_proxy"] = np.clip(share.astype(float), 0, 1)
    out["direct_number_of_floors"] = pd.to_numeric(
        out.get("numberoffloors", np.nan), errors="coerce")
    out["direct_relative_height_m"] = pd.to_numeric(
        out.get("height_relativemax_m", np.nan), errors="coerce")
    return out


def _support_allocation(population, catchment, support, method, support_weight=None):
    """Allocate each LSOA over support geometry, with explicit P1 fallback."""
    rows = []
    for zone in population.itertuples():
        zone_population = float(zone.usual_residents_total)
        p1_fraction = polygonal(zone.geometry.intersection(catchment)).area / zone.geometry.area
        candidates = support[support.intersects(zone.geometry)]
        denominator = 0.0
        numerator = 0.0
        if support_weight is None:
            clipped = [polygonal(geometry.intersection(zone.geometry))
                       for geometry in candidates.geometry]
            support_union = polygonal(make_valid(unary_union(
                [geometry for geometry in clipped if not geometry.is_empty])))
            denominator = float(support_union.area)
            numerator = float(support_union.intersection(catchment).area)
        else:
            for feature in candidates.itertuples():
                clipped = polygonal(feature.geometry.intersection(zone.geometry))
                if clipped.is_empty:
                    continue
                weight = float(getattr(feature, support_weight))
                denominator += clipped.area * weight
                numerator += clipped.intersection(catchment).area * weight
        fallback = denominator <= 0
        fraction = p1_fraction if fallback else min(1.0, max(0.0, numerator / denominator))
        rows.append({
            "method": method,
            "geography_id": zone.LSOA21CD,
            "original_population": zone_population,
            "zone_area_m2": float(zone.geometry.area),
            "support_denominator_m2_equivalent": denominator,
            "support_in_catchment_m2_equivalent": numerator,
            "allocation_fraction": fraction,
            "estimated_population": zone_population * fraction,
            "fallback_to_p1": bool(fallback),
            "p1_fraction": p1_fraction,
        })
    return pd.DataFrame(rows)


def _p4_support(buildings, mixed_use_share):
    """Create an explicit floor-space weight without inventing storeys."""
    out = buildings.copy()
    out["p4_residential_share"] = np.select(
        [out.population_support_class.eq("residential"),
         out.population_support_class.eq("mixed_use")],
        [1.0, float(mixed_use_share)], default=0.0)
    out["floor_area_proxy"] = (out["p4_residential_share"] *
                               out["direct_number_of_floors"])
    return out[out.floor_area_proxy.gt(0)].copy()


def _method_row(minutes, method, details, catchment_area, p1_total,
                assumptions, method_name, allocated_pct=None, unclassified=0):
    estimate = float(details.estimated_population.sum())
    fallback = int(details.fallback_to_p1.sum()) if "fallback_to_p1" in details else 0
    if allocated_pct is None:
        if "fallback_to_p1" in details and float(details.original_population.sum()) > 0:
            allocated_pct = 100 * float(details.loc[
                ~details.fallback_to_p1, "original_population"].sum()) / float(
                    details.original_population.sum())
        else:
            allocated_pct = 100.0
    return {
        "time_minutes": minutes,
        "method": method,
        "method_name": method_name,
        "result_role": "primary" if method == "P1" else "sensitivity",
        "estimated_residents": estimate,
        "residents_per_hectare": estimate / (catchment_area / 10000),
        "absolute_difference_from_p1": estimate - p1_total,
        "percentage_difference_from_p1": 100 * (estimate / p1_total - 1) if p1_total else np.nan,
        "census_population_successfully_allocated_pct": allocated_pct,
        "fallback_zone_count": fallback,
        "unclassified_building_count": unclassified,
        "major_assumptions": assumptions,
    }


def analyse_population(population, catchments, frames):
    sites = classify_sites(frames["site"])
    buildings = classify_buildings(frames["building"])
    residential_sites = sites[sites.population_support_class == "residential_land"].copy()
    residential_buildings = buildings[
        buildings.population_support_class.isin(["residential", "mixed_use"]) &
        buildings.residential_share_proxy.gt(0)].copy()
    unclassified_count = int((buildings.population_support_class == "unclassified").sum())

    residential_footprint = float((residential_buildings.geometry.area *
                                    residential_buildings.residential_share_proxy).sum())
    floors_present = residential_buildings.direct_number_of_floors.gt(0)
    floor_covered = float((residential_buildings.loc[floors_present].geometry.area *
                           residential_buildings.loc[floors_present].residential_share_proxy).sum())
    floor_coverage = floor_covered / residential_footprint if residential_footprint else 0.0
    height_coverage = float(residential_buildings.direct_relative_height_m.gt(0).mean()) if len(residential_buildings) else 0.0
    p4_available = floor_coverage >= DIRECT_FLOOR_COVERAGE_THRESHOLD

    access = frames.get("building_access_location", gpd.GeoDataFrame(geometry=[], crs=CRS))
    accessed_ids = set(access.get("accessedbuildingid", pd.Series(dtype=str)).dropna().astype(str))
    residential_ids = set(residential_buildings.get("osid", pd.Series(dtype=str)).dropna().astype(str))
    entrance_coverage = len(accessed_ids & residential_ids) / len(residential_ids) if residential_ids else 0.0

    selected = catchments[catchments.time_minutes == 10]
    if len(selected) != 1:
        raise RuntimeError("Expected exactly one 10-minute analytical catchment")
    minutes = 10
    catchment = selected.geometry.iloc[0]
    relevant_population = population[population.intersects(catchment)].copy()
    result_rows = []
    zone_tables = []

    p1 = area_weight(relevant_population, catchment).drop(columns="geometry")
    p1 = p1.rename(columns={"area_fraction": "allocation_fraction"})
    p1["method"] = "P1"
    p1["fallback_to_p1"] = False
    p1_total = float(p1.estimated_population.sum())
    result_rows.append(_method_row(
        minutes, "P1", p1, catchment.area, p1_total,
        "ONS Census 2021 population is uniformly distributed over each full LSOA; EPSG:27700 analytical land catchment; cartographic bridge corridors excluded.",
        "P1 Area weighted"))

    p2 = _support_allocation(relevant_population, catchment, residential_sites, "P2")
    result_rows.append(_method_row(
        minutes, "P2", p2, catchment.area, p1_total,
        "Population is distributed over explicit OS NGD residential Site v2 polygons; zones with zero support use the documented P1 fallback.",
        "P2 Residential-site weighted"))

    p3 = _support_allocation(relevant_population, catchment, residential_buildings, "P3",
                             "residential_share_proxy")
    result_rows.append(_method_row(
        minutes, "P3", p3, catchment.area, p1_total,
        "Residential footprints receive weight 1; mixed-use footprints use the observed residential-address share; clearly non-residential buildings receive no population.",
        "P3 Residential-building footprint weighted",
        unclassified=unclassified_count))

    zone_tables.extend([p1.assign(time_minutes=minutes),
                        p2.assign(time_minutes=minutes),
                        p3.assign(time_minutes=minutes)])
    if p4_available:
        for scenario, mixed_share in MIXED_USE_SHARES.items():
            method = f"P4_{scenario}"
            p4 = _support_allocation(
                relevant_population, catchment,
                _p4_support(residential_buildings, mixed_share), method,
                "floor_area_proxy")
            result_rows.append(_method_row(
                minutes, method, p4, catchment.area, p1_total,
                f"Direct OS NGD numberoffloors × footprint; residential share 1.00 and mixed-use share {mixed_share:.2f}; no height-to-storey conversion; zero-support zones use P1.",
                f"P4 Floor-space proxy — {scenario}",
                unclassified=unclassified_count))
            zone_tables.append(p4.assign(time_minutes=minutes))

    results = pd.DataFrame(result_rows)
    zones = pd.concat(zone_tables, ignore_index=True, sort=False)
    feasibility = pd.DataFrame([
        {"method":"P1", "available":True, "required_ngd_fields":"none",
         "limitations":"Uniform population distribution within each 2021 LSOA."},
        {"method":"P2", "available":not residential_sites.empty,
         "required_ngd_fields":"Site v2: oslandusetiera, nlud_orderdescription, address_primarydescription",
         "limitations":"Site polygons are land-use support, not dwelling counts; missing-support zones use P1."},
        {"method":"P3", "available":not residential_buildings.empty,
         "required_ngd_fields":"Building v4: buildinguse, address counts, geometry",
         "limitations":"Footprint does not represent floor space in high-rise areas; mixed use uses residential-address share."},
        {"method":"P4 low/central/high", "available":p4_available,
         "required_ngd_fields":"Building v4: direct numberoffloors plus P3 fields",
         "limitations":f"Direct-floor weighted footprint coverage is {floor_coverage:.1%}; requires {DIRECT_FLOOR_COVERAGE_THRESHOLD:.0%}. Mixed-use shares 0.25/0.50/0.75 are explicit sensitivity assumptions; heights are not converted to floors."},
        {"method":"P5", "available":False,
         "required_ngd_fields":"Building Access Location v1: accessedbuildingid and pedestrian access; reviewed barrier-safe network connection",
         "limitations":f"Direct entrance coverage of classified residential buildings is {entrance_coverage:.2%}; no automatic barrier-crossing snaps are accepted."},
    ])
    qa = pd.DataFrame([
        {"check":"residential_site_count", "value":len(residential_sites), "status":"information"},
        {"check":"residential_or_mixed_building_count", "value":len(residential_buildings), "status":"information"},
        {"check":"unclassified_building_count", "value":unclassified_count, "status":"review" if unclassified_count else "pass"},
        {"check":"direct_floor_weighted_coverage_pct", "value":100*floor_coverage, "status":"pass" if p4_available else "insufficient_for_p4"},
        {"check":"relative_height_feature_coverage_pct", "value":100*height_coverage, "status":"information_only"},
        {"check":"residential_buildings_10_or_more_floors", "value":int(((residential_buildings.population_support_class == "residential") & (residential_buildings.direct_number_of_floors >= 10)).sum()), "status":"information"},
        {"check":"mixed_use_buildings_10_or_more_floors", "value":int(((residential_buildings.population_support_class == "mixed_use") & (residential_buildings.direct_number_of_floors >= 10)).sum()), "status":"sensitivity_required"},
        {"check":"maximum_direct_number_of_floors", "value":float(residential_buildings.direct_number_of_floors.max()), "status":"information"},
        {"check":"residential_building_direct_entrance_coverage_pct", "value":100*entrance_coverage, "status":"insufficient_for_p5" if entrance_coverage < .95 else "manual_network_review_required"},
        {"check":"fallback_zones_p2_10min", "value":int(zones[(zones.method=="P2") & (zones.time_minutes==10)].fallback_to_p1.sum()), "status":"documented"},
        {"check":"fallback_zones_p3_10min", "value":int(zones[(zones.method=="P3") & (zones.time_minutes==10)].fallback_to_p1.sum()), "status":"documented"},
    ])
    p5 = pd.DataFrame([{
        "method": "P5 entrance-accessible population",
        "status": "not_implemented_insufficient_coverage",
        "entrance_accessible_population": np.nan,
        "entrance_inaccessible_population": np.nan,
        "access_unknown_population": np.nan,
        "access_classified_population_pct": 0.0,
        "access_unknown_population_pct": 100.0,
        "associated_residential_building_pct": 100 * entrance_coverage,
        "reason": ("Building Access Location coverage is insufficient and no automatic "
                   "barrier-crossing network connectors are accepted; population is not "
                   "classified as inaccessible merely because an entrance is absent."),
    }])
    return (results, zones, feasibility, qa, sites, buildings,
            residential_sites, residential_buildings, p5)


def _clipped_union(frame, geometry):
    candidates = frame[frame.intersects(geometry)]
    pieces = [polygonal(item.intersection(geometry)) for item in candidates.geometry]
    pieces = [item for item in pieces if not item.is_empty]
    return polygonal(make_valid(unary_union(pieces))) if pieces else polygonal(
        make_valid(unary_union([])))


def build_zone_diagnostics(population, catchment, residential_sites,
                           residential_buildings, zone_methods):
    """Create one auditable row per LSOA intersecting the 10-minute catchment."""
    allocations = zone_methods.pivot_table(
        index="geography_id", columns="method", values="estimated_population",
        aggfunc="first")
    allocation_fractions = zone_methods.pivot_table(
        index="geography_id", columns="method", values="allocation_fraction",
        aggfunc="first")
    rows = []
    relevant = population[population.intersects(catchment)].copy()
    for zone in relevant.itertuples():
        zone_geometry = zone.geometry
        overlap_area = polygonal(zone_geometry.intersection(catchment)).area
        site_geometry = _clipped_union(residential_sites, zone_geometry)
        site_in_catchment = site_geometry.intersection(catchment).area
        zone_buildings = residential_buildings[
            residential_buildings.intersects(zone_geometry)]
        residential = zone_buildings[
            zone_buildings.population_support_class == "residential"]
        mixed = zone_buildings[zone_buildings.population_support_class == "mixed_use"]

        def clipped_area(frame):
            return float(sum(polygonal(item.intersection(zone_geometry)).area
                             for item in frame.geometry))

        highrise = zone_buildings[zone_buildings.direct_number_of_floors.ge(10)]
        highrise_weight = 0.0
        for building in highrise.itertuples():
            area = polygonal(building.geometry.intersection(zone_geometry)).area
            share = 1.0 if building.population_support_class == "residential" else 0.5
            highrise_weight += area * float(building.direct_number_of_floors) * share
        values = allocations.loc[zone.LSOA21CD] if zone.LSOA21CD in allocations.index else pd.Series()
        row = {
            "LSOA21CD": zone.LSOA21CD,
            "census_population": float(zone.usual_residents_total),
            "zone_area_m2": float(zone_geometry.area),
            "catchment_overlap_area_m2": float(overlap_area),
            "catchment_overlap_pct": 100 * overlap_area / zone_geometry.area,
            "ngd_residential_site_area_m2": float(site_geometry.area),
            "residential_site_in_catchment_area_m2": float(site_in_catchment),
            "residential_site_overlap_with_catchment_pct": (
                100 * site_in_catchment / site_geometry.area if site_geometry.area else 0.0),
            "residential_building_footprint_m2": clipped_area(residential),
            "mixed_use_building_footprint_m2": clipped_area(mixed),
            "highrise_residential_proxy_weight_m2_floors": highrise_weight,
        }
        for method in ["P1", "P2", "P3", "P4_low", "P4_central", "P4_high"]:
            row[f"{method.lower()}_allocated_population"] = float(
                values.get(method, np.nan))
        fractions = (allocation_fractions.loc[zone.LSOA21CD]
                     if zone.LSOA21CD in allocation_fractions.index else pd.Series())
        for method in ["P4_low", "P4_central", "P4_high"]:
            row[f"{method.lower()}_catchment_support_pct"] = 100 * float(
                fractions.get(method, np.nan))
        row["p2_difference_from_p1"] = (
            row["p2_allocated_population"] - row["p1_allocated_population"])
        row["p3_difference_from_p1"] = (
            row["p3_allocated_population"] - row["p1_allocated_population"])
        row["p4_central_difference_from_p1"] = (
            row["p4_central_allocated_population"] - row["p1_allocated_population"])
        row["p4_high_minus_low_population"] = (
            row["p4_high_allocated_population"] -
            row["p4_low_allocated_population"])
        rows.append(row)
    diagnostics = pd.DataFrame(rows)
    diagnostics["largest_absolute_ngd_difference"] = diagnostics[[
        "p2_difference_from_p1", "p3_difference_from_p1",
        "p4_central_difference_from_p1"]].abs().max(axis=1)
    diagnostics["difference_rank"] = diagnostics[
        "largest_absolute_ngd_difference"].rank(method="dense", ascending=False).astype(int)
    return diagnostics.sort_values("difference_rank").reset_index(drop=True)


def build_spatial_qa_issues(population, catchment, diagnostics, residential_sites,
                            residential_buildings, buildings, access, network, water):
    """Return explicit feature/zone QA findings without repairing the inputs."""
    rows = []

    def add(issue_type, feature_id, geometry, evidence, severity="REVIEW"):
        point = geometry.representative_point() if not geometry.is_empty else None
        rows.append({
            "issue_type": issue_type,
            "severity": severity,
            "feature_id": str(feature_id),
            "easting": point.x if point else np.nan,
            "northing": point.y if point else np.nan,
            "evidence": evidence,
        })

    zone_geometry = population[population.intersects(catchment)].geometry.union_all()
    zone_by_id = population.set_index("LSOA21CD")
    for row in diagnostics.itertuples():
        geometry = zone_by_id.loc[row.LSOA21CD].geometry
        site_pct = 100 * row.ngd_residential_site_area_m2 / row.zone_area_m2
        if row.ngd_residential_site_area_m2 <= 0:
            add("census_zone_zero_residential_support", row.LSOA21CD, geometry,
                "No classified NGD residential-site area; P2 uses P1 fallback.", "HIGH")
        elif site_pct < 1.0:
            add("census_zone_low_residential_coverage", row.LSOA21CD, geometry,
                f"Residential-site support covers {site_pct:.2f}% of the LSOA.", "MEDIUM")

    relevant_buildings = residential_buildings[
        residential_buildings.intersects(zone_geometry)].copy()
    site_union = residential_sites.geometry.union_all()
    accessed_ids = set(access.get(
        "accessedbuildingid", pd.Series(dtype=str)).dropna().astype(str))
    zones = population[population.intersects(catchment)][["LSOA21CD", "geometry"]]
    for building in relevant_buildings.itertuples():
        feature_id = str(getattr(building, "osid", getattr(building, "feature_id", "")))
        footprint = max(building.geometry.area, 1e-9)
        site_share = building.geometry.intersection(site_union).area / footprint
        if site_share < 0.5:
            add("residential_building_outside_residential_site", feature_id,
                building.geometry, f"Only {site_share:.1%} of footprint overlaps residential sites.")
        if building.population_support_class == "mixed_use":
            add("mixed_use_building", feature_id, building.geometry,
                "Residential share is uncertain and is tested explicitly.", "INFORMATION")
        if pd.notna(building.direct_number_of_floors) and building.direct_number_of_floors >= 10:
            add(f"highrise_{building.population_support_class}_building", feature_id,
                building.geometry, f"Direct numberoffloors={building.direct_number_of_floors:g}.",
                "INFORMATION")
        if sum(building.geometry.intersects(item) for item in zones.geometry) > 1:
            add("building_crosses_census_boundary", feature_id, building.geometry,
                "Building footprint intersects more than one LSOA; weights are zone-clipped.")
        if feature_id not in accessed_ids:
            add("building_without_access_point", feature_id, building.geometry,
                "No Building Access Location references this residential-capable building.")

    building_ids = set(buildings.get("osid", pd.Series(dtype=str)).dropna().astype(str))
    for point in access.itertuples():
        point_id = str(getattr(point, "osid", getattr(point, "feature_id", "")))
        building_id = str(getattr(point, "accessedbuildingid", ""))
        if building_id not in building_ids:
            add("access_point_without_downloaded_building", point_id, point.geometry,
                f"accessedbuildingid={building_id} is absent from the bounded building extract.")
        nearest = nearest_points(point.geometry, network)[1]
        distance = point.geometry.distance(nearest)
        if distance > LONG_ACCESS_SNAP_M:
            add("access_point_long_network_snap", point_id, point.geometry,
                f"Straight-line distance to reachable network is {distance:.1f} m (> {LONG_ACCESS_SNAP_M:g} m).",
                "HIGH")
        connector = LineString([point.geometry.coords[0], nearest.coords[0]])
        if connector.intersection(water).length > 0.5:
            add("access_point_connector_crosses_water", point_id, point.geometry,
                "Straight candidate connector intersects mapped water; automatic snapping is prohibited.",
                "HIGH")
    return pd.DataFrame(rows)


def _write_gpkg(frame, layer, first=False):
    if first:
        OUT_GPKG.unlink(missing_ok=True)
    if frame.empty:
        return
    frame.to_file(OUT_GPKG, layer=layer, driver="GPKG", engine="pyogrio",
                  append=OUT_GPKG.exists())


def _exterior_only_boundary(geometry):
    """Return polygon shell lines without drawing any interior holes."""
    geometry = polygonal(make_valid(geometry))
    if geometry.is_empty:
        return LineString()
    parts = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return unary_union([LineString(part.exterior.coords) for part in parts])


def _figure_01_to_04_bounds():
    """Use exactly the fixed extent recorded for the final C1 map series."""
    if not C1_MAP_METADATA.exists():
        raise RuntimeError(
            "Final C1 map metadata is missing; run scripts/16_export_final_c1_maps.py first")
    metadata = json.loads(C1_MAP_METADATA.read_text(encoding="utf-8"))
    bounds = metadata.get("individual_map_bounds_epsg27700")
    if not isinstance(bounds, list) or len(bounds) != 4:
        raise RuntimeError("Final C1 metadata does not contain valid map bounds")
    return tuple(float(value) for value in bounds)


def _plot_qa(population, catchment, display_outline, water, network, sites,
             buildings, access):
    catchment_colour = "#202428"
    # Match Figures 01–04 exactly so this QA map can be compared at the same
    # scale and extent.
    bounds = _figure_01_to_04_bounds()
    fig = plt.figure(figsize=(14.14, 10), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    basemap = os_maps_basemap(bounds)
    if basemap:
        ax.imshow(basemap["image"], extent=basemap["extent"], alpha=.65, zorder=0)
    visible_zones = population[
        population.intersects(catchment) & population.intersects(box(*bounds))]
    visible_zones.plot(
        ax=ax, facecolor="#f1dfc8", alpha=.18,
        edgecolor="#7d766d", linewidth=.65, zorder=1)
    gpd.GeoSeries([water.intersection(box(*bounds))], crs=CRS).plot(
        ax=ax, facecolor="#b8dce8", edgecolor="#6b9eae", alpha=.80, linewidth=.5, zorder=2)
    nonres = buildings[buildings.population_support_class == "non_residential"]
    unclassified = buildings[buildings.population_support_class == "unclassified"]
    residential = buildings[buildings.population_support_class == "residential"]
    mixed = buildings[buildings.population_support_class == "mixed_use"]
    if not nonres.empty:
        nonres.plot(ax=ax, facecolor="#b9b9b4", edgecolor="none", alpha=.60, zorder=3)
    if not unclassified.empty:
        unclassified.plot(ax=ax, facecolor="#e8e4dc", edgecolor="#9c9488", linewidth=.2, zorder=3)
    # Population uses the analytical land geometry.  The exterior-only outline
    # comes from the connected display geometry, whose reviewed bridge corridor
    # avoids a distracting 584 m² satellite polygon without entering the area
    # or population denominator.  Interior rings are never outlined.
    gpd.GeoSeries([_exterior_only_boundary(display_outline)], crs=CRS).plot(
        ax=ax, color=catchment_colour, linewidth=.9, alpha=.92, zorder=4.5)
    sites[sites.population_support_class == "residential_land"].plot(
        ax=ax, facecolor="#8fc5a5", edgecolor="#4c8061", linewidth=.45, alpha=.38, zorder=4)
    if not residential.empty:
        residential.plot(ax=ax, facecolor="#2d7f5e", edgecolor="white", linewidth=.18, alpha=.82, zorder=5)
    if not mixed.empty:
        mixed.plot(ax=ax, facecolor="#9a6eb1", edgecolor="white", linewidth=.18, alpha=.84, zorder=5)
    highrise = buildings[
        buildings.population_support_class.isin(["residential", "mixed_use"]) &
        buildings.direct_number_of_floors.ge(10)]
    if not highrise.empty:
        highrise.boundary.plot(ax=ax, color="#241a15", linewidth=.75, zorder=6)
    handles = [
        Patch(facecolor="#f1dfc8", edgecolor="#7d766d", alpha=.5, label="2021 LSOA"),
        Patch(facecolor="none", edgecolor=catchment_colour, linewidth=.9,
              label="10 min catchment"),
        Patch(facecolor="#8fc5a5", edgecolor="#4c8061", alpha=.5, label="NGD residential site"),
        Patch(facecolor="#2d7f5e", label="Residential building"),
        Patch(facecolor="#9a6eb1", label="Mixed-use building"),
        Patch(facecolor="none", edgecolor="#241a15", linewidth=1.0, label="Building ≥10 floors"),
        Patch(facecolor="#b9b9b4", label="Non-residential building"),
    ]
    legend = ax.legend(
        handles=handles, loc="upper left", bbox_to_anchor=(.015, .985),
        fontsize=8, frameon=True, facecolor="white", framealpha=.96,
        edgecolor="#adb5b8", ncol=2)
    # Matplotlib artists with a higher z-order can otherwise be drawn across
    # the legend in vector output.  Keep the legend above every map layer.
    legend.set_zorder(1000)
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal"); ax.set_axis_off()
    os_credit = (basemap["attribution"] if basemap else
                 "Contains OS data © Crown copyright and database rights 2026")
    attribution = (
        f"{os_credit} | ONS Census 2021 and LSOA boundaries (OGL v3.0) | "
        "© OpenStreetMap contributors (ODbL)")
    ax.text(.995, .006, attribution, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=5.8, color="#46545a", bbox={"facecolor":"white", "edgecolor":"none", "alpha":.8})
    fig.savefig(OUT_MAP_PNG, dpi=220, facecolor="white")
    plt.close(fig)

    fig = plt.figure(figsize=(420/25.4, 297/25.4), facecolor="white")
    ax = fig.add_axes([.035, .075, .93, .80])
    image = plt.imread(OUT_MAP_PNG)
    ax.imshow(image); ax.set_axis_off()
    fig.text(.035, .955, "Crossharbour Population Allocation QA", ha="left", va="top",
             fontsize=20, color="#253842", weight="bold")
    fig.text(.035, .918, "ONS Census 2021 baseline with OS NGD residential-site and building sensitivity evidence",
             ha="left", va="top", fontsize=9.3, color="#586970")
    fig.text(.035, .025, "Census area weighting remains the baseline. Land, building and floor-space allocations are sensitivity scenarios, not confidence intervals.",
             ha="left", va="bottom", fontsize=7.4, color="#586970")
    fig.savefig(OUT_MAP_PDF, facecolor="white", metadata={"Title":"Crossharbour Population Allocation QA"})
    plt.close(fig)


def _write_report(results, feasibility, qa, metadata, diagnostics, p5):
    primary = results.copy()
    feasibility_lines = feasibility.to_markdown(index=False)
    result_lines = primary[["method_name", "result_role", "estimated_residents",
                            "residents_per_hectare", "absolute_difference_from_p1",
                            "percentage_difference_from_p1",
                            "census_population_successfully_allocated_pct",
                            "fallback_zone_count"]].to_markdown(
                                index=False, floatfmt=".1f")
    p1 = primary[primary.method == "P1"].iloc[0]
    inventory = pd.read_csv(INVENTORY)
    downloads = pd.DataFrame(metadata["downloads"])
    inventory_summary = inventory.merge(
        downloads[["collection_id", "feature_count"]], on="collection_id", how="left")
    inventory_summary["feature_count"] = inventory_summary["feature_count"].apply(
        lambda value: str(int(value)) if pd.notna(value) else "schema/sample only")
    inventory_lines = inventory_summary[["collection_id", "collection_title",
                                         "geometry_type", "access_status",
                                         "feature_count"]].to_markdown(index=False)
    highrise_count = int(qa.loc[
        qa.check == "residential_buildings_10_or_more_floors", "value"].iloc[0])
    maximum_floors = int(qa.loc[
        qa.check == "maximum_direct_number_of_floors", "value"].iloc[0])
    floor_coverage = float(qa.loc[
        qa.check == "direct_floor_weighted_coverage_pct", "value"].iloc[0])
    entrance_coverage = float(qa.loc[
        qa.check == "residential_building_direct_entrance_coverage_pct", "value"].iloc[0])
    top_differences = diagnostics.head(5)[[
        "LSOA21CD", "p1_allocated_population", "p2_allocated_population",
        "p3_allocated_population", "p4_central_allocated_population",
        "largest_absolute_ngd_difference"]].to_markdown(index=False, floatfmt=".1f")
    p4_low = primary[primary.method == "P4_low"].iloc[0]
    p4_central = primary[primary.method == "P4_central"].iloc[0]
    p4_high = primary[primary.method == "P4_high"].iloc[0]
    lines = [
        "# Population allocation sensitivity",
        "",
        "## Audited primary method",
        "",
        "The code applies `P_i,c = P_i × Area(Catchment ∩ LSOA_i) / Area(LSOA_i)` and sums the allocated values. The source is `usual_residents_total` from ONS Census 2021 LSOAs. Areas are calculated in EPSG:27700. The cleaned source contains 109 unique, valid LSOAs; one exact duplicate was removed and one self-intersection was repaired with `make_valid`, both in the logged cleaning stage.",
        "",
        "P1 uses each original full LSOA area as its denominator. Water is already absent from the final analytical C1 catchment, but water is not subtracted from the LSOA denominator. Cartographic bridge corridors are excluded. Density divides the estimated population by analytical catchment land hectares.",
        "",
        f"The primary 10-minute result is **{p1.estimated_residents:,.0f} residents** over the selected analytical C1 land catchment ({p1.residents_per_hectare:.1f} residents/ha). It assumes population is uniform within each LSOA.",
        "",
        "## OS NGD feasibility",
        "",
        feasibility_lines,
        "",
        "## OS NGD inventory",
        "",
        "The live collections endpoint exposed 94 collections under the current entitlement. Twelve relevant current-version collections were schema-checked; all were accessible. Five layers needed for population testing were then downloaded only for the bounded intersecting-LSOA extent. `schema/sample only` means that collection was inspected but not included in the full bounded population extract. The complete field-level inventory is in `outputs/methodology/os_ngd_feature_inventory.csv` and `outputs/qa/os_ngd_sample_schema.csv`.",
        "",
        inventory_lines,
        "",
        "## 10-minute comparison",
        "",
        result_lines,
        "",
        "P2 uses explicit residential Site v2 classifications. P3 uses residential building footprints and the observed residential-address share for mixed-use buildings. Clearly non-residential buildings receive zero weight. A zone with no classified support falls back to P1 and remains visible in the QA outputs.",
        "",
        f"P4 uses direct `numberoffloors` because weighted-footprint coverage is {floor_coverage:.1f}%, above the declared 95% threshold. Residential buildings receive share 1.00; mixed-use shares are tested at 0.25, 0.50 and 0.75. This produces a 10-minute range of {min(p4_low.estimated_residents, p4_high.estimated_residents):,.0f}–{max(p4_low.estimated_residents, p4_high.estimated_residents):,.0f} residents, with {p4_central.estimated_residents:,.0f} in the central scenario. Heights are inventoried but are never converted into invented storeys.",
        "The scenario names describe the assumed mixed-use residential share, not a guaranteed low-to-high ordering of the final population estimate. Here, raising the mixed-use share from 0.25 to 0.75 reduces the aggregate estimate by "
        f"{abs(p4_high.estimated_residents-p4_low.estimated_residents):,.0f} residents. Mixed-use floor-space is proportionally more concentrated outside the 10-minute catchment in the affected LSOAs, so its added weight lowers their within-zone allocation fractions; the per-LSOA support percentages and high-minus-low changes are reported in the spatial diagnostics table.",
        "",
        "## Spatial diagnostics — why estimates change",
        "",
        "The following LSOAs produce the largest absolute difference between P1 and at least one NGD-weighted method. Changes occur where the catchment intersects a different proportion of residential sites, residential footprints or floor-space proxy than of the full Census zone.",
        "",
        top_differences,
        "",
        "The complete per-zone table reports Census population, full-zone area, catchment overlap, residential-site support, residential and mixed-use footprint, high-rise proxy weight and every P1–P4 allocation.",
        "",
        "## P5 entrance accessibility",
        "",
        f"P5 is not calculated. Direct Building Access Location association covers only {entrance_coverage:.2f}% of classified residential-capable buildings. The remaining population is treated as access unknown, not inaccessible. No automatic connector is allowed to cross mapped water or other obvious barriers.",
        "",
        "## Crossharbour limitation and recommendation",
        "",
        f"Crossharbour and the Isle of Dogs contain substantial high-rise and mixed-use development. The bounded extract contains {highrise_count} residential buildings with at least 10 directly recorded floors; the maximum is {maximum_floors} floors. Similar footprints can contain very different residential floor area, so P3 is not inherently more accurate than P1. Use P1 as the primary workshop estimate because it is transparent and tied directly to authoritative ONS totals. Use P4 central as the principal NGD sensitivity, accompanied by the P4 low/high range. Do not publish P5 until entrance association and barrier-safe network coverage are materially improved.",
        "",
        "## Reproducibility",
        "",
        "ONS Census 2021 supplies authoritative population counts. OS NGD supplies ancillary spatial support for within-zone allocation. The walking network is the accessibility model, and the catchment polygon is its derived 2D representation. None of the NGD allocations is claimed as ground truth.",
        "",
        f"NGD query coverage: `{metadata['query_bbox_epsg27700']}`. API keys are not stored in data, logs or reports. Detailed method QA is in `{OUT_QA.relative_to(ROOT)}`; feature/zone findings are in `{OUT_QA_ISSUES.relative_to(ROOT)}`; spatial diagnostics are in `{OUT_ZONE.relative_to(ROOT)}`.",
    ]
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_calculation_summary(results, qa, diagnostics, p5):
    """Write one concise place containing methods, formulas and final results."""
    table = results[[
        "method_name", "result_role", "estimated_residents",
        "absolute_difference_from_p1", "percentage_difference_from_p1",
        "residents_per_hectare",
        "census_population_successfully_allocated_pct",
    ]].copy()
    table.columns = [
        "Method", "Role", "Population", "Difference from P1",
        "Difference from P1 (%)", "Residents per ha", "Allocation coverage (%)",
    ]
    p1 = results.loc[results.method == "P1"].iloc[0]
    p4 = results.loc[results.method == "P4_central"].iloc[0]
    p4_low = results.loc[results.method == "P4_low"].iloc[0]
    p4_high = results.loc[results.method == "P4_high"].iloc[0]
    catchment_area_ha = p1.estimated_residents / p1.residents_per_hectare
    fallback_p2 = int(qa.loc[qa.check == "fallback_zones_p2_10min", "value"].iloc[0])
    fallback_p3 = int(qa.loc[qa.check == "fallback_zones_p3_10min", "value"].iloc[0])
    floor_coverage = float(qa.loc[
        qa.check == "direct_floor_weighted_coverage_pct", "value"].iloc[0])
    entrance_coverage = float(qa.loc[
        qa.check == "residential_building_direct_entrance_coverage_pct", "value"].iloc[0])
    p4_min = min(p4_low.estimated_residents, p4_high.estimated_residents)
    p4_max = max(p4_low.estimated_residents, p4_high.estimated_residents)
    lines = [
        "# Crossharbour 10-minute population calculation",
        "",
        "## Scope and common denominator",
        "",
        f"All methods use the same {catchment_area_ha:.3f} ha analytical 10-minute land catchment in EPSG:27700. Water is excluded from the catchment, cartographic bridge corridors are excluded from the denominator, and OS NGD is used only as ancillary spatial support. ONS Census 2021 remains the authoritative population source.",
        "",
        "Figure 06 fills this analytical land geometry. Its exterior outline uses the connected display geometry so the reviewed bridge display corridor avoids a distracting small satellite polygon. The corridor is not included in population or density calculations, and internal holes are not outlined.",
        "",
        "## Calculation methods",
        "",
        "### P1 — Census area weighting (primary)",
        "",
        "For each LSOA `i`: `P_i,catchment = P_i × Area(Catchment ∩ LSOA_i) / Area(LSOA_i)`. The final estimate is the sum across intersecting LSOAs. This assumes population is uniformly distributed within each LSOA.",
        "",
        "### P2 — NGD residential-site weighting",
        "",
        "For each LSOA, Census population is redistributed over the union of OS NGD residential Site polygons: `P_i,catchment = P_i × Area(Catchment ∩ ResidentialSite_i) / Area(ResidentialSite_i)`. Zones with no usable residential support fall back to P1, so Census population is never silently discarded. The current result has " + f"{fallback_p2} fallback zones.",
        "",
        "### P3 — Residential-building footprint weighting",
        "",
        "Buildings are clipped to each LSOA before weighting. Residential footprints receive weight 1; mixed-use footprints use their observed residential-address share; clearly non-residential buildings receive weight 0. The method then allocates each LSOA total in proportion to those weights. The current result has " + f"{fallback_p3} fallback zones. Footprint is a weak capacity proxy in a high-rise area.",
        "",
        "### P4 — Residential floor-space proxy",
        "",
        "The building weight is `clipped footprint area × direct numberoffloors × residential share`. Only the recorded OS NGD `numberoffloors` field is used; building height is not converted into invented storeys. Residential share is 1 for residential buildings and 0 for non-residential buildings. Mixed-use shares of 0.25, 0.50 and 0.75 form the low, central and high modelling scenarios. Direct-floor weighted coverage is " + f"{floor_coverage:.1f}%.",
        "",
        "The scenario labels refer to the mixed-use share assumption. They do not guarantee that the final population estimate rises with the label. In this dataset, adding mixed-use weight slightly reduces the total because more of that weighted floor-space lies outside the catchment in the affected LSOAs.",
        "",
        "### P5 — Building-access accessibility",
        "",
        f"P5 is not calculated because direct Building Access Location association covers only {entrance_coverage:.2f}% of classified residential-capable buildings. Missing access information is classified as `ACCESS_UNKNOWN`, never automatically inaccessible.",
        "",
        "## Results",
        "",
        table.to_markdown(index=False, floatfmt=".1f"),
        "",
        "## Recommended figures",
        "",
        f"Use **P1: {p1.estimated_residents:,.0f} residents ({p1.residents_per_hectare:.1f} residents/ha)** as the primary workshop estimate. It is transparent and directly tied to authoritative Census totals.",
        "",
        f"Use **P4 central: {p4.estimated_residents:,.0f} residents ({p4.residents_per_hectare:.1f} residents/ha)** as the principal NGD sensitivity, accompanied by the full mixed-use-share range of **{p4_min:,.0f}–{p4_max:,.0f} residents**.",
        "",
        "P2 and P3 remain supporting sensitivity tests. P5 should not be published as an accessible-population estimate until entrance coverage is materially improved.",
        "",
        "## Why the estimates differ",
        "",
        f"The catchment intersects {len(diagnostics)} LSOAs. P1 responds only to the share of each full LSOA inside the catchment. P2–P4 respond to where residential land, residential footprints and recorded floor-space sit within each LSOA. The largest differences are listed in `population_spatial_diagnostics_by_lsoa.csv`, including each method's allocation and the P4 scenario support percentages.",
        "",
        "## Source roles",
        "",
        "- **ONS Census 2021:** authoritative population counts.",
        "- **OS NGD:** ancillary spatial information for testing within-zone allocation.",
        "- **Walking network:** accessibility model.",
        "- **Catchment polygon:** derived two-dimensional representation of network accessibility.",
        "",
        "NGD-weighted estimates are sensitivity results, not ground truth.",
    ]
    OUT_CALCULATION_SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_population_sensitivity(force_download=False):
    for path in [OS_DIR, OUT_TABLE.parent, OUT_QA.parent, OUT_GPKG.parent,
                 OUT_MAP_PNG.parent, OUT_REPORT.parent]:
        path.mkdir(parents=True, exist_ok=True)
    population = gpd.read_file(CLEANED / "lsoa_2021_population_cleaned.gpkg").to_crs(CRS)
    catchments = gpd.read_file(C1_GPKG, layer="analytical_land_catchments").to_crs(CRS)
    frames, metadata = download_population_support(population, catchments, force_download)
    required = {"building", "site", "building_access_location"}
    if not required.issubset(frames):
        raise RuntimeError(f"Downloaded NGD layers missing: {sorted(required-set(frames))}")
    (results, zones, feasibility, qa, sites, buildings,
     residential_sites, residential_buildings, p5) = analyse_population(
         population, catchments, frames)
    catchment10 = catchments[catchments.time_minutes == 10].geometry.iloc[0]
    display_catchments = gpd.read_file(
        C1_GPKG, layer="display_catchments").to_crs(CRS)
    display_outline10 = display_catchments[
        display_catchments.time_minutes == 10].geometry.iloc[0]
    water = gpd.read_file(selected_water_path()).to_crs(CRS).geometry.union_all()
    # Entrance QA must use the complete reviewed walking graph. Comparing all
    # NGD access points with only the 10-minute reachable subset creates false
    # long-snap and barrier flags outside the catchment.
    network = gpd.read_file(
        REVIEWED_NETWORK_GPKG, layer="edges").to_crs(CRS).geometry.union_all()
    diagnostics = build_zone_diagnostics(
        population, catchment10, residential_sites, residential_buildings, zones)
    access = frames["building_access_location"]
    qa_issues = build_spatial_qa_issues(
        population, catchment10, diagnostics, residential_sites,
        residential_buildings, buildings, access, network, water)
    observed_counts = (qa_issues.issue_type.value_counts().to_dict()
                       if not qa_issues.empty else {})
    issue_counts = pd.DataFrame([{
        "check": f"issue_count_{issue_type}",
        "value": int(observed_counts.get(issue_type, 0)),
        "status": "pass" if observed_counts.get(issue_type, 0) == 0 else "review",
    } for issue_type in SPATIAL_QA_TYPES])
    qa = pd.concat([qa, issue_counts], ignore_index=True)
    LEGACY_ZONE_TABLE.unlink(missing_ok=True)
    results.to_csv(OUT_TABLE, index=False)
    zones.to_csv(OUT_ZONE_METHODS, index=False)
    diagnostics.to_csv(OUT_ZONE, index=False)
    p5.to_csv(OUT_P5, index=False)
    feasibility.to_csv(OUT_FEASIBILITY, index=False)
    qa.to_csv(OUT_QA, index=False)
    qa_issues.to_csv(OUT_QA_ISSUES, index=False)

    _write_gpkg(catchments, "analytical_c1_catchments", first=True)
    _write_gpkg(population[population.intersects(catchments.geometry.union_all())], "census_lsoa")
    _write_gpkg(residential_sites, "residential_sites")
    _write_gpkg(residential_buildings, "residential_buildings")
    _write_gpkg(buildings[buildings.population_support_class == "non_residential"], "non_residential_buildings")
    _write_gpkg(buildings[buildings.population_support_class == "unclassified"], "unclassified_buildings")
    _write_gpkg(access, "building_access_points")
    diagnostic_geometry = population[["LSOA21CD", "geometry"]].merge(
        diagnostics, on="LSOA21CD", how="inner")
    _write_gpkg(gpd.GeoDataFrame(diagnostic_geometry, geometry="geometry", crs=CRS),
                "lsoa_population_diagnostics")

    _plot_qa(population, catchment10, display_outline10, water, network, sites,
             buildings, access)
    _write_report(results, feasibility, qa, metadata, diagnostics, p5)
    _write_calculation_summary(results, qa, diagnostics, p5)
    OUT_METADATA.write_text(json.dumps({
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "primary_method": "P1 standard Census area weighting",
        "source_population_field": "usual_residents_total",
        "census_geography": "2021 LSOA",
        "analysis_crs": CRS,
        "lsoa_denominator": "original full zone area; not water-adjusted",
        "bridge_corridors_in_population": False,
        "figure_06_boundary_geometry": (
            "exterior shell of connected display catchment; analytical land "
            "geometry remains the fill and population denominator"),
        "direct_floor_coverage_threshold": DIRECT_FLOOR_COVERAGE_THRESHOLD,
        "ngd_download": metadata,
        "p4_mixed_use_share_scenarios": MIXED_USE_SHARES,
        "p5_status": p5.iloc[0].status,
        "outputs": [str(path.relative_to(ROOT)) for path in [OUT_TABLE, OUT_ZONE,
                    OUT_ZONE_METHODS, OUT_P5, OUT_FEASIBILITY, OUT_QA,
                    OUT_QA_ISSUES, OUT_GPKG, OUT_MAP_PNG, OUT_MAP_PDF,
                    OUT_REPORT, OUT_CALCULATION_SUMMARY]],
    }, indent=2), encoding="utf-8")
    print(results.to_string(index=False))
    print(feasibility.to_string(index=False))
    return results
