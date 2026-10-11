"""Insert reviewed origins into OSM edges and build network walking catchments."""
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from matplotlib.lines import Line2D
from shapely import LineString
from shapely.ops import substring, unary_union

from .paths import OPENSTREETMAP, OUTPUTS, PROCESSED, RAW
from .pedestrian_network import (
    load_pedestrian_graph, load_reviewed_pedestrian_graph, validate_pedestrian_graph,
)
from .utils import raw_hashes


PEDESTRIAN_HIGHWAYS = {"footway", "path", "pedestrian", "steps", "living_street"}
BLOCKED_ACCESS = {"no", "private"}


def _tag_values(value):
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return set()
    return {str(value).lower()}


def _display_value(value):
    if isinstance(value, (list, tuple, set)):
        return " | ".join(str(item) for item in value)
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return value


def _edge_geometry(graph, u, v, data):
    geometry = data.get("geometry")
    if geometry is None:
        geometry = LineString([
            (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"])),
            (float(graph.nodes[v]["x"]), float(graph.nodes[v]["y"])),
        ])
    if geometry.geom_type != "LineString":
        raise RuntimeError("Routing edge geometry must be a LineString")
    start = geometry.coords[0]
    end = geometry.coords[-1]
    u_point = (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"]))
    if np.hypot(start[0] - u_point[0], start[1] - u_point[1]) > np.hypot(
            end[0] - u_point[0], end[1] - u_point[1]):
        geometry = LineString(list(geometry.coords)[::-1])
    return geometry


def recalculate_edge_costs(graph, speed_kmh):
    """Use projected geometries to calculate positive metric lengths and times."""
    speed_mps = float(speed_kmh) * 1000 / 3600
    for u, v, _, data in graph.edges(keys=True, data=True):
        geometry = _edge_geometry(graph, u, v, data)
        length = float(geometry.length)
        if not np.isfinite(length) or length <= 0:
            raise RuntimeError(f"Edge {u}->{v} has non-positive projected length")
        data["geometry"] = geometry
        data["length"] = length
        data["travel_time_s"] = length / speed_mps
    return graph


def _matching_directed_edges(graph, u, v, point, tolerance_m):
    matches = []
    for a, b in [(u, v), (v, u)]:
        edge_bundle = graph.get_edge_data(a, b, default={})
        for key, data in edge_bundle.items():
            geometry = _edge_geometry(graph, a, b, data)
            if geometry.distance(point) <= tolerance_m:
                matches.append((a, b, key, data.copy(), geometry))
    return matches


def insert_origins_into_edges(graph, origins, speed_kmh, tolerance_m=0.05):
    """Insert every origin into its matched physical edge without connector edges."""
    graph = graph.copy()
    speed_mps = float(speed_kmh) * 1000 / 3600
    qa_rows = []

    for _, origin in origins.sort_values("origin_id").iterrows():
        origin_id = str(origin["origin_id"])
        node_id = int(origin["site_access_node"])
        u = int(origin["matched_edge_u"])
        v = int(origin["matched_edge_v"])
        point = origin.geometry
        matches = _matching_directed_edges(graph, u, v, point, tolerance_m)
        if not matches:
            raise RuntimeError(f"Origin {origin_id} does not lie on its recorded OSM edge")

        representative = matches[0]
        rep_data = representative[3]
        highways = _tag_values(rep_data.get("highway"))
        access = _tag_values(rep_data.get("access"))
        pedestrian_accessible = bool(highways & PEDESTRIAN_HIGHWAYS and not access & BLOCKED_ACCESS)
        if not pedestrian_accessible:
            raise RuntimeError(f"Origin {origin_id} matched edge is not pedestrian accessible")

        graph.add_node(
            node_id, x=float(point.x), y=float(point.y), osmid=node_id,
            origin_id=origin_id, access_name=str(origin["access_name"]),
        )
        split_details = []
        for a, b, key, data, geometry in matches:
            split_distance = float(geometry.project(point))
            if not tolerance_m < split_distance < geometry.length - tolerance_m:
                raise RuntimeError(f"Origin {origin_id} is too close to an existing edge endpoint")
            first = substring(geometry, 0, split_distance)
            second = substring(geometry, split_distance, geometry.length)
            if first.geom_type != "LineString" or second.geom_type != "LineString":
                raise RuntimeError(f"Origin {origin_id} did not split its edge into two lines")

            graph.remove_edge(a, b, key)
            first_data = data.copy()
            second_data = data.copy()
            for attrs, part in [(first_data, first), (second_data, second)]:
                attrs["geometry"] = part
                attrs["length"] = float(part.length)
                attrs["travel_time_s"] = float(part.length) / speed_mps
                attrs["split_origin_id"] = origin_id
            graph.add_edge(a, node_id, **first_data)
            graph.add_edge(node_id, b, **second_data)
            split_details.append((geometry.length, first.length + second.length))

        original_length = float(split_details[0][0])
        split_length = float(split_details[0][1])
        point_distance = float(representative[4].distance(point))
        qa_rows.append({
            "origin_id": origin_id,
            "inserted_node": node_id,
            "matched_u": u,
            "matched_v": v,
            "matched_key": int(origin["matched_edge_key"]),
            "point_to_edge_distance_m": point_distance,
            "highway": _display_value(rep_data.get("highway")),
            "foot": _display_value(rep_data.get("foot")),
            "access": _display_value(rep_data.get("access")),
            "service": _display_value(rep_data.get("service")),
            "name": _display_value(rep_data.get("name")),
            "osmid": _display_value(rep_data.get("osmid")),
            "pedestrian_accessible": pedestrian_accessible,
            "directed_edges_split": len(matches),
            "original_edge_length_m": original_length,
            "split_parts_length_m": split_length,
            "length_difference_m": abs(original_length - split_length),
            "status": "pass" if abs(original_length - split_length) <= 0.001 else "review",
            "warning": "" if abs(original_length - split_length) <= 0.001 else "Split lengths do not sum to original length",
        })

    recalculate_edge_costs(graph, speed_kmh)
    if any(str(data.get("highway")) == "site_connector" for *_, data in graph.edges(data=True)):
        raise RuntimeError("Artificial site connector edges remain in the routing graph")
    return graph, pd.DataFrame(qa_rows)


def _line_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        parts = []
        for item in geometry.geoms:
            parts.extend(_line_parts(item))
        return parts
    return []


def reachable_network(graph, travel_times, threshold_s, speed_kmh):
    """Return full and truncated reachable edge geometry for one time threshold."""
    speed_mps = float(speed_kmh) * 1000 / 3600
    segments = []
    partial_count = 0
    for u, v, _, data in graph.edges(keys=True, data=True):
        start_time = travel_times.get(u)
        if start_time is None or start_time >= threshold_s:
            continue
        geometry = _edge_geometry(graph, u, v, data)
        reachable_length = min(float(data["length"]), (threshold_s - start_time) * speed_mps)
        if reachable_length <= 0:
            continue
        if reachable_length < float(data["length"]) - 1e-6:
            partial_count += 1
        part = substring(geometry, 0, min(reachable_length, geometry.length))
        if part.geom_type == "LineString" and part.length > 0:
            segments.append(part)
    if not segments:
        raise RuntimeError(f"No network geometry is reachable within {threshold_s} seconds")
    geometry = unary_union(segments)
    return geometry, partial_count


def _network_frame(geometry, minutes, crs):
    parts = _line_parts(geometry)
    return gpd.GeoDataFrame(
        [{"time_minutes": minutes, "part_id": index + 1, "length_m": part.length,
          "geometry": part} for index, part in enumerate(parts)],
        geometry="geometry", crs=crs,
    )


def _polygon_frame(geometry, minutes, crs):
    parts = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return gpd.GeoDataFrame(
        [{"time_minutes": minutes, "part_id": index + 1, "area_m2": part.area,
          "geometry": part} for index, part in enumerate(parts)],
        geometry="geometry", crs=crs,
    )


def _write_gpkg(frame, path, layer):
    path = Path(path)
    if path.exists():
        path.unlink()
    frame.to_file(path, layer=layer, driver="GPKG", engine="pyogrio")


def _plot_qa(site, origins, full_edges, networks, polygons, output_path):
    bounds = polygons[max(polygons)].bounds
    margin = 150
    local = full_edges.cx[bounds[0] - margin:bounds[2] + margin,
                          bounds[1] - margin:bounds[3] + margin]
    fig, ax = plt.subplots(figsize=(10, 10))
    local.plot(ax=ax, color="#d0d0d0", linewidth=0.35)
    colors = {15: "#7b6fd0", 10: "#7a3db8", 5: "#d73027"}
    for minutes in [15, 10, 5]:
        gpd.GeoSeries([polygons[minutes]], crs=origins.crs).plot(
            ax=ax, color=colors[minutes], alpha=0.06, edgecolor="none"
        )
        _network_frame(networks[minutes], minutes, origins.crs).plot(
            ax=ax, color=colors[minutes], linewidth=1.1
        )
    site.boundary.plot(ax=ax, color="#1675b6", linewidth=2)
    origins.plot(ax=ax, color="#ffd92f", edgecolor="black", markersize=75, zorder=6)
    for _, row in origins.iterrows():
        ax.annotate(str(row["origin_id"]), (row.geometry.x, row.geometry.y),
                    xytext=(5, 5), textcoords="offset points", weight="bold")
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title("Walking catchment QA: network reach from reviewed site origins")
    ax.legend(handles=[
        Line2D([0], [0], color=colors[5], lw=2, label="5 minutes"),
        Line2D([0], [0], color=colors[10], lw=2, label="10 minutes (primary)"),
        Line2D([0], [0], color=colors[15], lw=2, label="15 minutes"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#ffd92f",
               markeredgecolor="black", label="Origins A/B"),
    ], loc="lower left")
    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def run_walking_catchments(cfg):
    """Create 5, 10, and 15 minute authoritative networks and buffered polygons."""
    spatial = OUTPUTS / "spatial"
    tables = OUTPUTS / "tables"
    figures = OUTPUTS / "figures"
    for folder in [spatial, tables, figures, PROCESSED]:
        folder.mkdir(parents=True, exist_ok=True)
    hashes_before = raw_hashes()

    graph = load_reviewed_pedestrian_graph()
    origins = gpd.read_file(PROCESSED / "site_origins.gpkg", layer="site_access_points")
    analysis_crs = cfg["project"]["analysis_crs"]
    if origins.crs is None or str(origins.crs) != analysis_crs:
        origins = origins.to_crs(analysis_crs)
    if len(origins) != 2 or set(origins["origin_id"].astype(str)) != {"A", "B"}:
        raise RuntimeError("Exactly the reviewed origins A and B are required")

    graph, origin_qa = insert_origins_into_edges(graph, origins, cfg["walking"]["speed_kmh"])
    validate_pedestrian_graph(graph, analysis_crs)
    sources = [int(value) for value in origins["site_access_node"]]
    for source in sources:
        if source not in graph or len(nx.descendants(graph, source)) == 0:
            raise RuntimeError(f"Inserted origin node {source} is not connected to the graph")
    origin_qa["reachable_node_count"] = [len(nx.descendants(graph, source)) + 1 for source in sources]
    origin_qa.to_csv(tables / "origin_network_qa.csv", index=False)

    routing_graph_path = OPENSTREETMAP / "pedestrian_network_reviewed_with_origins.graphml"
    ox.save_graphml(graph, routing_graph_path)
    reloaded = load_pedestrian_graph(routing_graph_path)
    validate_pedestrian_graph(reloaded, analysis_crs)
    origins_output = origins.copy()
    origins_output["inserted_in_graph"] = True
    _write_gpkg(origins_output, spatial / "origins_on_network.gpkg", "origins_on_network")

    travel_times = nx.multi_source_dijkstra_path_length(
        graph, sources, weight="travel_time_s"
    )
    networks = {}
    polygons = {}
    summary_rows = []
    for minutes in cfg["walking"]["sensitivity_minutes"]:
        threshold_s = minutes * 60
        network_geometry, partial_count = reachable_network(
            graph, travel_times, threshold_s, cfg["walking"]["speed_kmh"]
        )
        polygon = network_geometry.buffer(cfg["walking"]["edge_buffer_m"])
        networks[minutes] = network_geometry
        polygons[minutes] = polygon
        _write_gpkg(
            _network_frame(network_geometry, minutes, analysis_crs),
            spatial / f"reachable_network_{minutes}min.gpkg", f"reachable_network_{minutes}min",
        )
        _write_gpkg(
            _polygon_frame(polygon, minutes, analysis_crs),
            spatial / f"walking_catchment_{minutes}min.gpkg", f"walking_catchment_{minutes}min",
        )
        summary_rows.append({
            "time_minutes": minutes,
            "reachable_network_length_m": float(network_geometry.length),
            "catchment_area_m2": float(polygon.area),
            "catchment_area_ha": float(polygon.area / 10_000),
            "number_of_origins": len(origins),
            "walking_speed_kmh": cfg["walking"]["speed_kmh"],
            "partial_directed_edge_count": partial_count,
            "polygon_buffer_m": cfg["walking"]["edge_buffer_m"],
            "polygon_note": "Cartographic network buffer; authoritative result is reachable network",
        })

    ordered_minutes = sorted(networks)
    for smaller, larger in zip(ordered_minutes, ordered_minutes[1:]):
        outside_length = networks[smaller].difference(networks[larger].buffer(0.001)).length
        if outside_length > 0.01:
            raise RuntimeError(f"{smaller}-minute network is not contained in {larger}-minute network")
        if polygons[smaller].area > polygons[larger].area + 0.01:
            raise RuntimeError("Catchment polygon areas are not monotonic")

    summary = pd.DataFrame(summary_rows).sort_values("time_minutes")
    summary["network_containment_status"] = "pass"
    summary["area_monotonic_status"] = "pass"
    summary["water_mask_status"] = "warning_not_applied"
    summary.to_csv(tables / "walking_catchment_summary.csv", index=False)
    full_edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)
    site = gpd.read_file(RAW / "site_boundary.geojson").to_crs(analysis_crs)
    _plot_qa(
        site, origins, full_edges, networks, polygons,
        figures / "walking_catchment_qa.png",
    )

    if raw_hashes() != hashes_before:
        raise RuntimeError("Original files changed during walking catchment analysis")
    return {
        "origin_qa": origin_qa,
        "summary": summary,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "routing_graph": str(routing_graph_path),
        "qa_map": str(figures / "walking_catchment_qa.png"),
        "warning": "Buffered polygons are cartographic representations and are not water-masked",
    }
