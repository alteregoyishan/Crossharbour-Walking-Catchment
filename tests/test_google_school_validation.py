"""Tests for selective Google validation of low-confidence school coordinates."""
import unittest
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point

from src.config import load_config
from src.google_school_validation import (
    apply_google_coordinates,
    request_google_results,
    validate_review_records,
)


class GoogleSchoolValidationTests(unittest.TestCase):
    @staticmethod
    def _google_result(easting, northing, postcode="E14 0AA"):
        longitude, latitude = Transformer.from_crs(
            "EPSG:27700", "EPSG:4326", always_xy=True
        ).transform(easting, northing)
        return [{
            "placeId": "google-place-1",
            "formattedAddress": f"Review School, London {postcode}, UK",
            "location": {"latitude": latitude, "longitude": longitude},
            "addressComponents": [{
                "longText": postcode,
                "types": ["postal_code"],
            }],
        }]

    def test_only_os_review_records_are_queried(self):
        audit = pd.DataFrame([
            {
                "URN": "1", "EstablishmentName": "Review School", "Street": "1 Road",
                "Town": "London", "Postcode": "E14 0AA", "os_confidence": "review",
                "os_easting": 538000.0, "os_northing": 179000.0,
            },
            {
                "URN": "2", "EstablishmentName": "High School", "Street": "2 Road",
                "Town": "London", "Postcode": "E14 0AB", "os_confidence": "high",
                "os_easting": 538100.0, "os_northing": 179100.0,
            },
        ])
        with patch(
            "src.google_school_validation.request_google_results",
            return_value=self._google_result(538000, 179000),
        ) as request:
            result = validate_review_records(audit, load_config(), "test-key", session=object())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["URN"].tolist(), ["1"])
        self.assertEqual(result.iloc[0]["coordinate_status"], "accepted_google")
        self.assertLess(result.iloc[0]["google_distance_to_os_m"], 0.1)

    def test_google_permission_error_explains_required_setup(self):
        class Response:
            status_code = 403

            @staticmethod
            def json():
                return {"error": {"status": "PERMISSION_DENIED", "message": "No permission"}}

        class Session:
            @staticmethod
            def get(*args, **kwargs):
                return Response()

        with self.assertRaisesRegex(RuntimeError, "Billing and Geocoding API"):
            request_google_results(Session(), "secret-test-key", "Example School")

    def test_google_postcode_match_replaces_low_confidence_os_coordinate(self):
        schools = gpd.GeoDataFrame([{
            "URN": "1", "EstablishmentName": "Review School", "Street": "1 Road",
            "Town": "London", "Postcode": "E14 0AA", "os_confidence": "review",
            "os_easting": 538000.0, "os_northing": 179000.0,
            "Easting": 538000.0, "Northing": 179000.0,
            "coordinate_source": "OS Places API",
            "os_address": "OS REVIEW SCHOOL ADDRESS",
        }], geometry=[Point(538000, 179000)], crs="EPSG:27700")
        with patch(
            "src.google_school_validation.request_google_results",
            return_value=self._google_result(538100, 179100),
        ):
            resolved, validation = apply_google_coordinates(
                schools, load_config(), "test-key", session=object()
            )
        self.assertEqual(resolved.iloc[0]["coordinate_source"], "Google Geocoding API")
        self.assertEqual(resolved.iloc[0]["coordinate_status"], "accepted_google")
        self.assertEqual(resolved.iloc[0]["api_returned_address"], "Review School, London E14 0AA, UK")
        self.assertAlmostEqual(resolved.iloc[0]["Easting"], 538100, places=1)
        self.assertEqual(validation.iloc[0]["google_place_id"], "google-place-1")

    def test_google_postcode_mismatch_keeps_os_coordinate_for_review(self):
        schools = gpd.GeoDataFrame([{
            "URN": "1", "EstablishmentName": "Review School", "Street": "1 Road",
            "Town": "London", "Postcode": "E14 0AA", "os_confidence": "review",
            "os_easting": 538000.0, "os_northing": 179000.0,
            "Easting": 538000.0, "Northing": 179000.0,
            "coordinate_source": "OS Places API",
            "os_address": "OS REVIEW SCHOOL ADDRESS",
        }], geometry=[Point(538000, 179000)], crs="EPSG:27700")
        with patch(
            "src.google_school_validation.request_google_results",
            return_value=self._google_result(538100, 179100, postcode="E14 9ZZ"),
        ):
            resolved, validation = apply_google_coordinates(
                schools, load_config(), "test-key", session=object()
            )
        self.assertEqual(resolved.iloc[0]["coordinate_source"], "OS Places API")
        self.assertEqual(resolved.iloc[0]["api_returned_address"], "OS REVIEW SCHOOL ADDRESS")
        self.assertEqual(resolved.iloc[0]["coordinate_status"], "manual_review")
        self.assertEqual(resolved.iloc[0]["Easting"], 538000)
        self.assertEqual(validation.iloc[0]["google_validation_reason"], "google_postcode_mismatch")


if __name__ == "__main__":
    unittest.main()
