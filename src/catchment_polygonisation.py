"""Diagnose 10-minute service-area holes and compare polygonisation methods.

This module preserves the routed network and existing catchment.  It creates
new review outputs only.  Method C is deliberately labelled experimental: it
uses a dense, ordinary Delaunay time surface because the Lin et al. reference
implementation's CGAL constrained triangulation is unavailable here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import Delaunay
from shapely import LineString, Point, Polygon, make_valid, normalize
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .os_data_services import os_maps_basemap, selected_water_path
from .paths import CLEANED, OPENSTREETMAP, OUTPUTS, PROCESSED, RAW, ROOT
from .pedestrian_network import load_pedestrian_graph
from .service_area_analysis import area_weight, polygonal
from .walking_catchment import _edge_geometry


CRS = "EPSG:27700"
SPEED_KMH = 4.8
SPEED_MPS = SPEED_KMH / 3.6
THRESHOLD_S = 600.0
GRID_SPACING_M = 10.0
NETWORK_SAMPLE_SPACING_M = 15.0
MAX_TRIANGLE_EDGE_M = 150.0
REFERENCE_COMMIT = "066c6d6796250fd186a382dc612678b779d671cc"

GIS = OUTPUTS / "gis"
TABLES = OUTPUTS / "tables"
MAPS = OUTPUTS / "maps"
METHOD = OUTPUTS / "methodology"
OUT_METHODS = GIS / "catchment_polygonisation_methods.gpkg"
OUT_HOLES = GIS / "catchment_hole_inventory.gpkg"
OUT_HOLE_CSV = TABLES / "catchment_hole_inventory.csv"
OUT_COMPARE = TABLES / "catchment_method_comparison.csv"
OUT_PAIRWISE = TABLES / "catchment_method_pairwise_difference.csv"
OUT_VALIDATION = TABLES / "catchment_method_validation.csv"
OUT_POPULATION = TABLES / "catchment_polygonisation_population.csv"
OUT_BARRIERS = TABLES / "catchment_barrier_crossings.csv"
OUT_WATER_FEATURES = TABLES / "catchment_water_features.csv"
OUT_HOLE_MAP = MAPS / "catchment_hole_diagnostic.png"
OUT_HOLE_MAP_PDF = MAPS / "catchment_hole_diagnostic.pdf"
OUT_COMPARE_MAP = MAPS / "figure_04_catchment_method_comparison.png"
OUT_COMPARE_MAP_PDF = MAPS / "figure_04_catchment_method_comparison.pdf"
OUT_REPORT = METHOD / "catchment_polygonisation_review.md"


@dataclass
class PhysicalEdge:
    geometry: object
    members: list


def _write_layer(frame, path, layer, first=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if first:
        path.unlink(missing_ok=True)
    frame.to_file(path, layer=layer, driver="GPKG", engine="pyogrio")


def _polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    return [part for part in getattr(geometry, "geoms", [])
            if part.geom_type in ("Polygon", "MultiPolygon")]


def _hole_count(geometry):
    return sum(len(part.interiors) for part in _polygon_parts(geometry))


def _hole_area(geometry):
    return sum(Polygon(ring).area for part in _polygon_parts(geometry)
               for ring in part.interiors)


def _physical_edges(graph):
    """Group reverse directed copies of the same physical geometry."""
    grouped = {}
    for u, v, key, data in graph.edges(keys=True, data=True):
        geometry = _edge_geometry(graph, u, v, data)
        canonical = normalize(geometry)
        group_key = canonical.wkb
        if group_key not in grouped:
            grouped[group_key] = PhysicalEdge(canonical, [])
        grouped[group_key].members.append((u, v, key, geometry, data))
    return list(grouped.values())


def _network_arrival(edge, point, travel_times):
    """Minimum directed arrival time at a point on one physical edge."""
    candidates = []
    for u, _, _, geometry, _ in edge.members:
        if u in travel_times:
            candidates.append(float(travel_times[u]) + geometry.project(point) / SPEED_MPS)
    return min(candidates, default=np.inf)


def _reference_point(point, edge_tree, physical_edges, travel_times, water):
    edge_index = int(edge_tree.nearest(point))
    edge = physical_edges[edge_index]
    projection = edge.geometry.interpolate(edge.geometry.project(point))
    network_time = _network_arrival(edge, projection, travel_times)
    off_network_m = float(point.distance(projection))
    connector = LineString([point, projection])
    water_barrier = bool(water.covers(point) or connector.intersection(water).length > 0.5)
    total = network_time + off_network_m / SPEED_MPS
    return {
        "reference_time_s": total,
        "network_time_s": network_time,
        "off_network_distance_m": off_network_m,
        "water_barrier": water_barrier,
        "reference_accessible": bool(np.isfinite(total) and total <= THRESHOLD_S and not water_barrier),
        "nearest_edge_index": edge_index,
    }


def build_reference_grid(raw_polygon, physical_edges, travel_times, water):
    """Independent 10 m point-access model inspired by Lin et al. section 2.4."""
    xmin, ymin, xmax, ymax = raw_polygon.bounds
    pad = 100.0
    xs = np.arange(math.floor((xmin-pad)/GRID_SPACING_M)*GRID_SPACING_M,
                   math.ceil((xmax+pad)/GRID_SPACING_M)*GRID_SPACING_M + 0.1,
                   GRID_SPACING_M)
    ys = np.arange(math.floor((ymin-pad)/GRID_SPACING_M)*GRID_SPACING_M,
                   math.ceil((ymax+pad)/GRID_SPACING_M)*GRID_SPACING_M + 0.1,
                   GRID_SPACING_M)
    points = [Point(x, y) for y in ys for x in xs]
    edge_tree = STRtree([edge.geometry for edge in physical_edges])
    nearest = edge_tree.nearest(np.asarray(points, dtype=object))
    rows = []
    for point, edge_index in zip(points, nearest):
        edge = physical_edges[int(edge_index)]
        projection = edge.geometry.interpolate(edge.geometry.project(point))
        network_time = _network_arrival(edge, projection, travel_times)
        off_network_m = float(point.distance(projection))
        connector = LineString([point, projection])
        water_barrier = bool(water.covers(point) or connector.intersection(water).length > 0.5)
        total = network_time + off_network_m / SPEED_MPS
        rows.append({
            "point_id": len(rows) + 1,
            "reference_time_s": total if np.isfinite(total) else np.nan,
            "network_time_s": network_time if np.isfinite(network_time) else np.nan,
            "off_network_distance_m": off_network_m,
            "water_barrier": water_barrier,
            "reference_accessible": bool(np.isfinite(total) and total <= THRESHOLD_S and not water_barrier),
            "nearest_edge_index": int(edge_index),
            "geometry": point,
        })
    return gpd.GeoDataFrame(rows, crs=CRS), edge_tree


def _load_ngd_samples():
    path = ROOT / "data/qa/os_ngd_discovery_samples.gpkg"
    result = {"buildings": None, "land": None, "sites": None}
    if not path.exists():
        return result
    layers = {
        "buildings": "bld-fts-building-4",
        "land": "lnd-fts-land-3",
        "sites": "lus-fts-site-2",
    }
    for name, layer in layers.items():
        try:
            result[name] = gpd.read_file(path, layer=layer).to_crs(CRS)
        except Exception:
            result[name] = None
    return result


def _context_values(hole, frame, fields):
    if frame is None or frame.empty:
        return ""
    subset = frame[frame.intersects(hole)]
    values = []
    for field in fields:
        if field in subset:
            values.extend(subset[field].dropna().astype(str).tolist())
    return " | ".join(sorted(set(value for value in values if value and value != "[]")))


def _boundary_support(hole, reachable_network):
    boundary = hole.boundary
    count = max(1, int(math.ceil(boundary.length / GRID_SPACING_M)))
    points = [boundary.interpolate(i / count, normalized=True) for i in range(count)]
    supported = [point.distance(reachable_network) <= 30.0 for point in points]
    return 100.0 * sum(supported) / len(supported)


def diagnose_holes(raw_polygon, reachable_network, physical_edges, reference_grid,
                   edge_tree, travel_times, water, ngd):
    holes = []
    for part in _polygon_parts(raw_polygon):
        holes.extend(Polygon(ring) for ring in part.interiors)
    holes.sort(key=lambda geometry: geometry.area, reverse=True)
    full_edge_geometries = [edge.geometry for edge in physical_edges]
    rows = []
    for number, hole in enumerate(holes, 1):
        centroid = hole.centroid
        representative = hole.representative_point()
        inside = reference_grid[reference_grid.geometry.apply(hole.covers)]
        accessible_pct = (100.0 * inside.reference_accessible.mean()
                          if len(inside) else np.nan)
        point_result = _reference_point(representative, edge_tree, physical_edges,
                                        travel_times, water)
        nearby_indices = edge_tree.query(hole)
        inside_link_parts = [full_edge_geometries[int(index)].intersection(hole)
                             for index in nearby_indices
                             if full_edge_geometries[int(index)].intersects(hole)]
        full_link_inside = unary_union(inside_link_parts).length if inside_link_parts else 0.0
        reachable_inside = reachable_network.intersection(hole).length
        water_area = hole.intersection(water).area
        water_pct = 100.0 * water_area / hole.area if hole.area else 0.0
        support = _boundary_support(hole, reachable_network)
        buildings = ngd["buildings"]
        building_overlap = (buildings.geometry.intersection(hole).area.sum()
                            if buildings is not None and not buildings.empty else np.nan)
        land_values = _context_values(
            hole, ngd["land"], ["description", "oslandusetiera", "oslandusetierb"])
        site_values = _context_values(
            hole, ngd["sites"], ["description", "oslandusetiera", "oslandusetierb"])

        if water_pct >= 20.0:
            category = "A"
            label = "Water-related"
            fill = False
            reason = "Mapped water occupies at least 20% of the hole."
        elif full_link_inside > 1.0 and reachable_inside <= 0.1:
            category = "D"
            label = "Network-data uncertainty"
            fill = False
            reason = "A reviewed walk-network edge lies inside the hole but is not in the 10-minute reachable geometry."
        elif (len(inside) >= 3 and accessible_pct >= 80.0 and support >= 60.0):
            category = "B"
            label = "Likely buffer artefact"
            fill = True
            reason = "At least 80% of independent 10 m reference points are accessible and reachable links support at least 60% of the boundary."
        elif (len(inside) < 3 and point_result["reference_accessible"]
              and support >= 75.0 and hole.area <= 500.0):
            category = "B"
            label = "Likely buffer artefact"
            fill = True
            reason = "Small ring has too few 10 m samples; its representative point is independently accessible and reachable links support at least 75% of its boundary."
        elif len(inside) >= 3 and accessible_pct <= 75.0:
            category = "C"
            label = "Potentially inaccessible urban land"
            fill = False
            reason = "At most 75% of independent reference points are accessible; retain for conservative review."
        else:
            category = "E"
            label = "Unclassified / manual review"
            fill = False
            reason = "Available water, network and reference-grid evidence does not justify automatic filling."

        rows.append({
            "hole_id": f"H{number:03d}",
            "classification": category,
            "classification_label": label,
            "method_b_action": "fill" if fill else "retain",
            "justification": reason,
            "area_m2": float(hole.area),
            "perimeter_m": float(hole.length),
            "centroid_easting": float(centroid.x),
            "centroid_northing": float(centroid.y),
            "water_intersection_m2": float(water_area),
            "water_overlap_pct": float(water_pct),
            "building_sample_overlap_m2": float(building_overlap) if np.isfinite(building_overlap) else np.nan,
            "land_use_sample": land_values,
            "site_use_sample": site_values,
            "ngd_context_status": "limited discovery sample (10 features per collection); not complete coverage",
            "distance_to_reachable_network_m": float(hole.distance(reachable_network)),
            "centroid_distance_to_reachable_network_m": float(centroid.distance(reachable_network)),
            "reachable_link_inside_m": float(reachable_inside),
            "full_walk_link_inside_m": float(full_link_inside),
            "reachable_boundary_support_pct": float(support),
            "reference_grid_point_count": int(len(inside)),
            "reference_accessible_pct": float(accessible_pct) if np.isfinite(accessible_pct) else np.nan,
            "representative_point_accessible": bool(point_result["reference_accessible"]),
            "representative_point_time_s": (float(point_result["reference_time_s"])
                                              if np.isfinite(point_result["reference_time_s"]) else np.nan),
            "accessible_pedestrian_link_present": bool(reachable_inside > 0.1),
            "geometry": hole,
        })
    return gpd.GeoDataFrame(rows, crs=CRS)


def build_method_b(raw_polygon, holes):
    filled = holes[holes.method_b_action == "fill"]
    additions = filled.geometry.union_all() if not filled.empty else unary_union([])
    controlled = polygonal(make_valid(raw_polygon.union(additions)))
    if controlled.is_empty or not controlled.is_valid:
        raise RuntimeError("Method B produced invalid geometry")
    # Filling an interior ring must not move the exterior boundary.
    raw_shell = unary_union([part.exterior for part in _polygon_parts(raw_polygon)])
    controlled_shell = unary_union([part.exterior for part in _polygon_parts(controlled)])
    shell_difference = raw_shell.hausdorff_distance(controlled_shell)
    if shell_difference > 0.001:
        raise RuntimeError(f"Method B changed the exterior boundary by {shell_difference:.6f} m")
    return controlled, shell_difference


def _clip_triangle_below_level(coordinates, values, level):
    """Clip one linearly interpolated triangle to values <= level."""
    output = []
    for index in range(3):
        start = coordinates[index]
        end = coordinates[(index + 1) % 3]
        start_value = values[index]
        end_value = values[(index + 1) % 3]
        start_inside = start_value <= level
        end_inside = end_value <= level
        if start_inside:
            output.append(tuple(start))
        if start_inside != end_inside:
            fraction = (level - start_value) / (end_value - start_value)
            crossing = start + fraction * (end - start)
            output.append(tuple(crossing))
    if len(output) < 3:
        return None
    polygon = Polygon(output)
    return polygon if polygon.is_valid and polygon.area > 0 else None


def build_method_c(raw_polygon, physical_edges, travel_times, water):
    """Experimental dense ordinary-Delaunay contour, not the paper's CDT."""
    search_extent = raw_polygon.envelope.buffer(300.0)
    samples = {}
    for edge in physical_edges:
        geometry = edge.geometry
        if not geometry.intersects(search_extent):
            continue
        segment_count = max(1, int(math.ceil(geometry.length / NETWORK_SAMPLE_SPACING_M)))
        for distance in np.linspace(0.0, geometry.length, segment_count + 1):
            point = geometry.interpolate(float(distance))
            arrival = _network_arrival(edge, point, travel_times)
            if not np.isfinite(arrival) or arrival > 1200.0:
                continue
            coordinate = (round(point.x, 3), round(point.y, 3))
            samples[coordinate] = min(samples.get(coordinate, np.inf), arrival)
    if len(samples) < 4:
        raise RuntimeError("Too few network samples for Method C")
    coordinates = np.asarray(list(samples), dtype=float)
    values = np.asarray([samples[tuple(coordinate)] for coordinate in coordinates])
    triangulation = Delaunay(coordinates)
    pieces = []
    masked_count = 0
    for simplex in triangulation.simplices:
        triangle_coordinates = coordinates[simplex]
        triangle_values = values[simplex]
        side_lengths = [np.linalg.norm(triangle_coordinates[i] - triangle_coordinates[(i+1) % 3])
                        for i in range(3)]
        if max(side_lengths) > MAX_TRIANGLE_EDGE_M:
            masked_count += 1
            continue
        piece = _clip_triangle_below_level(triangle_coordinates, triangle_values, THRESHOLD_S)
        if piece is not None:
            pieces.append(piece)
    if not pieces:
        raise RuntimeError("Method C generated no contour pieces")
    unmasked = polygonal(make_valid(unary_union(pieces)))
    barrier_aware = polygonal(make_valid(unmasked.difference(water)))
    if barrier_aware.is_empty or not barrier_aware.is_valid:
        raise RuntimeError("Method C produced invalid barrier-aware geometry")
    metadata = {
        "implementation_status": "experimental deviation; not a reproduction of Lin et al. (2020)",
        "network_sample_spacing_m": NETWORK_SAMPLE_SPACING_M,
        "sample_count": len(coordinates),
        "triangle_count": len(triangulation.simplices),
        "triangles_masked_over_max_edge": masked_count,
        "max_triangle_edge_m": MAX_TRIANGLE_EDGE_M,
        "threshold_s": THRESHOLD_S,
        "triangulation": "SciPy ordinary Delaunay",
        "constraint_status": "network edges are not enforced as CDT constraints",
        "barrier_treatment": "selected OSM water subtracted after contour construction",
    }
    sample_frame = gpd.GeoDataFrame({
        "sample_id": np.arange(1, len(coordinates)+1),
        "travel_time_s": values,
        "geometry": [Point(x, y) for x, y in coordinates],
    }, crs=CRS)
    return barrier_aware, unmasked, sample_frame, metadata


def _validation_metrics(reference_grid, polygon):
    predicted = reference_grid.geometry.apply(polygon.covers).to_numpy(dtype=bool)
    reference = reference_grid.reference_accessible.to_numpy(dtype=bool)
    true_positive = int(np.sum(predicted & reference))
    false_positive = int(np.sum(predicted & ~reference))
    false_negative = int(np.sum(~predicted & reference))
    true_negative = int(np.sum(~predicted & ~reference))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else np.nan
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else np.nan
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else np.nan
    return {
        "precision": precision, "recall": recall, "f1_score": f1,
        "true_positive": true_positive, "false_positive": false_positive,
        "false_negative": false_negative, "true_negative": true_negative,
        "validation_status": "model-consistency test against independent 10 m point-access classification; not empirical ground truth",
    }


def _population_metrics(population, polygon, water):
    standard = area_weight(population, polygon)
    land_population = population.copy()
    land_population.geometry = population.geometry.apply(lambda geometry: polygonal(geometry.difference(water)))
    land_polygon = polygonal(polygon.difference(water))
    land_adjusted = area_weight(land_population, land_polygon)
    return {
        "standard_population": float(standard.estimated_population.sum()),
        "standard_density_per_ha": float(standard.estimated_population.sum() / (polygon.area / 10000)),
        "water_adjusted_area_ha": float(land_polygon.area / 10000),
        "water_adjusted_population": float(land_adjusted.estimated_population.sum()),
        "water_adjusted_density_per_ha": float(
            land_adjusted.estimated_population.sum() / (land_polygon.area / 10000)),
    }, standard, land_adjusted


def compare_methods(methods, reachable_network, water, population, reference_grid):
    comparison_rows = []
    validation_rows = []
    population_rows = []
    intersections = {}
    total_network_length = float(reachable_network.length)
    for method_id, details in methods.items():
        geometry = details["geometry"]
        population_metrics, standard_intersections, land_intersections = _population_metrics(
            population, geometry, water)
        intersections[method_id] = (standard_intersections, land_intersections)
        validation = _validation_metrics(reference_grid, geometry)
        network_covered = reachable_network.intersection(geometry.buffer(0.01)).length
        comparison_rows.append({
            "method": method_id,
            "method_name": details["name"],
            "status": details["status"],
            "catchment_area_ha": geometry.area / 10000,
            "estimated_population": population_metrics["standard_population"],
            "population_density_per_ha": population_metrics["standard_density_per_ha"],
            "hole_count": _hole_count(geometry),
            "total_hole_area_ha": _hole_area(geometry) / 10000,
            "water_overlap_ha": geometry.intersection(water).area / 10000,
            "reachable_network_coverage_pct": 100 * network_covered / total_network_length,
            "polygon_component_count": len(_polygon_parts(geometry)),
            "precision": validation["precision"],
            "recall": validation["recall"],
            "f1_score": validation["f1_score"],
        })
        validation_rows.append({"method": method_id, **validation})
        population_rows.extend([
            {"method": method_id, "area_method": "standard_area_weighting",
             "catchment_area_ha": geometry.area/10000,
             "estimated_population": population_metrics["standard_population"],
             "population_density_per_ha": population_metrics["standard_density_per_ha"],
             "population_baseline": "ONS Census 2021"},
            {"method": method_id, "area_method": "water_adjusted_area_weighting",
             "catchment_area_ha": population_metrics["water_adjusted_area_ha"],
             "estimated_population": population_metrics["water_adjusted_population"],
             "population_density_per_ha": population_metrics["water_adjusted_density_per_ha"],
             "population_baseline": "ONS Census 2021"},
        ])
    pairwise_rows = []
    ids = list(methods)
    for start_index, first in enumerate(ids):
        for second in ids[start_index+1:]:
            first_geometry = methods[first]["geometry"]
            second_geometry = methods[second]["geometry"]
            symmetric = first_geometry.symmetric_difference(second_geometry)
            pairwise_rows.append({
                "method_1": first, "method_2": second,
                "symmetric_difference_ha": symmetric.area/10000,
                "intersection_ha": first_geometry.intersection(second_geometry).area/10000,
                "method_1_only_ha": first_geometry.difference(second_geometry).area/10000,
                "method_2_only_ha": second_geometry.difference(first_geometry).area/10000,
            })
    return (pd.DataFrame(comparison_rows), pd.DataFrame(validation_rows),
            pd.DataFrame(population_rows), pd.DataFrame(pairwise_rows), intersections)


def _tag_text(values):
    cleaned = []
    for value in values:
        if value is None or (not isinstance(value, (list, tuple, set, str)) and pd.isna(value)):
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        cleaned.extend(str(item) for item in items if str(item).strip())
    return " | ".join(sorted(set(cleaned)))


def barrier_review(physical_edges, reachable_network, water_frame, comparison_extent):
    """Record reachable grade-separated edges from graph topology and OSM tags."""
    water_union = water_frame.geometry.union_all()
    reachable_zone = reachable_network.buffer(.05)
    rows = []
    for edge_id, edge in enumerate(physical_edges, 1):
        data = [member[4] for member in edge.members]
        bridge = _tag_text([item.get("bridge") for item in data])
        tunnel = _tag_text([item.get("tunnel") for item in data])
        layer = _tag_text([item.get("layer") for item in data])
        if not any((bridge, tunnel, layer)):
            continue
        reachable_length = edge.geometry.intersection(reachable_zone).length
        if reachable_length <= .01 or not edge.geometry.intersects(comparison_extent):
            continue
        water_length = edge.geometry.intersection(water_union).length
        rows.append({
            "crossing_id": f"X{len(rows)+1:03d}",
            "physical_edge_id": edge_id,
            "osmid": _tag_text([item.get("osmid") for item in data]),
            "highway": _tag_text([item.get("highway") for item in data]),
            "foot": _tag_text([item.get("foot") for item in data]),
            "access": _tag_text([item.get("access") for item in data]),
            "bridge": bridge, "tunnel": tunnel, "layer": layer,
            "reachable_length_m": float(reachable_length),
            "length_over_mapped_water_m": float(water_length),
            "topology_basis": "edge is in reviewed routed graph; connectivity is not inferred from 2D intersection",
            "treatment": "retain reachable land on both sides; water subtraction removes water surface only",
            "geometry": edge.geometry,
        })
    columns = ["crossing_id", "physical_edge_id", "osmid", "highway", "foot", "access",
               "bridge", "tunnel", "layer", "reachable_length_m",
               "length_over_mapped_water_m", "topology_basis", "treatment", "geometry"]
    crossings = gpd.GeoDataFrame(rows, columns=columns, crs=CRS)
    local_water = water_frame[water_frame.intersects(comparison_extent)].copy()
    keep = [column for column in ["element_type", "osmid", "name", "natural", "water",
                                   "waterway", "geometry"] if column in local_water]
    local_water = local_water[keep]
    return crossings, local_water


def _map_context():
    edges = gpd.read_file(OPENSTREETMAP / "pedestrian_network_reviewed.gpkg",
                          layer="edges").to_crs(CRS)
    highway = edges.get("highway", pd.Series("", index=edges.index)).fillna("").astype(str)
    major = edges[highway.str.contains("trunk|primary|secondary|tertiary", case=False, regex=True)]
    site = gpd.read_file(RAW / "site_boundary.geojson").to_crs(CRS)
    return edges, major, site


def _map_base(ax, bounds, basemap, water, buildings, major_roads):
    if basemap:
        ax.imshow(basemap["image"], extent=basemap["extent"], zorder=0)
    else:
        ax.set_facecolor("#f8f8f4")
    if major_roads is not None and not major_roads.empty:
        major_roads.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]].plot(
            ax=ax, color="#8e9799", linewidth=.7, alpha=.65, zorder=1)
    if buildings is not None and not buildings.empty:
        buildings.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]].plot(
            ax=ax, facecolor="#d5d3ca", edgecolor="#aaa89f", linewidth=.25,
            alpha=.65, zorder=2)
    water_frame = gpd.GeoDataFrame(geometry=[water], crs=CRS)
    water_frame.plot(ax=ax, facecolor="#d5e8ef", edgecolor="#7fa7b5",
                     linewidth=.3, alpha=.65, zorder=3)
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#aeb8ba")


def make_hole_map(raw_polygon, holes, reachable_network, water, ngd):
    xmin, ymin, xmax, ymax = raw_polygon.bounds
    bounds = (xmin-90, ymin-90, xmax+90, ymax+90)
    basemap = os_maps_basemap(bounds)
    _, major, site = _map_context()
    fig = plt.figure(figsize=(420/25.4, 297/25.4), facecolor="white")
    ax = fig.add_axes([.045, .10, .66, .77])
    panel = fig.add_axes([.74, .10, .23, .80]); panel.axis("off")
    _map_base(ax, bounds, basemap, water, ngd["buildings"], major)
    gpd.GeoSeries([raw_polygon], crs=CRS).boundary.plot(
        ax=ax, color="#176b72", linewidth=1.1, zorder=5)
    gpd.GeoSeries([reachable_network], crs=CRS).plot(
        ax=ax, color="#313f45", linewidth=.55, zorder=6)
    colours = {"A":"#5ba7c2", "B":"#e59e45", "C":"#b95950",
               "D":"#7d64a7", "E":"#8c9292"}
    for category, frame in holes.groupby("classification"):
        frame.plot(ax=ax, facecolor=colours[category], edgecolor="white",
                   linewidth=.7, alpha=.82, zorder=7)
    site.plot(ax=ax, facecolor="#bd542a", edgecolor="white", linewidth=.7, zorder=8)
    for row in holes.itertuples():
        point = row.geometry.representative_point()
        ax.text(point.x, point.y, row.hole_id[1:], ha="center", va="center",
                fontsize=5.5, color="#172a32", zorder=9,
                bbox={"facecolor":"white", "edgecolor":"none", "alpha":.72, "pad":.4})
    fig.text(.045, .955, "CROSSHARBOUR  /  HOLE DIAGNOSIS", fontsize=10,
             color="#176b72", weight="bold")
    fig.text(.045, .920, "10-Minute Catchment: Interior-Ring Diagnosis",
             fontsize=21, color="#253842", weight="bold")
    fig.text(.045, .892,
             "Existing 25 m network buffer retained | EPSG:27700 | IDs correspond to the exported inventory",
             fontsize=9.5, color="#586970")
    panel.text(0, .98, "CLASSIFICATION", va="top", fontsize=11,
               color="#176b72", weight="bold")
    y = .92
    labels = {
        "A":"Water-related", "B":"Likely buffer artefact",
        "C":"Potentially inaccessible urban land",
        "D":"Network-data uncertainty", "E":"Manual review",
    }
    counts = holes.classification.value_counts()
    for category in "ABCDE":
        panel.add_patch(plt.Rectangle((0, y-.018), .045, .025,
                        facecolor=colours[category], transform=panel.transAxes))
        panel.text(.065, y, f"{category}  {labels[category]} ({counts.get(category, 0)})",
                   va="top", fontsize=9, color="#253842")
        y -= .058
    filled = holes[holes.method_b_action == "fill"]
    retained = holes[holes.method_b_action == "retain"]
    panel.text(0, y-.02, "METHOD B DECISION", va="top", fontsize=11,
               color="#176b72", weight="bold")
    y -= .08
    statements = [
        f"{len(holes)} interior rings; {holes.area.sum()/10000:.2f} ha total.",
        f"Fill {len(filled)} evidence-supported artefacts ({filled.area.sum()/10000:.2f} ha).",
        f"Retain {len(retained)} uncertain or potentially inaccessible rings ({retained.area.sum()/10000:.2f} ha).",
        "Evidence combines water, reviewed/full OSM network, boundary support and an independent 10 m point-access model.",
        "OS NGD building/land layers shown here are discovery samples only and do not provide complete coverage.",
    ]
    for statement in statements:
        panel.text(0, y, statement, va="top", fontsize=9.2, color="#253842",
                   wrap=True, linespacing=1.35)
        y -= .105 if len(statement) > 70 else .07
    source = (f"OS Maps API {basemap['layer']}; {basemap['attribution']}"
              if basemap else "Local vector context; © OpenStreetMap contributors")
    fig.text(.045, .045,
             f"{source}  |  OSM water and pedestrian network  |  OS NGD samples © Crown copyright and database rights 2026",
             fontsize=7.5, color="#586970")
    fig.savefig(OUT_HOLE_MAP, dpi=220, facecolor="white")
    fig.savefig(OUT_HOLE_MAP_PDF, facecolor="white", metadata={
        "Title":"Crossharbour catchment hole diagnosis",
        "Subject":"10-minute 25 m network-buffer interior-ring review; EPSG:27700"})
    plt.close(fig)


def make_comparison_map(methods, comparison, reachable_network, water, ngd):
    all_geometry = unary_union([details["geometry"] for details in methods.values()])
    xmin, ymin, xmax, ymax = all_geometry.bounds
    bounds = (xmin-100, ymin-100, xmax+100, ymax+100)
    basemap = os_maps_basemap(bounds)
    _, major, site = _map_context()
    fig, axes = plt.subplots(1, 3, figsize=(420/25.4, 297/25.4),
                             facecolor="white", sharex=True, sharey=True)
    colours = {"A":"#176b72", "B":"#ca7b32", "C":"#746398"}
    titles = {
        "A":"A  Existing 25 m buffer",
        "B":"B  Controlled hole treatment",
        "C":"C  Experimental contour proxy",
    }
    for ax, method_id in zip(axes, ("A", "B", "C")):
        _map_base(ax, bounds, basemap, water, ngd["buildings"], major)
        geometry = methods[method_id]["geometry"]
        gpd.GeoSeries([geometry], crs=CRS).plot(
            ax=ax, facecolor=colours[method_id], edgecolor=colours[method_id],
            linewidth=1.1, alpha=.20, zorder=4)
        gpd.GeoSeries([reachable_network], crs=CRS).plot(
            ax=ax, color="#333c3f", linewidth=.42, zorder=5)
        site.plot(ax=ax, facecolor="#bd542a", edgecolor="white", linewidth=.7, zorder=6)
        row = comparison[comparison.method == method_id].iloc[0]
        ax.set_title(
            f"{titles[method_id]}\n{row.catchment_area_ha:.1f} ha  |  "
            f"~{row.estimated_population:,.0f} residents  |  {int(row.hole_count)} holes\n"
            f"Grid-model F1 {row.f1_score:.3f}", fontsize=10.5, color="#253842")
    fig.suptitle("Crossharbour 10-Minute Catchment: Polygonisation Methods",
                 x=.045, y=.965, ha="left", fontsize=20, color="#253842", weight="bold")
    fig.text(.045, .922,
             "Same routed network, origins, 4.8 km/h speed and 600 s threshold. Method C uses ordinary Delaunay and is not a reproduction of Lin et al. (2020).",
             fontsize=8.7, color="#586970")
    handles = [Patch(facecolor="#176b72", alpha=.20, label="Catchment polygon"),
               Line2D([], [], color="#333c3f", lw=1, label="Reachable pedestrian network"),
               Patch(facecolor="#d5e8ef", label="OSM water"),
               Patch(facecolor="#bd542a", label="Development site"),
               Patch(facecolor="#d5d3ca", edgecolor="#aaa89f", label="Limited NGD building sample")]
    fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(.045, .035),
               ncol=5, fontsize=8.5, frameon=False)
    source = (f"OS Maps API {basemap['layer']}; {basemap['attribution']}"
              if basemap else "Local vector context; © OpenStreetMap contributors")
    fig.text(.045, .018, f"{source}  |  OSM network/water  |  EPSG:27700",
             fontsize=7.5, color="#586970")
    fig.subplots_adjust(left=.035, right=.99, bottom=.085, top=.88, wspace=.025)
    fig.savefig(OUT_COMPARE_MAP, dpi=220, facecolor="white")
    fig.savefig(OUT_COMPARE_MAP_PDF, facecolor="white", metadata={
        "Title":"Crossharbour catchment polygonisation comparison",
        "Subject":"Methods A/B/C comparison; Figure 01 preserved"})
    plt.close(fig)


def write_report(comparison, validation, population_table, pairwise, holes,
                 method_c_metadata, method_b_shell_difference, crossings, water_features):
    def method_row(method):
        return comparison[comparison.method == method].iloc[0]

    a, b, c = (method_row(method) for method in ("A", "B", "C"))
    counts = holes.classification.value_counts()
    pair_lines = "\n".join(
        f"- {row.method_1} versus {row.method_2}: {row.symmetric_difference_ha:.2f} ha symmetric difference "
        f"({row.method_1_only_ha:.2f} ha only in {row.method_1}; {row.method_2_only_ha:.2f} ha only in {row.method_2})."
        for row in pairwise.itertuples())
    table_lines = [
        "| Method | Area (ha) | Population | Density (/ha) | Holes | Hole area (ha) | Water overlap (ha) | Components | Network coverage | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison.itertuples():
        table_lines.append(
            f"| {row.method} — {row.method_name} | {row.catchment_area_ha:.2f} | {row.estimated_population:,.0f} | "
            f"{row.population_density_per_ha:.1f} | {int(row.hole_count)} | {row.total_hole_area_ha:.2f} | "
            f"{row.water_overlap_ha:.2f} | {int(row.polygon_component_count)} | {row.reachable_network_coverage_pct:.2f}% | "
            f"{row.precision:.3f} | {row.recall:.3f} | {row.f1_score:.3f} |")
    pop_lines = [
        "| Method | Weighting | Area (ha) | Estimated population | Density (/ha) |",
        "|---|---|---:|---:|---:|",
    ]
    for row in population_table.itertuples():
        pop_lines.append(
            f"| {row.method} | {row.area_method} | {row.catchment_area_ha:.2f} | "
            f"{row.estimated_population:,.0f} | {row.population_density_per_ha:.1f} |")
    text = f"""# Crossharbour 10-minute catchment polygonisation review

## Scope and preserved analysis

This review leaves the existing OSMnx graph, reviewed origins A/B, multi-source Dijkstra travel times, 4.8 km/h speed, partial-edge handling, ONS Census 2021 data, DfE schools, OSM water and Figure 01 unchanged. All metric geometry operations use EPSG:27700. New outputs are comparison and QA artefacts.

## Reference method

[Lin et al. (2020)](https://doi.org/10.3390/ijgi9080467) construct a local subgraph, extend the shortest-path tree with break points on non-tree edges, segment every network edge into constrained triangulation edges, build a CGAL constrained Delaunay triangulation and trace the cut-off contour. Directed networks require direction-specific extended-tree handling; multiple entrances can be linked to a zero-cost virtual root. Section 2.4 evaluates polygons against grid points classified through a network plus off-network distance model.

The [official reference implementation](https://gitlab.com/Drsulmp/tcageneration) was inspected at commit `{REFERENCE_COMMIT}`. It is a C++/Visual Studio project documented against GDAL 2.3.2, Boost 1.69, CGAL 4.13 and optional Qt 5.13.2. CGAL is unavailable in this project's Python environment, and the source is not a drop-in adapter for an OSMnx directed MultiDiGraph with time weights. Rebuilding that stack for this take-home would add material dependency and verification risk.

## Hole diagnosis

The existing polygon has **{len(holes)}** interior rings covering **{holes.area.sum()/10000:.2f} ha**. The classifications are: A water-related {counts.get('A', 0)}, B likely buffer artefact {counts.get('B', 0)}, C potentially inaccessible land {counts.get('C', 0)}, D network uncertainty {counts.get('D', 0)}, and E manual review {counts.get('E', 0)}.

Each ring is assessed with its geometry, mapped-water overlap, minimum and centroid distance to the reachable network, reviewed/full-network links within it, reachable-boundary support, and a separate 10 m point-access classification. OS NGD Building, Land and Site attributes are included only where the existing ten-feature discovery samples intersect. Those samples are incomplete, so their absence is never treated as proof that a building or land use is absent.

Method B fills **{int((holes.method_b_action == 'fill').sum())}** rings covering **{holes.loc[holes.method_b_action == 'fill'].area.sum()/10000:.2f} ha** and retains **{int((holes.method_b_action == 'retain').sum())}** rings. Its exterior boundary Hausdorff difference from Method A is **{method_b_shell_difference:.6f} m**.

## Methods

- **A — raw 25 m network buffer.** The existing unmodified baseline.
- **B — controlled hole treatment.** Fills only Category B rings. A ring needs water/network evidence plus either at least three 10 m reference points with at least 80% accessibility and 60% reachable-boundary support, or a separately accessible representative point, at least 75% boundary support and area no more than 500 m² when the grid is too coarse. Water, uncertain-network and weakly accessible rings are retained. The exterior boundary is preserved.
- **C — experimental research-inspired contour proxy.** Samples every physical directed network edge at 15 m, evaluates the minimum A/B arrival time at each sample, constructs a SciPy ordinary Delaunay triangulation, masks triangles with sides over 150 m, clips triangles at 600 seconds and subtracts mapped water. It includes all relevant edges and intermediate points, but it does **not** insert the paper's exact extended-tree break nodes and does **not** enforce network segments as constrained Delaunay edges. It is therefore an experimental comparator, not a reproduction of Lin et al.

Method C metadata: {json.dumps(method_c_metadata, sort_keys=True)}

## Comparison

{chr(10).join(table_lines)}

{pair_lines}

Method A's holes are mainly ordinary spaces between 25 m edge buffers. Method B fills the rings supported by the independent point-access model while retaining ambiguous land. Method C changes both internal and external geometry because it interpolates a travel-time surface rather than editing buffer rings; water subtraction also splits narrow water crossings while preserving reachable land on both sides of valid bridges.

## Barrier and crossing review

The comparison extent intersects **{len(water_features)}** selected OSM water features. The reviewed routed graph contains **{len(crossings)}** reachable physical edges in this extent with a non-empty `bridge`, `tunnel` or `layer` tag; **{int((crossings.length_over_mapped_water_m > 0).sum()) if len(crossings) else 0}** overlap mapped water geometry. These records are exported for review. Their connectivity comes from graph edges and tags, never from apparent 2D line intersection. Water subtraction removes mapped water surfaces but retains reachable land polygons on each side of graph-connected bridge crossings.

## Accessibility validation

The reference labels come from a regular 10 m grid independent of all candidate polygons. For each point, the model uses the nearest reviewed physical network edge, the minimum directed multi-source arrival time at the projected location, Euclidean off-network time at 4.8 km/h, and a water/connector barrier check. Candidate polygons are used only as predictions. Precision, recall and F1 are therefore available as **model-consistency metrics**, not empirical ground truth. Building interiors, gates, private parcels and vertical circulation remain incompletely modelled.

Method C achieves the highest model-consistency F1 ({c.f1_score:.3f}), but the unavailable constrained triangulation means this score does not validate the paper's algorithm. Method B improves F1 from {a.f1_score:.3f} to {b.f1_score:.3f} without moving the baseline exterior boundary.

## Population sensitivity

{chr(10).join(pop_lines)}

Standard estimates use each full original LSOA area as the denominator. The separate water-adjusted scenario subtracts mapped water from both source-zone denominators and catchment intersections. Both remain area-weighted estimates using the ONS Census 2021 baseline.

## Recommendation

Use **Method B — controlled hole treatment** as the recommended planning/workshop sensitivity after review. It directly explains which baseline rings changed and why, preserves the established external reach, is reproducible in the existing Python stack, and materially improves consistency with the independent point-access model. Keep Method A beside it as the conservative audit baseline. Keep Method C as an experimental research comparator because its higher F1 is promising but its ordinary Delaunay surface cannot provide the geometric guarantees of the paper's CGAL constrained triangulation.

Do not overwrite Figure 01 yet. Review the hole inventory and Figure 04 first. If a later production implementation installs CGAL and ports the directed, multi-origin extended-tree workflow with tests against the reference program, Method C can be reassessed.

## Remaining limitations

- The 10 m reference grid is a model-based benchmark rather than observed pedestrian traces or parcel-entry evidence.
- Straight off-network movement can still simplify buildings, fences, private land and vertical access; water crossings are explicitly checked.
- OS NGD discovery samples are not complete building or land-use coverage.
- OSM topology, access tags and the three manually approved local connections remain inherited inputs.
- Area-weighted population assumes residents are uniformly distributed within each source zone; the water-adjusted version only changes the assumed allocatable area.
"""
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(text, encoding="utf-8")


def run_polygonisation_review():
    for folder in (GIS, TABLES, MAPS, METHOD):
        folder.mkdir(parents=True, exist_ok=True)
    raw_path = GIS / "walking_catchment_10min.gpkg"
    raw_polygon = gpd.read_file(raw_path, layer="catchment_raw").to_crs(CRS).geometry.union_all()
    reachable_network = gpd.read_file(GIS / "reachable_network_10min.gpkg").to_crs(CRS).geometry.union_all()
    water_path = selected_water_path()
    water_frame = gpd.read_file(water_path).to_crs(CRS)
    water = water_frame.geometry.union_all()
    population = gpd.read_file(CLEANED / "lsoa_2021_population_cleaned.gpkg").to_crs(CRS)
    graph = load_pedestrian_graph(
        OPENSTREETMAP / "pedestrian_network_reviewed_with_origins.graphml")
    origins = gpd.read_file(PROCESSED / "site_origins.gpkg").to_crs(CRS)
    source_nodes = origins.site_access_node.astype(int).tolist()
    travel_times = nx.multi_source_dijkstra_path_length(
        graph, source_nodes, weight="travel_time_s")
    physical_edges = _physical_edges(graph)
    reference_grid, edge_tree = build_reference_grid(
        raw_polygon, physical_edges, travel_times, water)
    ngd = _load_ngd_samples()
    holes = diagnose_holes(raw_polygon, reachable_network, physical_edges,
                           reference_grid, edge_tree, travel_times, water, ngd)
    method_b, shell_difference = build_method_b(raw_polygon, holes)
    method_c, method_c_unmasked, method_c_samples, method_c_metadata = build_method_c(
        raw_polygon, physical_edges, travel_times, water)
    methods = {
        "A": {"name":"Raw 25 m network buffer", "status":"existing baseline, unchanged",
              "geometry":raw_polygon},
        "B": {"name":"Controlled hole treatment", "status":"recommended review candidate",
              "geometry":method_b},
        "C": {"name":"Research-inspired contour proxy", "status":"experimental; ordinary Delaunay deviation",
              "geometry":method_c},
    }
    comparison, validation, population_table, pairwise, intersections = compare_methods(
        methods, reachable_network, water, population, reference_grid)
    comparison_extent = unary_union([details["geometry"] for details in methods.values()]).envelope
    crossings, water_features = barrier_review(
        physical_edges, reachable_network, water_frame, comparison_extent)

    method_rows = []
    for method_id, details in methods.items():
        row = comparison[comparison.method == method_id].iloc[0]
        method_rows.append({"method":method_id, "method_name":details["name"],
                            "status":details["status"], "area_ha":row.catchment_area_ha,
                            "geometry":details["geometry"]})
    method_frame = gpd.GeoDataFrame(method_rows, crs=CRS)
    _write_layer(method_frame, OUT_METHODS, "methods_a_b_c", first=True)
    _write_layer(gpd.GeoDataFrame([{
        "method":"C_unmasked", "status":"diagnostic before OSM-water subtraction",
        "area_ha":method_c_unmasked.area/10000, "geometry":method_c_unmasked}], crs=CRS),
        OUT_METHODS, "method_c_unmasked_diagnostic")
    # Store the validation grid and contour samples for full reproducibility.
    _write_layer(reference_grid, OUT_METHODS, "validation_grid_10m")
    _write_layer(method_c_samples, OUT_METHODS, "method_c_network_samples")
    if not crossings.empty:
        _write_layer(crossings, OUT_METHODS, "reachable_barrier_crossings")
    if not water_features.empty:
        _write_layer(water_features, OUT_METHODS, "local_water_features")

    _write_layer(holes, OUT_HOLES, "all_holes", first=True)
    _write_layer(holes[holes.method_b_action == "fill"], OUT_HOLES, "method_b_filled")
    _write_layer(holes[holes.method_b_action == "retain"], OUT_HOLES, "method_b_retained")
    holes.drop(columns="geometry").to_csv(OUT_HOLE_CSV, index=False)
    comparison.to_csv(OUT_COMPARE, index=False)
    validation.to_csv(OUT_VALIDATION, index=False)
    population_table.to_csv(OUT_POPULATION, index=False)
    pairwise.to_csv(OUT_PAIRWISE, index=False)
    crossings.drop(columns="geometry").to_csv(OUT_BARRIERS, index=False)
    water_features.drop(columns="geometry").to_csv(OUT_WATER_FEATURES, index=False)
    for method_id, (standard, land) in intersections.items():
        _write_layer(standard, OUT_METHODS, f"population_{method_id.lower()}_standard")
        _write_layer(land, OUT_METHODS, f"population_{method_id.lower()}_water_adjusted")

    make_hole_map(raw_polygon, holes, reachable_network, water, ngd)
    make_comparison_map(methods, comparison, reachable_network, water, ngd)
    write_report(comparison, validation, population_table, pairwise, holes,
                 method_c_metadata, shell_difference, crossings, water_features)
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_crs": CRS,
        "existing_figure_01_modified": False,
        "reference_article": "https://doi.org/10.3390/ijgi9080467",
        "reference_implementation": "https://gitlab.com/Drsulmp/tcageneration",
        "reference_commit_inspected": REFERENCE_COMMIT,
        "water_source": str(water_path.relative_to(ROOT)),
        "parameters": {"walking_speed_kmh":SPEED_KMH, "threshold_s":THRESHOLD_S,
            "grid_spacing_m":GRID_SPACING_M,
            "network_sample_spacing_m":NETWORK_SAMPLE_SPACING_M,
            "max_triangle_edge_m":MAX_TRIANGLE_EDGE_M},
        "method_c": method_c_metadata,
        "method_b_exterior_hausdorff_difference_m": shell_difference,
        "reachable_tagged_barrier_crossing_count": len(crossings),
        "local_water_feature_count": len(water_features),
    }
    (METHOD / "catchment_polygonisation_metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8")
    print(comparison.to_string(index=False))
    print(f"Wrote {OUT_METHODS}, {OUT_HOLES}, {OUT_COMPARE_MAP_PDF} and {OUT_REPORT}")
    return comparison
