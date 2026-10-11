"""Clean school records and geocode every retained school with OS Places API."""
from difflib import SequenceMatcher
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import requests
from requests.adapters import HTTPAdapter
from shapely.geometry import Point
from urllib3.util.retry import Retry

from .data_validation import find_column
from .google_school_validation import apply_google_coordinates, load_google_api_key
from .paths import CLEANED, OUTPUTS, QA, RAW, ROOT
from .utils import raw_hashes


OS_PLACES_FIND_URL = "https://api.os.uk/search/places/v1/find"
SCHOOL_OUTPUT_FIELDS = [
    "URN", "EstablishmentName", "LA (name)", "TypeOfEstablishment (name)",
    "PhaseOfEducation (name)", "StatutoryLowAge", "StatutoryHighAge",
    "SchoolCapacity", "NumberOfPupils", "Street", "Town", "Postcode",
    "Easting", "Northing",
]


def _load_api_key():
    """Read OS_PLACES_API_KEY from the environment or the project .env file."""
    key = os.getenv("OS_PLACES_API_KEY")
    if key:
        return key.strip()
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "OS_PLACES_API_KEY":
            return value.strip().strip('"').strip("'")
    return None


def _required_column(frame, *names):
    column = find_column(frame, *names)
    if column is None:
        raise ValueError(f"Could not identify required school field: {' / '.join(names)}")
    return column


def prepare_school_records(frame, log_rows):
    """Apply source-data exclusions before any OS Places requests."""
    identifier = _required_column(frame, "URN")
    status_column = _required_column(frame, "EstablishmentStatus (name)", "EstablishmentStatus")
    local_authority = _required_column(frame, "LA (name)", "LA")
    working = frame.copy()
    working["_source_row"] = range(2, len(working) + 2)
    working["qa_action"] = "keep"
    working["qa_reason"] = ""

    closed = working[status_column].fillna("").str.strip().str.casefold().eq("closed")
    overseas = (
        ~closed
        & working[local_authority].fillna("").str.strip().str.casefold().str.contains("overseas")
    )
    working.loc[closed, ["qa_action", "qa_reason"]] = ["remove", "Closed"]
    working.loc[overseas, ["qa_action", "qa_reason"]] = ["remove", "Overseas"]
    for _, row in working.loc[closed].iterrows():
        log_rows.append({
            "dataset": "schools",
            "record_id": str(row[identifier]),
            "source_row": int(row["_source_row"]),
            "action": "exclude_closed_school",
            "before": str(row[status_column]),
            "after": "excluded",
            "notes": "Closed schools are outside the current-school analysis",
        })
    for _, row in working.loc[overseas].iterrows():
        log_rows.append({
            "dataset": "schools",
            "record_id": str(row[identifier]),
            "source_row": int(row["_source_row"]),
            "action": "exclude_overseas_school",
            "before": str(row[local_authority]),
            "after": "excluded before OS Places lookup",
            "notes": "LA (name) identifies an overseas establishment",
        })
    prepared = working.loc[working["qa_action"].eq("keep")].copy()

    duplicate_mask = prepared[identifier].notna() & prepared[identifier].duplicated(keep=False)
    drop_indices = []
    for value, group in prepared.loc[duplicate_mask].groupby(identifier, sort=False):
        comparable = group.drop(columns=["_source_row", "qa_action", "qa_reason"])
        if len(comparable.drop_duplicates()) != 1:
            rows = group["_source_row"].astype(int).tolist()
            raise ValueError(f"Conflicting duplicate {identifier}={value} at source rows {rows}; manual decision required")
        keep_row = int(group.iloc[0]["_source_row"])
        for index, row in group.iloc[1:].iterrows():
            drop_indices.append(index)
            working.loc[index, ["qa_action", "qa_reason"]] = ["remove", "Duplicate"]
            log_rows.append({
                "dataset": "schools",
                "record_id": str(value),
                "source_row": int(row["_source_row"]),
                "action": "remove_exact_duplicate",
                "before": f"duplicate of source row {keep_row}",
                "after": f"removed; source row {keep_row} retained",
                "notes": "All school attributes were identical",
            })
    prepared = prepared.drop(index=drop_indices).drop(columns=["qa_action", "qa_reason"]).reset_index(drop=True)

    qa_table = working.rename(columns={"_source_row": "source_row"})
    school_name = find_column(qa_table, "EstablishmentName", "SchoolName")
    leading = ["source_row", identifier, school_name, status_column, local_authority, "qa_action", "qa_reason"]
    leading = list(dict.fromkeys(column for column in leading if column))
    qa_table = qa_table[leading + [column for column in qa_table.columns if column not in leading]]
    return (
        prepared, qa_table, identifier, status_column,
        int(closed.sum()), int(overseas.sum()), len(drop_indices),
    )


def build_school_query(row, frame):
    """Build one address query from the fields available in the source table."""
    columns = [
        find_column(frame, "EstablishmentName", "SchoolName"),
        find_column(frame, "Street"),
        find_column(frame, "Town"),
        find_column(frame, "Postcode"),
    ]
    values = []
    for column in columns:
        if column and pd.notna(row[column]) and str(row[column]).strip():
            value = str(row[column]).strip()
            if value not in values:
                values.append(value)
    if not values:
        raise ValueError(f"No usable school name/address at source row {row['_source_row']}")
    return ", ".join(values)


def create_os_session():
    """Create a retrying HTTP session without logging credentials."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def request_os_candidates(session, api_key, query, london_bounds, timeout=30):
    """Call OS Places Find once and return its candidate records."""
    response = session.get(
        OS_PLACES_FIND_URL,
        headers={"key": api_key, "Accept": "application/json"},
        params={
            "query": query,
            "bbox": ",".join(str(value) for value in london_bounds),
            "maxresults": 10,
            "dataset": "DPA,LPI",
            "minmatch": 0.5,
            "output_srs": "EPSG:27700",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def _normalised_text(value):
    return " ".join(str(value or "").casefold().split())


def select_london_candidate(results, school_name, school_postcode, london_bounds):
    """Rank returned DPA/LPI addresses after an explicit London bounds check."""
    candidates = []
    for result in results:
        dataset = "DPA" if "DPA" in result else "LPI" if "LPI" in result else None
        if dataset is None:
            continue
        item = result[dataset]
        try:
            easting = float(item["X_COORDINATE"])
            northing = float(item["Y_COORDINATE"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (london_bounds[0] <= easting <= london_bounds[2] and london_bounds[1] <= northing <= london_bounds[3]):
            continue
        address = item.get("ADDRESS", "")
        organisation = item.get("ORGANISATION_NAME") or item.get("ORGANISATION") or ""
        postcode = item.get("POSTCODE") or item.get("POSTCODE_LOCATOR") or ""
        match = float(item.get("MATCH") or 0)
        name_similarity = SequenceMatcher(
            None,
            _normalised_text(school_name),
            _normalised_text(organisation or address),
        ).ratio()
        postcode_match = bool(school_postcode) and _normalised_text(postcode) == _normalised_text(school_postcode)
        rank_score = 0.60 * match + 0.25 * name_similarity + 0.15 * float(postcode_match)
        candidates.append({
            "dataset": dataset,
            "easting": easting,
            "northing": northing,
            "address": address,
            "organisation": organisation,
            "postcode": postcode,
            "uprn": item.get("UPRN"),
            "rpc": item.get("RPC"),
            "match": match,
            "match_description": item.get("MATCH_DESCRIPTION", ""),
            "name_similarity": name_similarity,
            "postcode_match": postcode_match,
            "rank_score": rank_score,
        })
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate["rank_score"])


def _match_confidence(candidate):
    if candidate is None:
        return "no_match"
    if candidate["match"] >= 0.8 and (candidate["postcode_match"] or candidate["name_similarity"] >= 0.6):
        return "high"
    if candidate["match"] >= 0.7 and (candidate["postcode_match"] or candidate["name_similarity"] >= 0.6):
        return "medium"
    return "review"


def geocode_schools_with_os_places(frame, identifier, cfg, api_key, log_rows, session=None):
    """Query every retained school and replace coordinates with the selected OS result."""
    school_name = _required_column(frame, "EstablishmentName", "SchoolName")
    postcode = find_column(frame, "Postcode")
    old_easting = find_column(frame, "Easting")
    old_northing = find_column(frame, "Northing")
    london_bounds = cfg["qa"]["london_bounds_bng"]
    client = session or create_os_session()
    cleaned = frame.copy()
    cleaned["original_easting"] = cleaned[old_easting] if old_easting else pd.NA
    cleaned["original_northing"] = cleaned[old_northing] if old_northing else pd.NA

    matches = []
    for _, row in cleaned.iterrows():
        query = build_school_query(row, cleaned)
        results = request_os_candidates(client, api_key, query, london_bounds)
        candidate = select_london_candidate(
            results,
            row[school_name],
            row[postcode] if postcode and pd.notna(row[postcode]) else "",
            london_bounds,
        )
        confidence = _match_confidence(candidate)
        match = {
            "query": query,
            "candidate_count": len(results),
            "confidence": confidence,
            "easting": candidate["easting"] if candidate else np.nan,
            "northing": candidate["northing"] if candidate else np.nan,
            "address": candidate["address"] if candidate else "",
            "organisation": candidate["organisation"] if candidate else "",
            "postcode": candidate["postcode"] if candidate else "",
            "uprn": candidate["uprn"] if candidate else None,
            "dataset": candidate["dataset"] if candidate else "",
            "match": candidate["match"] if candidate else np.nan,
            "match_description": candidate["match_description"] if candidate else "",
            "name_similarity": candidate["name_similarity"] if candidate else np.nan,
            "postcode_match": candidate["postcode_match"] if candidate else False,
            "rank_score": candidate["rank_score"] if candidate else np.nan,
            "rpc": candidate["rpc"] if candidate else None,
        }
        matches.append(match)
        log_rows.append({
            "dataset": "schools",
            "record_id": str(row[identifier]),
            "source_row": int(row["_source_row"]),
            "action": "os_places_coordinate_lookup",
            "before": json.dumps({"easting": row[old_easting] if old_easting else None, "northing": row[old_northing] if old_northing else None}, default=str),
            "after": json.dumps({"easting": match["easting"], "northing": match["northing"], "address": match["address"], "uprn": match["uprn"]}, default=str),
            "notes": f"confidence={confidence}; match={match['match']}; candidates={len(results)}",
        })

    match_table = pd.DataFrame(matches, index=cleaned.index).add_prefix("os_")
    cleaned = pd.concat([cleaned, match_table], axis=1)
    if old_easting:
        cleaned[old_easting] = cleaned["os_easting"]
    else:
        cleaned["Easting"] = cleaned["os_easting"]
        old_easting = "Easting"
    if old_northing:
        cleaned[old_northing] = cleaned["os_northing"]
    else:
        cleaned["Northing"] = cleaned["os_northing"]
        old_northing = "Northing"
    cleaned["coordinate_source"] = np.where(cleaned["os_confidence"].eq("no_match"), "unmatched", "OS Places API")
    original_easting = pd.to_numeric(cleaned["original_easting"], errors="coerce")
    original_northing = pd.to_numeric(cleaned["original_northing"], errors="coerce")
    cleaned["os_distance_from_original_m"] = np.hypot(
        cleaned["os_easting"] - original_easting,
        cleaned["os_northing"] - original_northing,
    )
    geometry = [
        Point(easting, northing) if np.isfinite(easting) and np.isfinite(northing) else None
        for easting, northing in zip(cleaned[old_easting], cleaned[old_northing])
    ]
    return gpd.GeoDataFrame(cleaned, geometry=geometry, crs=cfg["project"]["analysis_crs"])


def classify_school_matches(frame, identifier):
    """Explain OS match confidence after source-data exclusions."""
    schools = frame.copy()
    reasons = []
    for _, row in schools.iterrows():
        confidence = row["os_confidence"]
        if confidence == "high":
            reason = "accepted_high_confidence"
        elif confidence == "medium":
            reason = "accepted_partial_match"
        elif confidence == "no_match":
            reason = "no_london_match"
        else:
            flags = ["low_os_match"]
            if not bool(row.get("os_postcode_match", False)):
                flags.append("postcode_mismatch")
            similarity = pd.to_numeric(pd.Series([row.get("os_name_similarity")]), errors="coerce").iloc[0]
            if pd.notna(similarity) and similarity < 0.6:
                flags.append("low_name_similarity")
            distance = pd.to_numeric(pd.Series([row.get("os_distance_from_original_m")]), errors="coerce").iloc[0]
            if pd.notna(distance) and distance > 250:
                flags.append("far_from_original")
            reason = ";".join(flags)
        reasons.append(reason)
    schools["os_review_reason"] = reasons
    unresolved = schools["os_confidence"].eq("no_match")
    if unresolved.any():
        urns = schools.loc[unresolved, identifier].astype(str).tolist()
        raise RuntimeError(f"Schools have no London OS Places match: {urns}")
    return schools.reset_index(drop=True)


def validate_cleaned_schools(frame, identifier, status_column, london_bounds):
    """Validate exclusions, uniqueness and the spatial restriction."""
    if status_column in frame.columns and frame[status_column].fillna("").str.strip().str.casefold().eq("closed").any():
        raise RuntimeError("Cleaned school data still contains closed schools")
    if frame[identifier].isna().any() or frame[identifier].duplicated().any():
        raise RuntimeError("Cleaned school URNs are not complete and unique")
    if frame.geometry.isna().any():
        raise RuntimeError("Cleaned London school data still contains a record without coordinates")
    x, y = frame.geometry.x, frame.geometry.y
    inside = (x >= london_bounds[0]) & (x <= london_bounds[2]) & (y >= london_bounds[1]) & (y <= london_bounds[3])
    if not inside.all():
        raise RuntimeError("Cleaned school coordinates fall outside the London bounds")


def build_public_school_output(frame):
    """Return the compact school table used by later spatial analysis."""
    missing = [column for column in SCHOOL_OUTPUT_FIELDS if column not in frame.columns]
    if missing:
        raise ValueError(f"Cleaned school output is missing fields: {missing}")
    columns = SCHOOL_OUTPUT_FIELDS + (["geometry"] if "geometry" in frame.columns else [])
    output = frame[columns].copy()
    output["api_returned_address"] = frame["api_returned_address"].values
    output["coordinate_source"] = frame["coordinate_source"].values
    output["coordinate_confidence"] = np.where(
        frame["coordinate_source"].eq("Google Geocoding API"),
        "google_validated",
        frame["os_confidence"],
    )
    output["coordinate_status"] = frame["coordinate_status"].values
    ordered = SCHOOL_OUTPUT_FIELDS + [
        "api_returned_address", "coordinate_source", "coordinate_confidence", "coordinate_status",
    ]
    if "geometry" in output.columns:
        ordered.append("geometry")
    return output[ordered]


def clean_schools(cfg, api_key=None, session=None, google_api_key=None, google_session=None):
    """Filter schools, query OS, then resolve OS review records with Google."""
    source_path = RAW / "schools.csv"
    csv_path = CLEANED / "schools_cleaned.csv"
    gpkg_path = CLEANED / "schools_cleaned.gpkg"
    log_path = OUTPUTS / "tables" / "school_cleaning_log.csv"
    audit_path = OUTPUTS / "tables" / "os_places_school_matches.csv"
    review_path = OUTPUTS / "tables" / "school_coordinate_review.csv"
    google_path = OUTPUTS / "tables" / "google_school_validation.csv"
    qa_path = QA / "schools_qa.csv"
    CLEANED.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_before = raw_hashes()

    source = pd.read_csv(source_path, dtype=str, encoding="utf-8-sig")
    log_rows = []
    (
        prepared, qa_table, identifier, status_column,
        closed_count, overseas_count, duplicate_count,
    ) = prepare_school_records(source, log_rows)
    qa_table.to_csv(qa_path, index=False)
    key = api_key or _load_api_key()
    if not key or key == "your_os_places_api_key_here":
        raise ValueError(
            "School QA was written, but OS_PLACES_API_KEY must be set in .env before geocoding"
        )
    google_key = google_api_key or load_google_api_key()
    if not google_key or google_key == "your_google_maps_api_key_here":
        raise ValueError(
            "School QA was written, but GOOGLE_MAPS_API_KEY must be set in .env for automatic fallback"
        )
    matched = geocode_schools_with_os_places(prepared, identifier, cfg, key, log_rows, session=session)
    cleaned = classify_school_matches(matched, identifier)
    cleaned, google_validation = apply_google_coordinates(
        cleaned, cfg, google_key, session=google_session
    )
    google_validation.to_csv(google_path, index=False)
    validate_cleaned_schools(cleaned, identifier, status_column, cfg["qa"]["london_bounds_bng"])

    public_output = build_public_school_output(cleaned)
    output_table = public_output.drop(columns="geometry")
    output_table.to_csv(csv_path, index=False)
    audit_table = cleaned.drop(columns="geometry").rename(columns={"_source_row": "source_row"})
    audit_table.to_csv(audit_path, index=False)
    review_columns = [
        "source_row", identifier, find_column(audit_table, "EstablishmentName", "SchoolName"),
        find_column(audit_table, "Postcode"), "original_easting", "original_northing",
        "os_easting", "os_northing", "os_address", "os_match", "os_match_description",
        "os_name_similarity", "os_postcode_match", "os_distance_from_original_m", "os_review_reason",
        "coordinate_status",
    ]
    review_columns = [column for column in review_columns if column and column in audit_table.columns]
    unresolved = audit_table["coordinate_status"].eq("manual_review")
    review_table = audit_table.loc[unresolved, review_columns].copy()
    if not google_validation.empty:
        review_table = review_table.merge(
            google_validation[["URN", "google_result", "google_returned_address", "google_postcode_match", "google_distance_to_os_m", "google_validation_reason"]],
            on="URN", how="left",
        )
    review_table.to_csv(review_path, index=False)
    spatial = public_output
    temporary = gpkg_path.with_name(gpkg_path.stem + ".tmp.gpkg")
    if temporary.exists():
        temporary.unlink()
    pyogrio.write_dataframe(spatial, temporary, layer="schools_cleaned", driver="GPKG")
    written = gpd.read_file(temporary, layer="schools_cleaned", engine="pyogrio")
    validate_cleaned_schools(written, identifier, status_column, cfg["qa"]["london_bounds_bng"])
    temporary.replace(gpkg_path)

    pd.DataFrame(log_rows, columns=[
        "dataset", "record_id", "source_row", "action", "before", "after", "notes",
    ]).to_csv(log_path, index=False)
    if raw_hashes() != hashes_before:
        raise RuntimeError("Original files changed during school cleaning")

    return {
        "source_count": len(source),
        "cleaned_count": len(cleaned),
        "closed_excluded": closed_count,
        "duplicates_removed": duplicate_count,
        "api_lookups": len(prepared),
        "matched_count": int(cleaned.geometry.notna().sum()),
        "os_accepted_count": int(cleaned["os_confidence"].isin(["high", "medium"]).sum()),
        "google_lookups": len(google_validation),
        "google_used_count": int(cleaned["coordinate_source"].eq("Google Geocoding API").sum()),
        "accepted_count": int(cleaned["coordinate_status"].isin(["accepted_os", "accepted_google"]).sum()),
        "review_count": int(cleaned["coordinate_status"].eq("manual_review").sum()),
        "overseas_excluded": overseas_count,
        "csv_output": str(csv_path),
        "gpkg_output": str(gpkg_path),
        "cleaning_log": str(log_path),
        "match_audit": str(audit_path),
        "coordinate_review": str(review_path),
        "google_validation": str(google_path),
        "qa_output": str(qa_path),
    }
