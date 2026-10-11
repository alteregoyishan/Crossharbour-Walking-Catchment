"""Tests for the small, config-driven QA workflow."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from src.config import load_config
from src.data_validation import IssueLog, run_validation, validate_population, validate_schools, validate_site
from src.utils import raw_hashes


def school_table():
    return pd.DataFrame([{
        "URN": "X1",
        "EstablishmentName": "Example Primary School",
        "Easting": "538000",
        "Northing": "179000",
        "EstablishmentStatus (name)": "Open",
        "TypeOfEstablishment (name)": "Community school",
        "PhaseOfEducation (name)": "Primary",
        "StatutoryLowAge": "3",
        "StatutoryHighAge": "11",
        "SchoolCapacity": "200",
        "NumberOfPupils": "100",
        "Street": "Example Road",
        "Town": "London",
        "Postcode": "E14 1AA",
    }])


def site_frame():
    geometry = Polygon([
        (538000, 179000), (538010, 179000), (538010, 179010),
        (538000, 179010), (538000, 179000),
    ])
    return gpd.GeoDataFrame({"note": ["Area approx. 0.02 ha"]}, geometry=[geometry], crs=27700)


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_columns_are_discovered_from_the_loaded_table(self):
        source = school_table().rename(columns={"URN": "urn", "Easting": "easting", "Northing": "northing"})
        issues = IssueLog()
        result = validate_schools(source, site_frame(), self.cfg, issues)
        self.assertNotIn("datasets", self.cfg["qa"])
        self.assertTrue(result["coordinate_usable"].iloc[0])
        self.assertEqual(result["record_id"].iloc[0], "X1:row2")

    def test_duplicate_identifier_is_reported_without_removing_rows(self):
        source = pd.concat([school_table(), school_table()], ignore_index=True)
        issues = IssueLog()
        result = validate_schools(source, site_frame(), self.cfg, issues)
        duplicate = [row for row in issues.rows if row["issue"] == "duplicate_identifier"]
        self.assertEqual(len(result), 2)
        self.assertEqual(len(duplicate), 1)
        self.assertIn("X1", duplicate[0]["details"])

    def test_unusable_coordinates_are_one_clear_issue(self):
        source = pd.concat([school_table()] * 3, ignore_index=True)
        source["URN"] = ["A", "B", "C"]
        source["Easting"] = [None, "not numeric", "0"]
        issues = IssueLog()
        result = validate_schools(source, site_frame(), self.cfg, issues)
        coordinate_issues = [row for row in issues.rows if row["issue"] == "unusable_coordinates"]
        self.assertTrue(result["qa_unusable_coordinates"].all())
        self.assertEqual(len(coordinate_issues), 3)
        self.assertEqual(result["original_easting"].tolist(), source["Easting"].tolist())

    def test_distant_coordinate_only_needs_verification(self):
        source = school_table()
        source["Easting"], source["Northing"] = "400000", "400000"
        source["TypeOfEstablishment (name)"] = "Service children's education"
        issues = IssueLog()
        result = validate_schools(source, site_frame(), self.cfg, issues)
        matching = [row for row in issues.rows if row["issue"] == "coordinate_needs_verification"]
        self.assertTrue(result["coordinate_usable"].iloc[0])
        self.assertEqual(len(matching), 1)
        self.assertNotIn("overseas", matching[0]["issue"])

    def test_invalid_polygon_and_population_sum_are_detected(self):
        shape = Polygon([
            (538000, 179000), (538010, 179010), (538000, 179010),
            (538010, 179000), (538000, 179000),
        ])
        frame = gpd.GeoDataFrame({
            "LSOA21CD": ["arbitrary"],
            "usual_residents_total": [10],
            "usual_residents_household": [9],
            "usual_residents_communal": [2],
        }, geometry=[shape], crs=27700)
        issues = IssueLog()
        result = validate_population(frame, self.cfg, issues)
        names = {row["issue"] for row in issues.rows}
        self.assertEqual(result.geometry.iloc[0].wkb, shape.wkb)
        self.assertTrue({"invalid_geometry", "population_sum_mismatch"}.issubset(names))

    def test_site_area_statement_is_compared_with_geometry(self):
        issues = IssueLog()
        result = validate_site(site_frame(), self.cfg, issues)
        self.assertAlmostEqual(result["area_ha"].iloc[0], 0.01)
        self.assertTrue(any(row["issue"] == "stated_area_differs_from_geometry" for row in issues.rows))

    def test_full_run_writes_issue_detail_and_category_summary(self):
        before = raw_hashes()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch("src.data_validation.OUTPUTS", output):
                summary = run_validation(self.cfg)
            issue_path = output / "tables" / "qa_issues.csv"
            summary_path = output / "tables" / "qa_summary.csv"
            self.assertTrue(issue_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertEqual(set((output / "tables").iterdir()), {issue_path, summary_path})
            issues = pd.read_csv(issue_path)
            summary_table = pd.read_csv(summary_path)
            self.assertIn("duplicate_identifier", set(issues["issue"]))
            self.assertIn("invalid_geometry", set(issues["issue"]))
            self.assertTrue({"field_structure", "geometry_topology"}.issubset(set(summary_table["check_category"])))
            self.assertEqual(summary_table.loc[summary_table["check_category"].eq("crs"), "status"].iloc[0], "pass")
            duplicate_summary = summary_table.loc[summary_table["issue"].eq("duplicate_identifier")]
            self.assertIn("population", set(duplicate_summary["dataset"]))
            self.assertTrue(duplicate_summary["example_records"].astype(str).str.contains("E01035705").any())
            self.assertEqual(summary["corrections_applied"], [])
        self.assertEqual(before, raw_hashes())


if __name__ == "__main__":
    unittest.main()
