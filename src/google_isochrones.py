"""Optional Google walking-isochrone comparison with the OSMnx analysis."""
from datetime import datetime, timezone
import json

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import pandas as pd
import requests
from shapely import make_valid
from shapely.geometry import shape

from .api_credentials import env_value
from .os_data_services import os_maps_basemap, selected_water_path
from .paths import PROCESSED, OPENSTREETMAP, OUTPUTS, RAW

CRS = 'EPSG:27700'
MINUTES = (5, 10, 15)
ENDPOINT = 'https://isochrones.googleapis.com/v1/isochrones:generate'
OUT_GPKG = OUTPUTS / 'gis/google_walking_isochrones.gpkg'
OUT_LAND_GPKG = OUTPUTS / 'gis/google_walking_isochrones_land_only.gpkg'
OUT_TABLE = OUTPUTS / 'tables/google_isochrone_comparison.csv'
OUT_META = OUTPUTS / 'methodology/google_isochrone_metadata.json'
OUT_MAP = OUTPUTS / 'maps/google_isochrone_comparison.png'


def _request_isochrone(session, key, latitude, longitude, minutes):
    body = {
        'location': {'latitude': float(latitude), 'longitude': float(longitude)},
        'travelDuration': f'{minutes * 60}s',
        'travelMode': 'WALK',
        'travelDirection': 'FROM',
        'routingPreference': 'TRAFFIC_UNAWARE',
        'enableSmoothing': False,
        'polygonFidelity': 'HIGH',
    }
    response = session.post(ENDPOINT, headers={
        'X-Goog-Api-Key': key,
        'Content-Type': 'application/json',
    }, json=body, timeout=120)
    if response.status_code == 403:
        raise RuntimeError(
            'Google returned 403. Enable the Isochrones API for the project, attach billing, '
            'and allow isochrones.googleapis.com in this key restriction.')
    response.raise_for_status()
    payload = response.json()
    geojson = payload.get('isochrone', {}).get('geoJson')
    if not geojson:
        raise RuntimeError(f'Google returned no GeoJSON isochrone for {minutes} minutes.')
    geometry = shape(geojson)
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.is_empty:
        raise RuntimeError(f'Unexpected Google isochrone geometry: {geometry.geom_type}')
    return geometry


def acquire_google_isochrones(force=False):
    """Request A and B separately, then union equal-duration polygons."""
    key = env_value('GOOGLE_MAPS_API_KEY')
    if not key:
        raise RuntimeError('Add GOOGLE_MAPS_API_KEY to .env before running Google Isochrones.')
    if OUT_GPKG.exists() and not force:
        individuals = gpd.read_file(OUT_GPKG, layer='origin_isochrones')
        unions = gpd.read_file(OUT_GPKG, layer='combined_isochrones')
        print(f'Reusing Google isochrones: {OUT_GPKG}')
        return individuals, unions
    origins = gpd.read_file(PROCESSED / 'site_origins.gpkg').to_crs(4326)
    if set(origins.origin_id) != {'A', 'B'}:
        raise ValueError('Expected reviewed site origins A and B.')
    acquired = datetime.now(timezone.utc).isoformat()
    rows = []
    with requests.Session() as session:
        for origin in origins.itertuples():
            for minutes in MINUTES:
                geom = _request_isochrone(session, key, origin.geometry.y, origin.geometry.x, minutes)
                rows.append({'origin_id': origin.origin_id, 'minutes': minutes,
                             'travel_mode': 'WALK', 'travel_direction': 'FROM',
                             'routing_preference': 'TRAFFIC_UNAWARE',
                             'smoothing': False, 'polygon_fidelity': 'HIGH',
                             'acquired_at_utc': acquired, 'geometry': geom})
    individuals = gpd.GeoDataFrame(rows, crs=4326).to_crs(CRS)
    union_rows = []
    for minutes, frame in individuals.groupby('minutes'):
        geom = frame.geometry.union_all()
        union_rows.append({'minutes': int(minutes), 'origins': 'A+B',
                           'combination_rule': 'union of equal-duration FROM polygons',
                           'area_ha': geom.area / 10000, 'geometry': geom})
    unions = gpd.GeoDataFrame(union_rows, crs=CRS)
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    OUT_GPKG.unlink(missing_ok=True)
    individuals.to_file(OUT_GPKG, layer='origin_isochrones', driver='GPKG', engine='pyogrio')
    unions.to_file(OUT_GPKG, layer='combined_isochrones', driver='GPKG', engine='pyogrio', append=True)
    OUT_META.parent.mkdir(parents=True, exist_ok=True)
    OUT_META.write_text(json.dumps({
        'source': 'Google Maps Platform Isochrones API',
        'endpoint': ENDPOINT,
        'documentation': 'https://developers.google.com/maps/documentation/isochrones',
        'acquired_at_utc': acquired,
        'requests': len(rows),
        'origins': ['A', 'B'],
        'minutes': list(MINUTES),
        'request_parameters': {'travelMode': 'WALK', 'travelDirection': 'FROM',
            'routingPreference': 'TRAFFIC_UNAWARE', 'enableSmoothing': False,
            'polygonFidelity': 'HIGH'},
        'combination': 'A and B requested separately; union at each duration to represent min(T_A,T_B).',
        'api_key_recorded': False,
    }, indent=2), encoding='utf-8')
    return individuals, unions


def derive_land_only_isochrones(unions):
    """Subtract mapped water for presentation while preserving Google raw data."""
    water_path = selected_water_path()
    water = gpd.read_file(water_path).to_crs(CRS).geometry.union_all()
    rows = []
    for row in unions.itertuples():
        raw = row.geometry
        land = make_valid(raw.difference(water))
        rows.append({
            'minutes': int(row.minutes),
            'source': 'derived from cached Google isochrone',
            'display_treatment': 'selected water polygons subtracted; routing result unchanged',
            'water_source_file': str(water_path),
            'raw_area_ha': raw.area / 10000,
            'water_removed_ha': raw.intersection(water).area / 10000,
            'area_ha': land.area / 10000,
            'geometry': land,
        })
    land_only = gpd.GeoDataFrame(rows, crs=CRS)
    OUT_LAND_GPKG.parent.mkdir(parents=True, exist_ok=True)
    OUT_LAND_GPKG.unlink(missing_ok=True)
    land_only.to_file(OUT_LAND_GPKG, layer='combined_land_only', driver='GPKG', engine='pyogrio')
    return land_only


def compare_isochrones(unions, land_only):
    rows = []
    for minutes in MINUTES:
        google = unions.loc[unions.minutes == minutes, 'geometry'].iloc[0]
        google_land = land_only.loc[land_only.minutes == minutes, 'geometry'].iloc[0]
        osm = gpd.read_file(OUTPUTS / f'gis/walking_catchment_{minutes}min.gpkg',
                            layer='catchment_raw').to_crs(CRS).geometry.union_all()
        intersection = google.intersection(osm).area
        combined = google.union(osm).area
        water_overlap = google.area - google_land.area
        rows.append({'minutes': minutes, 'google_raw_area_ha': google.area / 10000,
                     'google_water_masked_display_area_ha': google_land.area / 10000,
                     'google_water_overlap_ha': water_overlap / 10000,
                     'google_water_overlap_pct': 100 * water_overlap / google.area if google.area else 0,
                     'osm_25m_representation_area_ha': osm.area / 10000,
                     'intersection_area_ha': intersection / 10000,
                     'google_only_area_ha': google.difference(osm).area / 10000,
                     'osm_only_area_ha': osm.difference(google).area / 10000,
                     'jaccard_overlap': intersection / combined if combined else 0,
                     'interpretation': 'Comparison of two different polygon constructions; not an accuracy score.'})
    comparison = pd.DataFrame(rows)
    OUT_TABLE.parent.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(OUT_TABLE, index=False)
    return comparison


def make_comparison_map(unions, land_only, comparison):
    site = gpd.read_file(RAW / 'site_boundary.geojson').to_crs(CRS)
    origins = gpd.read_file(PROCESSED / 'site_origins.gpkg').to_crs(CRS)
    edges = gpd.read_file(OPENSTREETMAP / 'pedestrian_network_reviewed.gpkg', layer='edges').to_crs(CRS)
    all_geoms = []
    osm_geoms = {}
    for minutes in MINUTES:
        geom = gpd.read_file(OUTPUTS / f'gis/walking_catchment_{minutes}min.gpkg',
                             layer='catchment_raw').to_crs(CRS).geometry.union_all()
        osm_geoms[minutes] = geom
        all_geoms.extend([geom, unions.loc[unions.minutes == minutes, 'geometry'].iloc[0]])
    bounds = gpd.GeoSeries(all_geoms, crs=CRS).total_bounds
    pad = 250
    bounds = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
    basemap = os_maps_basemap(bounds)
    fig, axes = plt.subplots(1, 3, figsize=(16.54, 9.2), sharex=True, sharey=True)
    colours = {5: '#c16b38', 10: '#176b72', 15: '#746398'}
    for ax, minutes in zip(axes, MINUTES):
        if basemap:
            ax.imshow(basemap['image'], extent=basemap['extent'], zorder=0)
        else:
            local = edges.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]]
            local.plot(ax=ax, color='#d7d9d4', linewidth=.35, zorder=0)
        google = unions.loc[unions.minutes == minutes, 'geometry'].iloc[0]
        google_land = land_only.loc[land_only.minutes == minutes, 'geometry'].iloc[0]
        gpd.GeoSeries([google_land], crs=CRS).plot(
            ax=ax, facecolor=colours[minutes], alpha=.20, edgecolor='none', zorder=2)
        # Keep the unmodified API boundary visible for audit. Only the fill is
        # water-masked so the map does not visually imply walking on water.
        gpd.GeoSeries([google], crs=CRS).boundary.plot(
            ax=ax, color=colours[minutes], linewidth=1.5, zorder=3)
        gpd.GeoSeries([osm_geoms[minutes]], crs=CRS).boundary.plot(
            ax=ax, color='#202f37', linewidth=1.2, linestyle='--', zorder=4)
        site.plot(ax=ax, facecolor='#bd542a', edgecolor='white', linewidth=.7, zorder=5)
        origins.plot(ax=ax, color='#fff7c6', edgecolor='#202f37', markersize=24, zorder=6)
        row = comparison[comparison.minutes == minutes].iloc[0]
        ax.set_title(
            f'{minutes} minutes  |  raw overlap {row.jaccard_overlap:.2f}\n'
            f'{row.google_water_overlap_pct:.1f}% of Google polygon overlaps mapped water',
            fontsize=11)
        ax.set(xlim=(bounds[0], bounds[2]), ylim=(bounds[1], bounds[3]))
        ax.set_aspect('equal'); ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle('Google Walking Isochrones compared with the OSMnx service-area representation',
                 x=.055, ha='left', fontsize=20, weight='bold', color='#253842')
    fig.text(.055, .925,
             'Google raw boundary retained; display fill subtracts selected OSM water. '
             'OSMnx: reachable network buffered by 25 m.', fontsize=9, color='#586970')
    handles = [Patch(facecolor='#176b72', alpha=.20, edgecolor='none', label='Google land-only display fill'),
               Line2D([], [], color='#176b72', label='Google raw API boundary'),
               Line2D([], [], color='#202f37', linestyle='--', label='OSM/OSMnx 25 m representation'),
               Patch(facecolor='#bd542a', label='Development site')]
    fig.legend(handles=handles, loc='lower left', bbox_to_anchor=(.05, .035), ncol=4, frameon=False)
    source = ('OS Maps API Light basemap; ' + basemap['attribution']) if basemap else 'Local OSM network context; © OpenStreetMap contributors'
    fig.text(.055, .018, f'{source}  |  Google isochrones © Google  |  EPSG:27700', fontsize=8, color='#586970')
    fig.subplots_adjust(left=.045, right=.985, bottom=.08, top=.89, wspace=.025)
    OUT_MAP.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_MAP, dpi=220, facecolor='white')
    plt.close(fig)


def run_google_comparison(force=False):
    individuals, unions = acquire_google_isochrones(force=force)
    land_only = derive_land_only_isochrones(unions)
    comparison = compare_isochrones(unions, land_only)
    make_comparison_map(unions, land_only, comparison)
    metadata = json.loads(OUT_META.read_text()) if OUT_META.exists() else {}
    metadata['derived_display_output'] = {
        'file': str(OUT_LAND_GPKG),
        'layer': 'combined_land_only',
        'treatment': 'Selected OSM water polygons subtracted for display only; raw Google outputs preserved.',
        'new_google_requests': 6 if force else 0,
    }
    OUT_META.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(f'Google comparison written to {OUT_GPKG}, {OUT_LAND_GPKG}, {OUT_TABLE} and {OUT_MAP}')
    return comparison
