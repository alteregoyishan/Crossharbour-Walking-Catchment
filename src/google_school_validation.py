"""Resolve low-confidence OS school coordinates with Google Geocoding API."""
import os
from urllib.parse import quote

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from requests.adapters import HTTPAdapter
from shapely.geometry import Point
from urllib3.util.retry import Retry

from .paths import ROOT


GOOGLE_GEOCODING_URL = "https://geocode.googleapis.com/v4/geocode/address"
GOOGLE_VALIDATION_COLUMNS = [
    "URN", "EstablishmentName", "google_query", "google_result",
    "google_place_id", "google_returned_address", "google_postcode_match",
    "google_distance_to_os_m", "coordinate_status", "google_validation_reason",
]
GOOGLE_INTERNAL_COLUMNS = GOOGLE_VALIDATION_COLUMNS + ["_google_easting", "_google_northing"]


def load_google_api_key():
    """Read GOOGLE_MAPS_API_KEY from the environment or project .env file."""
    key = os.getenv("GOOGLE_MAPS_API_KEY")
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
        if name.strip() == "GOOGLE_MAPS_API_KEY":
            return value.strip().strip('"').strip("'")
    return None


def create_google_session():
    """Create a retrying Google API session."""
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


def request_google_results(session, api_key, query, timeout=30):
    """Request an address geocode without putting the API key in the URL."""
    response = session.get(
        f"{GOOGLE_GEOCODING_URL}/{quote(query, safe='')}",
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "results.placeId,results.formattedAddress,results.location,"
                "results.addressComponents"
            ),
            "Accept": "application/json",
        },
        params={"regionCode": "uk", "languageCode": "en"},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Google Geocoding API returned HTTP {response.status_code} without a JSON error"
        ) from exc
    if response.status_code >= 400:
        error = payload.get("error", {})
        status = error.get("status", "HTTP_ERROR")
        message = error.get("message", "Request rejected")
        if response.status_code == 403:
            message += (
                ". Confirm that Billing and Geocoding API are enabled in the Google Cloud "
                "project used by this key, and that the key permits Geocoding API"
            )
        raise RuntimeError(f"Google Geocoding API {status}: {message}")
    return payload.get("results", [])


def _normalise_postcode(value):
    return "".join(str(value or "").upper().split())


def _google_postcode(result):
    for component in result.get("addressComponents", []):
        types = {str(value).casefold() for value in component.get("types", [])}
        if "postal_code" in types or "postalcode" in types:
            return component.get("longText") or component.get("shortText") or ""
    return ""


def select_google_candidate(results, source_postcode, os_easting, os_northing, london_bounds):
    """Select Google's first London result and calculate its distance from OS."""
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    for result in results:
        location = result.get("location", {})
        try:
            easting, northing = transformer.transform(
                float(location["longitude"]), float(location["latitude"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        inside = (
            london_bounds[0] <= easting <= london_bounds[2]
            and london_bounds[1] <= northing <= london_bounds[3]
        )
        if not inside:
            continue
        postcode = _google_postcode(result)
        return {
            "place_id": result.get("placeId", ""),
            "returned_address": result.get("formattedAddress", ""),
            "postcode_match": bool(postcode) and _normalise_postcode(postcode) == _normalise_postcode(source_postcode),
            "distance_to_os_m": float(np.hypot(easting - float(os_easting), northing - float(os_northing))),
            "easting": easting,
            "northing": northing,
        }
    return None


def _google_query(row):
    values = []
    for column in ["EstablishmentName", "Street", "Town", "Postcode"]:
        if column in row.index and pd.notna(row[column]) and str(row[column]).strip():
            value = str(row[column]).strip()
            if value not in values:
                values.append(value)
    return ", ".join(values)


def validate_review_records(audit, cfg, api_key, session=None):
    """Query Google only for records classified as review by OS Places."""
    review = audit.loc[audit["os_confidence"].eq("review")].copy()
    client = session or create_google_session()
    rows = []
    for _, row in review.iterrows():
        query = _google_query(row)
        results = request_google_results(client, api_key, query)
        candidate = select_google_candidate(
            results,
            row.get("Postcode", ""),
            row["os_easting"],
            row["os_northing"],
            cfg["qa"]["london_bounds_bng"],
        )
        if candidate is None:
            status = "manual_review"
            reason = "google_no_london_match"
        elif not candidate["postcode_match"]:
            status = "manual_review"
            reason = "google_postcode_mismatch"
        else:
            status = "accepted_google"
            reason = "google_london_postcode_match"
        rows.append({
            "URN": str(row["URN"]),
            "EstablishmentName": row.get("EstablishmentName", ""),
            "google_query": query,
            "google_result": "matched" if candidate else "no_match",
            "google_place_id": candidate["place_id"] if candidate else "",
            "google_returned_address": candidate["returned_address"] if candidate else "",
            "google_postcode_match": candidate["postcode_match"] if candidate else False,
            "google_distance_to_os_m": candidate["distance_to_os_m"] if candidate else np.nan,
            "coordinate_status": status,
            "google_validation_reason": reason,
            "_google_easting": candidate["easting"] if candidate else np.nan,
            "_google_northing": candidate["northing"] if candidate else np.nan,
        })
    return pd.DataFrame(rows, columns=GOOGLE_INTERNAL_COLUMNS)


def apply_google_coordinates(frame, cfg, api_key, session=None):
    """Replace OS review coordinates only when Google returns the same postcode."""
    resolved = frame.copy()
    resolved["api_returned_address"] = resolved["os_address"]
    resolved["coordinate_status"] = np.where(
        resolved["os_confidence"].isin(["high", "medium"]),
        "accepted_os",
        "google_check_required",
    )
    validation = validate_review_records(resolved, cfg, api_key, session=session)
    for _, result in validation.iterrows():
        mask = resolved["URN"].astype(str).eq(str(result["URN"]))
        resolved.loc[mask, "coordinate_status"] = result["coordinate_status"]
        if result["coordinate_status"] != "accepted_google":
            continue
        easting = float(result["_google_easting"])
        northing = float(result["_google_northing"])
        resolved.loc[mask, "Easting"] = easting
        resolved.loc[mask, "Northing"] = northing
        resolved.loc[mask, "coordinate_source"] = "Google Geocoding API"
        resolved.loc[mask, "api_returned_address"] = result["google_returned_address"]
        geometry_index = resolved.index[mask]
        resolved.loc[geometry_index, "geometry"] = gpd.GeoSeries(
            [Point(easting, northing)] * len(geometry_index),
            index=geometry_index,
            crs=resolved.crs,
        )
    return resolved, validation[GOOGLE_VALIDATION_COLUMNS].copy()
