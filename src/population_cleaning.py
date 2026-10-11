"""Clean the two confirmed population issues without modifying raw data."""
import json

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from shapely.validation import explain_validity

from .config import load_config
from .data_validation import IssueLog, find_column, find_column_containing, validate_population
from .paths import CLEANED, OUTPUTS, RAW
from .utils import raw_hashes


def _population_identifier(frame):
    return (
        find_column(frame, "LSOA21CD")
        or find_column_containing(frame, "lsoa", "cd")
        or find_column_containing(frame, "lsoa")
    )


def _comparable_rows(frame):
    comparable = pd.DataFrame(frame.copy())
    comparable = comparable.drop(columns=[column for column in comparable.columns if str(column).startswith("_source_")])
    comparable["geometry"] = comparable.geometry.map(
        lambda geometry: None if geometry is None else shapely.normalize(geometry).wkb_hex
    )
    return comparable


def remove_exact_duplicate_lsoas(frame, identifier, log_rows):
    """Keep the first exact copy of each LSOA; reject conflicting duplicates."""
    cleaned = frame.copy()
    source_rows = frame["_source_row"] if "_source_row" in frame else pd.Series(range(1, len(frame) + 1), index=frame.index)
    duplicate_mask = frame[identifier].notna() & frame[identifier].duplicated(keep=False)
    drop_indices = []

    for value, group in frame.loc[duplicate_mask].groupby(identifier, sort=False):
        comparable = _comparable_rows(group)
        if len(comparable.drop_duplicates()) != 1:
            rows = source_rows.loc[group.index].tolist()
            raise ValueError(f"Conflicting duplicate {identifier}={value} at source rows {rows}; manual decision required")
        keep_index = group.index[0]
        keep_row = int(source_rows.loc[keep_index])
        for index in group.index[1:]:
            removed_row = int(source_rows.loc[index])
            drop_indices.append(index)
            log_rows.append({
                "dataset": "population",
                "record_id": str(value),
                "source_row": removed_row,
                "action": "remove_exact_duplicate",
                "before": f"duplicate of source row {keep_row}",
                "after": f"removed; source row {keep_row} retained",
                "notes": "All attributes and normalised geometry were identical",
            })

    return cleaned.drop(index=drop_indices).reset_index(drop=True), len(drop_indices)


def _polygonal_geometry(geometry):
    """Return the polygonal part of a make_valid result."""
    fixed = shapely.make_valid(geometry)
    if fixed.geom_type in {"Polygon", "MultiPolygon"}:
        return fixed
    parts = [part for part in shapely.get_parts(fixed) if part.geom_type in {"Polygon", "MultiPolygon"}]
    if not parts:
        raise ValueError(f"make_valid produced no polygonal geometry ({fixed.geom_type})")
    return shapely.union_all(parts)


def repair_invalid_population_geometries(frame, identifier, log_rows):
    """Repair invalid polygons with make_valid and record the measurable change."""
    cleaned = frame.copy()
    repaired_count = 0
    for index, geometry in cleaned.geometry.items():
        if geometry is None or geometry.is_empty:
            raise ValueError(f"Cannot automatically repair missing/empty geometry at cleaned row {index + 1}")
        if geometry.is_valid:
            continue
        fixed = _polygonal_geometry(geometry)
        if fixed.is_empty or not fixed.is_valid:
            raise ValueError(f"Geometry repair failed for {cleaned.loc[index, identifier]}")
        old_area = float(geometry.area)
        new_area = float(fixed.area)
        cleaned.at[index, "geometry"] = fixed
        repaired_count += 1
        log_rows.append({
            "dataset": "population",
            "record_id": str(cleaned.loc[index, identifier]),
            "source_row": int(cleaned.loc[index, "_source_row"]) if "_source_row" in cleaned else index + 1,
            "action": "repair_invalid_geometry",
            "before": json.dumps({
                "geometry_type": geometry.geom_type,
                "validity": explain_validity(geometry),
                "area_m2": old_area,
            }),
            "after": json.dumps({
                "geometry_type": fixed.geom_type,
                "valid": bool(fixed.is_valid),
                "area_m2": new_area,
                "area_change_m2": new_area - old_area,
            }),
            "notes": "Repaired with shapely.make_valid; attributes unchanged",
        })
    return cleaned, repaired_count


def validate_cleaned_population(frame, identifier):
    """Stop rather than publish a population layer that still fails core checks."""
    if frame[identifier].isna().any() or frame[identifier].duplicated().any():
        raise RuntimeError("Cleaned population identifiers are not complete and unique")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any() or not frame.geometry.is_valid.all():
        raise RuntimeError("Cleaned population geometry is missing, empty or invalid")
    if frame.crs is None:
        raise RuntimeError("Cleaned population CRS is missing")


def clean_population():
    """Read raw population data, apply confirmed fixes and write cleaned outputs."""
    source_path = RAW / "lsoa_2021_population.gpkg"
    destination = CLEANED / "lsoa_2021_population_cleaned.gpkg"
    log_path = OUTPUTS / "tables" / "population_cleaning_log.csv"
    CLEANED.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    hashes_before = raw_hashes()
    layers = pyogrio.list_layers(source_path)
    if len(layers) != 1:
        raise ValueError(f"Expected one population layer, found {len(layers)}")
    source = gpd.read_file(source_path, layer=layers[0][0], engine="pyogrio")
    identifier = _population_identifier(source)
    if identifier is None:
        raise ValueError("Could not identify the LSOA code field")

    working = source.copy()
    working["_source_row"] = range(1, len(working) + 1)
    log_rows = []
    cleaned, removed_count = remove_exact_duplicate_lsoas(working, identifier, log_rows)
    cleaned, repaired_count = repair_invalid_population_geometries(cleaned, identifier, log_rows)
    validate_cleaned_population(cleaned, identifier)
    cleaned = cleaned.drop(columns="_source_row")

    temporary = destination.with_name(destination.stem + ".tmp.gpkg")
    if temporary.exists():
        temporary.unlink()
    pyogrio.write_dataframe(
        cleaned,
        temporary,
        layer="population_cleaned",
        driver="GPKG",
        promote_to_multi=False,
    )
    written = gpd.read_file(temporary, layer="population_cleaned", engine="pyogrio")
    validate_cleaned_population(written, identifier)
    if len(written) != len(source) - removed_count:
        raise RuntimeError("Written cleaned population record count is incorrect")
    post_clean_issues = IssueLog()
    validate_population(written, load_config(), post_clean_issues)
    if post_clean_issues.rows:
        remaining = sorted({row["issue"] for row in post_clean_issues.rows})
        raise RuntimeError(f"Cleaned population still has QA issues: {remaining}")
    temporary.replace(destination)

    pd.DataFrame(log_rows, columns=[
        "dataset", "record_id", "source_row", "action", "before", "after", "notes",
    ]).to_csv(log_path, index=False)
    if raw_hashes() != hashes_before:
        raise RuntimeError("Original files changed during population cleaning")

    return {
        "source_count": len(source),
        "cleaned_count": len(cleaned),
        "duplicates_removed": removed_count,
        "geometries_repaired": repaired_count,
        "cleaned_output": str(destination),
        "cleaning_log": str(log_path),
    }
