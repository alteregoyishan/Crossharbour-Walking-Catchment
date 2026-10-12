"""Generate the selected C1 5/10/15-minute catchments and final map series.

C1 remains an ordinary-Delaunay travel-time contour proxy.  Routing, origins,
speed and partially reachable edges are inherited unchanged from earlier steps.
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
from pyproj import network as proj_network
from scipy.spatial import Delaunay
from shapely import Point, Polygon, box, make_valid, normalize
from shapely.ops import unary_union

from .catchment_barrier_review import (
    SELECTED_BRIDGE_BUFFER_M,
    _load_ngd_structures,
    _sample_network,
    apply_common_barrier_policy,
    review_reachable_water_crossings,
    validate_water_layer,
)
from .catchment_polygonisation import (
    CRS,
    MAX_TRIANGLE_EDGE_M,
    NETWORK_SAMPLE_SPACING_M,
    SPEED_MPS,
    _clip_triangle_below_level,
    _hole_count,
    _physical_edges,
    _polygon_parts,
    _write_layer,
)
from .os_data_services import os_maps_basemap, selected_water_path
from .paths import CLEANED, OPENSTREETMAP, OUTPUTS, PROCESSED, RAW, ROOT
from .pedestrian_network import load_pedestrian_graph
from .service_area_analysis import polygonal


# The deliverable must render offline.  Some Conda installations set
# PROJ_NETWORK=ON, which can turn a valid WGS84 -> British National Grid
# transformation into non-finite coordinates when the remote grid service is
# unavailable.  Use the installed PROJ database consistently instead.
proj_network.set_network_enabled(False)


GIS = OUTPUTS / "gis"
TABLES = OUTPUTS / "tables"
MAPS = OUTPUTS / "maps"
METHOD = OUTPUTS / "methodology"

OUT_GPKG = GIS / "c1_walking_catchments_final.gpkg"
OUT_SUMMARY = TABLES / "c1_walking_catchment_summary.csv"
OUT_REPORT = METHOD / "final_c1_map_series.md"
OUT_METADATA = METHOD / "final_c1_map_series_metadata.json"

MAP_BASENAMES = {
    "origin_selection": "figure_00a_site_origin_selection",
    "schools_overview": "figure_00b_all_cleaned_schools",
    5: "figure_01_c1_05min",
    10: "figure_02_c1_10min",
    15: "figure_03_c1_15min",
    "combined": "figure_04_c1_combined",
    "circle": "figure_05_c1_combined_2400m_diameter",
}

COLOURS = {5: "#E7903C", 10: "#2B8C7F", 15: "#416788"}
NETWORK_COLOURS = {5: "#A95512", 10: "#17685F", 15: "#274963"}
REFERENCE_CIRCLE_MINUTES = 15
THEORETICAL_RADIUS_M = REFERENCE_CIRCLE_MINUTES * 60 * SPEED_MPS
CARTOGRAPHIC_REGULARISATION_M = 10.0
CARTOGRAPHIC_SIMPLIFY_TOLERANCE_M = 10.0
NETWORK_INCLUSION_HALF_WIDTH_M = 2.0
DISPLAY_NETWORK_INCLUSION_HALF_WIDTH_M = 6.0
SITE_COLOUR = "#202428"
ORIGIN_COLOUR = "#172B3A"
SCHOOL_COLOUR = "#713E73"
SCHOOL_MARKER_AREA = 49.0
ORIGIN_MARKER_AREA = 22.0
SITE_LINEWIDTH = 0.9

OBSOLETE_MAP_NAMES = {
    "catchment_hole_diagnostic.pdf", "catchment_hole_diagnostic.png",
    "figure_01_workshop_map.pdf", "figure_01_workshop_map.png",
    "figure_02_network_accessibility.pdf", "figure_02_network_accessibility.png",
    "figure_03_population_method.pdf", "figure_03_population_method.png",
    "figure_04_catchment_method_comparison.pdf", "figure_04_catchment_method_comparison.png",
    "figure_05_barrier_consistent_methods_v2.pdf", "figure_05_barrier_consistent_methods_v2.png",
    "figure_06_bridge_barrier_qa_v2.pdf", "figure_06_bridge_barrier_qa_v2.png",
    "google_isochrone_comparison.png",
    "figure_05_c1_combined_1600m_diameter.pdf",
    "figure_05_c1_combined_1600m_diameter.png",
}


def _fit_bounds(bounds, target_ratio=420 / 297, padding_fraction=0.045):
    xmin, ymin, xmax, ymax = bounds
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)
    xmin -= width * padding_fraction
    xmax += width * padding_fraction
    ymin -= height * padding_fraction
    ymax += height * padding_fraction
    width, height = xmax - xmin, ymax - ymin
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    if width / height < target_ratio:
        width = height * target_ratio
    else:
        height = width / target_ratio
    return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


def _build_common_c1_surface(raw_15, physical_edges, travel_times):
    coordinates, values, samples, _ = _sample_network(
        raw_15, physical_edges, travel_times, max_arrival_s=1500.0)
    if len(coordinates) < 4:
        raise RuntimeError("Too few network samples for the C1 map series")
    triangulation = Delaunay(coordinates)
    triangle_records = []
    for triangle_id, simplex in enumerate(triangulation.simplices, 1):
        triangle_coordinates = coordinates[simplex]
        side_lengths = [
            np.linalg.norm(triangle_coordinates[index] -
                           triangle_coordinates[(index + 1) % 3])
            for index in range(3)
        ]
        triangle_records.append({
            "triangle_id": triangle_id,
            "simplex": simplex,
            "max_edge_m": float(max(side_lengths)),
        })
    return coordinates, values, samples, triangle_records


def _contour_at_threshold(coordinates, values, triangle_records, threshold_s):
    pieces = []
    used_triangles = 0
    for record in triangle_records:
        if record["max_edge_m"] > MAX_TRIANGLE_EDGE_M:
            continue
        simplex = record["simplex"]
        piece = _clip_triangle_below_level(
            coordinates[simplex], values[simplex], threshold_s)
        if piece is not None:
            pieces.append(piece)
            used_triangles += 1
    if not pieces:
        raise RuntimeError(f"C1 created no polygon at {threshold_s:.0f} seconds")
    contour = polygonal(make_valid(unary_union(pieces)))
    if contour.is_empty or not contour.is_valid:
        raise RuntimeError(f"C1 created invalid geometry at {threshold_s:.0f} seconds")
    return contour, used_triangles


def _fill_internal_rings(geometry):
    """Fill interpolation gaps; the mapped water mask is reapplied afterwards."""
    shells = [Polygon(part.exterior.coords) for part in _polygon_parts(geometry)]
    return polygonal(make_valid(unary_union(shells)))


def _regularise_for_display(geometry, water, network_support,
                            radius_m=CARTOGRAPHIC_REGULARISATION_M,
                            simplify_m=CARTOGRAPHIC_SIMPLIFY_TOLERANCE_M):
    """Simplify the display edge while retaining a visible network envelope."""
    simplified = geometry.simplify(simplify_m, preserve_topology=True)
    closed = simplified.buffer(radius_m, join_style="round").buffer(
        -radius_m, join_style="round")
    opened = closed.buffer(-radius_m, join_style="round").buffer(
        radius_m, join_style="round")
    # Buffer operations can create tiny artificial rings around short network
    # branches.  Fill those rings before reapplying the mapped water mask so
    # only real water holes remain in the cartographic polygon.
    supported = _fill_internal_rings(unary_union([opened, network_support]))
    return polygonal(make_valid(supported.difference(water)))


def build_c1_catchments():
    raw_15 = gpd.read_file(
        GIS / "walking_catchment_15min.gpkg", layer="catchment_raw").to_crs(CRS).geometry.union_all()
    water_frame, water = validate_water_layer(gpd.read_file(selected_water_path()))
    graph = load_pedestrian_graph(
        OPENSTREETMAP / "pedestrian_network_reviewed_with_origins.graphml")
    origins = gpd.read_file(PROCESSED / "site_origins.gpkg").to_crs(CRS)
    travel_times = nx.multi_source_dijkstra_path_length(
        graph, origins.site_access_node.astype(int).tolist(), weight="travel_time_s")
    physical_edges = _physical_edges(graph)
    coordinates, values, samples, triangle_records = _build_common_c1_surface(
        raw_15, physical_edges, travel_times)
    ngd_structures = _load_ngd_structures()

    results = {}
    crossing_frames = []
    for minutes in (5, 10, 15):
        threshold_s = minutes * 60.0
        unmasked, used_triangles = _contour_at_threshold(
            coordinates, values, triangle_records, threshold_s)
        network_frame = gpd.read_file(GIS / f"reachable_network_{minutes}min.gpkg").to_crs(CRS)
        network = network_frame.geometry.union_all()
        crossings = review_reachable_water_crossings(
            physical_edges, network, water, ngd_structures)
        if not crossings.empty:
            crossings = crossings.copy()
            crossings["time_minutes"] = minutes
            crossing_frames.append(crossings)
        original_land, corridor, original_display = apply_common_barrier_policy(
            unmasked, water, crossings, SELECTED_BRIDGE_BUFFER_M, ngd_structures)
        # C1's 150 m maximum-triangle rule can leave fully enclosed gaps over
        # open land (notably Millwall Park).  They are not modelled barriers:
        # fill the rings and then reapply the authoritative water mask.
        hole_filled_land = polygonal(make_valid(
            _fill_internal_rings(original_land).difference(water)))
        # A time contour interpolated over Delaunay triangles can omit short
        # reachable line tips where valid triangles are sparse.  Add a narrow,
        # explicit 2 m/side support strip on land so the authoritative routed
        # network is contained.  Accepted over-water links remain governed by
        # the separate bridge-corridor policy.
        network_support = polygonal(make_valid(
            network.buffer(NETWORK_INCLUSION_HALF_WIDTH_M,
                           cap_style="round", join_style="round").difference(water)))
        display_network_support = polygonal(make_valid(
            network.buffer(DISPLAY_NETWORK_INCLUSION_HALF_WIDTH_M,
                           cap_style="round", join_style="round").difference(water)))
        # The union itself can create tiny rings where the support strip loops
        # around a network branch.  Treat these as interpolation artefacts and
        # reapply water after filling them.
        supported_land = _fill_internal_rings(
            unary_union([hole_filled_land, network_support]))
        land = polygonal(make_valid(supported_land.difference(water)))
        display = polygonal(make_valid(unary_union([land, corridor])))
        cartographic_land = _regularise_for_display(
            land, water, display_network_support)
        cartographic_display = polygonal(make_valid(
            unary_union([cartographic_land, corridor])))
        results[minutes] = {
            "unmasked": unmasked,
            "land": land,
            "corridor": corridor,
            "display": display,
            "cartographic_land": cartographic_land,
            "cartographic_display": cartographic_display,
            "original_land": original_land,
            "original_display": original_display,
            "hole_filled_land": hole_filled_land,
            "network_support": network_support,
            "display_network_support": display_network_support,
            "network": network,
            "network_frame": network_frame,
            "crossings": crossings,
            "used_triangles": used_triangles,
        }

    # Simplification is not mathematically monotonic.  Explicitly retain each
    # smaller display in the next threshold so the final maps remain nested.
    for smaller, larger in ((5, 10), (10, 15)):
        results[larger]["cartographic_land"] = polygonal(make_valid(
            unary_union([results[larger]["cartographic_land"],
                         results[smaller]["cartographic_land"]]).difference(water)))
        results[larger]["cartographic_display"] = polygonal(make_valid(
            unary_union([results[larger]["cartographic_land"],
                         results[larger]["corridor"]])))

    tolerance_m2 = 0.01
    if results[5]["display"].difference(results[10]["display"]).area > tolerance_m2:
        raise RuntimeError("C1 5-minute display polygon is not nested inside 10 minutes")
    if results[10]["display"].difference(results[15]["display"]).area > tolerance_m2:
        raise RuntimeError("C1 10-minute display polygon is not nested inside 15 minutes")
    if any(details["land"].intersection(water).area > 1e-6 for details in results.values()):
        raise RuntimeError("A final C1 analytical polygon overlaps mapped water")
    for minutes, details in results.items():
        non_water_network = details["network"].difference(water)
        outside_length = non_water_network.difference(details["land"].buffer(0.01)).length
        if outside_length > 0.01:
            raise RuntimeError(
                f"{minutes}-minute analytical polygon omits {outside_length:.3f} m of reachable land network")
    if results[5]["cartographic_display"].difference(
            results[10]["cartographic_display"]).area > tolerance_m2:
        raise RuntimeError("Cartographic 5-minute polygon is not nested inside 10 minutes")
    if results[10]["cartographic_display"].difference(
            results[15]["cartographic_display"]).area > tolerance_m2:
        raise RuntimeError("Cartographic 10-minute polygon is not nested inside 15 minutes")

    crossings_all = (gpd.GeoDataFrame(pd.concat(crossing_frames, ignore_index=True), crs=CRS)
                     if crossing_frames else gpd.GeoDataFrame(geometry=[], crs=CRS))
    metadata = {
        "sample_count": len(samples),
        "triangle_count": len(triangle_records),
        "triangles_over_150m": sum(
            record["max_edge_m"] > MAX_TRIANGLE_EDGE_M for record in triangle_records),
        "sample_spacing_m": NETWORK_SAMPLE_SPACING_M,
        "triangulation": "SciPy ordinary Delaunay; experimental C1 proxy",
    }
    return results, origins, water_frame, water, samples, crossings_all, metadata


def _scale_length(map_width):
    candidates = np.asarray([100, 200, 250, 500, 750, 1000, 1500, 2000], dtype=float)
    target = map_width / 5.5
    return float(candidates[np.argmin(np.abs(candidates - target))])


def _draw_scale_and_north(ax, bounds):
    xmin, ymin, xmax, ymax = bounds
    width, height = xmax - xmin, ymax - ymin
    length = _scale_length(width)
    x0 = xmin + width * 0.045
    y0 = ymin + height * 0.045
    ax.plot([x0, x0 + length], [y0, y0], color="#20262a", linewidth=3.0,
            solid_capstyle="butt", zorder=30)
    ax.plot([x0, x0], [y0 - height*0.006, y0 + height*0.006],
            color="#20262a", linewidth=1.1, zorder=30)
    ax.plot([x0 + length, x0 + length], [y0 - height*0.006, y0 + height*0.006],
            color="#20262a", linewidth=1.1, zorder=30)
    label = f"{int(length)} m" if length < 1000 else f"{length/1000:g} km"
    ax.text(x0 + length/2, y0 + height*0.012, label, ha="center", va="bottom",
            fontsize=8.5, color="#20262a", weight="bold", zorder=31,
            bbox={"facecolor":"white", "edgecolor":"none", "alpha":0.72, "pad":1.5})
    ax.text(xmax - width*0.045, ymax - height*0.05, "N\n▲", ha="center", va="top",
            fontsize=11, color="#20262a", weight="bold", linespacing=0.75, zorder=31,
            bbox={"facecolor":"white", "edgecolor":"none", "alpha":0.68, "pad":2})


def _map_base(ax, bounds, water_frame):
    basemap = os_maps_basemap(bounds)
    if basemap:
        ax.imshow(basemap["image"], extent=basemap["extent"], alpha=.65, zorder=0)
    else:
        ax.set_facecolor("#f5f4ef")
    clipped_water = water_frame.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    if not clipped_water.empty:
        clipped_water.plot(ax=ax, facecolor="#d6eaf1", edgecolor="#7ba5b2",
                           linewidth=0.35, alpha=0.72, zorder=1)
    return basemap


def _plot_site_outline(ax, site, linewidth=SITE_LINEWIDTH, zorder=101):
    """Draw one thin black dashed line, without a white casing."""
    for part in _polygon_parts(site.geometry.union_all()):
        x, y = part.exterior.xy
        ax.plot(x, y, color=SITE_COLOUR, linewidth=linewidth,
                linestyle=(0, (5, 3)), solid_joinstyle="round", zorder=zorder)


def _plot_site_origins_schools(ax, site, origins, schools, bounds):
    _plot_site_outline(ax, site)
    visible_schools = schools.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
    if not visible_schools.empty:
        visible_schools.plot(
            ax=ax, marker="o", color=SCHOOL_COLOUR, edgecolor="white",
            linewidth=0.65, markersize=SCHOOL_MARKER_AREA, zorder=102)
    origins.plot(ax=ax, marker="o", color=ORIGIN_COLOUR, edgecolor="white",
                 linewidth=0.65, markersize=ORIGIN_MARKER_AREA, zorder=103)
    for row in origins.itertuples():
        ax.text(row.geometry.x + 7, row.geometry.y + 7, str(row.origin_id),
                ha="left", va="bottom", fontsize=9, color=ORIGIN_COLOUR,
                weight="bold", zorder=104)


def _legend_handles(minutes_list, include_circle=False):
    handles = []
    handles.append(Line2D([], [], color=SITE_COLOUR, lw=SITE_LINEWIDTH,
                          linestyle=(0, (5, 3)), label="Development site"))
    for minutes in minutes_list:
        handles.extend([
            Patch(facecolor=COLOURS[minutes], edgecolor=COLOURS[minutes],
                  alpha=0.30, label=f"{minutes} min catchment"),
            Line2D([], [], color=NETWORK_COLOURS[minutes], lw=1.6,
                   label=f"{minutes} min pedestrian network"),
        ])
    handles.extend([
        Line2D([], [], marker="o", linestyle="none", markerfacecolor=SCHOOL_COLOUR,
               markeredgecolor="white", markeredgewidth=.65,
               markersize=math.sqrt(SCHOOL_MARKER_AREA), label="School"),
        Line2D([], [], marker="o", linestyle="none", markerfacecolor=ORIGIN_COLOUR,
               markeredgecolor="white", markeredgewidth=.65,
               markersize=math.sqrt(ORIGIN_MARKER_AREA), label="Origin"),
    ])
    if include_circle:
        handles.append(Line2D([], [], color="#535b60", lw=1.5,
                              linestyle=(0, (7, 4)),
                              label=f"{2*THEORETICAL_RADIUS_M/1000:g} km diameter"))
    return handles


def _draw_map(ax, results, minutes_list, water_frame, site, origins, schools,
              bounds, theoretical_circle=None):
    basemap = _map_base(ax, bounds, water_frame)
    for minutes in sorted(minutes_list, reverse=True):
        geometry = results[minutes]["cartographic_display"]
        gpd.GeoSeries([geometry], crs=CRS).plot(
            ax=ax, facecolor=COLOURS[minutes], edgecolor=COLOURS[minutes],
            alpha=0.28, linewidth=1.15, zorder=3 + (20-minutes)/10)
    # Plot the longest threshold first.  Shorter thresholds then remain
    # visible over shared links in combined maps.
    for minutes in sorted(minutes_list, reverse=True):
        gpd.GeoSeries([results[minutes]["network"]], crs=CRS).plot(
            ax=ax, color=NETWORK_COLOURS[minutes], linewidth=0.48,
            alpha=0.70, zorder=8 + (20-minutes)/10)
    if theoretical_circle is not None:
        gpd.GeoSeries([theoretical_circle.boundary], crs=CRS).plot(
            ax=ax, color="#535b60", linewidth=1.45, linestyle=(0, (7, 4)), zorder=18)
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    # Draw the project features only after fixing the map extent.  This keeps
    # the site outline above all polygon and network collections in every
    # raster and vector backend.
    site = gpd.read_file(RAW / "site_boundary.geojson").to_crs(CRS)
    _plot_site_origins_schools(ax, site, origins, schools, bounds)
    _draw_scale_and_north(ax, bounds)
    legend = ax.legend(
        handles=_legend_handles(minutes_list, theoretical_circle is not None),
        loc="upper left", bbox_to_anchor=(0.015, 0.985), fontsize=8.1,
        ncol=1 if len(minutes_list) == 1 else 2, frameon=True,
        facecolor="white", edgecolor="#b7bec0", framealpha=0.90,
        borderpad=0.7, labelspacing=0.5, handlelength=2.2)
    legend.set_zorder(1000)
    os_credit = (basemap["attribution"] if basemap else
                 "Contains OS data © Crown copyright and database rights 2026")
    attribution = (
        f"{os_credit} | © OpenStreetMap contributors (ODbL) | "
        "Schools: DfE Get Information about Schools (OGL v3.0)")
    ax.text(0.995, 0.006, attribution, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=5.8, color="#445057", zorder=40,
            bbox={"facecolor":"white", "edgecolor":"none", "alpha":0.70, "pad":1})


def _save_clean_png(path, results, minutes_list, water_frame, site, origins,
                    schools, bounds, circle=None):
    fig = plt.figure(figsize=(14.14, 10.0), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    _draw_map(ax, results, minutes_list, water_frame, site, origins, schools,
              bounds, circle)
    fig.savefig(path, dpi=220, facecolor="white")
    plt.close(fig)


def _save_a3_pdf(path, results, minutes_list, water_frame, site, origins,
                 schools, bounds, summary, circle=None):
    fig = plt.figure(figsize=(420/25.4, 297/25.4), facecolor="white")
    ax = fig.add_axes([0.035, 0.075, 0.93, 0.80])
    _draw_map(ax, results, minutes_list, water_frame, site, origins, schools,
              bounds, circle)
    if len(minutes_list) == 1:
        minutes = minutes_list[0]
        title = f"Crossharbour {minutes}-Minute Walking Catchment"
        row = summary[summary.time_minutes == minutes].iloc[0]
        subtitle = (f"Network travel-time contour  |  {row.analytical_land_area_ha:.1f} ha analytical land  |  "
                    f"{row.reachable_network_length_km:.1f} km reachable network")
    elif circle is None:
        title = "Crossharbour Walking Catchments"
        subtitle = "Network travel-time contours  |  5, 10 and 15 minutes"
    else:
        title = "Crossharbour Walking Catchments and Straight-Line Reference"
        subtitle = (f"Network travel-time contours  |  Dashed circle: {2*THEORETICAL_RADIUS_M/1000:g} km "
                    f"diameter based on 4.8 km/h × {REFERENCE_CIRCLE_MINUTES} minutes")
    fig.text(0.035, 0.955, title, ha="left", va="top", fontsize=20,
             color="#253842", weight="bold")
    fig.text(0.035, 0.918, subtitle, ha="left", va="top", fontsize=9.3,
             color="#586970")
    fig.text(0.035, 0.025,
             "Reviewed pedestrian network; access proxies A/B; 4.8 km/h; shortest travel time from either origin. "
             "Travel-time contour with analytical water exclusion. School markers represent cleaned location coordinates.",
             ha="left", va="bottom", fontsize=7.2, color="#586970")
    fig.savefig(path, facecolor="white", metadata={
        "Title": title,
        "Subject": "Selected network travel-time walking catchment; EPSG:27700; no LSOA boundaries"})
    plt.close(fig)


def _finish_context_map(ax, bounds, basemap, handles, legend_columns=1,
                        data_credit=""):
    ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_aspect("equal"); ax.set_axis_off()
    _draw_scale_and_north(ax, bounds)
    legend = ax.legend(
        handles=handles, loc="upper left", bbox_to_anchor=(.015, .985),
        fontsize=8.1, ncol=legend_columns, frameon=True, facecolor="white",
        edgecolor="#b7bec0", framealpha=.94, borderpad=.7,
        labelspacing=.5, handlelength=2.2)
    legend.set_zorder(1000)
    os_credit = (basemap["attribution"] if basemap else
                 "Contains OS data © Crown copyright and database rights 2026")
    attribution = os_credit
    if data_credit:
        attribution = f"{attribution} | {data_credit}"
    ax.text(.995, .006, attribution, transform=ax.transAxes, ha="right",
            va="bottom", fontsize=5.8, color="#445057", zorder=1001,
            bbox={"facecolor":"white", "edgecolor":"none", "alpha":.70, "pad":1})


def _draw_origin_selection_map(ax, site, origins, network_edges, water_frame):
    site_geometry = site.geometry.union_all()
    # Keep enough context to show both connecting footways while allowing the
    # development boundary and reviewed crossings to remain the visual focus.
    bounds = _fit_bounds(site_geometry.buffer(75).bounds, padding_fraction=.02)
    ax.set_facecolor("#F4F3EF")
    # Use the requested outdoor cartography one matrix level finer than the
    # project default.  The labels are baked into OS raster tiles, so they
    # cannot be resized independently; downsampling the finer tiles and
    # lightly muting the raster keeps them subordinate to the access evidence.
    basemap = os_maps_basemap(
        bounds, layer="Outdoor_27700", tile_matrix="EPSG:27700:11")
    if basemap:
        ax.imshow(
            basemap["image"], extent=basemap["extent"], alpha=.40,
            interpolation="bilinear", zorder=0)
    window = box(*bounds)
    local_edges = network_edges[network_edges.intersects(window)]
    if not local_edges.empty:
        local_edges.plot(ax=ax, color="#68787C", linewidth=.75,
                         alpha=.62, zorder=5)
    selected_mask = pd.Series(False, index=local_edges.index)
    for origin in origins.itertuples():
        u, v = int(origin.matched_edge_u), int(origin.matched_edge_v)
        osmid = str(origin.osmid)
        selected_mask |= (
            ((local_edges.u == u) & (local_edges.v == v) &
             (local_edges.key == int(origin.matched_edge_key))) |
            ((local_edges.u == v) & (local_edges.v == u) &
             local_edges.osmid.astype(str).eq(osmid))
        )
    selected_edges = local_edges[selected_mask].copy()
    if not selected_edges.empty:
        selected_edges["_geometry_key"] = selected_edges.geometry.apply(
            lambda geometry: normalize(geometry).wkb)
        selected_edges = selected_edges.drop_duplicates("_geometry_key")
    if not selected_edges.empty:
        selected_edges.plot(ax=ax, color="#17685F", linewidth=2.4,
                            alpha=.98, zorder=20)
    _plot_site_outline(ax, site, linewidth=1.85, zorder=30)
    origins.plot(
        ax=ax, marker="o", color=ORIGIN_COLOUR, edgecolor="white",
        linewidth=.65, markersize=ORIGIN_MARKER_AREA, zorder=40)
    for row in origins.itertuples():
        is_left = str(row.origin_id) == "A"
        ax.text(
            row.geometry.x + (-5 if is_left else 5), row.geometry.y + 5,
            str(row.origin_id),
            ha="right" if is_left else "left", va="bottom", fontsize=9,
            color=ORIGIN_COLOUR, weight="bold", zorder=41)
    handles = [
        Line2D([], [], color="#68787C", lw=.9, label="Reviewed pedestrian network"),
        Line2D([], [], color="#17685F", lw=2.4,
               label="Selected boundary-crossing footway"),
        Line2D([], [], color=SITE_COLOUR, lw=1.85, linestyle=(0, (5, 3)),
               label="Development site"),
        Line2D([], [], marker="o", linestyle="none",
               markerfacecolor=ORIGIN_COLOUR, markeredgecolor="white",
               markeredgewidth=.65, markersize=math.sqrt(ORIGIN_MARKER_AREA),
               label="Origin"),
    ]
    _finish_context_map(
        ax, bounds, basemap, handles,
        data_credit="© OpenStreetMap contributors (ODbL)")


def _draw_all_schools_map(ax, site, schools, water_frame):
    valid_schools = schools[
        schools.geometry.notna() & ~schools.geometry.is_empty].copy()
    extent_geometry = unary_union(
        [valid_schools.geometry.union_all(), site.geometry.union_all()])
    bounds = _fit_bounds(extent_geometry.bounds, padding_fraction=.075)
    basemap = _map_base(ax, bounds, water_frame)
    valid_schools.plot(
        ax=ax, marker="o", color=SCHOOL_COLOUR, edgecolor="white",
        linewidth=.65, markersize=SCHOOL_MARKER_AREA, zorder=20)
    _plot_site_outline(ax, site, linewidth=SITE_LINEWIDTH, zorder=30)
    handles = [
        Line2D([], [], marker="o", linestyle="none",
               markerfacecolor=SCHOOL_COLOUR, markeredgecolor="white",
               markeredgewidth=.65, markersize=math.sqrt(SCHOOL_MARKER_AREA),
               label="School"),
        Line2D([], [], color=SITE_COLOUR, lw=SITE_LINEWIDTH,
               linestyle=(0, (5, 3)), label="Development site"),
    ]
    _finish_context_map(
        ax, bounds, basemap, handles,
        data_credit="Schools: DfE Get Information about Schools (OGL v3.0)")
    return len(valid_schools)


def _save_context_map_pair(basename, draw_function, title, subtitle, footer):
    png = MAPS / f"{basename}.png"
    pdf = MAPS / f"{basename}.pdf"
    fig = plt.figure(figsize=(14.14, 10), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    draw_function(ax)
    fig.savefig(png, dpi=220, facecolor="white")
    plt.close(fig)

    fig = plt.figure(figsize=(420/25.4, 297/25.4), facecolor="white")
    ax = fig.add_axes([.035, .075, .93, .80])
    ax.imshow(plt.imread(png)); ax.set_axis_off()
    fig.text(.035, .955, title, ha="left", va="top", fontsize=20,
             color="#253842", weight="bold")
    fig.text(.035, .918, subtitle, ha="left", va="top", fontsize=9.3,
             color="#586970")
    fig.text(.035, .025, footer, ha="left", va="bottom", fontsize=7.2,
             color="#586970")
    fig.savefig(pdf, facecolor="white", metadata={"Title": title})
    plt.close(fig)
    return [png, pdf]


def _write_outputs(results, origins, water_frame, samples, crossings, metadata):
    rows_land, rows_display, rows_cartographic, rows_unmasked, rows_corridor, summary_rows = [], [], [], [], [], []
    for minutes, details in results.items():
        base = {"time_minutes": minutes, "threshold_s": minutes*60,
                "method": "C1 ordinary-Delaunay contour proxy"}
        rows_land.append({**base, "geometry_role": "analytical_land_catchment",
                          "area_ha": details["land"].area/10000, "geometry":details["land"]})
        rows_display.append({**base, "geometry_role": "display_catchment_with_bridge_corridors",
                             "area_ha": details["display"].area/10000, "geometry":details["display"]})
        rows_cartographic.append({**base, "geometry_role": "cartographic_regularised_display",
                                  "area_ha": details["cartographic_display"].area/10000,
                                  "geometry": details["cartographic_display"]})
        rows_unmasked.append({**base, "geometry_role": "contour_before_water_mask",
                              "area_ha": details["unmasked"].area/10000, "geometry":details["unmasked"]})
        if not details["corridor"].is_empty:
            rows_corridor.append({**base, "geometry_role":"cartographic_bridge_corridors",
                                  "area_m2":details["corridor"].area,
                                  "geometry":details["corridor"]})
        summary_rows.append({
            "time_minutes": minutes,
            "threshold_s": minutes*60,
            "analytical_land_area_ha": details["land"].area/10000,
            "display_area_ha": details["display"].area/10000,
            "bridge_corridor_area_m2": details["corridor"].area,
            "hole_count": _hole_count(details["land"]),
            "non_water_hole_area_filled_m2": details["hole_filled_land"].difference(
                details["original_land"]).area,
            "network_support_area_added_m2": details["land"].difference(
                details["hole_filled_land"]).area,
            "reachable_land_network_outside_m": details["network"].difference(
                water_frame.geometry.union_all()).difference(details["land"].buffer(0.01)).length,
            "cartographic_symmetric_difference_m2": details["land"].symmetric_difference(
                details["cartographic_land"]).area,
            "analytical_component_count": len(_polygon_parts(details["land"])),
            "display_component_count": len(_polygon_parts(details["display"])),
            "reachable_network_length_km": details["network"].length/1000,
            "triangle_count_contributing_to_contour": details["used_triangles"],
            "water_overlap_m2": details["land"].intersection(water_frame.geometry.union_all()).area,
        })
    _write_layer(gpd.GeoDataFrame(rows_land, crs=CRS), OUT_GPKG,
                 "analytical_land_catchments", first=True)
    _write_layer(gpd.GeoDataFrame(rows_display, crs=CRS), OUT_GPKG,
                 "display_catchments")
    _write_layer(gpd.GeoDataFrame(rows_cartographic, crs=CRS), OUT_GPKG,
                 "cartographic_display_catchments")
    _write_layer(gpd.GeoDataFrame(rows_unmasked, crs=CRS), OUT_GPKG,
                 "unmasked_c1_contours")
    if rows_corridor:
        _write_layer(gpd.GeoDataFrame(rows_corridor, crs=CRS), OUT_GPKG,
                     "bridge_connectivity_corridors")
    _write_layer(samples, OUT_GPKG, "c1_network_samples")
    if not crossings.empty:
        _write_layer(crossings, OUT_GPKG, "reachable_water_crossings")
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT_SUMMARY, index=False)
    return summary


def _write_report(summary, metadata, circle_center):
    lines = [
        "# Final C1 walking-catchment map series",
        "",
        "The final workshop map series uses C1, the ordinary-Delaunay travel-time contour proxy selected by the project author. Routing inputs remain unchanged. C1 is not described as a reproduction of Lin et al. because it does not use constrained Delaunay triangulation.",
        "",
        f"A single triangulation and time surface is used for 300, 600 and 900 seconds, ensuring nested 5/10/15-minute contours. Every analytical polygon subtracts the validated OSM water layer. Fully enclosed non-water gaps caused by the 150 m triangle filter are filled, then water is subtracted again. A {NETWORK_INCLUSION_HALF_WIDTH_M:g} m/side land-only support strip is unioned around the authoritative reachable network so routed lines cannot protrude from the analytical catchment. Threshold-specific reachable bridge links remain governed by the separate bridge-corridor policy.",
        "",
        "| Time | Analytical land | Display area | Network length | Holes | Land components | Display components |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples():
        lines.append(
            f"| {row.time_minutes} min | {row.analytical_land_area_ha:.2f} ha | "
            f"{row.display_area_ha:.2f} ha | {row.reachable_network_length_km:.2f} km | "
            f"{int(row.hole_count)} | {int(row.analytical_component_count)} | "
            f"{int(row.display_component_count)} |")
    lines.extend([
        "",
        "## Cartographic rules",
        "",
        "- PNG files contain only the map, legend, scale bar, north arrow and required attribution.",
        "- PDF files add a title and concise method note.",
        "- Schools are shown as `School` point symbols without individual names or accessibility claims.",
        "- The development site is an unfilled, thin black dashed outline without a white casing.",
        "- Origins A and B are labelled beside the points without leader lines.",
        "- School and origin symbols use the same point diameter in the map and legend.",
        "- All three individual maps use the same 15-minute map extent and therefore the same scale.",
        f"- The plotted edge uses {CARTOGRAPHIC_SIMPLIFY_TOLERANCE_M:g} m topology-preserving simplification followed by a {CARTOGRAPHIC_REGULARISATION_M:g} m close/open operation; a {DISPLAY_NETWORK_INCLUSION_HALF_WIDTH_M:g} m/side visible network support strip is restored after both operations.",
        "- Reachable network lines use darker versions of their corresponding 5-, 10- and 15-minute catchment colours.",
        "- LSOA boundaries are not drawn.",
        "- Bridge links use the normal catchment/network symbology and are not highlighted in red.",
        "",
        "## Straight-line reference circle",
        "",
        f"The combined reference map uses a {THEORETICAL_RADIUS_M:.0f} m radius ({2*THEORETICAL_RADIUS_M:.0f} m diameter) circle centred on the midpoint of origins A and B at approximately E {circle_center.x:.1f}, N {circle_center.y:.1f}. The radius equals 4.8 km/h multiplied by {REFERENCE_CIRCLE_MINUTES} minutes and contains the complete 15-minute cartographic catchment. It is a theoretical unobstructed straight-line reference, not a walking catchment.",
        "",
        "## Context maps",
        "",
        "`figure_00a_site_origin_selection` enlarges the development site over a muted OS Maps Outdoor_27700 background and shows the reviewed OSM pedestrian network, the two selected boundary-crossing footways and origins A/B with their crossing angles. A finer WMTS tile matrix reduces the visual prominence of baked-in place labels. `figure_00b_all_cleaned_schools` shows every cleaned school with a usable geometry before catchment filtering.",
        "",
        f"C1 surface metadata: `{json.dumps(metadata, sort_keys=True)}`",
    ])
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_obsolete_maps():
    removed = []
    for name in sorted(OBSOLETE_MAP_NAMES):
        path = MAPS / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return removed


def run_final_c1_maps():
    for folder in (GIS, TABLES, MAPS, METHOD):
        folder.mkdir(parents=True, exist_ok=True)
    results, origins, water_frame, _, samples, crossings, metadata = build_c1_catchments()
    site = gpd.read_file(RAW / "site_boundary.geojson").to_crs(CRS)
    schools = gpd.read_file(CLEANED / "schools_cleaned.gpkg").to_crs(CRS)
    summary = _write_outputs(results, origins, water_frame, samples, crossings, metadata)

    # Re-read the written geometries for rendering.  This isolates the map
    # backend from the temporary geometry collections used while constructing
    # and writing the C1 surface, and makes raster/PDF rendering deterministic.
    cartographic = gpd.read_file(
        OUT_GPKG, layer="cartographic_display_catchments").to_crs(CRS)
    map_results = {}
    for minutes in (5, 10, 15):
        map_results[minutes] = {
            "cartographic_display": cartographic.loc[
                cartographic.time_minutes == minutes].geometry.iloc[0],
            "network": gpd.read_file(
                GIS / f"reachable_network_{minutes}min.gpkg").to_crs(
                    CRS).geometry.union_all(),
        }
    map_origins = gpd.read_file(PROCESSED / "site_origins.gpkg").to_crs(CRS)
    network_edges = gpd.read_file(
        OPENSTREETMAP / "pedestrian_network_reviewed.gpkg", layer="edges").to_crs(CRS)

    output_paths = []
    output_paths.extend(_save_context_map_pair(
        MAP_BASENAMES["origin_selection"],
        lambda ax: _draw_origin_selection_map(
            ax, site, map_origins, network_edges, water_frame),
        "Crossharbour Site Origin Selection",
        "Two existing public footways intersect the site boundary at distinct northern access locations",
        "A and B are planning proxy origins at two distinct existing footway crossings of the northern site boundary; private/service links and tangential contacts are excluded.",
    ))
    visible_school_count = int((schools.geometry.notna() & ~schools.geometry.is_empty).sum())
    output_paths.extend(_save_context_map_pair(
        MAP_BASENAMES["schools_overview"],
        lambda ax: _draw_all_schools_map(ax, site, schools, water_frame),
        "Cleaned Schools Available for Mapping",
        f"{visible_school_count} open London schools retained after status, overseas and duplicate exclusions",
        "Points use accepted cleaned coordinates from the school geocoding stage. They represent school locations, not manually verified pedestrian entrances.",
    ))

    center = Point(float(map_origins.geometry.x.mean()),
                   float(map_origins.geometry.y.mean()))
    circle = center.buffer(THEORETICAL_RADIUS_M, resolution=256)
    if not circle.buffer(.01).covers(map_results[15]["cartographic_display"]):
        raise RuntimeError(
            "The straight-line reference circle does not contain the 15-minute catchment")
    shared_single_bounds = _fit_bounds(
        map_results[15]["cartographic_display"].bounds)
    for minutes in (5, 10, 15):
        bounds = shared_single_bounds
        png = MAPS / f"{MAP_BASENAMES[minutes]}.png"
        pdf = MAPS / f"{MAP_BASENAMES[minutes]}.pdf"
        _save_clean_png(png, map_results, [minutes], water_frame, site, map_origins,
                        schools, bounds)
        _save_a3_pdf(pdf, map_results, [minutes], water_frame, site, map_origins,
                     schools, bounds, summary)
        output_paths.extend([png, pdf])

    combined_geometry = unary_union(
        [map_results[m]["cartographic_display"] for m in (5, 10, 15)])
    combined_bounds = _fit_bounds(combined_geometry.bounds)
    combined_png = MAPS / f"{MAP_BASENAMES['combined']}.png"
    combined_pdf = MAPS / f"{MAP_BASENAMES['combined']}.pdf"
    _save_clean_png(combined_png, map_results, [5, 10, 15], water_frame, site,
                    map_origins, schools, combined_bounds)
    _save_a3_pdf(combined_pdf, map_results, [5, 10, 15], water_frame, site,
                 map_origins, schools, combined_bounds, summary)
    output_paths.extend([combined_png, combined_pdf])

    circle_bounds = _fit_bounds(unary_union([combined_geometry, circle]).bounds)
    circle_png = MAPS / f"{MAP_BASENAMES['circle']}.png"
    circle_pdf = MAPS / f"{MAP_BASENAMES['circle']}.pdf"
    _save_clean_png(circle_png, map_results, [5, 10, 15], water_frame, site,
                    map_origins, schools, circle_bounds, circle)
    _save_a3_pdf(circle_pdf, map_results, [5, 10, 15], water_frame, site,
                 map_origins, schools, circle_bounds, summary, circle)
    output_paths.extend([circle_png, circle_pdf])

    if not all(path.exists() and path.stat().st_size > 1000 for path in output_paths):
        raise RuntimeError("One or more final maps were not written correctly")
    removed = _remove_obsolete_maps()
    _write_report(summary, metadata, center)
    OUT_METADATA.write_text(json.dumps({
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_method": "C1 ordinary-Delaunay contour proxy",
        "routing_modified": False,
        "analysis_crs": CRS,
        "theoretical_circle_radius_m": THEORETICAL_RADIUS_M,
        "theoretical_circle_diameter_m": 2*THEORETICAL_RADIUS_M,
        "reference_circle_minutes": REFERENCE_CIRCLE_MINUTES,
        "reference_circle_contains_15min_cartographic_catchment": True,
        "school_marker_area_points_squared": SCHOOL_MARKER_AREA,
        "origin_marker_area_points_squared": ORIGIN_MARKER_AREA,
        "site_outline_colour": SITE_COLOUR,
        "site_outline_width_points": SITE_LINEWIDTH,
        "all_cleaned_school_count": visible_school_count,
        "individual_map_bounds_epsg27700": list(shared_single_bounds),
        "cartographic_regularisation_m": CARTOGRAPHIC_REGULARISATION_M,
        "cartographic_simplify_tolerance_m": CARTOGRAPHIC_SIMPLIFY_TOLERANCE_M,
        "network_inclusion_half_width_m": NETWORK_INCLUSION_HALF_WIDTH_M,
        "display_network_inclusion_half_width_m": DISPLAY_NETWORK_INCLUSION_HALF_WIDTH_M,
        "circle_center_easting": center.x,
        "circle_center_northing": center.y,
        "map_outputs": [str(path.relative_to(ROOT)) for path in output_paths],
        "obsolete_map_outputs_removed": removed,
        "surface": metadata,
    }, indent=2), encoding="utf-8")
    print(summary.to_string(index=False))
    print(f"Wrote {len(output_paths)} final map files; removed {len(removed)} obsolete map files")
    return summary
