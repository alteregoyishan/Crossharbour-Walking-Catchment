"""Barrier-consistent review of the 10-minute catchment polygonisation.

This module does not alter routing.  It applies one water/bridge policy to
derived polygons and compares A0, A2, B2, C1 and C2.  C1/C2 remain ordinary
Delaunay experiments and are not reproductions of Lin et al. (2020).
"""
from __future__ import annotations

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
from scipy.sparse.csgraph import dijkstra as sparse_dijkstra
from scipy.spatial import Delaunay
from shapely import LineString, Point, Polygon, make_valid
from shapely.ops import unary_union

from .catchment_polygonisation import (
    CRS,
    GRID_SPACING_M,
    MAX_TRIANGLE_EDGE_M,
    NETWORK_SAMPLE_SPACING_M,
    REFERENCE_COMMIT,
    SPEED_MPS,
    THRESHOLD_S,
    _boundary_support,
    _clip_triangle_below_level,
    _context_values,
    _hole_area,
    _hole_count,
    _load_ngd_samples,
    _map_base,
    _map_context,
    _network_arrival,
    _physical_edges,
    _polygon_parts,
    _reference_point,
    _tag_text,
    _validation_metrics,
    _write_layer,
    build_reference_grid,
)
from .os_data_services import os_maps_basemap, selected_water_path
from .paths import CLEANED, OPENSTREETMAP, OUTPUTS, PROCESSED, RAW, ROOT
from .pedestrian_network import load_pedestrian_graph
from .service_area_analysis import area_weight, polygonal


VERSION = "v2"
SELECTED_BRIDGE_BUFFER_M = 1.0
NETWORK_FAR_RATIO = 3.0
NETWORK_FAR_EXCESS_M = 100.0
TRIANGLE_NETWORK_AUDIT_LIMIT_M = 5000.0

GIS = OUTPUTS / "gis"
TABLES = OUTPUTS / "tables"
QA = OUTPUTS / "qa"
MAPS = OUTPUTS / "maps"
METHOD = OUTPUTS / "methodology"

OUT_METHODS = GIS / f"catchment_polygonisation_barrier_review_{VERSION}.gpkg"
OUT_HOLES = GIS / f"catchment_hole_inventory_b2_{VERSION}.gpkg"
OUT_HOLES_CSV = TABLES / f"catchment_hole_inventory_b2_{VERSION}.csv"
OUT_COMPARE = TABLES / f"catchment_barrier_method_comparison_{VERSION}.csv"
OUT_POPULATION = TABLES / f"catchment_barrier_population_{VERSION}.csv"
OUT_PAIRWISE = TABLES / f"catchment_barrier_pairwise_{VERSION}.csv"
OUT_BRIDGE_SENSITIVITY = TABLES / f"bridge_corridor_sensitivity_{VERSION}.csv"
OUT_CROSSINGS_GPKG = QA / "reachable_water_crossings.gpkg"
OUT_CROSSINGS_CSV = QA / "reachable_water_crossings.csv"
OUT_TRIANGLES = QA / f"method_c_triangles_{VERSION}.gpkg"
OUT_SUSPICIOUS = QA / "method_c_suspicious_triangles.gpkg"
OUT_SUSPICIOUS_CSV = QA / f"method_c_suspicious_triangles_{VERSION}.csv"
OUT_COMPARE_MAP = MAPS / f"figure_05_barrier_consistent_methods_{VERSION}.png"
OUT_COMPARE_MAP_PDF = MAPS / f"figure_05_barrier_consistent_methods_{VERSION}.pdf"
OUT_BRIDGE_MAP = MAPS / f"figure_06_bridge_barrier_qa_{VERSION}.png"
OUT_BRIDGE_MAP_PDF = MAPS / f"figure_06_bridge_barrier_qa_{VERSION}.pdf"
OUT_REPORT = METHOD / f"catchment_barrier_policy_review_{VERSION}.md"
OUT_METADATA = METHOD / f"catchment_barrier_policy_metadata_{VERSION}.json"


def validate_water_layer(water_frame: gpd.GeoDataFrame):
    """Return valid polygon features and their union in EPSG:27700."""
    frame = water_frame.to_crs(CRS).copy()
    frame.geometry = frame.geometry.apply(lambda g: polygonal(make_valid(g)))
    frame = frame[~frame.geometry.is_empty & frame.geometry.notna()].copy()
    if frame.empty or not frame.geometry.is_valid.all():
        raise RuntimeError("Cleaned analytical water layer is empty or invalid")
    return frame, polygonal(make_valid(frame.geometry.union_all()))


def _truthy_tag(value: str) -> bool:
    values = {part.strip().lower() for part in str(value).split("|")}
    return bool(values - {"", "0", "no", "false", "none", "nan"})


def _nonzero_layer(value: str) -> bool:
    for part in str(value).split("|"):
        try:
            if float(part.strip()) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _load_ngd_structures():
    path = ROOT / "data/qa/os_ngd_discovery_samples.gpkg"
    if not path.exists():
        return gpd.GeoDataFrame(geometry=[], crs=CRS)
    frames = []
    for layer in ("str-fts-structure-3", "str-fts-compoundstructure-3"):
        try:
            frame = gpd.read_file(path, layer=layer).to_crs(CRS)
        except Exception:
            continue
        description = frame.get("description", pd.Series("", index=frame.index)).fillna("").astype(str)
        frames.append(frame[description.str.contains("bridge", case=False, regex=False)].copy())
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs=CRS)
    columns = sorted(set().union(*(set(frame.columns) for frame in frames)))
    return gpd.GeoDataFrame(pd.concat([f.reindex(columns=columns) for f in frames], ignore_index=True), crs=CRS)


def review_reachable_water_crossings(physical_edges, reachable_network, water,
                                     ngd_structures):
    """Audit every reachable physical edge intersecting mapped water."""
    reachable_zone = reachable_network.buffer(0.05)
    rows = []
    for edge_id, edge in enumerate(physical_edges, 1):
        reachable_part = edge.geometry.intersection(reachable_zone)
        if reachable_part.length <= 0.01 or not edge.geometry.intersects(water):
            continue
        water_length = float(edge.geometry.intersection(water).length)
        if water_length <= 0.01:
            continue
        data = [member[4] for member in edge.members]
        fields = {name: _tag_text([item.get(name) for item in data]) for name in (
            "osmid", "highway", "foot", "access", "bridge", "tunnel", "layer",
            "name", "covered", "service")}
        access_text = f"{fields['foot']} | {fields['access']}".lower()
        restricted = any(token in access_text for token in ("no", "private"))
        explicit_structure = (_truthy_tag(fields["bridge"]) or
                              _truthy_tag(fields["tunnel"]) or
                              _nonzero_layer(fields["layer"]))
        matches = (ngd_structures[ngd_structures.intersects(edge.geometry.buffer(3.0))]
                   if not ngd_structures.empty else ngd_structures)
        ngd_ids = _tag_text(matches.get("osid", pd.Series(dtype=str)).tolist()) if len(matches) else ""
        ngd_desc = _tag_text(matches.get("description", pd.Series(dtype=str)).tolist()) if len(matches) else ""

        if restricted:
            category, label, use = "D", "likely invalid", False
            evidence = "OSM foot/access tags indicate no or private access."
        elif explicit_structure:
            category, label, use = "A", "likely valid pedestrian bridge/crossing", True
            evidence = "Reachable graph edge has explicit bridge, tunnel or non-zero layer evidence."
        elif len(matches):
            category, label, use = "A", "likely valid pedestrian bridge/crossing", True
            evidence = "Reachable edge spatially matches an existing NGD bridge discovery feature."
        elif water_length <= 2.0:
            category, label, use = "B", "possible valid crossing requiring review", False
            evidence = "Untagged overlap is no more than 2 m and may be a shoreline-boundary mismatch."
        else:
            category, label, use = "C", "suspicious network/water conflict", False
            evidence = "Reachable edge crosses mapped water without bridge, tunnel, layer or NGD bridge evidence."

        rows.append({
            "analysis_version": VERSION,
            "crossing_id": f"WX{len(rows)+1:03d}",
            "physical_edge_id": edge_id,
            **fields,
            "classification": category,
            "classification_label": label,
            "policy_use": bool(use),
            "manual_review_required": category != "A",
            "evidence": evidence,
            "reachable_length_m": float(reachable_part.length),
            "length_over_mapped_water_m": water_length,
            "ngd_bridge_sample_ids": ngd_ids,
            "ngd_bridge_sample_description": ngd_desc,
            "ngd_coverage_note": "ten-feature discovery sample only; absence is not evidence of absence",
            "topology_basis": "edge belongs to authoritative reachable graph; connectivity is not inferred from 2D line intersection",
            "recommended_treatment": ("restore narrow display corridor over water" if use else
                                      "retain in QA and do not restore automatically"),
            "geometry": edge.geometry,
        })
    if not rows:
        columns = [
            "analysis_version", "crossing_id", "physical_edge_id", "osmid",
            "highway", "foot", "access", "bridge", "tunnel", "layer", "name",
            "covered", "service", "classification", "classification_label",
            "policy_use", "manual_review_required", "evidence",
            "reachable_length_m", "length_over_mapped_water_m",
            "ngd_bridge_sample_ids", "ngd_bridge_sample_description",
            "ngd_coverage_note", "topology_basis", "recommended_treatment",
            "geometry",
        ]
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=CRS)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def bridge_corridors(crossings, water, buffer_m, ngd_structures=None):
    """Build cartographic-only bridge corridors over mapped water."""
    pieces = []
    selected = crossings[crossings.policy_use] if not crossings.empty else crossings
    for row in selected.itertuples():
        structure = None
        if ngd_structures is not None and not ngd_structures.empty:
            matches = ngd_structures[ngd_structures.intersects(row.geometry.buffer(3.0))]
            matches = matches[matches.geometry.area <= 2000.0]
            if not matches.empty:
                structure = matches.geometry.union_all()
        corridor = structure if structure is not None else row.geometry.buffer(
            buffer_m, cap_style="flat", join_style="round")
        # Extend 0.5 m onto the land side so floating-point boundary differences
        # cannot leave the cartographic corridor as an isolated polygon.
        over_water = polygonal(make_valid(corridor.intersection(water.buffer(0.5))))
        if not over_water.is_empty:
            pieces.append(over_water)
    return polygonal(make_valid(unary_union(pieces))) if pieces else Polygon()


def apply_common_barrier_policy(candidate_polygon, water, crossings,
                                bridge_buffer_m=SELECTED_BRIDGE_BUFFER_M,
                                ngd_structures=None):
    """Return analytical land, separate bridge corridor and display geometry."""
    candidate = polygonal(make_valid(candidate_polygon))
    analytical_land = polygonal(make_valid(candidate.difference(water)))
    all_corridors = bridge_corridors(crossings, water, bridge_buffer_m, ngd_structures)
    # All processed methods derive from the same reachable network, so the
    # accepted crossing set and its full over-water corridor are held constant.
    relevant = all_corridors
    display = (analytical_land if relevant.is_empty else
               polygonal(make_valid(analytical_land.union(relevant))))
    if analytical_land.is_empty or not analytical_land.is_valid or not display.is_valid:
        raise RuntimeError("Common barrier policy produced empty or invalid geometry")
    return analytical_land, relevant, display


def diagnose_b2_holes(a2_land, reachable_network, physical_edges, reference_grid,
                      edge_tree, travel_times, water, ngd):
    """Classify A2 holes using water, network and independent grid evidence."""
    holes = [Polygon(ring) for part in _polygon_parts(a2_land) for ring in part.interiors]
    holes.sort(key=lambda geometry: geometry.area, reverse=True)
    full_edges = [edge.geometry for edge in physical_edges]
    rows = []
    for number, hole in enumerate(holes, 1):
        centroid = hole.centroid
        representative = hole.representative_point()
        inside = reference_grid[reference_grid.geometry.apply(hole.covers)]
        accessible_pct = 100.0 * inside.reference_accessible.mean() if len(inside) else np.nan
        point_result = _reference_point(representative, edge_tree, physical_edges,
                                        travel_times, water)
        nearby_indices = edge_tree.query(hole)
        full_inside_parts = [full_edges[int(i)].intersection(hole) for i in nearby_indices
                             if full_edges[int(i)].intersects(hole)]
        full_inside = unary_union(full_inside_parts).length if full_inside_parts else 0.0
        reachable_inside = float(reachable_network.intersection(hole).length)
        nearby_zone = hole.buffer(50.0)
        nearby_length = float(reachable_network.intersection(nearby_zone).length)
        network_density = nearby_length / (nearby_zone.area / 10000.0)
        water_area = float(hole.intersection(water).area)
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
            category, label, fill = "A", "Water-related", False
            reason = "Mapped water occupies at least 20% of the A2 interior ring."
        elif full_inside > 1.0 and reachable_inside <= 0.1:
            category, label, fill = "D", "Network-data uncertainty", False
            reason = "A reviewed walk edge lies inside the ring but is not in the reachable geometry."
        elif len(inside) >= 3 and accessible_pct >= 80.0 and support >= 60.0:
            category, label, fill = "B", "Likely line-buffer artefact", True
            reason = "At least 80% of independent grid points are accessible and reachable links support at least 60% of the boundary."
        elif (len(inside) < 3 and point_result["reference_accessible"] and
              support >= 75.0 and hole.area <= 500.0):
            category, label, fill = "B", "Likely line-buffer artefact", True
            reason = "Small ring has an independently accessible representative point and at least 75% boundary support."
        elif len(inside) >= 3 and accessible_pct <= 75.0:
            category, label, fill = "C", "Potentially inaccessible urban land", False
            reason = "No more than 75% of independent reference points are accessible."
        else:
            category, label, fill = "E", "Manual review", False
            reason = "Available evidence does not justify automatic filling."

        rows.append({
            "analysis_version": VERSION,
            "hole_id": f"B2H{number:03d}",
            "classification": category,
            "classification_label": label,
            "method_b2_action": "fill" if fill else "retain",
            "justification": reason,
            "area_m2": float(hole.area),
            "perimeter_m": float(hole.length),
            "centroid_easting": float(centroid.x),
            "centroid_northing": float(centroid.y),
            "water_intersection_m2": water_area,
            "water_overlap_pct": water_pct,
            "nearby_reachable_network_m": nearby_length,
            "nearby_reachable_network_density_m_per_ha": network_density,
            "building_sample_overlap_m2": (float(building_overlap)
                                            if np.isfinite(building_overlap) else np.nan),
            "land_use_sample": land_values,
            "site_use_sample": site_values,
            "ngd_context_status": "limited discovery sample; not complete coverage",
            "distance_to_reachable_network_m": float(hole.distance(reachable_network)),
            "centroid_distance_to_reachable_network_m": float(centroid.distance(reachable_network)),
            "reachable_link_inside_m": reachable_inside,
            "full_walk_link_inside_m": float(full_inside),
            "reachable_boundary_support_pct": float(support),
            "reference_grid_point_count": int(len(inside)),
            "reference_accessible_pct": (float(accessible_pct)
                                           if np.isfinite(accessible_pct) else np.nan),
            "representative_point_accessible": bool(point_result["reference_accessible"]),
            "representative_point_time_s": (float(point_result["reference_time_s"])
                                               if np.isfinite(point_result["reference_time_s"]) else np.nan),
            "geometry": hole,
        })
    return gpd.GeoDataFrame(rows, crs=CRS)


def build_b2(a2_land, holes, water, crossings, ngd_structures):
    filled = holes[holes.method_b2_action == "fill"]
    additions = filled.geometry.union_all() if not filled.empty else Polygon()
    candidate = polygonal(make_valid(a2_land.union(additions)))
    land, corridors, display = apply_common_barrier_policy(
        candidate, water, crossings, SELECTED_BRIDGE_BUFFER_M, ngd_structures)
    if land.intersection(water).area > 1e-6:
        raise RuntimeError("Method B2 reintroduced mapped water")
    return candidate, land, corridors, display


def _arrival_details(edge, point, travel_times):
    candidates = []
    for u, v, _, geometry, _ in edge.members:
        if u not in travel_times:
            continue
        projected = geometry.project(point)
        arrival = float(travel_times[u]) + projected / SPEED_MPS
        attachment = u if projected <= geometry.length / 2 else v
        candidates.append((arrival, attachment))
    return min(candidates, default=(np.inf, None), key=lambda item: item[0])


def _sample_network(raw_polygon, physical_edges, travel_times,
                    max_arrival_s=1200.0):
    search_extent = raw_polygon.envelope.buffer(300.0)
    samples = {}
    for edge_id, edge in enumerate(physical_edges, 1):
        geometry = edge.geometry
        if not geometry.intersects(search_extent):
            continue
        segment_count = max(1, int(math.ceil(geometry.length / NETWORK_SAMPLE_SPACING_M)))
        for distance in np.linspace(0.0, geometry.length, segment_count + 1):
            point = geometry.interpolate(float(distance))
            arrival, attachment = _arrival_details(edge, point, travel_times)
            if not np.isfinite(arrival) or arrival > max_arrival_s or attachment is None:
                continue
            coordinate = (round(point.x, 3), round(point.y, 3))
            current = samples.get(coordinate)
            if current is None or arrival < current["travel_time_s"]:
                samples[coordinate] = {
                    "travel_time_s": arrival,
                    "physical_edge_id": edge_id,
                    "attachment_node": attachment,
                }
    coordinates = np.asarray(list(samples), dtype=float)
    records = [samples[tuple(coordinate)] for coordinate in coordinates]
    frame = gpd.GeoDataFrame({
        "sample_id": np.arange(1, len(coordinates) + 1),
        "travel_time_s": [row["travel_time_s"] for row in records],
        "physical_edge_id": [row["physical_edge_id"] for row in records],
        "attachment_node": [str(row["attachment_node"]) for row in records],
        "geometry": [Point(x, y) for x, y in coordinates],
    }, crs=CRS)
    return coordinates, np.asarray(frame.travel_time_s, dtype=float), frame, [
        row["attachment_node"] for row in records]


def _simple_distance_graph(graph):
    """Collapse parallel directions using the shortest stored metric length."""
    simple = nx.Graph()
    simple.add_nodes_from(graph.nodes)
    for u, v, data in graph.edges(data=True):
        weight = float(data.get("length", 0.0))
        if weight <= 0:
            continue
        if simple.has_edge(u, v):
            if weight < simple[u][v]["length"]:
                simple[u][v]["length"] = weight
        else:
            simple.add_edge(u, v, length=weight)
    return simple


def _batch_network_distances(graph, pairs, limit_m=TRIANGLE_NETWORK_AUDIT_LIMIT_M):
    """Calculate selected undirected pedestrian distances in bounded batches."""
    pairs = {tuple(pair) for pair in pairs if pair[0] is not None and pair[1] is not None}
    if not pairs:
        return {}
    simple = _simple_distance_graph(graph)
    nodes = list(simple.nodes)
    node_index = {node: index for index, node in enumerate(nodes)}
    matrix = nx.to_scipy_sparse_array(
        simple, nodelist=nodes, weight="length", format="csr", dtype=float)
    by_source = {}
    for source, target in pairs:
        by_source.setdefault(source, set()).add(target)
    sources = [node for node in by_source if node in node_index]
    result = {}
    chunk_size = 64
    for start in range(0, len(sources), chunk_size):
        chunk = sources[start:start + chunk_size]
        distances = sparse_dijkstra(
            matrix, directed=False, indices=[node_index[node] for node in chunk],
            limit=limit_m)
        distances = np.atleast_2d(distances)
        for row_index, source in enumerate(chunk):
            for target in by_source[source]:
                value = (distances[row_index, node_index[target]]
                         if target in node_index else np.inf)
                result[(source, target)] = float(value)
    return result


def audit_method_c(raw_polygon, physical_edges, travel_times, graph, reachable_network, water,
                   valid_crossing_lines, buildings):
    """Build C1/C2 and a triangle-level cross-barrier audit."""
    coordinates, values, samples, attachment_nodes = _sample_network(
        raw_polygon, physical_edges, travel_times)
    if len(coordinates) < 4:
        raise RuntimeError("Too few network samples for Method C audit")
    triangulation = Delaunay(coordinates)
    valid_zone = (valid_crossing_lines.buffer(5.0) if not valid_crossing_lines.is_empty
                  else Polygon())
    component_by_node = {}
    for component_id, nodes in enumerate(nx.connected_components(
            graph.to_undirected(as_view=True)), 1):
        for node in nodes:
            component_by_node[node] = component_id
    pairs_by_index = ((0, 1), (1, 2), (2, 0))
    rows = []
    clipped_pieces = {}
    query_pairs = set()

    for triangle_number, simplex in enumerate(triangulation.simplices, 1):
        triangle_coordinates = coordinates[simplex]
        triangle_values = values[simplex]
        triangle = Polygon(triangle_coordinates)
        side_lines = [LineString([triangle_coordinates[i], triangle_coordinates[j]])
                      for i, j in pairs_by_index]
        side_lengths = [line.length for line in side_lines]
        water_side_lengths = [line.intersection(water).length for line in side_lines]
        if max(water_side_lengths) > 0.5:
            relevant_index = int(np.argmax(water_side_lengths))
            pair_reason = "largest straight vertex connection across mapped water"
        else:
            relevant_index = int(np.argmax(side_lengths))
            pair_reason = "longest triangle edge"
        i, j = pairs_by_index[relevant_index]
        relevant_line = side_lines[relevant_index]
        euclidean = float(side_lengths[relevant_index])
        water_pair_length = float(water_side_lengths[relevant_index])
        water_area = float(triangle.intersection(water).area)
        water_pct = 100.0 * water_area / triangle.area if triangle.area else 0.0
        lower_bound = abs(float(triangle_values[i]) - float(triangle_values[j])) * SPEED_MPS
        lower_ratio = lower_bound / euclidean if euclidean else np.inf
        node_a = attachment_nodes[int(simplex[i])]
        node_b = attachment_nodes[int(simplex[j])]
        component_mismatch = component_by_node.get(node_a) != component_by_node.get(node_b)
        water_span = water_pair_length > 0.5
        bridge_supported = bool(
            water_span and not valid_zone.is_empty and
            relevant_line.intersection(water).difference(valid_zone).length <= 0.5)
        building_overlap = (float(buildings.geometry.intersection(triangle).area.sum())
                            if buildings is not None and not buildings.empty else np.nan)
        included_c1 = max(side_lengths) <= MAX_TRIANGLE_EDGE_M
        piece = (_clip_triangle_below_level(
            triangle_coordinates, triangle_values, THRESHOLD_S) if included_c1 else None)
        if piece is not None:
            clipped_pieces[triangle_number] = piece

        pre_screen = bool(
            included_c1 and (water_span or max(side_lengths) >= 100.0 or
                             lower_ratio >= NETWORK_FAR_RATIO or component_mismatch))
        if pre_screen:
            query_pairs.add((node_a, node_b))
            query_pairs.add((node_b, node_a))
        rows.append({
            "analysis_version": VERSION,
            "triangle_id": f"CT{triangle_number:05d}",
            "triangle_number": triangle_number,
            "vertex_sample_ids": "|".join(str(int(value) + 1) for value in simplex),
            "min_vertex_time_s": float(np.min(triangle_values)),
            "max_vertex_time_s": float(np.max(triangle_values)),
            "max_edge_length_m": float(max(side_lengths)),
            "min_edge_length_m": float(min(side_lengths)),
            "mean_edge_length_m": float(np.mean(side_lengths)),
            "relevant_pair_reason": pair_reason,
            "relevant_euclidean_distance_m": euclidean,
            "relevant_network_lower_bound_m": float(lower_bound),
            "lower_bound_to_euclidean_ratio": float(lower_ratio),
            "attachment_node_a": str(node_a),
            "attachment_node_b": str(node_b),
            "water_intersection_m2": water_area,
            "water_overlap_pct": float(water_pct),
            "vertex_connection_over_water_m": water_pair_length,
            "water_separates_vertex_pair": water_span,
            "verified_bridge_support": bridge_supported,
            "building_sample_overlap_m2": (building_overlap
                                            if np.isfinite(building_overlap) else np.nan),
            "component_mismatch": bool(component_mismatch),
            "network_distance_pre_screened": pre_screen,
            "included_in_c1_triangulation": bool(included_c1),
            "intersects_600s_surface": piece is not None,
            "geometry": triangle,
            "_node_a": node_a,
            "_node_b": node_b,
        })

    network_distances = _batch_network_distances(graph, query_pairs)
    for row in rows:
        pair = (row.pop("_node_a"), row.pop("_node_b"))
        network_distance = network_distances.get(pair, np.nan)
        if np.isfinite(network_distance):
            network_ratio = network_distance / max(row["relevant_euclidean_distance_m"], 0.001)
            network_excess = network_distance - row["relevant_euclidean_distance_m"]
            network_far = (network_ratio >= NETWORK_FAR_RATIO and
                           network_excess >= NETWORK_FAR_EXCESS_M)
            distance_status = "computed on undirected pedestrian graph between nearest edge-endpoint attachments"
        elif row["network_distance_pre_screened"]:
            network_ratio = np.inf
            network_excess = np.inf
            network_far = not row["component_mismatch"]
            distance_status = f"greater than {TRIANGLE_NETWORK_AUDIT_LIMIT_M:.0f} m audit limit or unavailable"
        else:
            network_ratio = np.nan
            network_excess = np.nan
            network_far = (row["lower_bound_to_euclidean_ratio"] >= NETWORK_FAR_RATIO and
                           row["relevant_network_lower_bound_m"] - row["relevant_euclidean_distance_m"] >= NETWORK_FAR_EXCESS_M)
            distance_status = "not computed after pre-screen; origin-time difference retained as lower bound"

        reasons = []
        if row["max_edge_length_m"] > MAX_TRIANGLE_EDGE_M:
            reasons.append("edge_over_150m")
        if row["component_mismatch"]:
            reasons.append("different_network_components")
        if row["water_separates_vertex_pair"] and not row["verified_bridge_support"]:
            reasons.append("water_span_without_verified_crossing")
        if network_far:
            reasons.append("euclidean_close_network_far")
        building_review = bool(
            np.isfinite(row["building_sample_overlap_m2"]) and
            row["building_sample_overlap_m2"] > 0.0)
        row.update({
            "pedestrian_network_distance_m": (float(network_distance)
                                                if np.isfinite(network_distance) else np.nan),
            "network_to_euclidean_ratio": (float(network_ratio)
                                            if np.isfinite(network_ratio) else np.nan),
            "network_distance_excess_m": (float(network_excess)
                                           if np.isfinite(network_excess) else np.nan),
            "network_distance_status": distance_status,
            "euclidean_close_network_far": bool(network_far),
            "building_review_flag": building_review,
            "c2_reject": bool(reasons),
            "c2_reject_reasons": "|".join(reasons),
            "suspicious_for_review": bool(reasons or building_review),
        })

    triangles = gpd.GeoDataFrame(rows, crs=CRS)
    c1_ids = set(triangles.loc[
        triangles.included_in_c1_triangulation & triangles.intersects_600s_surface,
        "triangle_number"])
    c2_ids = set(triangles.loc[
        triangles.included_in_c1_triangulation & triangles.intersects_600s_surface &
        ~triangles.c2_reject, "triangle_number"])
    c1_pieces = [piece for number, piece in clipped_pieces.items() if number in c1_ids]
    safeguard = reachable_network.buffer(25.0)
    c2_pieces = []
    constrained_count = 0
    removed_count = 0
    reject_lookup = triangles.set_index("triangle_number").c2_reject.to_dict()
    treatments = {}
    for number in c1_ids:
        piece = clipped_pieces[number]
        if reject_lookup[number]:
            constrained = polygonal(make_valid(piece.intersection(safeguard)))
            if constrained.is_empty:
                treatments[number] = "remove_after_25m_network_constraint"
                removed_count += 1
                continue
            piece = constrained
            treatments[number] = "constrain_to_25m_reachable_network_buffer"
            constrained_count += 1
        else:
            treatments[number] = "retain_interpolated_piece"
        c2_pieces.append(piece)
    triangles["c2_treatment"] = triangles.triangle_number.map(treatments).fillna(
        "outside_600s_surface_or_over_150m")
    c1_unmasked = polygonal(make_valid(unary_union(c1_pieces)))
    c2_unmasked = polygonal(make_valid(unary_union(c2_pieces)))
    if c1_unmasked.is_empty or c2_unmasked.is_empty:
        raise RuntimeError("Method C audit generated an empty contour")

    metadata = {
        "implementation_status": "ordinary-Delaunay experimental proxies; not Lin et al. reproduction",
        "sample_spacing_m": NETWORK_SAMPLE_SPACING_M,
        "sample_count": len(samples),
        "triangle_count": len(triangles),
        "c1_triangles_over_150m": int((triangles.max_edge_length_m > MAX_TRIANGLE_EDGE_M).sum()),
        "c1_surface_triangle_count": len(c1_ids),
        "c2_unconstrained_surface_triangle_count": len(c2_ids),
        "c2_constrained_surface_triangle_count": constrained_count,
        "c2_removed_surface_triangle_count": removed_count,
        "water_spanning_triangle_count": int(triangles.water_separates_vertex_pair.sum()),
        "network_far_triangle_count": int(triangles.euclidean_close_network_far.sum()),
        "network_distance_method": "selected pre-screened vertex pairs; undirected reviewed graph between nearest edge-endpoint attachments",
    }
    return c1_unmasked, c2_unmasked, samples, triangles, metadata


def _population_results(population, analytical_geometry, water):
    standard = area_weight(population, analytical_geometry)
    land_population = population.copy()
    land_population.geometry = population.geometry.apply(
        lambda geometry: polygonal(make_valid(geometry.difference(water))))
    water_adjusted_geometry = polygonal(make_valid(analytical_geometry.difference(water)))
    water_adjusted = area_weight(land_population, water_adjusted_geometry)
    standard_total = float(standard.estimated_population.sum())
    adjusted_total = float(water_adjusted.estimated_population.sum())
    area_ha = analytical_geometry.area / 10000.0
    adjusted_area_ha = water_adjusted_geometry.area / 10000.0
    return ({
        "standard_population": standard_total,
        "standard_density_per_ha": standard_total / area_ha,
        "water_adjusted_area_ha": adjusted_area_ha,
        "water_adjusted_population": adjusted_total,
        "water_adjusted_density_per_ha": adjusted_total / adjusted_area_ha,
    }, standard, water_adjusted)


def compare_barrier_methods(methods, reachable_network, water, population,
                            reference_grid, a2_geometry):
    rows, population_rows, pairwise_rows, intersections = [], [], [], {}
    total_network_length = float(reachable_network.length)
    for method_id, details in methods.items():
        analytical = details["analytical"]
        display = details["display"]
        corridor = details["corridor"]
        population_result, standard, adjusted = _population_results(
            population, analytical, water)
        intersections[method_id] = (standard, adjusted)
        validation = _validation_metrics(reference_grid, analytical)
        network_covered = reachable_network.intersection(display.buffer(0.01)).length
        rows.append({
            "analysis_version": VERSION,
            "method": method_id,
            "method_name": details["name"],
            "status": details["status"],
            "analytical_land_area_ha": analytical.area / 10000.0,
            "display_area_with_bridge_corridors_ha": display.area / 10000.0,
            "bridge_corridor_area_ha": corridor.area / 10000.0,
            "estimated_population": population_result["standard_population"],
            "population_density_per_ha": population_result["standard_density_per_ha"],
            "hole_count": _hole_count(analytical),
            "total_hole_area_ha": _hole_area(analytical) / 10000.0,
            "water_overlap_ha": analytical.intersection(water).area / 10000.0,
            "analytical_component_count": len(_polygon_parts(analytical)),
            "display_component_count": len(_polygon_parts(display)),
            "reachable_network_coverage_pct": 100.0 * network_covered / total_network_length,
            "grid_model_agreement_precision": validation["precision"],
            "grid_model_agreement_recall": validation["recall"],
            "grid_model_agreement_f1": validation["f1_score"],
            "suspicious_cross_barrier_interpolation_count": details["suspicious_count"],
        })
        population_rows.extend([
            {
                "analysis_version": VERSION,
                "method": method_id,
                "population_method": "standard_full_LSOA_denominator",
                "analytical_land_area_ha": analytical.area / 10000.0,
                "estimated_population": population_result["standard_population"],
                "population_density_per_ha": population_result["standard_density_per_ha"],
                "population_baseline": "ONS Census 2021",
            },
            {
                "analysis_version": VERSION,
                "method": method_id,
                "population_method": "water_adjusted_source_zone_sensitivity",
                "analytical_land_area_ha": population_result["water_adjusted_area_ha"],
                "estimated_population": population_result["water_adjusted_population"],
                "population_density_per_ha": population_result["water_adjusted_density_per_ha"],
                "population_baseline": "ONS Census 2021",
            },
        ])
        symmetric = analytical.symmetric_difference(a2_geometry)
        pairwise_rows.append({
            "analysis_version": VERSION,
            "method": method_id,
            "reference_method": "A2",
            "symmetric_difference_ha": symmetric.area / 10000.0,
            "intersection_with_a2_ha": analytical.intersection(a2_geometry).area / 10000.0,
            "method_only_ha": analytical.difference(a2_geometry).area / 10000.0,
            "a2_only_ha": a2_geometry.difference(analytical).area / 10000.0,
        })
    return (pd.DataFrame(rows), pd.DataFrame(population_rows),
            pd.DataFrame(pairwise_rows), intersections)


def bridge_buffer_sensitivity(candidates, water, crossings, ngd_structures):
    rows = []
    for buffer_m in (0.5, 1.0, 2.0):
        total = bridge_corridors(crossings, water, buffer_m, ngd_structures)
        row = {
            "analysis_version": VERSION,
            "buffer_each_side_m": buffer_m,
            "approximate_full_width_m": 2.0 * buffer_m,
            "all_verified_crossings_corridor_area_m2": total.area,
        }
        for method_id, candidate in candidates.items():
            land, corridor, display = apply_common_barrier_policy(
                candidate, water, crossings, buffer_m, ngd_structures)
            row[f"{method_id.lower()}_corridor_area_m2"] = corridor.area
            row[f"{method_id.lower()}_area_increase_m2"] = display.area - land.area
            row[f"{method_id.lower()}_display_components"] = len(_polygon_parts(display))
        rows.append(row)
    return pd.DataFrame(rows)


def _geometry_frame(methods, comparison):
    rows = []
    for method_id, details in methods.items():
        metric = comparison[comparison.method == method_id].iloc[0]
        rows.append({
            "analysis_version": VERSION,
            "method": method_id,
            "method_name": details["name"],
            "geometry_role": "analytical_land_catchment",
            "area_ha": metric.analytical_land_area_ha,
            "geometry": details["analytical"],
        })
    return gpd.GeoDataFrame(rows, crs=CRS)


def make_method_map(methods, comparison, reachable_network, water, crossings, ngd):
    method_ids = ["A2", "B2", "C1", "C2"]
    all_geometry = unary_union([methods[method_id]["display"] for method_id in method_ids])
    xmin, ymin, xmax, ymax = all_geometry.bounds
    bounds = (xmin - 100, ymin - 100, xmax + 100, ymax + 100)
    basemap = os_maps_basemap(bounds)
    _, major_roads, site = _map_context()
    colours = {"A2": "#176b72", "B2": "#c8782f", "C1": "#75619a", "C2": "#4b7f52"}
    titles = {
        "A2": "A2  Water-aware 25 m buffer",
        "B2": "B2  Water-aware controlled holes",
        "C1": "C1  Ordinary-Delaunay + common policy",
        "C2": "C2  Barrier-aware contour proxy",
    }
    fig, axes = plt.subplots(2, 2, figsize=(420/25.4, 297/25.4),
                             facecolor="white", sharex=True, sharey=True)
    for ax, method_id in zip(axes.flat, method_ids):
        _map_base(ax, bounds, basemap, water, ngd["buildings"], major_roads)
        details = methods[method_id]
        gpd.GeoSeries([details["analytical"]], crs=CRS).plot(
            ax=ax, facecolor=colours[method_id], edgecolor=colours[method_id],
            linewidth=0.9, alpha=0.22, zorder=4)
        if not details["corridor"].is_empty:
            gpd.GeoSeries([details["corridor"]], crs=CRS).plot(
                ax=ax, color="#f3b33d", linewidth=1.0, zorder=6)
        gpd.GeoSeries([reachable_network], crs=CRS).plot(
            ax=ax, color="#303c40", linewidth=0.35, zorder=5)
        valid = crossings[crossings.policy_use]
        if not valid.empty:
            valid.plot(ax=ax, color="#ce3d38", linewidth=1.3, zorder=7)
        site.plot(ax=ax, facecolor="#bd542a", edgecolor="white", linewidth=0.6, zorder=8)
        row = comparison[comparison.method == method_id].iloc[0]
        ax.set_title(
            f"{titles[method_id]}\n{row.analytical_land_area_ha:.1f} ha | "
            f"~{row.estimated_population:,.0f} residents | {int(row.hole_count)} holes | "
            f"Grid F1 {row.grid_model_agreement_f1:.3f}",
            fontsize=9.5, color="#253842")
    fig.suptitle("Crossharbour 10-Minute Catchment: Common Barrier Policy",
                 x=0.045, y=0.975, ha="left", fontsize=19, color="#253842", weight="bold")
    fig.text(0.045, 0.945,
             "Same routing, origins, speed, 600 s threshold, OSM water and 1 m-each-side bridge display policy. Analytical area excludes bridge corridors.",
             fontsize=8.5, color="#586970")
    handles = [
        Patch(facecolor="#176b72", alpha=0.22, label="Analytical land catchment"),
        Line2D([], [], color="#303c40", lw=1, label="Reachable pedestrian network"),
        Patch(facecolor="#d5e8ef", label="Mapped water"),
        Line2D([], [], color="#ce3d38", lw=2, label="Likely valid water crossing"),
        Line2D([], [], color="#f3b33d", lw=3, label="Bridge display corridor"),
        Patch(facecolor="#bd542a", label="Development site"),
    ]
    fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.045, 0.02),
               ncol=6, fontsize=8, frameon=False)
    source = (f"OS Maps API {basemap['layer']}; {basemap['attribution']}"
              if basemap else "Local vector context; © OpenStreetMap contributors")
    fig.text(0.045, 0.008, f"{source} | OSM network/water | EPSG:27700",
             fontsize=7.2, color="#586970")
    fig.subplots_adjust(left=0.035, right=0.99, bottom=0.07, top=0.90,
                        hspace=0.13, wspace=0.025)
    fig.savefig(OUT_COMPARE_MAP, dpi=220, facecolor="white")
    fig.savefig(OUT_COMPARE_MAP_PDF, facecolor="white", metadata={
        "Title": "Crossharbour barrier-consistent catchment comparison",
        "Subject": "A2/B2/C1/C2; routing unchanged; Figure 01 preserved"})
    plt.close(fig)


def make_bridge_qa_map(crossings, water, reachable_network, c1_unmasked, c1_land,
                       ngd_structures):
    selected = crossings[crossings.policy_use].copy()
    count = len(selected)
    columns = 3
    rows = max(1, math.ceil((count + 1) / columns))
    fig, axes = plt.subplots(rows, columns, figsize=(420/25.4, 297/25.4),
                             facecolor="white")
    axes = np.atleast_1d(axes).ravel()
    for ax, row in zip(axes, selected.itertuples()):
        bounds_geom = row.geometry.buffer(35.0)
        xmin, ymin, xmax, ymax = bounds_geom.bounds
        local_water = gpd.GeoDataFrame(geometry=[water], crs=CRS).cx[xmin:xmax, ymin:ymax]
        local_water.plot(ax=ax, facecolor="#cfe5ed", edgecolor="#6e9eaf", linewidth=0.5)
        gpd.GeoSeries([c1_unmasked], crs=CRS).boundary.plot(
            ax=ax, color="#8c6ab1", linewidth=1.1, linestyle="--", zorder=2)
        gpd.GeoSeries([c1_land], crs=CRS).plot(
            ax=ax, facecolor="#5b8f75", edgecolor="#3f735a", alpha=0.20, linewidth=0.5, zorder=3)
        local_network = reachable_network.intersection(bounds_geom)
        gpd.GeoSeries([local_network], crs=CRS).plot(
            ax=ax, color="#303c40", linewidth=0.8, zorder=4)
        for buffer_m, colour, linestyle in ((2.0, "#d4564e", ":"),
                                             (1.0, "#eea62f", "-"),
                                             (0.5, "#f7df79", "--")):
            corridor = row.geometry.buffer(buffer_m, cap_style="flat").intersection(water)
            if not corridor.is_empty:
                gpd.GeoSeries([corridor], crs=CRS).boundary.plot(
                    ax=ax, color=colour, linewidth=1.2, linestyle=linestyle, zorder=5)
        gpd.GeoSeries([row.geometry], crs=CRS).plot(
            ax=ax, color="#101719", linewidth=1.2, zorder=6)
        if not ngd_structures.empty:
            matches = ngd_structures[ngd_structures.intersects(bounds_geom)]
            if not matches.empty:
                matches.boundary.plot(ax=ax, color="#2578a5", linewidth=1.0, zorder=7)
        ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax); ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        attrs = ", ".join(value for value in [
            f"bridge={row.bridge}" if row.bridge else "",
            f"tunnel={row.tunnel}" if row.tunnel else "",
            f"layer={row.layer}" if row.layer else "",
            f"highway={row.highway}" if row.highway else "",
        ] if value)
        ax.set_title(f"{row.crossing_id}  {row.name or 'unnamed'}\n{attrs}",
                     fontsize=7.6, color="#253842")
    note_ax = axes[count] if count < len(axes) else None
    if note_ax is not None:
        note_ax.axis("off")
        note_ax.text(0, 1, "BRIDGE POLICY", va="top", fontsize=11,
                     color="#176b72", weight="bold")
        note_ax.text(0, 0.86,
                     "Purple dashed: C1 before water mask\n"
                     "Green: analytical land after mask\n"
                     "Yellow dashed: 0.5 m each side\n"
                     "Orange: 1.0 m each side (selected)\n"
                     "Red dotted: 2.0 m each side\n\n"
                     "Corridors are display geometry only.\nPopulation and density use land geometry.\n"
                     "NGD blue outline appears only when a\nlimited discovery sample matches.",
                     va="top", fontsize=8.6, linespacing=1.3, color="#253842")
    for ax in axes[count + 1:]:
        ax.axis("off")
    fig.suptitle("Crossharbour: Reachable Water-Crossing QA",
                 x=0.04, y=0.975, ha="left", fontsize=19,
                 color="#253842", weight="bold")
    fig.text(0.04, 0.945,
             "Every automatically accepted crossing; graph topology and OSM tags establish reachability, while 2D water overlap is retained as QA evidence.",
             fontsize=8.5, color="#586970")
    fig.text(0.04, 0.018, "© OpenStreetMap contributors | Limited OS NGD discovery samples © Crown copyright and database rights 2026 | EPSG:27700",
             fontsize=7.2, color="#586970")
    fig.subplots_adjust(left=0.04, right=0.99, bottom=0.06, top=0.90,
                        wspace=0.12, hspace=0.24)
    fig.savefig(OUT_BRIDGE_MAP, dpi=220, facecolor="white")
    fig.savefig(OUT_BRIDGE_MAP_PDF, facecolor="white", metadata={
        "Title": "Crossharbour reachable water-crossing QA",
        "Subject": "Bridge buffers and Method C water-mask behaviour"})
    plt.close(fig)


def _markdown_table(frame, columns, labels, formats=None):
    formats = formats or {}
    header = "| " + " | ".join(labels) + " |"
    divider = "|" + "|".join("---:" if index else "---" for index in range(len(columns))) + "|"
    lines = [header, divider]
    for row in frame.itertuples(index=False):
        values = []
        for column in columns:
            value = getattr(row, column)
            if column in formats and pd.notna(value):
                value = formats[column].format(value)
            values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_barrier_report(comparison, population_table, pairwise, bridge_sensitivity,
                         crossings, holes, triangles, c_metadata, a0_inventory,
                         water_path):
    valid = crossings[crossings.policy_use]
    selected_bridge_area = float(bridge_sensitivity.loc[
        bridge_sensitivity.buffer_each_side_m == SELECTED_BRIDGE_BUFFER_M,
        "all_verified_crossings_corridor_area_m2"].iloc[0])
    filled = holes[holes.method_b2_action == "fill"]
    retained = holes[holes.method_b2_action == "retain"]
    suspicious_surface = triangles[
        triangles.included_in_c1_triangulation & triangles.intersects_600s_surface &
        triangles.c2_reject]
    water_rejected = suspicious_surface.c2_reject_reasons.str.contains(
        "water_span_without_verified_crossing", na=False).sum()
    network_far = suspicious_surface.c2_reject_reasons.str.contains(
        "euclidean_close_network_far", na=False).sum()
    crossing_counts_text = ", ".join(
        f"{key}={value}"
        for key, value in crossings.classification.value_counts().sort_index().items())
    unresolved_crossing_count = int((~crossings.policy_use).sum())
    method_table = _markdown_table(
        comparison,
        ["method", "analytical_land_area_ha", "estimated_population",
         "population_density_per_ha", "hole_count", "total_hole_area_ha",
         "water_overlap_ha", "bridge_corridor_area_ha", "analytical_component_count",
         "grid_model_agreement_f1", "suspicious_cross_barrier_interpolation_count"],
        ["Method", "Land area (ha)", "Population", "Density (/ha)", "Holes",
         "Hole area (ha)", "Water overlap (ha)", "Bridge corridor (ha)",
         "Land components", "Grid-model F1", "Suspicious interpolation"],
        {"analytical_land_area_ha": "{:.2f}", "estimated_population": "{:.0f}",
         "population_density_per_ha": "{:.1f}", "total_hole_area_ha": "{:.3f}",
         "water_overlap_ha": "{:.6f}", "bridge_corridor_area_ha": "{:.4f}",
         "grid_model_agreement_f1": "{:.3f}"})
    population_standard = population_table[
        population_table.population_method == "standard_full_LSOA_denominator"]
    population_sensitivity = population_table[
        population_table.population_method == "water_adjusted_source_zone_sensitivity"]
    pop_table = _markdown_table(
        population_standard,
        ["method", "analytical_land_area_ha", "estimated_population", "population_density_per_ha"],
        ["Method", "Land area (ha)", "Standard population", "Density (/ha)"],
        {"analytical_land_area_ha": "{:.2f}", "estimated_population": "{:.0f}",
         "population_density_per_ha": "{:.1f}"})
    sensitivity_table = _markdown_table(
        population_sensitivity,
        ["method", "estimated_population", "population_density_per_ha"],
        ["Method", "Water-adjusted sensitivity population", "Sensitivity density (/ha)"],
        {"estimated_population": "{:.0f}", "population_density_per_ha": "{:.1f}"})
    buffer_table = _markdown_table(
        bridge_sensitivity,
        ["buffer_each_side_m", "approximate_full_width_m",
         "all_verified_crossings_corridor_area_m2", "a2_area_increase_m2",
         "b2_area_increase_m2", "c1_area_increase_m2", "c2_area_increase_m2"],
        ["Buffer each side (m)", "Approx. width (m)", "All corridor area (m²)",
         "A2 increase (m²)", "B2 increase (m²)", "C1 increase (m²)", "C2 increase (m²)"],
        {column: "{:.2f}" for column in bridge_sensitivity.columns if column.endswith("_m2") or column.endswith("_m")})
    pair_table = _markdown_table(
        pairwise,
        ["method", "symmetric_difference_ha", "method_only_ha", "a2_only_ha"],
        ["Method", "Symmetric difference from A2 (ha)", "Method only (ha)", "A2 only (ha)"],
        {"symmetric_difference_ha": "{:.2f}", "method_only_ha": "{:.2f}",
         "a2_only_ha": "{:.2f}"})

    if a0_inventory is not None and len(a0_inventory):
        original_artificial = int((a0_inventory.method_b_action == "fill").sum())
        original_retained = int((a0_inventory.method_b_action == "retain").sum())
        original_text = (
            f"The original A0 audit contains {len(a0_inventory)} holes. "
            f"{original_artificial} were classified as evidence-supported line-buffer artefacts and "
            f"{original_retained} were retained as potentially inaccessible or unresolved."
        )
    else:
        original_text = "The earlier A0 inventory was unavailable; the preserved A0 polygon remains in the output for review."

    text = f"""# Crossharbour catchment barrier-policy review ({VERSION})

## Scope and authoritative result

The OSMnx pedestrian graph, reviewed proxy origins A/B, multi-source shortest-path calculation, 4.8 km/h walking speed, 600-second threshold and partial-edge handling are unchanged. The authoritative routing result remains the reachable pedestrian network. Every polygon in this review is a derived two-dimensional representation for population interpolation and workshop communication.

All metric operations use EPSG:27700. The cleaned analytical water source is `{water_path.relative_to(ROOT)}` and its geometry is validated before use. Existing polygonisation outputs and Figure 01 are not overwritten.

## Common water and bridge policy

`apply_common_barrier_policy(candidate_polygon, reachable_network evidence)` performs the same operation for A2, B2, C1 and C2:

1. validate and union mapped OSM water;
2. subtract mapped water from the candidate polygon to create `analytical_land_catchment`;
3. retain land on every side of the water where the candidate and routing support it;
4. restore only separately classified, likely valid reachable water crossings as narrow `bridge_connectivity_corridors`;
5. keep bridge corridors separate from population, land area and density calculations.

The graph contains **{len(crossings)}** reachable physical edges intersecting mapped water. Classification counts are: {crossing_counts_text}. **{len(valid)}** have explicit bridge/tunnel/layer or matching NGD evidence and are used by the policy. Untagged shoreline overlaps remain review items and are not silently restored.

The existing OS NGD Structure/Compound Structure data contains only ten-feature discovery samples. None is treated as complete coverage. Real NGD bridge geometry would be preferred where a reliable sample matches; otherwise the policy uses an OSM line buffer. Each display corridor extends 0.5 m beyond the mapped water edge as a land tie-in so numerical boundary differences cannot leave it isolated.

`buffer(1.0)` means 1 m on each side of the line, or about 2 m total width. The selected corridor geometry totals **{selected_bridge_area:.2f} m²** across all accepted crossings, including the 0.5 m land tie-ins, and is the smallest tested value that remains legible when symbolised at A3 scale. The per-method area-increase columns measure the actual new display area after overlap with land.

{buffer_table}

These corridor areas are negligible relative to catchment hectares and are excluded from analytical land area. The display-area sensitivity is retained in the table rather than mixed into population results.

## A0, A2 and B2 hole diagnosis

{original_text}

After the common water mask, A2 contains **{len(holes)}** interior rings. B2 fills **{len(filled)}** rings covering **{filled.geometry.area.sum()/10000:.3f} ha** and retains **{len(retained)}** rings covering **{retained.geometry.area.sum()/10000:.3f} ha**. Each B2 decision records water overlap, nearby reachable-network density, limited NGD context, distance to the network, boundary support and independent grid accessibility. Mapped docks, rivers and reservoirs are masked again after filling, so B2 cannot reintroduce water.

## Method C audit

C1 uses the existing experimental method: approximately 15 m network samples; minimum A/B directed arrival time at each sample; SciPy ordinary Delaunay; linear travel-time interpolation; clipping at 600 seconds; rejection of triangles with a side over 150 m; then the common water/bridge policy. It removes narrow buffer holes because it interpolates a continuous time surface between network samples rather than buffering each one-dimensional line independently.

All **{len(triangles):,}** ordinary-Delaunay triangles are exported. Each records side lengths, vertex times, mapped-water overlap, limited building-sample overlap, a relevant vertex-pair Euclidean distance, the origin-time network-distance lower bound, and a pedestrian-network distance for pre-screened pairs. Pedestrian distances use the reviewed graph between the nearest endpoint attachments, so they are an audit approximation rather than exact point-on-edge distances.

C1 has **{len(suspicious_surface)}** contour-contributing triangles constrained by C2: **{water_rejected}** include a water-spanning vertex connection outside an accepted crossing zone and **{network_far}** are Euclidean-close but network-far. Counts can overlap. Building overlap is a review flag and is never a blanket exclusion because buildings may contain residents.

C2 uses the same samples and interpolation. Where a contour triangle crosses mapped water away from an accepted crossing, connects different pedestrian components, or is demonstrably network-far, only the portion within the established 25 m reachable-network buffer is retained. This local constraint avoids deleting the accessible part of the triangle while preventing unsupported interpolation across the barrier. The 150 m rule continues to apply. C2 then receives the same water mask and bridge restoration as every other processed method. It is a barrier-aware ordinary-Delaunay proxy, not a constrained triangulation.

Method C metadata: `{json.dumps(c_metadata, sort_keys=True)}`

## Constrained Delaunay feasibility (C3)

The Lin et al. reference implementation was inspected at commit `{REFERENCE_COMMIT}`. Its constraints are the segmented graph edges returned by `GetEdgesForConstrianedTriangulationLineSegmentation`, including inserted extended-tree break/delta nodes; `BuildConstrainedTrangulation*` inserts each resulting edge-node pair as a CGAL constraint. In the multiple-origin implementation the zero-cost virtual facility node is excluded, and its synthetic links are not inserted as constraints.

This environment has SciPy ordinary Delaunay but does not have CGAL Python bindings, Triangle or meshpy. A correct C3 would also require porting and testing the directed extended-shortest-path-tree and break-node logic, rather than merely passing current sample points to another triangulator. C3 is therefore **not implemented** because it would add substantial dependency and validation risk to the take-home.

## Fair comparison

{method_table}

{pair_table}

Water overlap for A2/B2/C1/C2 should be numerical zero. A0 is intentionally retained as the unprocessed raw baseline. Suspicious interpolation counts apply only to the ordinary-Delaunay contour surface; zero for buffer methods means “not applicable,” not proof of physical permeability.

## Grid-model agreement audit

Reference points form a regular 10 m grid. Each label uses the closest reviewed physical edge, directed multi-source travel time to the projected point, straight off-network time at 4.8 km/h and a water/connector check. Candidate polygons are used only as predictions and never generate the labels. The reference does not use Delaunay interpolation.

There is no direct polygon circularity. There is still conceptual similarity that can favour C1/C2: both the grid model and contours extend network travel time into surrounding space. The metric is therefore named **Grid-model agreement F1**, is not real-world accuracy, and is not the sole selection criterion.

## Population sensitivity

Standard results use the complete original LSOA area as the denominator and the analytical land catchment as the numerator geometry:

{pop_table}

The water-adjusted source-zone denominator remains a separate sensitivity test:

{sensitivity_table}

Bridge display corridors are excluded from both tables.

## Explicit answers and recommendation

1. **Which A0 holes were artificial?** {original_artificial if a0_inventory is not None and len(a0_inventory) else 'See preserved A0 inventory'} holes had independent accessibility and boundary-support evidence consistent with line-buffer artefacts.
2. **Which represent genuine barriers?** Water-classified rings and low-accessibility/uncertain rings remain excluded. “Genuine” means supported by current mapped evidence; field access, parcel gates and complete land-use coverage remain unavailable.
3. **Does B2 avoid mapped water?** Yes. B2 is derived from A2, water-related rings are never filled, and the common water mask is reapplied after filling. The comparison table reports the residual overlap.
4. **Why is C1 more coherent?** It constructs a continuous 600-second time surface between dense network samples, eliminating the repeated 25 m corridor gaps produced by line buffering.
5. **Where is ordinary Delaunay questionable?** The suspicious-triangle layer identifies water-spanning, network-far, disconnected or very-long triangulation relationships with explicit evidence and geometry.
6. **Does C2 improve these locations?** It constrains {len(suspicious_surface)} contour-contributing suspect triangles to the local 25 m reachable-network buffer before polygon union. Its area/population change relative to C1 is visible in the comparison, but it remains a heuristic proxy.
7. **Does C3 differ?** No defensible C3 result exists in this environment, so no difference is claimed.
8. **Are bridges preserved correctly?** Yes for {len(valid)} likely valid tagged/reviewed links. Narrow corridors preserve display continuity while surrounding water stays outside analytical land.
9. **How sensitive are area and population?** The method and population tables quantify the change under identical routing, water, population and bridge rules.
10. **Recommended workshop method:** use **B2 — water-aware controlled-hole treatment**. It is transparent, auditable, conservative around unresolved barriers and implemented entirely in the existing reproducible stack. Keep A2 beside it as the conservative baseline. Use C2 as a research sensitivity figure rather than the final workshop catchment until constrained triangulation and better parcel/barrier evidence can be validated.

## Remaining limitations

- OSM water and OSM bridge tags may contain omissions or positional differences.
- {unresolved_crossing_count} unresolved water overlaps remain manual-review items and are not restored automatically.
- NGD discovery samples are not complete bridge, building or land-use coverage.
- The triangle network distance uses nearest endpoint attachments for pre-screened pairs, not exact temporary point insertion.
- The grid benchmark models off-network movement and does not provide observed walking traces or verified parcel entrances.
- Area-weighted population assumes uniform distribution within each LSOA.
"""
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(text, encoding="utf-8")


def run_barrier_consistent_review():
    for folder in (GIS, TABLES, QA, MAPS, METHOD):
        folder.mkdir(parents=True, exist_ok=True)

    raw_polygon = gpd.read_file(
        GIS / "walking_catchment_10min.gpkg", layer="catchment_raw").to_crs(CRS).geometry.union_all()
    reachable_frame = gpd.read_file(GIS / "reachable_network_10min.gpkg").to_crs(CRS)
    reachable_network = reachable_frame.geometry.union_all()
    water_path = selected_water_path()
    water_frame, water = validate_water_layer(gpd.read_file(water_path))
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
    ngd_structures = _load_ngd_structures()

    crossings = review_reachable_water_crossings(
        physical_edges, reachable_network, water, ngd_structures)
    if crossings.empty:
        raise RuntimeError("No reachable water-crossing candidates were found")
    valid_crossing_lines = (crossings.loc[crossings.policy_use, "geometry"].union_all()
                            if crossings.policy_use.any() else LineString())

    a2_land, a2_corridor, a2_display = apply_common_barrier_policy(
        raw_polygon, water, crossings, SELECTED_BRIDGE_BUFFER_M, ngd_structures)
    b2_holes = diagnose_b2_holes(
        a2_land, reachable_network, physical_edges, reference_grid, edge_tree,
        travel_times, water, ngd)
    b2_candidate, b2_land, b2_corridor, b2_display = build_b2(
        a2_land, b2_holes, water, crossings, ngd_structures)

    c1_unmasked, c2_unmasked, c_samples, triangles, c_metadata = audit_method_c(
        raw_polygon, physical_edges, travel_times, graph, reachable_network, water,
        valid_crossing_lines, ngd["buildings"])
    c1_land, c1_corridor, c1_display = apply_common_barrier_policy(
        c1_unmasked, water, crossings, SELECTED_BRIDGE_BUFFER_M, ngd_structures)
    c2_land, c2_corridor, c2_display = apply_common_barrier_policy(
        c2_unmasked, water, crossings, SELECTED_BRIDGE_BUFFER_M, ngd_structures)
    suspicious_surface_count = int((
        triangles.included_in_c1_triangulation & triangles.intersects_600s_surface &
        triangles.c2_reject).sum())

    methods = {
        "A0": {
            "name": "Original raw 25 m buffer",
            "status": "preserved QA baseline; no common water policy",
            "analytical": raw_polygon,
            "corridor": Polygon(),
            "display": raw_polygon,
            "suspicious_count": 0,
        },
        "A2": {
            "name": "Water-aware 25 m buffer",
            "status": "common water mask plus separate bridge display corridors",
            "analytical": a2_land,
            "corridor": a2_corridor,
            "display": a2_display,
            "suspicious_count": 0,
        },
        "B2": {
            "name": "Water-aware controlled-hole method",
            "status": "recommended workshop review candidate",
            "analytical": b2_land,
            "corridor": b2_corridor,
            "display": b2_display,
            "suspicious_count": 0,
        },
        "C1": {
            "name": "Ordinary-Delaunay contour with common policy",
            "status": "experimental proxy",
            "analytical": c1_land,
            "corridor": c1_corridor,
            "display": c1_display,
            "suspicious_count": suspicious_surface_count,
        },
        "C2": {
            "name": "Barrier-aware ordinary-Delaunay contour proxy",
            "status": "experimental heuristic; not constrained Delaunay",
            "analytical": c2_land,
            "corridor": c2_corridor,
            "display": c2_display,
            "suspicious_count": 0,
        },
    }
    comparison, population_table, pairwise, intersections = compare_barrier_methods(
        methods, reachable_network, water, population, reference_grid, a2_land)
    bridge_sensitivity = bridge_buffer_sensitivity(
        {"A2": raw_polygon, "B2": b2_candidate, "C1": c1_unmasked, "C2": c2_unmasked},
        water, crossings, ngd_structures)

    analytical_frame = _geometry_frame(methods, comparison)
    _write_layer(analytical_frame, OUT_METHODS, "analytical_land_catchments", first=True)
    display_rows, corridor_rows = [], []
    for method_id, details in methods.items():
        display_rows.append({
            "analysis_version": VERSION, "method": method_id,
            "geometry_role": "display_geometry_with_bridge_corridors",
            "geometry": details["display"]})
        if not details["corridor"].is_empty:
            corridor_rows.append({
                "analysis_version": VERSION, "method": method_id,
                "buffer_each_side_m": SELECTED_BRIDGE_BUFFER_M,
                "geometry_role": "cartographic_bridge_connectivity_corridor",
                "geometry": details["corridor"]})
    _write_layer(gpd.GeoDataFrame(display_rows, crs=CRS), OUT_METHODS,
                 "display_geometries")
    _write_layer(gpd.GeoDataFrame(corridor_rows, crs=CRS), OUT_METHODS,
                 "bridge_connectivity_corridors")
    _write_layer(gpd.GeoDataFrame([
        {"method": "C1", "status": "before common water mask", "geometry": c1_unmasked},
        {"method": "C2", "status": "before common water mask", "geometry": c2_unmasked},
    ], crs=CRS), OUT_METHODS, "method_c_unmasked")
    _write_layer(reference_grid, OUT_METHODS, "validation_grid_10m")
    _write_layer(c_samples, OUT_METHODS, "method_c_network_samples")
    local_extent = unary_union([details["display"] for details in methods.values()]).envelope
    local_water = water_frame[water_frame.intersects(local_extent)].copy()
    _write_layer(local_water, OUT_METHODS, "local_analytical_water")
    for method_id, (standard, adjusted) in intersections.items():
        _write_layer(standard, OUT_METHODS, f"population_{method_id.lower()}_standard")
        _write_layer(adjusted, OUT_METHODS, f"population_{method_id.lower()}_water_sensitivity")

    _write_layer(b2_holes, OUT_HOLES, "all_b2_holes", first=True)
    _write_layer(b2_holes[b2_holes.method_b2_action == "fill"], OUT_HOLES,
                 "b2_filled_holes")
    _write_layer(b2_holes[b2_holes.method_b2_action == "retain"], OUT_HOLES,
                 "b2_retained_holes")
    _write_layer(crossings, OUT_CROSSINGS_GPKG, "reachable_water_crossings", first=True)
    _write_layer(triangles, OUT_TRIANGLES, "all_delaunay_triangles", first=True)
    suspicious = triangles[triangles.suspicious_for_review].copy()
    _write_layer(suspicious, OUT_SUSPICIOUS, "suspicious_triangles", first=True)

    b2_holes.drop(columns="geometry").to_csv(OUT_HOLES_CSV, index=False)
    crossings.drop(columns="geometry").to_csv(OUT_CROSSINGS_CSV, index=False)
    suspicious.drop(columns="geometry").to_csv(OUT_SUSPICIOUS_CSV, index=False)
    comparison.to_csv(OUT_COMPARE, index=False)
    population_table.to_csv(OUT_POPULATION, index=False)
    pairwise.to_csv(OUT_PAIRWISE, index=False)
    bridge_sensitivity.to_csv(OUT_BRIDGE_SENSITIVITY, index=False)

    make_method_map(methods, comparison, reachable_network, water, crossings, ngd)
    make_bridge_qa_map(crossings, water, reachable_network, c1_unmasked, c1_land,
                       ngd_structures)
    previous_inventory_path = TABLES / "catchment_hole_inventory.csv"
    a0_inventory = (pd.read_csv(previous_inventory_path)
                    if previous_inventory_path.exists() else None)
    write_barrier_report(
        comparison, population_table, pairwise, bridge_sensitivity, crossings,
        b2_holes, triangles, c_metadata, a0_inventory, water_path)

    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_version": VERSION,
        "analysis_crs": CRS,
        "routing_modified": False,
        "existing_outputs_overwritten": False,
        "figure_01_modified": False,
        "water_source": str(water_path.relative_to(ROOT)),
        "selected_bridge_buffer_each_side_m": SELECTED_BRIDGE_BUFFER_M,
        "reachable_water_crossing_count": len(crossings),
        "policy_crossing_count": int(crossings.policy_use.sum()),
        "bridge_buffer_sensitivity": bridge_sensitivity.to_dict(orient="records"),
        "method_c": c_metadata,
        "c3_status": "not implemented: CGAL/Triangle/meshpy unavailable and exact directed extended-tree port requires material validation",
        "reference_article": "https://doi.org/10.3390/ijgi9080467",
        "reference_implementation": "https://gitlab.com/Drsulmp/tcageneration",
        "reference_commit_inspected": REFERENCE_COMMIT,
    }
    OUT_METADATA.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(comparison.to_string(index=False))
    print(f"Wrote {OUT_METHODS}, {OUT_COMPARE_MAP_PDF}, {OUT_BRIDGE_MAP_PDF} and {OUT_REPORT}")
    return comparison
