"""Apply only user-approved pedestrian connections to a copy of the base graph."""
from hashlib import sha256

import networkx as nx
import osmnx as ox
import pandas as pd
from shapely import LineString, Point
from shapely.ops import substring

from .paths import (
    BASE_PEDESTRIAN_GRAPH, OPENSTREETMAP, OUTPUTS, REVIEWED_PEDESTRIAN_GRAPH,
)
from .pedestrian_network import add_walking_costs, load_pedestrian_graph, validate_pedestrian_graph
from .walking_catchment import _edge_geometry, _matching_directed_edges


APPROVED_CONNECTIONS = [
    {
        "change_id": "NC-001",
        "source_node": 11073286329,
        "source_osm_way": "1192930895",
        "target_u": 11104322032,
        "target_v": 13238126354,
        "target_key": 0,
        "target_osm_way": "1196724993",
        "evidence": "User manually confirmed the pedestrian and footway features are connected.",
    },
    {
        "change_id": "NC-002",
        "source_node": 11104322032,
        "source_osm_way": "1196724993",
        "target_u": 13238126361,
        "target_v": 13238126363,
        "target_key": 0,
        "target_osm_way": "1442719672 | 1442719670",
        "evidence": "User manually confirmed the footway connects to the elevated footway system.",
    },
    {
        "change_id": "NC-003",
        "source_node": 663356609,
        "source_osm_way": "272697652",
        "target_u": 229785102,
        "target_v": 675222762,
        "target_key": 0,
        "target_osm_way": "1052186848",
        "evidence": "User manually confirmed the footway and adjacent service way are connected.",
    },
]


def _split_target_edges(graph, u, v, point, junction_node, speed_mps, tolerance_m=0.05):
    matches = _matching_directed_edges(graph, u, v, point, tolerance_m)
    if not matches:
        raise RuntimeError(f"No directed target edge contains approved connection point {u}-{v}")
    split_count = 0
    for a, b, key, data, geometry in matches:
        split_distance = float(geometry.project(point))
        if not tolerance_m < split_distance < geometry.length - tolerance_m:
            raise RuntimeError(f"Approved connection point lies at target edge endpoint {a}-{b}-{key}")
        first = substring(geometry, 0, split_distance)
        second = substring(geometry, split_distance, geometry.length)
        if first.geom_type != "LineString" or second.geom_type != "LineString":
            raise RuntimeError("Target edge split did not create two LineStrings")
        graph.remove_edge(a, b, key)
        for start, end, part in ((a, junction_node, first), (junction_node, b, second)):
            attrs = data.copy()
            attrs["geometry"] = part
            attrs["length"] = float(part.length)
            attrs["travel_time_s"] = float(part.length) / speed_mps
            attrs["manual_split"] = "yes"
            graph.add_edge(start, end, **attrs)
        split_count += 1
    return split_count


def apply_approved_network_changes(cfg):
    """Create a reviewed graph while leaving the downloaded base GraphML unchanged."""
    if not BASE_PEDESTRIAN_GRAPH.exists():
        raise FileNotFoundError(f"Base pedestrian graph not found: {BASE_PEDESTRIAN_GRAPH}")
    base_hash = sha256(BASE_PEDESTRIAN_GRAPH.read_bytes()).hexdigest()
    graph = load_pedestrian_graph(BASE_PEDESTRIAN_GRAPH)
    initial_components = nx.number_weakly_connected_components(graph)
    speed_mps = float(cfg["walking"]["speed_kmh"]) * 1000 / 3600
    change_rows = []

    for number, change in enumerate(APPROVED_CONNECTIONS, start=1):
        source = int(change["source_node"])
        u = int(change["target_u"])
        v = int(change["target_v"])
        key = int(change["target_key"])
        if source not in graph or not graph.has_edge(u, v, key):
            raise RuntimeError(f"Approved change {change['change_id']} does not match the base graph")
        if nx.node_connected_component(graph.to_undirected(as_view=True), source) == \
                nx.node_connected_component(graph.to_undirected(as_view=True), u):
            raise RuntimeError(f"Approved change {change['change_id']} is already topologically connected")

        target_data = graph.get_edge_data(u, v, key)
        target_geometry = _edge_geometry(graph, u, v, target_data)
        source_point = Point(float(graph.nodes[source]["x"]), float(graph.nodes[source]["y"]))
        connection_point = target_geometry.interpolate(target_geometry.project(source_point))
        connector_length = float(source_point.distance(connection_point))
        if not 0 < connector_length <= 5.0:
            raise RuntimeError(
                f"Approved change {change['change_id']} connector is {connector_length:.2f} m; expected <=5 m"
            )

        junction_node = -2_000_000_000 - number
        graph.add_node(
            junction_node, x=float(connection_point.x), y=float(connection_point.y),
            manual_connection_id=change["change_id"],
        )
        split_count = _split_target_edges(
            graph, u, v, connection_point, junction_node, speed_mps
        )
        forward = LineString([source_point, connection_point])
        reverse = LineString(list(forward.coords)[::-1])
        connector_attributes = {
            "osmid": -3_000_000_000 - number, "highway": "footway",
            "foot": "yes", "access": "yes", "manual_connection": "yes",
            "manual_connection_id": change["change_id"], "length": connector_length,
            "travel_time_s": connector_length / speed_mps,
        }
        graph.add_edge(source, junction_node, geometry=forward, **connector_attributes)
        graph.add_edge(junction_node, source, geometry=reverse, **connector_attributes)
        change_rows.append({
            "change_id": change["change_id"], "status": "approved_applied",
            "element_type": "manual_pedestrian_connection",
            "element_id": f"{source} to {u}-{v}-{key}", "change_type": "connect_components",
            "source_node": source, "source_osm_way": change["source_osm_way"],
            "target_edge": f"{u}-{v}-{key}", "target_osm_way": change["target_osm_way"],
            "connector_length_m": connector_length, "inserted_junction_node": junction_node,
            "directed_target_edges_split": split_count,
            "original_value": "separate weak components",
            "proposed_value": "bidirectional footway connector",
            "evidence": change["evidence"], "approved_by": "user_manual_review",
            "approval_date": "2026-10-10",
        })

    add_walking_costs(graph, cfg["walking"]["speed_kmh"])
    final_components = nx.number_weakly_connected_components(graph)
    if initial_components - final_components != len(APPROVED_CONNECTIONS):
        raise RuntimeError(
            f"Expected {len(APPROVED_CONNECTIONS)} component merges, observed "
            f"{initial_components - final_components}"
        )
    graph.graph["source_graph_sha256"] = base_hash
    graph.graph["manual_review_status"] = "three_user_confirmed_connections_applied"
    graph.graph["approved_network_change_count"] = str(len(APPROVED_CONNECTIONS))
    validate_pedestrian_graph(graph, cfg["project"]["analysis_crs"])

    REVIEWED_PEDESTRIAN_GRAPH.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, REVIEWED_PEDESTRIAN_GRAPH)
    reviewed_gpkg = OPENSTREETMAP / "pedestrian_network_reviewed.gpkg"
    for path in (reviewed_gpkg, reviewed_gpkg.with_name(reviewed_gpkg.name + "-shm"),
                 reviewed_gpkg.with_name(reviewed_gpkg.name + "-wal")):
        path.unlink(missing_ok=True)
    ox.save_graph_geopackage(graph, reviewed_gpkg, directed=True)
    changes_path = OUTPUTS / "tables" / "network_changes.csv"
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    changes = pd.DataFrame(change_rows)
    changes.to_csv(changes_path, index=False)
    if sha256(BASE_PEDESTRIAN_GRAPH.read_bytes()).hexdigest() != base_hash:
        raise RuntimeError("Base pedestrian GraphML changed while applying reviewed connections")
    return {
        "changes": changes, "initial_components": initial_components,
        "final_components": final_components, "reviewed_graph": str(REVIEWED_PEDESTRIAN_GRAPH),
        "reviewed_gpkg": str(reviewed_gpkg), "changes_output": str(changes_path),
        "base_sha256": base_hash,
    }
