"""Tests for closed-school filtering, deduplication and OS Places matching."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import pandas as pd

from src.config import load_config
from src.school_cleaning import (
    SCHOOL_OUTPUT_FIELDS,
    classify_school_matches,
    clean_schools,
    prepare_school_records,
    select_london_candidate,
)
from src.utils import raw_hashes


class SchoolCleaningTests(unittest.TestCase):
    def test_closed_schools_and_exact_duplicate_are_removed(self):
        frame = pd.DataFrame([
            {"URN": "1", "EstablishmentName": "Open School", "EstablishmentStatus (name)": "Open", "LA (name)": "London"},
            {"URN": "1", "EstablishmentName": "Open School", "EstablishmentStatus (name)": "Open", "LA (name)": "London"},
            {"URN": "2", "EstablishmentName": "Closed School", "EstablishmentStatus (name)": "Closed", "LA (name)": "London"},
            {"URN": "3", "EstablishmentName": "Overseas School", "EstablishmentStatus (name)": "Open", "LA (name)": "Fieldwork Overseas Establishments"},
        ])
        log = []
        cleaned, qa, identifier, status, closed, overseas, duplicates = prepare_school_records(frame, log)
        self.assertEqual((identifier, status), ("URN", "EstablishmentStatus (name)"))
        self.assertEqual((closed, overseas, duplicates, len(cleaned)), (1, 1, 1, 1))
        self.assertEqual(qa["qa_action"].value_counts().to_dict(), {"remove": 3, "keep": 1})
        self.assertEqual(set(qa.loc[qa.qa_action.eq("remove"), "qa_reason"]), {"Closed", "Duplicate", "Overseas"})
        self.assertEqual(set(row["action"] for row in log), {
            "exclude_closed_school", "exclude_overseas_school", "remove_exact_duplicate",
        })

    def test_candidate_must_be_inside_london(self):
        results = [
            {"DPA": {"ADDRESS": "Outside", "X_COORDINATE": 400000, "Y_COORDINATE": 400000, "MATCH": 1}},
            {"DPA": {"ADDRESS": "School, London", "ORGANISATION_NAME": "Example School",
                     "POSTCODE": "E14 1AA", "X_COORDINATE": 538000, "Y_COORDINATE": 179000,
                     "MATCH": 0.9, "UPRN": 123}},
        ]
        candidate = select_london_candidate(results, "Example School", "E14 1AA", [500000, 150000, 570000, 210000])
        self.assertEqual(candidate["uprn"], 123)
        self.assertEqual((candidate["easting"], candidate["northing"]), (538000, 179000))

    def test_match_classification_rejects_no_match(self):
        frame = gpd.GeoDataFrame([
            {
                "URN": "1", "_source_row": 2, "LA (name)": "Tower Hamlets",
                "TypeOfEstablishment (name)": "Community school", "os_confidence": "medium",
                "os_postcode_match": True, "os_name_similarity": 0.5,
                "os_distance_from_original_m": 20, "geometry": None,
            },
        ], geometry="geometry", crs="EPSG:27700")
        classified = classify_school_matches(frame, "URN")
        self.assertEqual(classified.iloc[0]["os_review_reason"], "accepted_partial_match")

    def test_full_clean_queries_every_retained_school(self):
        before = raw_hashes()
        calls = []

        def fake_request(session, api_key, query, london_bounds, timeout=30):
            calls.append(query)
            postcode = query.split(",")[-1].strip()
            offset = len(calls)
            return [{"DPA": {
                "ADDRESS": query,
                "ORGANISATION_NAME": query.split(",")[0],
                "POSTCODE": postcode,
                "X_COORDINATE": 520000 + offset,
                "Y_COORDINATE": 175000 + offset,
                "MATCH": 1.0,
                "MATCH_DESCRIPTION": "Exact",
                "UPRN": 100000 + offset,
                "RPC": "1",
            }}]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("src.school_cleaning.CLEANED", root / "cleaned"), patch(
                "src.school_cleaning.OUTPUTS", root / "outputs"
            ), patch(
                "src.school_cleaning.QA", root / "qa"
            ), patch("src.school_cleaning.request_os_candidates", side_effect=fake_request):
                result = clean_schools(
                    load_config(), api_key="test-key", session=object(),
                    google_api_key="test-google-key", google_session=object(),
                )
            schools = pd.read_csv(result["csv_output"], dtype={"URN": str})
            spatial = gpd.read_file(result["gpkg_output"], engine="pyogrio")
            log = pd.read_csv(result["cleaning_log"])
            self.assertEqual(result["source_count"], 87)
            self.assertEqual(result["cleaned_count"], 42)
            self.assertEqual(result["closed_excluded"], 29)
            self.assertEqual(result["duplicates_removed"], 1)
            self.assertEqual(result["overseas_excluded"], 15)
            self.assertEqual(result["api_lookups"], 42)
            self.assertEqual(result["accepted_count"], 42)
            self.assertEqual(result["review_count"], 0)
            self.assertEqual(len(calls), 42)
            self.assertTrue(schools["URN"].is_unique)
            self.assertEqual(
                schools.columns.tolist(),
                SCHOOL_OUTPUT_FIELDS + [
                    "api_returned_address", "coordinate_source",
                    "coordinate_confidence", "coordinate_status",
                ],
            )
            self.assertTrue(schools["coordinate_status"].eq("accepted_os").all())
            self.assertEqual(spatial.geometry.notna().sum(), 42)
            self.assertEqual((log["action"] == "os_places_coordinate_lookup").sum(), 42)
            self.assertEqual((log["action"] == "exclude_overseas_school").sum(), 15)
            self.assertEqual(len(pd.read_csv(result["match_audit"])), 42)
            self.assertEqual(len(pd.read_csv(result["coordinate_review"])), 0)
            qa = pd.read_csv(result["qa_output"])
            self.assertEqual(len(qa), 87)
            self.assertEqual(qa["qa_action"].value_counts().to_dict(), {"keep": 42, "remove": 45})
            self.assertEqual(qa.loc[qa["qa_reason"].eq("Overseas")].shape[0], 15)
        self.assertEqual(before, raw_hashes())


if __name__ == "__main__":
    unittest.main()
