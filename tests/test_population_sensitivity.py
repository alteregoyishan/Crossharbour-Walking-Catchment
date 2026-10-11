import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

from src.population_sensitivity import (
    _p4_support,
    _support_allocation,
    _exterior_only_boundary,
    classify_buildings,
)


def test_mixed_use_building_uses_residential_address_share():
    buildings = gpd.GeoDataFrame({
        "buildinguse": ["Commercial Activity: Retail|Residential Accommodation"],
        "description": ["Mixed Use Building"],
        "buildinguse_addresscount_residential": [8],
        "buildinguse_addresscount_commercial": [2],
        "buildinguse_addresscount_other": [0],
        "numberoffloors": [12],
        "height_relativemax_m": [38.0],
    }, geometry=[box(0, 0, 10, 10)], crs=27700)
    result = classify_buildings(buildings).iloc[0]
    assert result.population_support_class == "mixed_use"
    assert result.residential_share_proxy == pytest.approx(0.8)
    assert result.direct_number_of_floors == 12


def test_missing_residential_support_falls_back_to_p1_without_losing_population():
    population = gpd.GeoDataFrame({
        "LSOA21CD": ["A"],
        "usual_residents_total": [100],
    }, geometry=[box(0, 0, 10, 10)], crs=27700)
    empty_support = gpd.GeoDataFrame(
        {"population_support_class": pd.Series(dtype=str)},
        geometry=gpd.GeoSeries([], crs=27700), crs=27700)
    catchment = box(0, 0, 5, 10)
    result = _support_allocation(population, catchment, empty_support, "P2").iloc[0]
    assert bool(result.fallback_to_p1)
    assert result.allocation_fraction == pytest.approx(0.5)
    assert result.estimated_population == pytest.approx(50.0)


def test_p4_uses_direct_floors_and_explicit_mixed_use_share():
    buildings = gpd.GeoDataFrame({
        "population_support_class": ["residential", "mixed_use", "non_residential", "residential"],
        "direct_number_of_floors": [8.0, 12.0, 20.0, float("nan")],
    }, geometry=[box(i * 20, 0, i * 20 + 10, 10) for i in range(4)], crs=27700)

    support = _p4_support(buildings, mixed_use_share=0.25)

    assert support.index.tolist() == [0, 1]
    assert support.loc[0, "floor_area_proxy"] == pytest.approx(8.0)
    assert support.loc[1, "floor_area_proxy"] == pytest.approx(3.0)


def test_exterior_only_boundary_omits_hole_rings():
    polygon = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        holes=[[(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]],
    )

    boundary = _exterior_only_boundary(polygon)

    assert boundary.length == pytest.approx(40.0)
