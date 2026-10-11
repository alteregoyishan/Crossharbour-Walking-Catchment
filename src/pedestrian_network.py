"""Download and publish a walkable OpenStreetMap network around all schools."""
from hashlib import sha256
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from shapely import normalize

from .paths import (
    BASE_PEDESTRIAN_GRAPH, CLEANED, EXTERNAL, OPENSTREETMAP, OUTPUTS, QA,
    REVIEWED_PEDESTRIAN_GRAPH,
)
from .utils import raw_hashes


PEDESTRIAN_AUDIT_TAGS = [
    "foot", "layer", "sidewalk", "sidewalk:left", "sidewalk:right", "sidewalk:both",
]


def configure_osmnx_way_tags():
    """Retain pedestrian permission and level tags needed for post-download QA."""
    ox.settings.useful_tags_way = list(dict.fromkeys(
        [*ox.settings.useful_tags_way, *PEDESTRIAN_AUDIT_TAGS]
    ))
    return ox.settings.useful_tags_way


def build_download_area(schools, buffer_m):
    """Create a buffered convex hull that contains every cleaned school point."""
    if schools.empty or schools.geometry.isna().any() or schools.geometry.is_empty.any():
        raise ValueError("Cleaned school layer must contain non-empty point geometries")
    if not schools.crs or not schools.crs.is_projected:
        raise ValueError("School points must use a projected CRS before buffering")
    area = schools.geometry.union_all().convex_hull.buffer(float(buffer_m))
    if area.is_empty or not schools.geometry.apply(area.covers).all():
        raise RuntimeError("Pedestrian network download area does not contain every school")
    return area


def add_walking_costs(graph, speed_kmh):
    """Add seconds of walking time to every edge for NetworkX routing."""
    metres_per_second = float(speed_kmh) * 1000 / 3600
    for _, _, _, data in graph.edges(keys=True, data=True):
        length = float(data.get("length", np.nan))
        if not np.isfinite(length) or length < 0:
            raise RuntimeError("Pedestrian network contains an invalid edge length")
        data["travel_time_s"] = length / metres_per_second
    return graph


def validate_pedestrian_graph(graph, expected_crs):
    """Require a non-empty projected NetworkX MultiDiGraph with routing costs."""
    if not isinstance(graph, nx.MultiDiGraph):
        raise TypeError("Pedestrian network must be a NetworkX MultiDiGraph")
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise RuntimeError("Pedestrian network is empty")
    if str(graph.graph.get("crs")) != expected_crs:
        raise RuntimeError(f"Pedestrian network CRS must be {expected_crs}")
    if any("length" not in data or "travel_time_s" not in data for *_, data in graph.edges(data=True)):
        raise RuntimeError("Pedestrian network edges are missing routing costs")


def _geometry_hash(geometry):
    return sha256(normalize(geometry).wkb).hexdigest()


def _school_access_table(graph, schools, download_area):
    node_ids, distances = ox.distance.nearest_nodes(
        graph,
        X=schools.geometry.x.to_numpy(),
        Y=schools.geometry.y.to_numpy(),
        return_dist=True,
    )
    return pd.DataFrame({
        "URN": schools["URN"].astype(str).to_numpy(),
        "EstablishmentName": schools["EstablishmentName"].to_numpy(),
        "inside_download_area": schools.geometry.apply(download_area.covers).to_numpy(),
        "nearest_node": node_ids,
        "nearest_node_distance_m": distances,
    })


def download_pedestrian_network(cfg, force_download=False):
    """Build or load the OSM walk network covering all cleaned school points."""
    school_path = CLEANED / "schools_cleaned.gpkg"
    graphml_path = BASE_PEDESTRIAN_GRAPH
    gpkg_path = OPENSTREETMAP / "pedestrian_network.gpkg"
    area_path = OPENSTREETMAP / "pedestrian_network_download_area.gpkg"
    access_path = QA / "school_network_access.csv"
    summary_path = OUTPUTS / "tables" / "pedestrian_network_summary.csv"
    for folder in [OPENSTREETMAP, QA, EXTERNAL / "osm" / "cache"]:
        folder.mkdir(parents=True, exist_ok=True)

    hashes_before = raw_hashes()
    retained_way_tags = configure_osmnx_way_tags()
    schools = gpd.read_file(school_path, layer="schools_cleaned", engine="pyogrio")
    analysis_crs = cfg["project"]["analysis_crs"]
    if schools.crs is None:
        raise ValueError("Cleaned school layer has no CRS")
    schools = schools.to_crs(analysis_crs)
    area = build_download_area(schools, cfg["osm"]["download_buffer_m"])
    area_hash = _geometry_hash(area)
    area_gdf = gpd.GeoDataFrame(
        [{"school_count": len(schools), "buffer_m": cfg["osm"]["download_buffer_m"]}],
        geometry=[area], crs=analysis_crs,
    )
    area_gdf.to_file(area_path, layer="download_area", driver="GPKG")

    use_cache = bool(cfg["osm"]["use_cached_network"])
    reused_graph = False
    if use_cache and not force_download and graphml_path.exists():
        candidate = ox.load_graphml(graphml_path)
        if candidate.graph.get("download_area_hash") == area_hash:
            graph = candidate
            reused_graph = True
        else:
            graph = None
    else:
        graph = None

    if graph is None:
        ox.settings.use_cache = use_cache
        ox.settings.cache_folder = EXTERNAL / "osm" / "cache"
        ox.settings.requests_timeout = 300
        polygon_wgs84 = area_gdf.to_crs(cfg["project"]["geographic_crs"]).geometry.iloc[0]
        graph = ox.graph_from_polygon(
            polygon_wgs84,
            network_type=cfg["osm"]["network_type"],
            simplify=True,
            retain_all=True,
            truncate_by_edge=True,
        )
        graph = ox.project_graph(graph, to_crs=analysis_crs)
        graph.graph["download_area_hash"] = area_hash
        graph.graph["download_buffer_m"] = str(cfg["osm"]["download_buffer_m"])
        graph.graph["school_count"] = str(len(schools))
        graph.graph["network_type"] = cfg["osm"]["network_type"]
        graph.graph["retained_way_tags"] = " | ".join(retained_way_tags)

    add_walking_costs(graph, cfg["walking"]["speed_kmh"])
    validate_pedestrian_graph(graph, analysis_crs)
    access = _school_access_table(graph, schools, area)
    if not access["inside_download_area"].all():
        raise RuntimeError("At least one school lies outside the pedestrian network download area")

    ox.save_graphml(graph, graphml_path)
    ox.save_graph_geopackage(graph, filepath=gpkg_path, directed=True)
    access.to_csv(access_path, index=False)
    pd.DataFrame([{
        "school_count": len(schools),
        "node_count": graph.number_of_nodes(),
        "directed_edge_count": graph.number_of_edges(),
        "crs": str(graph.graph["crs"]),
        "network_type": cfg["osm"]["network_type"],
        "retained_way_tags": " | ".join(retained_way_tags),
        "edges_with_foot_tag": sum("foot" in data for *_, data in graph.edges(keys=True, data=True)),
        "edges_with_layer_tag": sum("layer" in data for *_, data in graph.edges(keys=True, data=True)),
        "download_buffer_m": cfg["osm"]["download_buffer_m"],
        "max_school_to_node_distance_m": float(access["nearest_node_distance_m"].max()),
        "reused_graphml": reused_graph,
    }]).to_csv(summary_path, index=False)

    if raw_hashes() != hashes_before:
        raise RuntimeError("Original files changed during pedestrian network download")
    return {
        "school_count": len(schools),
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "max_school_to_node_distance_m": float(access["nearest_node_distance_m"].max()),
        "reused_graphml": reused_graph,
        "graphml_output": str(graphml_path),
        "gpkg_output": str(gpkg_path),
        "download_area": str(area_path),
        "school_access_qa": str(access_path),
        "summary_output": str(summary_path),
    }


def load_pedestrian_graph(path=None):
    """Load the saved NetworkX MultiDiGraph for routing analysis."""
    graph_path = Path(path or BASE_PEDESTRIAN_GRAPH)
    compressed_path = graph_path.with_name(f"{graph_path.name}.gz")
    if not graph_path.exists() and compressed_path.exists():
        graph_path = compressed_path
    graph = ox.load_graphml(graph_path)
    for _, _, _, data in graph.edges(keys=True, data=True):
        if "travel_time_s" in data:
            data["travel_time_s"] = float(data["travel_time_s"])
    return graph


def load_reviewed_pedestrian_graph():
    """Load the manually reviewed routing graph required by downstream steps."""
    if not REVIEWED_PEDESTRIAN_GRAPH.exists():
        raise FileNotFoundError(
            "Reviewed pedestrian graph is missing; apply approved network changes first"
        )
    return load_pedestrian_graph(REVIEWED_PEDESTRIAN_GRAPH)
