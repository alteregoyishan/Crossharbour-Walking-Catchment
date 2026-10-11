"""Tests for reproducible site origin selection."""
import unittest

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, box

from src.site_origins import find_best_site_accesses


class SiteOriginTests(unittest.TestCase):
    def test_service_and_tangential_edges_are_excluded(self):
        nodes = gpd.GeoDataFrame(
            geometry=[Point(2, 8), Point(2, 15), Point(4, 8), Point(4, 15),
                      Point(8, 8), Point(8, 15), Point(-5, -1), Point(8, 0.3)],
            index=[1, 2, 3, 4, 5, 6, 7, 8], crs="EPSG:27700",
        )
        edge_index = pd.MultiIndex.from_tuples(
            [(1, 2, 0), (3, 4, 0), (5, 6, 0), (7, 8, 0)], names=["u", "v", "key"]
        )
        edges = gpd.GeoDataFrame(
            {
                "highway": ["service", "footway", "footway", "footway"],
                "access": [None, None, None, None],
                "geometry": [
                    LineString([(2, 8), (2, 15)]), LineString([(4, 8), (4, 15)]),
                    LineString([(8, 8), (8, 15)]), LineString([(-5, -1), (8, 0.3)]),
                ],
            }, index=edge_index, crs="EPSG:27700",
        )

        selected = find_best_site_accesses(
            box(0, 0, 10, 10), edges, nodes, 2, 45, 2, 2
        )

        self.assertEqual(len(selected), 2)
        self.assertTrue(all(item["side"] == "north" for item in selected))
        self.assertTrue(all(item["highway"] == "footway" for item in selected))


if __name__ == "__main__":
    unittest.main()
