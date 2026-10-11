"""Numerical checks for complete-zone area weighting and water sensitivity."""
import unittest
import geopandas as gpd
from shapely import box
from src.service_area_analysis import area_weight
from src.walking_catchment import reachable_network
import networkx as nx
from shapely import LineString


class AreaWeightTests(unittest.TestCase):
    def test_complete_zone_denominator_and_land_refinement(self):
        zone=gpd.GeoDataFrame([{'LSOA21CD':'A','usual_residents_total':100,
                               'geometry':box(0,0,10,10)}],crs=27700)
        catchment=box(0,0,5,10)
        standard=area_weight(zone,catchment)
        land=area_weight(zone,catchment,box(5,0,10,10))
        self.assertAlmostEqual(standard.estimated_population.sum(),50)
        self.assertAlmostEqual(land.estimated_population.sum(),100)
        self.assertEqual(standard.iloc[0].zone_area_m2,100)
        self.assertEqual(land.iloc[0].zone_area_m2,50)

    def test_zero_land_denominator_is_not_silently_ignored(self):
        zone=gpd.GeoDataFrame([{'LSOA21CD':'A','usual_residents_total':100,
                               'geometry':box(0,0,10,10)}],crs=27700)
        with self.assertRaises(ValueError):
            area_weight(zone,box(0,0,5,10),box(0,0,10,10))

    def test_550_seconds_leaves_only_50_seconds_of_edge(self):
        graph=nx.MultiDiGraph(crs=27700)
        graph.add_node(1,x=0,y=0);graph.add_node(2,x=400/3,y=0)
        graph.add_edge(1,2,length=400/3,travel_time_s=100,
                       geometry=LineString([(0,0),(400/3,0)]))
        geometry,count=reachable_network(graph,{1:550},600,4.8)
        self.assertAlmostEqual(geometry.length,200/3)
        self.assertEqual(count,1)


if __name__=='__main__':
    unittest.main()
