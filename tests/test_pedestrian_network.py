"""Tests for pedestrian network extent and routing costs."""
import unittest

import geopandas as gpd
import networkx as nx
from shapely.geometry import Point

from src.pedestrian_network import (
    add_walking_costs, build_download_area, configure_osmnx_way_tags,
    validate_pedestrian_graph,
)


class PedestrianNetworkTests(unittest.TestCase):
    def test_download_area_contains_every_school(self):
        schools = gpd.GeoDataFrame(
            {"URN": ["1", "2", "3"]},
            geometry=[Point(538000, 179000), Point(540000, 181000), Point(536000, 176000)],
            crs="EPSG:27700",
        )
        area = build_download_area(schools, 2000)
        self.assertTrue(schools.geometry.apply(area.covers).all())
        self.assertGreater(area.area, schools.geometry.union_all().convex_hull.area)

    def test_walk_speed_becomes_networkx_edge_cost(self):
        graph = nx.MultiDiGraph(crs="EPSG:27700")
        graph.add_node(1, x=0, y=0)
        graph.add_node(2, x=120, y=0)
        graph.add_edge(1, 2, length=120.0)
        add_walking_costs(graph, 4.8)
        self.assertAlmostEqual(graph.edges[1, 2, 0]["travel_time_s"], 90.0)
        validate_pedestrian_graph(graph, "EPSG:27700")

    def test_pedestrian_audit_tags_are_retained_for_future_downloads(self):
        tags = configure_osmnx_way_tags()
        for tag in ("foot", "layer", "sidewalk", "sidewalk:left", "sidewalk:right", "sidewalk:both"):
            self.assertIn(tag, tags)


if __name__ == "__main__":
    unittest.main()
