"""Select reproducible site access points from observed public footway crossings."""
import math
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
from shapely import normalize

from .paths import PROCESSED, QA, RAW
from .pedestrian_network import load_reviewed_pedestrian_graph
from .utils import raw_hashes


EXCLUDED_HIGHWAYS = {"service", "motorway", "motorway_link", "trunk", "trunk_link"}
EXCLUDED_ACCESS = {"no", "private"}


def _tag_values(value):
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return set()
    return {str(value).lower()}


def _boundary_side(point, centroid):
    dx, dy = point.x - centroid.x, point.y - centroid.y
    if abs(dx) >= abs(dy):
        return "east" if dx >= 0 else "west"
    return "north" if dy >= 0 else "south"


def _local_tangent(line, point, delta=1.0):
    distance = line.project(point)
    before = line.interpolate(max(0, distance - delta))
    after = line.interpolate(min(line.length, distance + delta))
    return after.x - before.x, after.y - before.y


def _crossing_angle(edge, boundary, point):
    edge_vector = _local_tangent(edge, point)
    boundary_vector = _local_tangent(boundary, point)
    denominator = math.hypot(*edge_vector) * math.hypot(*boundary_vector)
    if denominator == 0:
        return 0.0
    cosine = abs(
        (edge_vector[0] * boundary_vector[0] + edge_vector[1] * boundary_vector[1])
        / denominator
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _intersection_points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [part for part in geometry.geoms if part.geom_type == "Point"]
    return []


def find_best_site_accesses(site_geometry, edges, nodes, contact_tolerance_m,
                            min_crossing_angle_degrees, min_access_separation_m,
                            max_access_points):
    """Select distinct public footways that clearly cross the site boundary."""
    if site_geometry.is_empty or site_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Site boundary must contain a non-empty polygon")
    if edges.crs is None or nodes.crs is None or not edges.crs.is_projected:
        raise ValueError("Network nodes and edges must use a projected CRS")

    boundary = site_geometry.boundary
    centroid = site_geometry.centroid
    nearby = edges[edges.geometry.intersects(site_geometry.buffer(contact_tolerance_m))]
    candidates = []
    seen_geometries = set()

    for edge_index, row in nearby.iterrows():
        highways = _tag_values(row.get("highway"))
        access = _tag_values(row.get("access"))
        if highways & EXCLUDED_HIGHWAYS or access & EXCLUDED_ACCESS:
            continue
        if (row.geometry is None or row.geometry.is_empty
                or row.geometry.geom_type != "LineString"):
            continue
        pedestrian_highways = {"footway", "path", "pedestrian", "steps", "living_street"}
        if not highways & pedestrian_highways:
            continue
        geometry_key = normalize(row.geometry).wkb
        if geometry_key in seen_geometries:
            continue
        seen_geometries.add(geometry_key)

        for boundary_point in _intersection_points(row.geometry.intersection(boundary)):
            angle = _crossing_angle(row.geometry, boundary, boundary_point)
            if angle < min_crossing_angle_degrees:
                continue
            u, v, key = edge_index
            endpoint_options = [(u, nodes.loc[u].geometry), (v, nodes.loc[v].geometry)]
            network_node, node_geometry = min(
                endpoint_options, key=lambda item: item[1].distance(boundary_point)
            )
            candidates.append({
                "side": _boundary_side(boundary_point, centroid),
                "boundary_point": boundary_point,
                "network_point": boundary_point,
                "network_node": network_node,
                "node_geometry": node_geometry,
                "connector_distance_m": float(boundary_point.distance(node_geometry)),
                "crossing_angle_degrees": float(angle),
                "highway": ", ".join(sorted(highways)),
                "foot": row.get("foot"),
                "access": row.get("access"),
                "service": row.get("service"),
                "street_name": row.get("name") if pd.notna(row.get("name")) else None,
                "osmid": row.get("osmid"),
                "edge_u": u,
                "edge_v": v,
                "edge_key": key,
            })

    if not candidates:
        raise RuntimeError("No clear public pedestrian crossing was found at the site boundary")

    candidates.sort(key=lambda item: (item["connector_distance_m"], -item["crossing_angle_degrees"]))
    selected = []
    used_nodes = set()
    for candidate in candidates:
        if candidate["network_node"] in used_nodes:
            continue
        if any(candidate["boundary_point"].distance(item["boundary_point"])
               < min_access_separation_m for item in selected):
            continue
        selected.append(candidate)
        used_nodes.add(candidate["network_node"])
        if len(selected) == max_access_points:
            break
    if not selected:
        raise RuntimeError("Public pedestrian crossings were found but none passed separation checks")
    return sorted(selected, key=lambda item: item["boundary_point"].x)


def build_site_origins(cfg, output_path=None):
    """Save the two reviewed site-boundary access points without modifying the graph."""
    output_path = Path(output_path or PROCESSED / "site_origins.gpkg")
    qa_path = QA / "site_origin_selection.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_before = raw_hashes()

    graph = load_reviewed_pedestrian_graph()
    nodes, edges = ox.graph_to_gdfs(graph)
    site = gpd.read_file(RAW / "site_boundary.geojson", engine="pyogrio")
    if site.empty or site.crs is None:
        raise ValueError("Site boundary is missing or has no CRS")
    site = site.to_crs(graph.graph["crs"])
    site_geometry = site.geometry.union_all()
    origin_cfg = cfg["site_origins"]
    chosen = find_best_site_accesses(
        site_geometry, edges, nodes, origin_cfg["contact_tolerance_m"],
        origin_cfg["min_crossing_angle_degrees"], origin_cfg["min_access_separation_m"],
        origin_cfg["max_access_points"],
    )

    records = []
    for index, candidate in enumerate(chosen, start=1):
        node_geometry = candidate["node_geometry"]
        boundary_point = candidate["boundary_point"]
        access_node = -1_000_000_000 - index
        records.append({
            "origin_id": chr(64 + index),
            "access_name": "north_west_access" if index == 1 else "north_access",
            "status": "selected_existing_network_contact",
            "selection_reason": "public footway crosses site boundary at a clear angle",
            "site_side": candidate["side"],
            "site_access_node": str(access_node),
            "matched_edge_u": str(candidate["edge_u"]),
            "matched_edge_v": str(candidate["edge_v"]),
            "matched_edge_key": candidate["edge_key"],
            "osmid": candidate["osmid"],
            "highway": candidate["highway"],
            "foot": candidate["foot"],
            "access": candidate["access"],
            "service": candidate["service"],
            "street_name": candidate["street_name"],
            "point_to_edge_distance_m": 0.0,
            "nearest_endpoint_distance_m": float(boundary_point.distance(node_geometry)),
            "crossing_angle_degrees": candidate["crossing_angle_degrees"],
            "site_access_easting": boundary_point.x,
            "site_access_northing": boundary_point.y,
            "geometry": boundary_point,
        })
    site_access_points = gpd.GeoDataFrame(records, geometry="geometry", crs=site.crs)

    if output_path.exists():
        output_path.unlink()
    site_access_points.to_file(
        output_path, layer="site_access_points", driver="GPKG", engine="pyogrio"
    )
    site_access_points.drop(columns="geometry").to_csv(qa_path, index=False)
    if raw_hashes() != hashes_before:
        raise RuntimeError("Original files changed while selecting site origins")
    return {
        "origin_count": len(site_access_points),
        "sides": site_access_points["site_side"].tolist(),
        "max_point_to_edge_distance_m": float(site_access_points["point_to_edge_distance_m"].max()),
        "output": str(output_path),
        "summary": str(qa_path),
    }
