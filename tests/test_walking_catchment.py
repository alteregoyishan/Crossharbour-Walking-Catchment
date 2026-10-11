"""Tests for inserting origins and truncating reachable edges."""
import unittest

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point

from src.walking_catchment import insert_origins_into_edges, reachable_network


class WalkingCatchmentTests(unittest.TestCase):
    def test_origin_splits_bidirectional_edge_without_connector(self):
        graph = nx.MultiDiGraph(crs="EPSG:27700")
        graph.add_node(1, x=0.0, y=0.0)
        graph.add_node(2, x=100.0, y=0.0)
        forward = LineString([(0, 0), (100, 0)])
        reverse = LineString([(100, 0), (0, 0)])
        attrs = {"osmid": 10, "highway": "footway", "length": 100.0, "travel_time_s": 75.0}
        graph.add_edge(1, 2, geometry=forward, **attrs)
        graph.add_edge(2, 1, geometry=reverse, **attrs)
        origins = gpd.GeoDataFrame([{
            "origin_id": "A", "access_name": "test", "site_access_node": "-1",
            "matched_edge_u": "1", "matched_edge_v": "2", "matched_edge_key": 0,
            "geometry": Point(40, 0),
        }], geometry="geometry", crs="EPSG:27700")

        result, qa = insert_origins_into_edges(graph, origins, 4.8)

        self.assertIn(-1, result)
        self.assertEqual(result.number_of_edges(), 4)
        self.assertAlmostEqual(qa.loc[0, "length_difference_m"], 0.0)
        self.assertFalse(any(data.get("highway") == "site_connector"
                             for *_, data in result.edges(data=True)))

    def test_partially_reachable_edge_is_truncated(self):
        graph = nx.MultiDiGraph(crs="EPSG:27700")
        graph.add_node(1, x=0.0, y=0.0)
        graph.add_node(2, x=100.0, y=0.0)
        graph.add_edge(
            1, 2, geometry=LineString([(0, 0), (100, 0)]),
            length=100.0, travel_time_s=100.0,
        )

        geometry, partial_count = reachable_network(graph, {1: 0.0}, 30, 3.6)

        self.assertAlmostEqual(geometry.length, 30.0)
        self.assertEqual(partial_count, 1)


if __name__ == "__main__":
    unittest.main()
