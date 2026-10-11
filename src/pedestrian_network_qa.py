"""Read-only QA for the saved OpenStreetMap pedestrian routing graph."""
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
import re
import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from pyproj import CRS
from shapely import LineString, box, normalize
from shapely.ops import unary_union

from .paths import BASE_PEDESTRIAN_GRAPH, OPENSTREETMAP, OUTPUTS, PROCESSED, QA, RAW
from .pedestrian_network import load_pedestrian_graph
from .walking_catchment import insert_origins_into_edges


LOCAL_REVIEW_RADIUS_M = 1000.0
NEAR_SITE_RADIUS_M = 250.0
GAP_TOLERANCE_M = 5.0
ENDPOINT_TOLERANCE_M = 0.5
LENGTH_ABSOLUTE_TOLERANCE_M = 5.0
LENGTH_RELATIVE_TOLERANCE = 0.10
RESTRICTED_VALUES = {"no", "private"}
UNCONDITIONAL_FOOT_OVERRIDES = {"yes", "designated", "official"}
CONDITIONAL_VALUES = {"customers", "permit", "delivery", "unknown", "permissive", "destination"}
HIGH_SPEED_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link"}

ISSUE_COLUMNS = [
    "issue_id", "severity", "issue_type", "location", "element_type",
    "element_id", "osmid", "evidence", "possible_effect",
    "recommended_treatment", "manual_review_required", "geometry",
]
CHANGE_COLUMNS = [
    "change_id", "status", "element_type", "element_id", "change_type",
    "original_value", "proposed_value", "evidence", "approved_by", "approval_date",
]


def _tag_values(value):
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    if value is None or (isinstance(value, str) and not value.strip()) or (
            not isinstance(value, str) and pd.isna(value)):
        return set()
    return {item.strip().lower() for item in str(value).split("|") if item.strip()}


def _display(value):
    if isinstance(value, (list, tuple, set)):
        return " | ".join(str(item) for item in value)
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value)


def classify_pedestrian_access(access, foot, highway):
    """Classify saved OSM access tags, respecting mode-specific foot overrides."""
    access_values = _tag_values(access)
    foot_values = _tag_values(foot)
    highway_values = _tag_values(highway)
    if foot_values & RESTRICTED_VALUES:
        return "restricted_pedestrian_access"
    if access_values & RESTRICTED_VALUES:
        if foot_values & UNCONDITIONAL_FOOT_OVERRIDES:
            return None
        if foot_values & CONDITIONAL_VALUES:
            return "conditional_pedestrian_access"
        return "restricted_pedestrian_access"
    if access_values & CONDITIONAL_VALUES or foot_values & CONDITIONAL_VALUES:
        return "conditional_pedestrian_access"
    if highway_values & HIGH_SPEED_HIGHWAYS and not foot_values:
        return "high_speed_road_without_foot_tag"
    return None


def _edge_geometry(graph, u, v, data):
    geometry = data.get("geometry")
    if geometry is not None:
        return geometry, True
    return LineString([
        (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"])),
        (float(graph.nodes[v]["x"]), float(graph.nodes[v]["y"])),
    ]), False


def _location(geometry, site_geometry):
    point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
    distance = float(geometry.distance(site_geometry))
    return f"E {point.x:.1f}, N {point.y:.1f}; {distance:.1f} m from site"


def _component_lookup(graph):
    weak_components = sorted(nx.weakly_connected_components(graph), key=len, reverse=True)
    strong_components = sorted(nx.strongly_connected_components(graph), key=len, reverse=True)
    component_of = {node: index for index, members in enumerate(weak_components) for node in members}
    return weak_components, strong_components, component_of


def physical_dead_end_nodes(graph):
    """Return nodes with exactly one distinct neighbour, ignoring edge direction."""
    return {
        node for node in graph.nodes
        if len(set(graph.predecessors(node)) | set(graph.successors(node))) == 1
    }


def edge_geometry_metrics(graph):
    """Compare stored edge lengths and endpoints with projected edge geometry."""
    records = []
    for u, v, key, data in graph.edges(keys=True, data=True):
        geometry, explicit = _edge_geometry(graph, u, v, data)
        stored = float(data.get("length", np.nan))
        geometry_length = float(geometry.length)
        absolute = abs(stored - geometry_length) if np.isfinite(stored) else np.nan
        relative = absolute / geometry_length if geometry_length > 0 and np.isfinite(absolute) else np.nan
        start = geometry.coords[0]
        end = geometry.coords[-1]
        node_u = (float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"]))
        node_v = (float(graph.nodes[v]["x"]), float(graph.nodes[v]["y"]))
        direct = max(np.hypot(start[0] - node_u[0], start[1] - node_u[1]),
                     np.hypot(end[0] - node_v[0], end[1] - node_v[1]))
        reverse = max(np.hypot(end[0] - node_u[0], end[1] - node_u[1]),
                      np.hypot(start[0] - node_v[0], start[1] - node_v[1]))
        records.append({
            "u": u, "v": v, "key": key, "osmid": _display(data.get("osmid")),
            "explicit_geometry": explicit, "geometry_valid": bool(geometry.is_valid),
            "stored_length_m": stored, "geometry_length_m": geometry_length,
            "absolute_difference_m": absolute, "relative_difference": relative,
            "endpoint_mismatch_m": float(min(direct, reverse)), "geometry": geometry,
        })
    return gpd.GeoDataFrame(records, geometry="geometry", crs=graph.graph["crs"])


def _physical_edges(edge_metrics, graph):
    """Collapse reciprocal directed edges while retaining every graph edge identifier."""
    groups = defaultdict(list)
    for row in edge_metrics.itertuples(index=False):
        signature = sha256(normalize(row.geometry).wkb).hexdigest()
        groups[(min(row.u, row.v), max(row.u, row.v), signature)].append(row)

    records = []
    for rows in groups.values():
        first = rows[0]
        edge_data = [graph.get_edge_data(row.u, row.v, row.key) for row in rows]

        def combined(attribute):
            values = []
            for data in edge_data:
                raw = data.get(attribute)
                items = raw if isinstance(raw, (list, tuple, set)) else [raw]
                for item in items:
                    text = _display(item)
                    if text and text not in values:
                        values.append(text)
            return " | ".join(values)

        records.append({
            "u": first.u, "v": first.v, "key": first.key,
            "directed_edge_ids": "; ".join(f"{row.u}-{row.v}-{row.key}" for row in rows),
            "directed_edge_count": len(rows), "osmid": combined("osmid"),
            "highway": combined("highway"), "foot": combined("foot"),
            "access": combined("access"), "service": combined("service"),
            "bridge": combined("bridge"), "tunnel": combined("tunnel"),
            "layer": combined("layer"), "geometry": first.geometry,
        })
    return gpd.GeoDataFrame(records, geometry="geometry", crs=edge_metrics.crs)


def _summary_row(section, check, value, unit="", status="pass", severity="", notes=""):
    return {
        "section": section, "check": check, "value": value, "unit": unit,
        "status": status, "severity": severity, "notes": notes,
    }


def _issue(severity, issue_type, geometry, site, element_type, element_id, osmid,
           evidence, effect, treatment, manual=True):
    return {
        "issue_id": "", "severity": severity, "issue_type": issue_type,
        "location": "entire graph extent" if element_type == "graph" else _location(geometry, site),
        "element_type": element_type,
        "element_id": str(element_id), "osmid": _display(osmid), "evidence": evidence,
        "possible_effect": effect, "recommended_treatment": treatment,
        "manual_review_required": bool(manual), "geometry": geometry,
    }


def _tag_distribution(graph, name):
    counts = Counter()
    for *_, data in graph.edges(keys=True, data=True):
        value = data.get(name)
        if isinstance(value, (list, tuple, set)):
            counts.update(str(item) for item in value)
        elif value is None or (not isinstance(value, str) and pd.isna(value)):
            counts["<missing>"] += 1
        else:
            counts[str(value)] += 1
    return counts


def _local_gap_issues(graph, nodes, edges, component_of, site):
    local_edges = edges[edges.intersects(site.buffer(LOCAL_REVIEW_RADIUS_M))].copy()
    local_edges["component_id"] = [component_of[u] for u, _, _ in local_edges.index]
    dead_ends = physical_dead_end_nodes(graph)
    local_dead_ends = [
        node for node in dead_ends
        if nodes.loc[node].geometry.distance(site) <= LOCAL_REVIEW_RADIUS_M
    ]
    issues = []
    for node in local_dead_ends:
        point = nodes.loc[node].geometry
        candidates = local_edges.iloc[list(local_edges.sindex.query(point.buffer(GAP_TOLERANCE_M),
                                                                    predicate="intersects"))]
        matches = []
        for (u, v, key), edge in candidates.iterrows():
            if node in (u, v) or edge["component_id"] == component_of[node]:
                continue
            distance = float(point.distance(edge.geometry))
            if distance <= GAP_TOLERANCE_M:
                matches.append((distance, u, v, key, edge))
        if not matches:
            continue
        distance, u, v, key, edge = min(matches, key=lambda item: item[0])
        severity = "HIGH" if point.distance(site) <= NEAR_SITE_RADIUS_M else "MEDIUM"
        evidence = (
            f"Dead-end node is {distance:.2f} m from edge {u}-{v}-{key} in a different "
            f"weak component; components {component_of[node]} and {int(edge['component_id'])}."
        )
        issues.append(_issue(
            severity, "possible_network_gap_at_dead_end", point, site, "node", node,
            graph.nodes[node].get("osmid", node), evidence,
            "A genuine at-grade connection would be absent from routing and could shorten the catchment.",
            "Review OSM, aerial/street evidence and vertical separation; connect only after approval.",
        ))

    reset = local_edges.reset_index()
    joined = gpd.sjoin(
        reset[["u", "v", "key", "component_id", "geometry"]],
        reset[["u", "v", "key", "component_id", "geometry"]],
        predicate="intersects", how="inner", lsuffix="left", rsuffix="right",
    )
    joined = joined[(joined["component_id_left"] != joined["component_id_right"])
                    & (joined.index < joined["index_right"])]
    seen = set()
    for left_index, pair in joined.iterrows():
        left = reset.loc[left_index]
        right = reset.loc[pair["index_right"]]
        intersection = left.geometry.intersection(right.geometry)
        if intersection.geom_type != "Point":
            continue
        key = (round(intersection.x, 2), round(intersection.y, 2),
               *sorted((int(left.component_id), int(right.component_id))))
        if key in seen:
            continue
        seen.add(key)
        severity = "HIGH" if intersection.distance(site) <= NEAR_SITE_RADIUS_M else "MEDIUM"
        evidence = (
            f"Edges {left.u}-{left.v}-{left.key} and {right.u}-{right.v}-{right.key} "
            f"intersect in 2D but belong to weak components {left.component_id} and "
            f"{right.component_id}; no shared graph node."
        )
        issues.append(_issue(
            severity, "two_dimensional_crossing_without_shared_node", intersection, site,
            "edge_pair", f"{left.u}-{left.v}-{left.key} | {right.u}-{right.v}-{right.key}",
            "", evidence,
            "If the crossing is at grade, routing omits a connection; if grade separated, no change is needed.",
            "Manually check bridge, tunnel, level and imagery evidence; do not connect from 2D geometry alone.",
        ))
    return issues, local_edges, local_dead_ends


def _plot_local_qa(site_frame, origins, local_edges, edge_issues, node_issues, output_path):
    site = site_frame.geometry.union_all()
    bounds = site.buffer(LOCAL_REVIEW_RADIUS_M).bounds
    fig, ax = plt.subplots(figsize=(11, 11))
    main = local_edges[local_edges["component_id"] == 0]
    disconnected = local_edges[local_edges["component_id"] != 0]
    main.plot(ax=ax, color="#b8b8b8", linewidth=0.45, zorder=1)
    if not disconnected.empty:
        disconnected.plot(ax=ax, color="#f28e2b", linewidth=1.0, zorder=2)

    def plot_edge_type(issue_type, color, style="-", width=1.6):
        subset = edge_issues[edge_issues["issue_type"] == issue_type]
        if not subset.empty:
            subset.plot(ax=ax, color=color, linewidth=width, linestyle=style, zorder=4)

    plot_edge_type("restricted_pedestrian_access", "#d62728", "-", 2.0)
    plot_edge_type("conditional_pedestrian_access", "#e377c2", "-", 1.5)
    plot_edge_type("high_speed_road_without_foot_tag", "#8c564b", "-.", 1.5)
    bridge = local_edges[local_edges["bridge"].notna()]
    tunnel = local_edges[local_edges["tunnel"].notna()]
    if not bridge.empty:
        bridge.plot(ax=ax, color="#6a3d9a", linewidth=1.5, linestyle="--", zorder=3)
    if not tunnel.empty:
        tunnel.plot(ax=ax, color="#17becf", linewidth=1.5, linestyle=":", zorder=3)
    if not node_issues.empty:
        node_issues.plot(ax=ax, color="#ff1493", marker="x", markersize=38, linewidth=1.2, zorder=5)
    site_frame.plot(ax=ax, facecolor="#1f77b4", alpha=0.14, edgecolor="#005a9c", linewidth=2.2, zorder=6)
    if origins is not None and not origins.empty:
        origins.plot(ax=ax, color="#ffd92f", edgecolor="black", markersize=75, zorder=7)
        for _, row in origins.iterrows():
            ax.annotate(str(row["origin_id"]), (row.geometry.x, row.geometry.y),
                        xytext=(5, 5), textcoords="offset points", weight="bold")
    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title("Pedestrian network QA — 1 km around the Crossharbour site")
    ax.legend(handles=[
        Line2D([0], [0], color="#b8b8b8", lw=2, label="Main weak component"),
        Line2D([0], [0], color="#f28e2b", lw=2, label="Disconnected component"),
        Line2D([0], [0], color="#d62728", lw=2, label="Restricted access"),
        Line2D([0], [0], color="#e377c2", lw=2, label="Conditional access"),
        Line2D([0], [0], color="#8c564b", lw=2, ls="-.", label="Trunk link without foot tag"),
        Line2D([0], [0], color="#6a3d9a", lw=2, ls="--", label="Bridge"),
        Line2D([0], [0], color="#17becf", lw=2, ls=":", label="Tunnel"),
        Line2D([0], [0], marker="x", color="#ff1493", lw=0, label="Possible gap / 2D crossing"),
        Patch(facecolor="#1f77b4", alpha=0.25, edgecolor="#005a9c", label="Development site"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#ffd92f",
               markeredgecolor="black", label="Reviewed origins A/B"),
    ], loc="lower left", fontsize=8)
    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def _write_restricted_link_review(restricted_local, site, output_path, max_analysis_minutes):
    """Write a concise checklist for the restricted links that affect local routing."""
    rows = []
    for number, issue in enumerate(restricted_local.itertuples(), start=1):
        osm_way_ids = list(dict.fromkeys(re.findall(r"\d+", str(issue.osmid))))
        mixed_access = " | " in str(issue.evidence).split("access=", 1)[-1].split(";", 1)[0]
        travel_minutes = float(issue.minimum_origin_travel_time_min)
        within_horizon = np.isfinite(travel_minutes) and travel_minutes <= max_analysis_minutes
        rows.append({
            "review_id": f"RPL-{number:02d}",
            "qa_issue_id": issue.issue_id,
            "distance_from_site_m": round(float(issue.geometry.distance(site)), 1),
            "affected_graph_edges": issue.element_id,
            "osm_way_ids": " | ".join(osm_way_ids),
            "osm_links": " | ".join(
                f"https://www.openstreetmap.org/way/{way_id}" for way_id in osm_way_ids
            ),
            "saved_osm_tags": issue.evidence,
            "minimum_origin_travel_time_min": round(travel_minutes, 2),
            "affects_5_10_15_minute_analysis": "yes" if within_horizon else "no",
            "recommended_action": (
                "Review the individual OSM ways because this reachable simplified edge contains mixed access tags."
                if within_horizon and mixed_access else
                "Exclude from the routing graph unless manual evidence confirms public pedestrian access."
                if within_horizon else
                f"Retain in the audit log; review before analysis beyond {max_analysis_minutes} minutes."
            ),
            "review_status": "pending" if within_horizon else "outside_analysis_horizon",
            "manual_decision": "",
            "review_notes": "",
        })
    pd.DataFrame(rows).to_csv(output_path, index=False)


def _minimum_edge_issue_travel_seconds(element_id, travel_times):
    """Return the earliest routed arrival at either end of an affected physical edge."""
    endpoint_times = []
    for directed_edge in str(element_id).split("; "):
        try:
            u, v, _ = (int(value) for value in directed_edge.rsplit("-", 2))
        except ValueError:
            continue
        endpoint_times.extend((travel_times.get(u, np.inf), travel_times.get(v, np.inf)))
    return min(endpoint_times, default=np.inf)


def run_pedestrian_network_qa(cfg, graph_path=None):
    """Inspect the existing GraphML and write findings without changing the graph."""
    graph_path = Path(graph_path or BASE_PEDESTRIAN_GRAPH)
    if not graph_path.exists():
        raise FileNotFoundError(f"Pedestrian GraphML not found: {graph_path}")
    source_hash = sha256(graph_path.read_bytes()).hexdigest()
    tables = OUTPUTS / "tables"
    spatial = OUTPUTS / "spatial"
    figures = OUTPUTS / "figures"
    for folder in (tables, spatial, figures):
        folder.mkdir(parents=True, exist_ok=True)

    graph = load_pedestrian_graph(graph_path)
    if not isinstance(graph, nx.MultiDiGraph):
        raise TypeError("Pedestrian network must be a NetworkX MultiDiGraph")
    graph_crs = CRS.from_user_input(graph.graph.get("crs"))
    if not graph_crs.is_projected:
        raise ValueError("Geometry QA requires a projected graph CRS")
    nodes, edges = ox.graph_to_gdfs(graph)
    site_frame = gpd.read_file(RAW / "site_boundary.geojson", engine="pyogrio").to_crs(graph_crs)
    site = site_frame.geometry.union_all()
    origins_path = PROCESSED / "site_origins.gpkg"
    origins = (gpd.read_file(origins_path, layer="site_access_points", engine="pyogrio").to_crs(graph_crs)
               if origins_path.exists() else None)
    weak, strong, component_of = _component_lookup(graph)
    metrics = edge_geometry_metrics(graph)
    physical = _physical_edges(metrics, graph)
    graph_bounds = box(*nodes.total_bounds)
    summary = []
    issues = []

    def add(section, check, value, unit="", status="pass", severity="", notes=""):
        summary.append(_summary_row(section, check, value, unit, status, severity, notes))

    add("metadata", "osmnx_runtime_version", ox.__version__)
    add("metadata", "qa_source_graph", graph_path.name)
    add("metadata", "osmnx_created_with", graph.graph.get("created_with", "not recorded"))
    add("metadata", "graph_crs", graph_crs.to_string())
    add("metadata", "graph_type", type(graph).__name__)
    add("metadata", "directed", graph.is_directed())
    add("metadata", "node_count", graph.number_of_nodes(), "nodes")
    add("metadata", "directed_edge_count", graph.number_of_edges(), "edges")
    add("metadata", "simplified", graph.graph.get("simplified", "not recorded"))
    add("metadata", "network_bbox", ", ".join(f"{value:.3f}" for value in nodes.total_bounds),
        "minx,miny,maxx,maxy")
    for key in ("created_date", "network_type", "retained_way_tags", "download_area_hash",
                "download_buffer_m", "school_count"):
        add("acquisition", key, graph.graph.get(key, "not recorded"),
            status="pass" if key in graph.graph else "review",
            severity="LOW" if key not in graph.graph else "")
    add("acquisition", "osm_snapshot_date", "not recorded", status="review", severity="LOW",
        notes="The GraphML records creation time but not the OSM data snapshot timestamp.")

    weak_sizes = [len(item) for item in weak]
    strong_sizes = [len(item) for item in strong]
    isolated = list(nx.isolates(graph.to_undirected(as_view=False)))
    dead_ends = physical_dead_end_nodes(graph)
    add("topology", "weak_component_count", len(weak), "components", "review", "MEDIUM",
        f"Largest sizes: {weak_sizes[:10]}")
    add("topology", "weak_component_size_distribution",
        "; ".join(f"{size} nodes={count}" for size, count in sorted(
            Counter(weak_sizes).items(), reverse=True)), "component counts")
    add("topology", "strong_component_count", len(strong), "components",
        "pass" if len(strong) == len(weak) else "review", "",
        f"Largest sizes: {strong_sizes[:10]}")
    add("topology", "strong_component_size_distribution",
        "; ".join(f"{size} nodes={count}" for size, count in sorted(
            Counter(strong_sizes).items(), reverse=True)), "component counts")
    add("topology", "largest_weak_component_nodes", weak_sizes[0], "nodes")
    add("topology", "largest_weak_component_share", weak_sizes[0] / graph.number_of_nodes(), "proportion")
    add("topology", "isolated_node_count", len(isolated), "nodes", "pass" if not isolated else "review",
        "HIGH" if isolated else "")
    add("topology", "physical_dead_end_count", len(dead_ends), "nodes", "review", "LOW",
        "Degree uses distinct neighbours so reciprocal and parallel directed edges are not double-counted.")
    pair_counts = Counter((u, v) for u, v, _ in graph.edges(keys=True))
    parallel_pairs = sum(count > 1 for count in pair_counts.values())
    parallel_extra = sum(count - 1 for count in pair_counts.values() if count > 1)
    add("topology", "ordered_node_pairs_with_parallel_edges", parallel_pairs, "pairs", "pass", "",
        f"{parallel_extra} additional directed edges; parallel edges are retained and are not assumed to be errors.")
    duplicate_signatures = defaultdict(list)
    for row in metrics.itertuples(index=False):
        data = graph.get_edge_data(row.u, row.v, row.key)
        signature = (
            row.u, row.v, sha256(normalize(row.geometry).wkb).hexdigest(),
            _display(data.get("osmid")), _display(data.get("highway")),
            _display(data.get("access")), _display(data.get("service")),
        )
        duplicate_signatures[signature].append(row)
    exact_duplicates = sum(len(rows) - 1 for rows in duplicate_signatures.values() if len(rows) > 1)
    add("topology", "exact_duplicate_directed_edges", exact_duplicates, "additional edges",
        "pass" if exact_duplicates == 0 else "review", "LOW" if exact_duplicates else "",
        "Exact parallels are flagged for review but are not automatically treated as errors or removed.")
    for rows in duplicate_signatures.values():
        if len(rows) <= 1:
            continue
        first = rows[0]
        data = graph.get_edge_data(first.u, first.v, first.key)
        edge_ids = "; ".join(f"{row.u}-{row.v}-{row.key}" for row in rows)
        issues.append(_issue(
            "LOW", "exact_duplicate_directed_edges", first.geometry, site, "edge", edge_ids,
            data.get("osmid"),
            f"{len(rows)} parallel directed edges have the same endpoints, geometry and core tags.",
            "Equal-cost duplicates do not change shortest paths but can inflate edge-based counts.",
            "Retain unless OSM identity and tag review confirms a redundant duplicate.",
        ))

    issues.append(_issue(
        "MEDIUM", "multiple_disconnected_components", graph_bounds, site, "graph", "all",
        "", f"{len(weak)} weak components; largest contains {weak_sizes[0]} of {graph.number_of_nodes()} nodes.",
        "Destinations in minor components cannot be reached from origins in the main component.",
        "Review components near the site and destinations; preserve genuine grade-separated or private components.",
    ))

    local_component_ids = sorted({component_of[u] for u, _, _ in edges[edges.intersects(
        site.buffer(LOCAL_REVIEW_RADIUS_M))].index} - {0})
    add("local_review", "non_main_components_within_1km", len(local_component_ids), "components",
        "review" if local_component_ids else "pass", "MEDIUM" if local_component_ids else "")
    for component_id in local_component_ids:
        component_edges = edges[[component_of[u] == component_id for u, _, _ in edges.index]]
        geometry = unary_union(component_edges.geometry.tolist())
        issues.append(_issue(
            "MEDIUM", "disconnected_component_near_site", geometry, site, "weak_component",
            component_id, "", f"Component contains {len(weak[component_id])} nodes and "
            f"{len(component_edges)} directed edges.",
            "Pedestrian links and destinations on this component are unreachable from origins A/B.",
            "Review whether separation is real, private, indoor or grade-separated before changing topology.",
        ))

    origin_components = []
    if origins is not None:
        for _, origin in origins.iterrows():
            u = int(origin["matched_edge_u"])
            origin_components.append(component_of.get(u))
        on_main = bool(origin_components and all(item == 0 for item in origin_components))
        add("local_review", "origin_components", " | ".join(map(str, origin_components)), "component ids",
            "pass" if on_main else "fail", "CRITICAL" if not on_main else "",
            "Both reviewed origins must connect to the largest weak component.")
        if not on_main:
            issues.append(_issue(
                "CRITICAL", "origin_not_on_main_component", origins.geometry.union_all(), site,
                "origin", " | ".join(origins["origin_id"].astype(str)), "",
                f"Origin component IDs: {origin_components}.",
                "Catchments may be limited to a disconnected fragment.",
                "Resolve origin-edge/component selection before routing.",
            ))

    local_gap_issues, local_edges, local_dead_ends = _local_gap_issues(
        graph, nodes, edges, component_of, site
    )
    issues.extend(local_gap_issues)
    add("local_review", "dead_ends_within_1km", len(local_dead_ends), "nodes", "review", "LOW")
    add("local_review", "possible_gap_findings", len(local_gap_issues), "findings",
        "review" if local_gap_issues else "pass", "HIGH" if any(
            item["severity"] == "HIGH" for item in local_gap_issues) else "MEDIUM" if local_gap_issues else "")

    for tag in ("highway", "foot", "access", "service", "bridge", "tunnel", "layer"):
        counts = _tag_distribution(graph, tag)
        missing = counts.get("<missing>", 0)
        add("pedestrian_access", f"{tag}_tag_missing", missing, "directed edges",
            "review" if missing == graph.number_of_edges() and tag in {"foot", "layer"} else "pass",
            "MEDIUM" if tag == "foot" and missing == graph.number_of_edges() else
            "MEDIUM" if tag == "layer" and missing == graph.number_of_edges() else "",
            "; ".join(f"{name}={count}" for name, count in counts.most_common(12)))
    if _tag_distribution(graph, "foot").get("<missing>", 0) == graph.number_of_edges():
        issues.append(_issue(
            "MEDIUM", "foot_tag_not_retained", graph_bounds, site, "graph", "all_edges", "",
            "The graph was selected with OSMnx network_type=walk, but the foot attribute was not retained on edges.",
            "The walk filter excluded foot=no ways, but saved attributes cannot show positive foot overrides such as access=no plus foot=yes.",
            "For a fully auditable graph, re-acquire it with foot included in retained OSM way tags, then rerun QA.",
        ))
    if _tag_distribution(graph, "layer").get("<missing>", 0) == graph.number_of_edges():
        issues.append(_issue(
            "MEDIUM", "layer_tag_not_retained", graph_bounds, site, "graph", "all_edges", "",
            "The layer attribute is absent on all directed edges; bridge and tunnel tags are present.",
            "Some 2D crossings cannot be confidently interpreted as at-grade or grade-separated.",
            "Retain layer during graph acquisition and use it with bridge/tunnel evidence in manual review.",
        ))

    for row in physical.itertuples(index=False):
        access_class = classify_pedestrian_access(row.access, row.foot, row.highway)
        if access_class == "restricted_pedestrian_access":
            issues.append(_issue(
                "HIGH", "restricted_pedestrian_access", row.geometry, site, "edge",
                row.directed_edge_ids, row.osmid,
                f"highway={row.highway or '<missing>'}; foot={row.foot or '<missing>'}; "
                f"access={row.access or '<missing>'}; service={row.service or '<missing>'}.",
                "Routing may traverse a link that is unavailable to the public.",
                "Verify pedestrian permission; exclude or retain only with documented evidence and approval.",
            ))
        elif access_class == "conditional_pedestrian_access":
            issues.append(_issue(
                "MEDIUM", "conditional_pedestrian_access", row.geometry, site, "edge",
                row.directed_edge_ids, row.osmid,
                f"highway={row.highway or '<missing>'}; foot={row.foot or '<missing>'}; "
                f"access={row.access or '<missing>'}.",
                "Access may depend on user, purpose or opening conditions.",
                "Confirm public walking access before final routing; do not remove solely because it is a service road.",
            ))
        elif access_class == "high_speed_road_without_foot_tag":
            issues.append(_issue(
                "MEDIUM", "high_speed_road_without_foot_tag", row.geometry, site, "edge",
                row.directed_edge_ids, row.osmid,
                f"highway={row.highway}; no retained foot tag.",
                "A walk route could follow a road where pedestrian access is uncertain.",
                "Review underlying OSM foot/sidewalk evidence before final routing.",
            ))

    restricted_count = sum(item["issue_type"] == "restricted_pedestrian_access" for item in issues)
    restricted_near_site = sum(
        item["issue_type"] == "restricted_pedestrian_access"
        and item["geometry"].distance(site) <= LOCAL_REVIEW_RADIUS_M for item in issues
    )
    restricted_very_near_site = sum(
        item["issue_type"] == "restricted_pedestrian_access"
        and item["geometry"].distance(site) <= NEAR_SITE_RADIUS_M for item in issues
    )
    add("pedestrian_access", "restricted_physical_edges", restricted_count, "physical edges",
        "fail" if restricted_count else "pass", "HIGH" if restricted_count else "")
    add("local_review", "restricted_physical_edges_within_1km", restricted_near_site, "physical edges",
        "fail" if restricted_near_site else "pass", "HIGH" if restricted_near_site else "")
    add("local_review", "restricted_physical_edges_within_250m", restricted_very_near_site,
        "physical edges", "fail" if restricted_very_near_site else "pass",
        "HIGH" if restricted_very_near_site else "")
    add("pedestrian_access", "service_physical_edges", int(physical["highway"].str.contains(
        "service", case=False, na=False).sum()), "physical edges", "pass", "",
        "Service roads are retained; only access evidence creates a QA issue.")
    add("pedestrian_access", "bridge_directed_edges", int(edges["bridge"].notna().sum()), "edges")
    add("pedestrian_access", "tunnel_directed_edges", int(edges["tunnel"].notna().sum()), "edges")

    invalid_count = int((~metrics["geometry_valid"]).sum())
    missing_geometry = int((~metrics["explicit_geometry"]).sum())
    zero_stored = int((metrics["stored_length_m"] <= 0).sum())
    zero_geometry = int((metrics["geometry_length_m"] <= 0).sum())
    discrepancies = metrics[(metrics["absolute_difference_m"] > LENGTH_ABSOLUTE_TOLERANCE_M)
                            & (metrics["relative_difference"] > LENGTH_RELATIVE_TOLERANCE)]
    endpoint_mismatches = metrics[metrics["endpoint_mismatch_m"] > ENDPOINT_TOLERANCE_M]
    add("geometry", "invalid_geometry_count", invalid_count, "directed edges",
        "pass" if not invalid_count else "fail", "HIGH" if invalid_count else "")
    add("geometry", "missing_explicit_geometry_count", missing_geometry, "directed edges",
        "pass_with_note", "LOW",
        "OSMnx omits geometry for straight edges; QA reconstructs only for measurement from node endpoints.")
    add("geometry", "zero_stored_length_count", zero_stored, "directed edges",
        "pass" if not zero_stored else "fail", "CRITICAL" if zero_stored else "")
    add("geometry", "zero_geometry_length_count", zero_geometry, "directed edges",
        "pass" if not zero_geometry else "fail", "CRITICAL" if zero_geometry else "")
    add("geometry", "length_discrepancy_count", len(discrepancies), "directed edges",
        "pass" if discrepancies.empty else "review", "MEDIUM" if not discrepancies.empty else "",
        f"Threshold: >{LENGTH_ABSOLUTE_TOLERANCE_M} m and >{LENGTH_RELATIVE_TOLERANCE:.0%}.")
    add("geometry", "endpoint_mismatch_count", len(endpoint_mismatches), "directed edges",
        "pass" if endpoint_mismatches.empty else "fail", "HIGH" if not endpoint_mismatches.empty else "",
        f"Threshold: >{ENDPOINT_TOLERANCE_M} m from graph node coordinates.")
    for column, label, unit in (
        ("absolute_difference_m", "stored_vs_geometry_absolute_difference", "m"),
        ("relative_difference", "stored_vs_geometry_relative_difference", "proportion"),
    ):
        values = metrics[column].dropna()
        for quantile, suffix in ((0.0, "min"), (0.5, "median"), (0.95, "p95"), (0.99, "p99"), (1.0, "max")):
            add("geometry", f"{label}_{suffix}", float(values.quantile(quantile)), unit)
        add("geometry", f"{label}_mean", float(values.mean()), unit)
    crs_consistent = CRS.from_user_input(nodes.crs) == graph_crs == CRS.from_user_input(edges.crs)
    add("geometry", "crs_consistency", crs_consistent, "", "pass" if crs_consistent else "fail",
        "CRITICAL" if not crs_consistent else "")
    configured_crs_match = graph_crs == CRS.from_user_input(cfg["project"]["analysis_crs"])
    add("geometry", "configured_analysis_crs_match", configured_crs_match, "",
        "pass" if configured_crs_match else "fail", "CRITICAL" if not configured_crs_match else "",
        f"Configured analysis CRS: {cfg['project']['analysis_crs']}.")

    for row in discrepancies.itertuples():
        issues.append(_issue(
            "MEDIUM", "edge_length_discrepancy", row.geometry, site, "edge",
            f"{row.u}-{row.v}-{row.key}", row.osmid,
            f"Stored {row.stored_length_m:.3f} m; geometry {row.geometry_length_m:.3f} m; "
            f"difference {row.absolute_difference_m:.3f} m ({row.relative_difference:.1%}).",
            "Walking time could be biased on this edge.",
            "Review geometry and length source; change only after approval.",
        ))
    for row in endpoint_mismatches.itertuples():
        issues.append(_issue(
            "HIGH", "edge_endpoint_mismatch", row.geometry, site, "edge",
            f"{row.u}-{row.v}-{row.key}", row.osmid,
            f"Edge endpoint differs from graph node coordinates by {row.endpoint_mismatch_m:.3f} m.",
            "Displayed geometry may not represent routed topology.",
            "Review graph construction and geometry orientation before routing.",
        ))

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    issues.sort(key=lambda item: (severity_order[item["severity"]], item["issue_type"], item["element_id"]))
    for number, item in enumerate(issues, start=1):
        item["issue_id"] = f"NQA-{number:04d}"
    issue_frame = gpd.GeoDataFrame(issues, columns=ISSUE_COLUMNS, geometry="geometry", crs=graph_crs)
    issue_frame["minimum_origin_travel_time_min"] = np.nan
    max_analysis_minutes = max(int(value) for value in cfg["walking"]["sensitivity_minutes"])
    if origins is not None and not origins.empty:
        routing_graph, _ = insert_origins_into_edges(
            graph, origins.to_crs(graph_crs), cfg["walking"]["speed_kmh"]
        )
        source_nodes = [int(value) for value in origins["site_access_node"]]
        travel_times = nx.multi_source_dijkstra_path_length(
            routing_graph, source_nodes, weight="travel_time_s"
        )
        restricted_mask = issue_frame["issue_type"] == "restricted_pedestrian_access"
        issue_frame.loc[restricted_mask, "minimum_origin_travel_time_min"] = [
            _minimum_edge_issue_travel_seconds(value, travel_times) / 60
            for value in issue_frame.loc[restricted_mask, "element_id"]
        ]
    for severity in severity_order:
        count = int((issue_frame["severity"] == severity).sum())
        add("findings", f"{severity.lower()}_issue_count", count, "findings",
            "review" if count else "pass", severity if count else "")

    restricted_local = issue_frame[
        (issue_frame["issue_type"] == "restricted_pedestrian_access")
        & (issue_frame.geometry.distance(site) <= LOCAL_REVIEW_RADIUS_M)
    ]
    restricted_by_minutes = {
        int(minutes): restricted_local[
            restricted_local["minimum_origin_travel_time_min"] <= int(minutes)
        ]
        for minutes in cfg["walking"]["sensitivity_minutes"]
    }
    for minutes, frame in sorted(restricted_by_minutes.items()):
        add("local_review", f"restricted_edges_reachable_within_{minutes}min", len(frame),
            "physical edges", "fail" if len(frame) else "pass", "HIGH" if len(frame) else "")
    high_local_gaps = issue_frame[
        (issue_frame["issue_type"] == "possible_network_gap_at_dead_end")
        & (issue_frame["severity"] == "HIGH")
    ]
    critical_findings = issue_frame[issue_frame["severity"] == "CRITICAL"]
    endpoint_failures = issue_frame[issue_frame["issue_type"] == "edge_endpoint_mismatch"]
    add("local_review", "high_priority_gaps_within_250m", len(high_local_gaps), "findings",
        "review" if len(high_local_gaps) else "pass", "HIGH" if len(high_local_gaps) else "")
    actions = []
    if len(critical_findings):
        actions.append(f"resolve {len(critical_findings)} critical finding(s)")
    if len(endpoint_failures):
        actions.append(f"resolve {len(endpoint_failures)} edge endpoint failure(s)")
    restricted_within_horizon = restricted_by_minutes[max(restricted_by_minutes)]
    if len(restricted_within_horizon):
        actions.append(
            f"review {len(restricted_within_horizon)} restricted physical edge(s) reachable within "
            f"{max(restricted_by_minutes)} minutes"
        )
    if len(high_local_gaps):
        actions.append(f"manually review {len(high_local_gaps)} possible network gap(s) within 250 m")
    suitability = "NOT_READY" if actions else "SUITABLE_WITH_REVIEW"
    recommendation = (
        "Do not finalise 5-, 10- or 15-minute results yet: " + "; ".join(actions)
        + ". Record approved decisions in network_changes.csv, rerun QA, then rerun catchments before "
        "population or school calculations."
        if actions else
        "Graph is suitable for 5-, 10- and 15-minute routing; no unresolved critical, endpoint, "
        "near-site gap or reachable restricted-link finding affects these analysis thresholds."
    )
    for minutes in (5, 10, 15):
        band_blocked = bool(len(critical_findings) or len(endpoint_failures) or len(high_local_gaps)
                            or len(restricted_by_minutes.get(minutes, [])))
        band_status = "NOT_READY" if band_blocked else "SUITABLE_WITH_REVIEW"
        add("suitability", f"{minutes}_minute_walking_analysis", band_status, "",
            "fail" if band_blocked else "review", "HIGH" if band_blocked else "MEDIUM",
            recommendation)
    add("suitability", "required_before_population_and_school_accessibility", recommendation,
        status="action_required" if suitability == "NOT_READY" else "review")

    summary_frame = pd.DataFrame(summary)
    summary_path = tables / "network_qa_summary.csv"
    issue_path = spatial / "network_qa_issues.gpkg"
    map_path = figures / "network_qa_map.png"
    changes_path = tables / "network_changes.csv"
    restricted_review_path = QA / "network_restricted_links_review.csv"
    QA.mkdir(parents=True, exist_ok=True)
    summary_frame.to_csv(summary_path, index=False)
    _write_restricted_link_review(
        restricted_local, site, restricted_review_path, max_analysis_minutes
    )
    if not changes_path.exists():
        pd.DataFrame(columns=CHANGE_COLUMNS).to_csv(changes_path, index=False)
    if issue_path.exists():
        issue_path.unlink()
    geometry_layers = {
        "graph_issues": issue_frame[issue_frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"])],
        "component_issues": issue_frame[issue_frame.geometry.geom_type.isin(["LineString", "MultiLineString"])
                                        & (issue_frame["element_type"] == "weak_component")],
        "edge_issues": issue_frame[issue_frame.geometry.geom_type.isin(["LineString", "MultiLineString"])
                                   & (issue_frame["element_type"] != "weak_component")],
        "node_issues": issue_frame[issue_frame.geometry.geom_type.isin(["Point", "MultiPoint"])],
    }
    for layer, frame in geometry_layers.items():
        if not frame.empty:
            frame.to_file(issue_path, layer=layer, driver="GPKG", engine="pyogrio")

    edge_issues = geometry_layers["edge_issues"]
    edge_issues = edge_issues[edge_issues.intersects(site.buffer(LOCAL_REVIEW_RADIUS_M))]
    _plot_local_qa(site_frame, origins, local_edges, edge_issues,
                   geometry_layers["node_issues"], map_path)

    if sha256(graph_path.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("The original pedestrian GraphML changed during read-only QA")
    return {
        "summary": summary_frame, "issues": issue_frame, "suitability": suitability,
        "recommendation": recommendation, "source_sha256": source_hash,
        "summary_output": str(summary_path), "issues_output": str(issue_path),
        "map_output": str(map_path), "changes_output": str(changes_path),
        "restricted_review_output": str(restricted_review_path),
    }
