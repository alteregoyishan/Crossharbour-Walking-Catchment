"""OS NGD water acquisition and optional OS Maps raster basemap support."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import geopandas as gpd
import numpy as np
from PIL import Image
from pyproj import Transformer
import requests
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from shapely import box, make_valid
from shapely.geometry import shape
import xml.etree.ElementTree as ET

from .api_credentials import env_value, os_project_key
from .paths import CLEANED, EXTERNAL, PROCESSED, OUTPUTS

CRS = 'EPSG:27700'
OS_NGD_DIR = PROCESSED / 'OrdnanceSurvey'
OS_NGD_WATER = OS_NGD_DIR / 'water_polygons_os_ngd.gpkg'
OSM_WATER = PROCESSED / 'OpenStreetMap' / 'water_polygons.gpkg'
NGD_BASE = 'https://api.os.uk/features/ngd/ofa/v1'
MAPS_BASE = 'https://api.os.uk/maps/raster/v1/zxy'
WEB_MERCATOR_LIMIT = 20037508.342789244


def analysis_bounds():
    """Complete population coverage plus 300 m, in British National Grid."""
    population = gpd.read_file(CLEANED / 'lsoa_2021_population_cleaned.gpkg').to_crs(CRS)
    reach_path = OUTPUTS / 'spatial/reachable_network_15min.gpkg'
    geometries = [population.geometry.union_all()]
    if reach_path.exists():
        geometries.append(gpd.read_file(reach_path).to_crs(CRS).geometry.union_all())
    bounds = gpd.GeoSeries(geometries, crs=CRS).total_bounds
    return box(*bounds).buffer(300)


def selected_water_path():
    """Use the explicitly selected water source; OSM remains the default."""
    selected = env_value('WATER_SOURCE', 'osm').lower()
    if selected == 'osm' and OSM_WATER.exists():
        return OSM_WATER
    if selected == 'os-ngd' and OS_NGD_WATER.exists():
        return OS_NGD_WATER
    if OSM_WATER.exists():
        return OSM_WATER
    if OS_NGD_WATER.exists():
        return OS_NGD_WATER
    raise FileNotFoundError('No water polygons found. Run scripts/09_prepare_water.py first.')


def water_metadata_path(path=None):
    path = Path(path or selected_water_path())
    return path.with_suffix('.json')


def _serialisable(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean_next_url(url):
    """Remove any returned API key before using or recording a pagination URL."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != 'key']
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def download_os_ngd_water(force=False):
    """Download Water v3 polygons through OS NGD API – Features.

    The API is an OGC API Features service. Requests use the complete analysis
    bounding box in the collection's default EPSG:27700 CRS and follow every
    server-provided `next` link.
    """
    if OS_NGD_WATER.exists() and not force:
        print(f'Reusing OS NGD water: {OS_NGD_WATER}')
        return OS_NGD_WATER
    key = os_project_key()
    if not key:
        raise RuntimeError('Add the existing OS project key as OS_API_KEY or OS_PLACES_API_KEY in .env.')
    collection = env_value('OS_NGD_WATER_COLLECTION', 'wtr-fts-water-3')
    area = analysis_bounds()
    bbox = ','.join(f'{value:.3f}' for value in area.bounds)
    crs_uri = 'http://www.opengis.net/def/crs/EPSG/0/27700'
    endpoint = f'{NGD_BASE}/collections/{collection}/items'
    params = {'key': key, 'bbox': bbox, 'bbox-crs': crs_uri, 'crs': crs_uri, 'limit': 100}
    session = requests.Session()
    features = []
    page_count = 0
    url = endpoint
    while url:
        response = session.get(url, params=params, timeout=120)
        if response.status_code == 400 and page_count == 0:
            # Some service deployments infer the collection CRS and reject the
            # explicit OGC CRS parameters. The numerical bbox remains BNG.
            params = {'key': key, 'bbox': bbox, 'limit': 100}
            response = session.get(url, params=params, timeout=120)
        if response.status_code == 403:
            raise RuntimeError('OS NGD returned 403. Add OS NGD API – Features to the OS Data Hub project and check OS_NGD_API_KEY.')
        if response.status_code == 404:
            raise RuntimeError(f'OS NGD collection not available: {collection}. Check OS_NGD_WATER_COLLECTION and project entitlement.')
        response.raise_for_status()
        payload = response.json()
        features.extend(payload.get('features', []))
        page_count += 1
        next_link = next((link.get('href') for link in payload.get('links', [])
                          if link.get('rel') == 'next'), None)
        url = _clean_next_url(urljoin(NGD_BASE + '/', next_link)) if next_link else None
        params = {'key': key} if url else None
        if page_count > 10000:
            raise RuntimeError('OS NGD pagination exceeded 10,000 pages; stop and review the query.')
    rows = []
    repairs = []
    for feature in features:
        geometry_json = feature.get('geometry')
        if not geometry_json:
            continue
        geom = shape(geometry_json)
        if geom.geom_type not in ('Polygon', 'MultiPolygon'):
            continue
        before_valid = geom.is_valid
        if not before_valid:
            geom = make_valid(geom)
        if geom.geom_type not in ('Polygon', 'MultiPolygon'):
            polygon_parts = [part for part in getattr(geom, 'geoms', [])
                             if part.geom_type in ('Polygon', 'MultiPolygon')]
            if not polygon_parts:
                continue
            geom = gpd.GeoSeries(polygon_parts, crs=CRS).union_all()
        properties = {key_: _serialisable(value) for key_, value in feature.get('properties', {}).items()}
        properties['feature_id'] = str(feature.get('id', properties.get('osid', '')))
        properties['geometry'] = geom
        rows.append(properties)
        repairs.append({'feature_id': properties['feature_id'], 'valid_before': before_valid,
                        'valid_after': geom.is_valid, 'area_m2': float(geom.area)})
    water = gpd.GeoDataFrame(rows, geometry='geometry', crs=CRS)
    if water.empty or not water.is_valid.all():
        raise RuntimeError('OS NGD returned no valid Water polygon features for the analysis area.')
    # BBOX queries can return features crossing the box. Keep full returned
    # polygons because clipping them would corrupt land-only zone denominators.
    OS_NGD_DIR.mkdir(parents=True, exist_ok=True)
    OS_NGD_WATER.unlink(missing_ok=True)
    water.to_file(OS_NGD_WATER, layer='water_polygons', driver='GPKG', engine='pyogrio')
    qa_path = OUTPUTS / 'qa/os_ngd_water_geometry_qa.csv'
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    import pandas as pd
    pd.DataFrame(repairs).to_csv(qa_path, index=False)
    metadata = {
        'source': 'OS National Geographic Database, Water Features, Water Feature Type',
        'api': 'OS NGD API – Features',
        'endpoint': endpoint,
        'collection': collection,
        'downloaded_at_utc': datetime.now(timezone.utc).isoformat(),
        'query_bbox_epsg27700': list(area.bounds),
        'requested_crs': CRS,
        'feature_count': len(water),
        'page_count': page_count,
        'processing': 'Polygon/MultiPolygon only; retain full returned geometry; make_valid only where logged.',
        'coverage': 'Complete supplied population-zone bounds plus 300 m; also includes 15-minute network reach.',
        'api_key_recorded': False,
        'sha256': sha256(OS_NGD_WATER.read_bytes()).hexdigest(),
    }
    water_metadata_path(OS_NGD_WATER).write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(f'Cached {len(water)} OS NGD water polygons at {OS_NGD_WATER}')
    return OS_NGD_WATER


def _tile_index(x, y, zoom):
    size = 2 ** zoom
    tx = int(math.floor((x + WEB_MERCATOR_LIMIT) / (2 * WEB_MERCATOR_LIMIT) * size))
    ty = int(math.floor((WEB_MERCATOR_LIMIT - y) / (2 * WEB_MERCATOR_LIMIT) * size))
    return max(0, min(size - 1, tx)), max(0, min(size - 1, ty))


def _tile_bounds(x, y, zoom):
    tile_span = 2 * WEB_MERCATOR_LIMIT / (2 ** zoom)
    left = -WEB_MERCATOR_LIMIT + x * tile_span
    right = left + tile_span
    top = WEB_MERCATOR_LIMIT - y * tile_span
    bottom = top - tile_span
    return left, bottom, right, top


def os_maps_basemap(bounds_bng, zoom=15, layer=None, tile_matrix=None):
    """Return an OS Maps basemap array and BNG extent.

    Returns ``None`` when no key is configured, allowing reproducible maps to
    fall back to local vector context without making a network request.  The
    optional layer and tile-matrix arguments allow one figure to override the
    project-wide environment defaults without mutating process state.
    """
    key = os_project_key()
    if not key:
        return None
    layer = layer or env_value('OS_MAPS_LAYER', 'Light_27700')
    if layer.endswith('_27700'):
        return _os_maps_bng_basemap(bounds_bng, key, layer, tile_matrix)
    if layer not in {'Road_3857', 'Outdoor_3857', 'Light_3857'}:
        raise ValueError('Unsupported OS_MAPS_LAYER. Use Light_27700 or a documented OS Maps layer.')
    to_3857 = Transformer.from_crs(CRS, 3857, always_xy=True)
    xmin, ymin = to_3857.transform(bounds_bng[0], bounds_bng[1])
    xmax, ymax = to_3857.transform(bounds_bng[2], bounds_bng[3])
    x0, y1 = _tile_index(xmin, ymin, zoom)
    x1, y0 = _tile_index(xmax, ymax, zoom)
    cache = EXTERNAL / 'ordnance_survey/maps_tiles' / layer / str(zoom)
    cache.mkdir(parents=True, exist_ok=True)
    mosaic = Image.new('RGB', ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256))
    for tile_y in range(y0, y1 + 1):
        for tile_x in range(x0, x1 + 1):
            tile_path = cache / str(tile_x) / f'{tile_y}.png'
            if not tile_path.exists():
                tile_path.parent.mkdir(parents=True, exist_ok=True)
                url = f'{MAPS_BASE}/{layer}/{zoom}/{tile_x}/{tile_y}.png'
                response = requests.get(url, params={'key': key}, timeout=60)
                if response.status_code == 403:
                    raise RuntimeError('OS Maps returned 403. Add OS Maps API to the project and check OS_MAPS_API_KEY.')
                response.raise_for_status()
                tile_path.write_bytes(response.content)
            with Image.open(tile_path) as tile:
                mosaic.paste(tile.convert('RGB'), ((tile_x - x0) * 256, (tile_y - y0) * 256))
    source_bounds = (_tile_bounds(x0, y1, zoom)[0], _tile_bounds(x0, y1, zoom)[1],
                     _tile_bounds(x1, y0, zoom)[2], _tile_bounds(x1, y0, zoom)[3])
    source = np.asarray(mosaic)
    target_width = 1800
    target_height = max(600, int(target_width * (bounds_bng[3] - bounds_bng[1]) /
                                 (bounds_bng[2] - bounds_bng[0])))
    target = np.zeros((target_height, target_width, 3), dtype=np.uint8)
    source_transform = from_bounds(*source_bounds, source.shape[1], source.shape[0])
    target_transform = from_bounds(*bounds_bng, target_width, target_height)
    for band in range(3):
        reproject(source[:, :, band], target[:, :, band],
                  src_transform=source_transform, src_crs='EPSG:3857',
                  dst_transform=target_transform, dst_crs=CRS,
                  resampling=Resampling.bilinear)
    return {'image': target,
            'extent': (bounds_bng[0], bounds_bng[2], bounds_bng[1], bounds_bng[3]),
            'crs': CRS,
            'layer': layer, 'zoom': zoom,
            'attribution': env_value('OS_MAPS_ATTRIBUTION',
                'Contains OS data © Crown copyright and database rights 2026')}


def _wmts_matrix(key, identifier):
    """Read the official BNG tile grid from OS Maps GetCapabilities."""
    cache = EXTERNAL / 'ordnance_survey/maps_tiles/wmts_capabilities.xml'
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(f'{MAPS_BASE.rsplit("/zxy", 1)[0]}/wmts',
            headers={'key': key}, params={'service': 'WMTS', 'request': 'GetCapabilities',
                                         'version': '2.0.0'}, timeout=90)
        if response.status_code == 403:
            raise RuntimeError('OS Maps returned 403. Add OS Maps API to the existing OS Data Hub project.')
        response.raise_for_status()
        cache.write_bytes(response.content)
    root = ET.parse(cache).getroot()
    namespaces = {'wmts': 'http://www.opengis.net/wmts/1.0',
                  'ows': 'http://www.opengis.net/ows/1.1'}
    for matrix_set in root.findall('.//wmts:TileMatrixSet', namespaces):
        set_id = matrix_set.find('ows:Identifier', namespaces)
        if set_id is None or set_id.text != 'EPSG:27700':
            continue
        for matrix in matrix_set.findall('wmts:TileMatrix', namespaces):
            matrix_id = matrix.find('ows:Identifier', namespaces)
            if matrix_id is None or matrix_id.text != identifier:
                continue
            def value(tag):
                return matrix.find(f'wmts:{tag}', namespaces).text
            top_left = [float(item) for item in value('TopLeftCorner').split()]
            return {'identifier': identifier,
                    'resolution': float(value('ScaleDenominator')) * 0.00028,
                    'origin_x': top_left[0], 'origin_y': top_left[1],
                    'tile_width': int(value('TileWidth')),
                    'tile_height': int(value('TileHeight')),
                    'matrix_width': int(value('MatrixWidth')),
                    'matrix_height': int(value('MatrixHeight'))}
    raise RuntimeError(f'OS Maps tile matrix not found in capabilities: {identifier}')


def _os_maps_bng_basemap(bounds_bng, key, layer, tile_matrix=None):
    """Fetch native EPSG:27700 WMTS tiles without raster reprojection."""
    if layer not in {'Road_27700', 'Outdoor_27700', 'Light_27700', 'Leisure_27700'}:
        raise ValueError('Invalid EPSG:27700 OS Maps layer.')
    matrix_id = tile_matrix or env_value('OS_MAPS_TILE_MATRIX', 'EPSG:27700:9')
    matrix = _wmts_matrix(key, matrix_id)
    span_x = matrix['tile_width'] * matrix['resolution']
    span_y = matrix['tile_height'] * matrix['resolution']
    xmin, ymin, xmax, ymax = bounds_bng
    epsilon = 1e-8
    col0 = math.floor((xmin - matrix['origin_x']) / span_x)
    col1 = math.floor((xmax - matrix['origin_x'] - epsilon) / span_x)
    row0 = math.floor((matrix['origin_y'] - ymax) / span_y)
    row1 = math.floor((matrix['origin_y'] - ymin - epsilon) / span_y)
    if (col0 < 0 or row0 < 0 or col1 >= matrix['matrix_width'] or
            row1 >= matrix['matrix_height']):
        raise ValueError('Requested bounds are outside the OS Maps EPSG:27700 tile matrix.')
    cache = EXTERNAL / 'ordnance_survey/maps_tiles' / layer / matrix_id.replace(':', '_')
    mosaic = Image.new('RGB', ((col1 - col0 + 1) * matrix['tile_width'],
                               (row1 - row0 + 1) * matrix['tile_height']))
    wmts_url = f'{MAPS_BASE.rsplit("/zxy", 1)[0]}/wmts'
    for row in range(row0, row1 + 1):
        for col in range(col0, col1 + 1):
            tile_path = cache / str(col) / f'{row}.png'
            if not tile_path.exists():
                tile_path.parent.mkdir(parents=True, exist_ok=True)
                params = {'service': 'WMTS', 'request': 'GetTile', 'version': '2.0.0',
                          'layer': layer, 'style': 'default',
                          'tileMatrixSet': 'EPSG:27700', 'tileMatrix': matrix_id,
                          'tileRow': row, 'tileCol': col, 'outputFormat': 'image/png'}
                response = requests.get(wmts_url, headers={'key': key}, params=params, timeout=60)
                if response.status_code == 403:
                    raise RuntimeError('OS Maps returned 403. Add OS Maps API to the existing OS Data Hub project.')
                response.raise_for_status()
                tile_path.write_bytes(response.content)
            with Image.open(tile_path) as tile:
                mosaic.paste(tile.convert('RGB'), ((col - col0) * matrix['tile_width'],
                                                   (row - row0) * matrix['tile_height']))
    left = matrix['origin_x'] + col0 * span_x
    right = matrix['origin_x'] + (col1 + 1) * span_x
    bottom = matrix['origin_y'] - (row1 + 1) * span_y
    top = matrix['origin_y'] - row0 * span_y
    # Matplotlib imshow expects (left, right, bottom, top), unlike a GIS bbox.
    extent = (left, right, bottom, top)
    return {'image': np.asarray(mosaic), 'extent': extent, 'crs': CRS,
            'layer': layer, 'tile_matrix': matrix_id,
            'attribution': env_value('OS_MAPS_ATTRIBUTION',
                'Contains OS data © Crown copyright and database rights 2026')}
