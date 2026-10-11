"""Run staged QA checks and write issue details plus a short summary."""
from pathlib import Path
import csv
import json
import re
import shutil
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import Point
from shapely.validation import explain_validity

from .paths import OUTPUTS, RAW
from .utils import raw_hashes


ISSUE_COLUMNS = ["dataset", "check_category", "record_id", "issue", "severity", "details", "next_step"]
QA_CATEGORIES = [
    "field_structure",
    "crs",
    "spatial_location",
    "geometry_topology",
    "attribute_consistency",
]
ISSUE_CATEGORIES = {
    "empty_dataset": "field_structure",
    "missing_identifier": "field_structure",
    "duplicate_identifier": "field_structure",
    "duplicate_record": "field_structure",
    "duplicate_column_names": "field_structure",
    "missing_or_ambiguous_input": "field_structure",
    "file_unreadable": "field_structure",
    "unexpected_feature_count": "field_structure",
    "missing_crs": "crs",
    "nonfinite_coordinates": "crs",
    "coordinates_inconsistent_with_crs": "crs",
    "crs_transformation_failed": "crs",
    "coordinates_outside_bng": "crs",
    "unusable_coordinates": "spatial_location",
    "coordinate_needs_verification": "spatial_location",
    "duplicate_coordinates": "spatial_location",
    "missing_geometry": "geometry_topology",
    "empty_geometry": "geometry_topology",
    "invalid_geometry": "geometry_topology",
    "nonpositive_area": "geometry_topology",
    "overlapping_polygons": "geometry_topology",
}


class IssueLog:
    """Collect only findings that need review or correction."""

    def __init__(self):
        self.rows = []

    def add(self, dataset, record_id, issue, severity, details, next_step):
        if isinstance(details, (dict, list, tuple)):
            details = json.dumps(details, ensure_ascii=False, default=str)
        self.rows.append({
            "dataset": dataset,
            "check_category": ISSUE_CATEGORIES.get(issue, "attribute_consistency"),
            "record_id": str(record_id),
            "issue": issue,
            "severity": severity,
            "details": str(details),
            "next_step": next_step,
        })

    def save(self, issue_path, summary_path):
        table = pd.DataFrame(self.rows, columns=ISSUE_COLUMNS)
        table.to_csv(issue_path, index=False)
        summary = (
            table.groupby(["dataset", "check_category", "issue", "severity"], dropna=False)
            .agg(
                issue_count=("record_id", "size"),
                example_records=("record_id", lambda values: " ; ".join(values.astype(str).head(3))),
            )
            .reset_index()
        )
        summary["status"] = "issues_found"
        categories_with_issues = set(summary["check_category"])
        pass_rows = [{
            "dataset": "all",
            "check_category": category,
            "issue": "no_issues_found",
            "severity": "",
            "issue_count": 0,
            "example_records": "",
            "status": "pass",
        } for category in QA_CATEGORIES if category not in categories_with_issues]
        if pass_rows:
            summary = pd.concat([summary, pd.DataFrame(pass_rows)], ignore_index=True)
        summary["_category_order"] = summary["check_category"].map({name: index for index, name in enumerate(QA_CATEGORIES)})
        summary = summary.sort_values(["_category_order", "dataset", "issue"]).drop(columns="_category_order")
        summary.to_csv(summary_path, index=False)
        return table, summary


def record_ids(frame, identifier=None, csv_source=False):
    """Make traceable IDs that distinguish duplicate source rows."""
    first_row = 2 if csv_source else 1
    values = frame[identifier] if identifier in frame.columns else pd.Series("feature", index=frame.index)
    return pd.Series(
        [f"{value}:row{position + first_row}" for position, value in enumerate(values)],
        index=frame.index,
    )


def _blank(series):
    return series.isna() | series.astype(str).str.strip().eq("")


def _inside_bounds(x, y, bounds):
    return (x >= bounds[0]) & (x <= bounds[2]) & (y >= bounds[1]) & (y <= bounds[3])


def _normalise_name(value):
    """Normalise a source column name for case-insensitive discovery."""
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def find_column(frame, *names):
    """Return the actual column whose normalised name matches a discovered concept."""
    columns = {_normalise_name(column): column for column in frame.columns if column != "geometry"}
    for name in names:
        match = columns.get(_normalise_name(name))
        if match is not None:
            return match
    return None


def find_column_containing(frame, *parts):
    """Find a column containing all semantic parts, independent of punctuation/case."""
    wanted = [_normalise_name(part) for part in parts]
    for column in frame.columns:
        normalised = _normalise_name(column)
        if column != "geometry" and all(part in normalised for part in wanted):
            return column
    return None


def check_structure(frame, dataset, issues, identifier=None, csv_source=False):
    """Check record structure, duplicate rows and discovered identifiers."""
    ids = record_ids(frame, identifier, csv_source)

    if frame.empty:
        issues.add(dataset, "dataset", "empty_dataset", "error", "0 records", "Check the supplied file")

    identifier_problem = pd.Series(False, index=frame.index)
    if identifier and identifier in frame.columns:
        missing_id = _blank(frame[identifier])
        for idx in frame.index[missing_id]:
            identifier_problem.loc[idx] = True
            issues.add(dataset, ids.loc[idx], "missing_identifier", "error", identifier, "Supply a unique identifier")
        duplicated = frame.loc[~missing_id, identifier].duplicated(keep=False)
        duplicate_index = duplicated.index[duplicated]
        for value, group in frame.loc[duplicate_index].groupby(identifier, dropna=False):
            group_ids = ids.loc[group.index].tolist()
            identifier_problem.loc[group.index] = True
            issues.add(
                dataset,
                " | ".join(group_ids),
                "duplicate_identifier",
                "error",
                {"field": identifier, "value": value, "rows": group_ids},
                "Compare the records and retain or merge the correct record",
            )
    elif not identifier:
        comparable = pd.DataFrame(frame.copy())
        if "geometry" in comparable:
            comparable["geometry"] = comparable.geometry.map(
                lambda geometry: None if geometry is None else shapely.normalize(geometry).wkb_hex
            )
        duplicate_rows = comparable.duplicated(keep=False)
        if duplicate_rows.any():
            issues.add(
                dataset,
                " | ".join(ids.loc[duplicate_rows]),
                "duplicate_record",
                "error",
                "All source values and geometry are identical",
                "Retain one record after confirming the duplicate",
            )

    return {
        "ids": ids,
        "identifier_problem": identifier_problem,
    }


def check_geometry(frame, dataset, ids, issues):
    """Check geometry presence and validity without modifying geometries."""
    invalid_geometry = pd.Series(False, index=frame.index)
    for idx, geometry in frame.geometry.items():
        if geometry is None:
            invalid_geometry.loc[idx] = True
            issues.add(dataset, ids.loc[idx], "missing_geometry", "error", "No geometry", "Supply or recover the geometry")
        elif geometry.is_empty:
            invalid_geometry.loc[idx] = True
            issues.add(dataset, ids.loc[idx], "empty_geometry", "error", geometry.geom_type, "Supply a non-empty geometry")
        elif not geometry.is_valid:
            invalid_geometry.loc[idx] = True
            issues.add(
                dataset,
                ids.loc[idx],
                "invalid_geometry",
                "error",
                explain_validity(geometry),
                "Repair the geometry and document the change",
            )
    return invalid_geometry


def check_crs(frame, dataset, cfg, issues):
    """Validate the declared CRS and return an EPSG:27700 copy."""
    if frame.crs is None:
        issues.add(dataset, "dataset", "missing_crs", "error", "CRS is not declared", "Assign the verified source CRS")
        return None
    coordinates = shapely.get_coordinates(frame.geometry.to_numpy())
    if not np.isfinite(coordinates).all():
        issues.add(dataset, "dataset", "nonfinite_coordinates", "error", "NaN or Infinity", "Correct the source coordinates")
        return None
    if frame.crs.is_geographic and len(coordinates) and not (
        (np.abs(coordinates[:, 0]) <= 180).all() and (np.abs(coordinates[:, 1]) <= 90).all()
    ):
        issues.add(
            dataset,
            "dataset",
            "coordinates_inconsistent_with_crs",
            "error",
            frame.total_bounds.tolist(),
            "Verify the declared source CRS",
        )
        return None
    try:
        projected = frame.to_crs(cfg["project"]["analysis_crs"]).copy()
        projected_coordinates = shapely.get_coordinates(projected.geometry.to_numpy())
        if not np.isfinite(projected_coordinates).all():
            raise ValueError("transformation produced non-finite coordinates")
    except Exception as exc:
        issues.add(dataset, "dataset", "crs_transformation_failed", "error", exc, "Verify the CRS and transformation setup")
        return None
    if len(projected_coordinates) and not _inside_bounds(
        projected_coordinates[:, 0], projected_coordinates[:, 1], cfg["qa"]["bng_bounds"]
    ).all():
        issues.add(
            dataset,
            "dataset",
            "coordinates_outside_bng",
            "error",
            projected.total_bounds.tolist(),
            "Verify the source CRS and coordinates",
        )
    return projected


def validate_site(frame, cfg, issues):
    base = check_structure(frame, "site", issues)
    check_geometry(frame, "site", base["ids"], issues)
    if len(frame) != 1:
        issues.add("site", "dataset", "unexpected_feature_count", "warning", len(frame), "Confirm the intended site boundary")
    result = check_crs(frame, "site", cfg, issues)
    if result is None:
        return None
    result["area_m2"] = result.area
    result["area_ha"] = result.area_m2 / 10_000
    for idx, row in result.iterrows():
        if row.area_m2 <= 0:
            issues.add("site", base["ids"].loc[idx], "nonpositive_area", "error", row.area_m2, "Repair the site geometry")
        for column in frame.columns:
            value = row[column]
            if column == "geometry" or not isinstance(value, str):
                continue
            for match in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:ha\b|hectares?\b)", value, re.I):
                stated_area = float(match.group(1))
                relative_difference = abs(row.area_ha - stated_area) / stated_area if stated_area else np.inf
                if relative_difference > cfg["qa"]["site_area_relative_tolerance"]:
                    issues.add(
                        "site",
                        base["ids"].loc[idx],
                        "stated_area_differs_from_geometry",
                        "warning",
                        {"stated_ha": stated_area, "geometry_ha": row.area_ha, "source_field": column},
                        "Confirm the authoritative site area; preserve the supplied boundary until then",
                    )
    return result


def validate_schools(table, site, cfg, issues):
    identifier = find_column(table, "URN")
    easting = find_column(table, "Easting")
    northing = find_column(table, "Northing")
    base = check_structure(table, "schools", issues, identifier=identifier, csv_source=True)
    schools = table.copy()
    schools["record_id"] = base["ids"]
    schools["source_row"] = np.arange(2, len(schools) + 2)
    if easting and northing:
        east = pd.to_numeric(schools[easting], errors="coerce")
        north = pd.to_numeric(schools[northing], errors="coerce")
        schools["original_easting"] = schools[easting]
        schools["original_northing"] = schools[northing]
        missing_coordinates = _blank(schools[easting]) | _blank(schools[northing])
        nonnumeric_coordinates = (east.isna() | north.isna()) & ~missing_coordinates
        zero_coordinates = east.eq(0) | north.eq(0)
        nonfinite_coordinates = (~np.isfinite(east) | ~np.isfinite(north)) & ~(east.isna() | north.isna())
        outside_bng = np.isfinite(east) & np.isfinite(north) & ~_inside_bounds(east, north, cfg["qa"]["bng_bounds"])
        unusable = missing_coordinates | nonnumeric_coordinates | zero_coordinates | nonfinite_coordinates | outside_bng
        reasons = {
            "missing": missing_coordinates,
            "nonnumeric": nonnumeric_coordinates,
            "zero": zero_coordinates,
            "nonfinite": nonfinite_coordinates,
            "outside_bng": outside_bng,
        }
        for idx in schools.index[unusable]:
            row_reasons = [name for name, flag in reasons.items() if flag.loc[idx]]
            issues.add(
                "schools",
                base["ids"].loc[idx],
                "unusable_coordinates",
                "error",
                {"easting": schools.loc[idx, easting], "northing": schools.loc[idx, northing], "reason": row_reasons},
                "Verify the address, then recover coordinates in the correction stage",
            )
        geometry = [Point(x, y) if usable else None for x, y, usable in zip(east, north, ~unusable)]
    else:
        east = pd.Series(np.nan, index=schools.index)
        north = pd.Series(np.nan, index=schools.index)
        unusable = pd.Series(True, index=schools.index)
        geometry = [None] * len(schools)

    schools["qa_unusable_coordinates"] = unusable
    schools["coordinate_usable"] = ~unusable
    result = gpd.GeoDataFrame(schools, geometry=geometry, crs=cfg["project"]["analysis_crs"])
    result["distance_to_site_m"] = np.nan
    if site is not None and not site.empty and site.geometry.notna().all() and site.is_valid.all():
        result["distance_to_site_m"] = result.geometry.distance(shapely.union_all(site.geometry.to_numpy()))

    outside_study_area = (~unusable) & ~_inside_bounds(east, north, cfg["qa"]["london_bounds_bng"])
    distant = result["distance_to_site_m"].gt(cfg["qa"]["distance_outlier_m"])
    result["qa_coordinate_needs_verification"] = outside_study_area | distant
    for idx in result.index[result["qa_coordinate_needs_verification"]]:
        why = []
        if outside_study_area.loc[idx]:
            why.append("outside configured London bounds")
        if distant.loc[idx]:
            why.append(f"more than {cfg['qa']['distance_outlier_m']} m from site")
        issues.add(
            "schools",
            base["ids"].loc[idx],
            "coordinate_needs_verification",
            "warning",
            {"easting": east.loc[idx], "northing": north.loc[idx], "reason": why},
            "Verify against an authoritative address or place API; do not classify it as overseas from this flag alone",
        )

    usable = result["coordinate_usable"]
    duplicate_coordinates = pd.Series(False, index=result.index)
    if easting and northing:
        result["_qa_easting_numeric"] = east
        result["_qa_northing_numeric"] = north
        duplicate_coordinates.loc[usable] = result.loc[usable].duplicated(
            ["_qa_easting_numeric", "_qa_northing_numeric"], keep=False
        )
    if duplicate_coordinates.any():
        for coordinates, group in result.loc[duplicate_coordinates].groupby(
            ["_qa_easting_numeric", "_qa_northing_numeric"], dropna=False
        ):
            group_ids = base["ids"].loc[group.index].tolist()
            issues.add(
                "schools",
                " | ".join(group_ids),
                "duplicate_coordinates",
                "warning",
                {"coordinates": coordinates, "rows": group_ids},
                "Check whether the schools share a site or a coordinate was copied",
            )

    name = find_column(result, "EstablishmentName", "SchoolName")
    if name:
        for idx in result.index[_blank(result[name])]:
            issues.add("schools", base["ids"].loc[idx], "missing_school_name", "warning", name, "Supply the school name")

    capacity_column = find_column(result, "SchoolCapacity", "Capacity")
    pupils_column = find_column(result, "NumberOfPupils", "Pupils")
    values_by_label = {}
    for label, column, minimum in [("capacity", capacity_column, 1), ("pupils", pupils_column, 0)]:
        if column is None:
            continue
        values = pd.to_numeric(result[column], errors="coerce")
        values_by_label[label] = values
        bad = values.isna() | ~np.isfinite(values) | (values < minimum) | (values % 1 != 0)
        for idx in result.index[bad]:
            issues.add(
                "schools",
                base["ids"].loc[idx],
                f"invalid_or_missing_{label}",
                "warning",
                result.loc[idx, column],
                f"Verify a whole number greater than or equal to {minimum}",
            )
    if {"capacity", "pupils"}.issubset(values_by_label):
        capacity = values_by_label["capacity"]
        pupils = values_by_label["pupils"]
        for idx in result.index[pupils.gt(capacity) & capacity.gt(0)]:
            issues.add(
                "schools",
                base["ids"].loc[idx],
                "pupils_exceed_capacity",
                "warning",
                {"pupils": pupils.loc[idx], "capacity": capacity.loc[idx]},
                "Verify both values",
            )

    low_column = find_column(result, "StatutoryLowAge", "LowAge")
    high_column = find_column(result, "StatutoryHighAge", "HighAge")
    if low_column and high_column:
        low = pd.to_numeric(result[low_column], errors="coerce")
        high = pd.to_numeric(result[high_column], errors="coerce")
        bad_age = low.isna() | high.isna() | (low < 0) | (high < low) | (high > cfg["qa"]["max_statutory_age"]) | (low % 1 != 0) | (high % 1 != 0)
        for idx in result.index[bad_age]:
            issues.add(
                "schools",
                base["ids"].loc[idx],
                "invalid_statutory_age_range",
                "warning",
                {"low_age": result.loc[idx, low_column], "high_age": result.loc[idx, high_column]},
                "Verify the statutory age range",
            )
    return result


def validate_population(frame, cfg, issues):
    identifier = find_column(frame, "LSOA21CD") or find_column_containing(frame, "lsoa", "cd") or find_column_containing(frame, "lsoa")
    total = find_column(frame, "usual_residents_total") or find_column_containing(frame, "usual", "resident", "total")
    household = find_column(frame, "usual_residents_household") or find_column_containing(frame, "usual", "resident", "household")
    communal = find_column(frame, "usual_residents_communal") or find_column_containing(frame, "usual", "resident", "communal")
    base = check_structure(frame, "population", issues, identifier=identifier)
    invalid_geometry = check_geometry(frame, "population", base["ids"], issues)
    population = check_crs(frame, "population", cfg, issues)
    if population is None:
        return None
    population["record_id"] = base["ids"]
    population["qa_invalid_geometry"] = invalid_geometry

    value_fields = [field for field in [total, household, communal] if field is not None]
    numbers = {}
    for field in value_fields:
        values = pd.to_numeric(population[field], errors="coerce")
        numbers[field] = values
        invalid = values.isna() | ~np.isfinite(values) | (values < 0) | (values % 1 != 0)
        for idx in population.index[invalid]:
            issues.add(
                "population",
                base["ids"].loc[idx],
                "invalid_population_value",
                "error",
                {"field": field, "value": population.loc[idx, field]},
                "Verify a non-negative whole number",
            )
    if total and household and communal:
        mismatch = numbers[total].ne(numbers[household] + numbers[communal])
        for idx in population.index[mismatch]:
            issues.add(
                "population",
                base["ids"].loc[idx],
                "population_sum_mismatch",
                "error",
                {field: population.loc[idx, field] for field in value_fields},
                "Verify that component populations sum to the total",
            )

    valid = population.loc[~invalid_geometry]
    normalized = valid.geometry.map(lambda geometry: shapely.normalize(geometry).wkb_hex)
    for idx, row in valid.iterrows():
        for position in valid.sindex.query(row.geometry, predicate="intersects"):
            other = valid.index[position]
            if other <= idx or normalized.loc[idx] == normalized.loc[other]:
                continue
            overlap = row.geometry.intersection(valid.loc[other].geometry).area
            if overlap > cfg["qa"]["overlap_tolerance_m2"]:
                issues.add(
                    "population",
                    f"{base['ids'].loc[idx]} | {base['ids'].loc[other]}",
                    "overlapping_polygons",
                    "error",
                    {"overlap_m2": overlap},
                    "Review the boundary topology and repair the authoritative polygon if needed",
                )
    return population


def _read_inputs(temporary_folder, issues):
    found = {"site": [], "schools": [], "population": []}
    for path in sorted(temporary_folder.iterdir()):
        try:
            if path.suffix.lower() == ".csv":
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    header = next(csv.reader(stream))
                if len(header) != len(set(header)):
                    issues.add(path.name, "dataset", "duplicate_column_names", "error", header, "Rename duplicate columns")
                frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
                found["schools"].append(frame)
            elif path.suffix.lower() in {".gpkg", ".geojson", ".shp"}:
                for layer, geometry_type in pyogrio.list_layers(path):
                    if geometry_type is None:
                        continue
                    frame = gpd.read_file(path, layer=layer, engine="pyogrio")
                    names = [_normalise_name(column) for column in frame.columns]
                    role = "population" if any("lsoa" in name or "usualresident" in name for name in names) else "site"
                    found[role].append(frame)
        except Exception as exc:
            issues.add(path.name, "dataset", "file_unreadable", "error", exc, "Replace or repair the input file")
    return found


def run_validation(cfg):
    """Run the independent QA stage and write details plus a grouped summary."""
    issues = IssueLog()
    hashes_before = raw_hashes()
    issue_path = OUTPUTS / "tables" / "qa_issues.csv"
    summary_path = OUTPUTS / "tables" / "qa_summary.csv"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    results = {"site": None, "schools": None, "population": None}
    try:
        with tempfile.TemporaryDirectory(prefix="crossharbour-qa-") as temporary:
            temporary_folder = Path(temporary)
            for source in RAW.iterdir():
                if source.is_file() and source.name != ".DS_Store":
                    shutil.copy2(source, temporary_folder / source.name)
            found = _read_inputs(temporary_folder, issues)

        inputs = {}
        for dataset, candidates in found.items():
            if len(candidates) == 1:
                inputs[dataset] = candidates[0]
            else:
                issues.add(
                    dataset,
                    "dataset",
                    "missing_or_ambiguous_input",
                    "error",
                    f"{len(candidates)} matching datasets",
                    "Keep exactly one matching input dataset",
                )
        if "site" in inputs:
            results["site"] = validate_site(inputs["site"], cfg, issues)
        if "schools" in inputs:
            results["schools"] = validate_schools(inputs["schools"], results["site"], cfg, issues)
        if "population" in inputs:
            results["population"] = validate_population(inputs["population"], cfg, issues)
    finally:
        table, summary = issues.save(issue_path, summary_path)
        if raw_hashes() != hashes_before:
            raise RuntimeError("Original files changed during QA")

    return {
        "issue_count": len(table),
        "error_count": int(table["severity"].eq("error").sum()),
        "warning_count": int(table["severity"].eq("warning").sum()),
        "category_counts": {
            category: int(table["check_category"].eq(category).sum())
            for category in QA_CATEGORIES
        },
        "issue_output": str(issue_path),
        "summary_output": str(summary_path),
        "corrections_applied": [],
    }
