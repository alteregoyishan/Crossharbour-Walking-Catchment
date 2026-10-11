"""Tests for pedestrian network topology and geometry QA helpers."""
import unittest

import networkx as nx
from shapely.geometry import LineString

from src.pedestrian_network_qa import (
    _minimum_edge_issue_travel_seconds, classify_pedestrian_access,
    edge_geometry_metrics, physical_dead_end_nodes,
)


class PedestrianNetworkQATests(unittest.TestCase):
    def test_dead_end_count_uses_distinct_neighbours(self):
        graph = nx.MultiDiGraph(crs="EPSG:27700")
        for node, x in ((1, 0.0), (2, 10.0), (3, 20.0)):
            graph.add_node(node, x=x, y=0.0)
        graph.add_edge(1, 2, length=10.0)
        graph.add_edge(2, 1, length=10.0)
        graph.add_edge(2, 3, length=10.0)
        graph.add_edge(3, 2, length=10.0)
        graph.add_edge(1, 2, length=10.0)

        self.assertEqual(physical_dead_end_nodes(graph), {1, 3})

    def test_geometry_metrics_compare_stored_and_projected_lengths(self):
        graph = nx.MultiDiGraph(crs="EPSG:27700")
        graph.add_node(1, x=0.0, y=0.0)
        graph.add_node(2, x=100.0, y=0.0)
        graph.add_edge(
            1, 2, length=110.0,
            geometry=LineString([(0.0, 0.0), (100.0, 0.0)]),
        )

        result = edge_geometry_metrics(graph).iloc[0]

        self.assertAlmostEqual(result["absolute_difference_m"], 10.0)
        self.assertAlmostEqual(result["relative_difference"], 0.1)
        self.assertAlmostEqual(result["endpoint_mismatch_m"], 0.0)
        self.assertTrue(result["explicit_geometry"])

    def test_foot_yes_overrides_general_access_no(self):
        self.assertIsNone(classify_pedestrian_access("no", "yes", "footway"))
        self.assertEqual(
            classify_pedestrian_access("no", None, "footway"),
            "restricted_pedestrian_access",
        )
        self.assertEqual(
            classify_pedestrian_access("no", "permissive", "footway"),
            "conditional_pedestrian_access",
        )

    def test_restricted_edge_relevance_uses_routed_arrival_time(self):
        travel_times = {10: 940.0, 20: 980.0, 30: 1200.0}

        result = _minimum_edge_issue_travel_seconds(
            "10-20-0; 20-10-0; 20-30-0", travel_times
        )

        self.assertEqual(result, 940.0)


if __name__ == "__main__":
    unittest.main()
