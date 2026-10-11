"""Tests for the population-only cleaning stage."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from src.population_cleaning import (
    clean_population,
    remove_exact_duplicate_lsoas,
    repair_invalid_population_geometries,
)
from src.utils import raw_hashes


class PopulationCleaningTests(unittest.TestCase):
    def test_exact_duplicate_keeps_first_record(self):
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        frame = gpd.GeoDataFrame({
            "LSOA21CD": ["A", "A"],
            "population": [10, 10],
            "_source_row": [1, 2],
        }, geometry=[polygon, polygon], crs=27700)
        log = []
        cleaned, removed = remove_exact_duplicate_lsoas(frame, "LSOA21CD", log)
        self.assertEqual(removed, 1)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(log[0]["source_row"], 2)

    def test_conflicting_duplicate_is_not_automatically_removed(self):
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        frame = gpd.GeoDataFrame({
            "LSOA21CD": ["A", "A"],
            "population": [10, 11],
            "_source_row": [1, 2],
        }, geometry=[polygon, polygon], crs=27700)
        with self.assertRaises(ValueError):
            remove_exact_duplicate_lsoas(frame, "LSOA21CD", [])

    def test_self_intersection_is_repaired(self):
        bowtie = Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)])
        frame = gpd.GeoDataFrame({"LSOA21CD": ["A"], "_source_row": [7]}, geometry=[bowtie], crs=27700)
        log = []
        cleaned, repaired = repair_invalid_population_geometries(frame, "LSOA21CD", log)
        self.assertEqual(repaired, 1)
        self.assertTrue(cleaned.geometry.iloc[0].is_valid)
        self.assertIn(cleaned.geometry.iloc[0].geom_type, {"Polygon", "MultiPolygon"})
        self.assertEqual(log[0]["source_row"], 7)

    def test_full_clean_preserves_raw_and_records_both_changes(self):
        before = raw_hashes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("src.population_cleaning.CLEANED", root / "cleaned"), patch(
                "src.population_cleaning.OUTPUTS", root / "outputs"
            ):
                result = clean_population()
            cleaned = gpd.read_file(result["cleaned_output"], engine="pyogrio")
            log = pd.read_csv(result["cleaning_log"])
            self.assertEqual(len(cleaned), 109)
            self.assertTrue(cleaned["LSOA21CD"].is_unique)
            self.assertTrue(cleaned.geometry.is_valid.all())
            self.assertEqual(result["duplicates_removed"], 1)
            self.assertEqual(result["geometries_repaired"], 1)
            self.assertEqual(set(log["action"]), {"remove_exact_duplicate", "repair_invalid_geometry"})
            self.assertEqual(set(log["source_row"]), {103, 109})
        self.assertEqual(before, raw_hashes())


if __name__ == "__main__":
    unittest.main()
