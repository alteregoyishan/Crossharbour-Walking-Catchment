import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point, Polygon

from src.catchment_polygonisation import (
    _clip_triangle_below_level,
    _hole_count,
    _validation_metrics,
    build_method_b,
)
from src.catchment_barrier_review import (
    apply_common_barrier_policy,
    bridge_corridors,
    validate_water_layer,
)


def test_triangle_time_surface_is_linearly_clipped_at_threshold():
    coordinates = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    values = np.array([0.0, 1000.0, 0.0])
    clipped = _clip_triangle_below_level(coordinates, values, 600.0)
    assert clipped.is_valid
    assert np.isclose(clipped.area, 0.42)


def test_controlled_hole_fill_preserves_exterior_boundary():
    shell = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    hole = [(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]
    raw = Polygon(shell, [hole])
    holes = gpd.GeoDataFrame(
        [{"method_b_action": "fill", "geometry": Polygon(hole)}], crs="EPSG:27700")
    controlled, difference = build_method_b(raw, holes)
    assert difference == 0
    assert _hole_count(controlled) == 0
    assert np.isclose(controlled.area, 100)


def test_validation_uses_reference_labels_independent_of_candidate_polygon():
    grid = gpd.GeoDataFrame({
        "reference_accessible": [True, True, False, False],
        "geometry": [Point(0, 0), Point(2, 0), Point(0, 2), Point(2, 2)],
    }, crs="EPSG:27700")
    candidate = Polygon([(-1, -1), (1, -1), (1, 3), (-1, 3)])
    metrics = _validation_metrics(grid, candidate)
    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["true_negative"] == 1
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 0.5
    assert metrics["f1_score"] == 0.5


def test_common_barrier_policy_separates_land_and_bridge_display_geometry():
    candidate = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
    water = Polygon([(8, -1), (12, -1), (12, 11), (8, 11)])
    crossings = gpd.GeoDataFrame({
        "policy_use": [True],
        "geometry": [LineString([(0, 5), (20, 5)])],
    }, crs="EPSG:27700")
    land, corridor, display = apply_common_barrier_policy(
        candidate, water, crossings, bridge_buffer_m=1.0)
    assert np.isclose(land.intersection(water).area, 0.0)
    assert corridor.area > 0
    assert display.area > land.area
    assert display.area - land.area < corridor.area
    assert display.geom_type == "Polygon"


def test_bridge_buffer_reports_one_metre_each_side():
    water = Polygon([(8, -1), (12, -1), (12, 11), (8, 11)])
    crossings = gpd.GeoDataFrame({
        "policy_use": [True],
        "geometry": [LineString([(0, 5), (20, 5)])],
    }, crs="EPSG:27700")
    corridor = bridge_corridors(crossings, water, buffer_m=1.0)
    # Four metres of water plus a 0.5 m land tie-in at each end, two metres wide.
    assert np.isclose(corridor.area, 10.0)


def test_water_validation_repairs_polygon_geometry():
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    frame = gpd.GeoDataFrame(geometry=[bowtie], crs="EPSG:27700")
    cleaned, union = validate_water_layer(frame)
    assert cleaned.geometry.is_valid.all()
    assert union.is_valid
    assert np.isclose(union.area, 2.0)
