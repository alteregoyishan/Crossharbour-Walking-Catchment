"""Read-only discovery of OS NGD collections around the Crossharbour site."""
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

from .api_credentials import os_project_key
from .paths import RAW, QA, OUTPUTS

BASE = 'https://api.os.uk/features/ngd/ofa/v1'
CRS_URI = 'http://www.opengis.net/def/crs/EPSG/0/27700'
CRS = 'EPSG:27700'
DISCOVERY = QA / 'os_ngd_discovery'
INVENTORY_CSV = OUTPUTS / 'methodology/os_ngd_feature_inventory.csv'
SCHEMA_CSV = OUTPUTS / 'qa/os_ngd_sample_schema.csv'
SAMPLES_GPKG = QA / 'os_ngd_discovery_samples.gpkg'
METADATA_JSON = OUTPUTS / 'methodology/os_ngd_discovery_metadata.json'

# These are collection-name families, resolved against the live collections
# endpoint. The highest available numeric schema version is selected.
TARGET_FAMILIES = {
    'building': 'bld-fts-building-',
    'building_part': 'bld-fts-buildingpart-',
    'building_access_location': 'bld-fts-buildingaccesslocation-',
    'site': 'lus-fts-site-',
    'site_access_location': 'lus-fts-siteaccesslocation-',
    'site_routing_point': 'lus-fts-siteroutingpoint-',
    'land': 'lnd-fts-land-',
    'structure': 'str-fts-structure-',
    'compound_structure': 'str-fts-compoundstructure-',
    'road_track_or_path': 'trn-fts-roadtrackorpath-',
    'path_link': 'trn-ntwk-pathlink-',
    'pavement_link': 'trn-ntwk-pavementlink-',
}

PURPOSES = {
    'building': 'building footprint, use and residential allocation',
    'building_part': 'building subdivision and height/floor refinement',
    'building_access_location': 'candidate building entrance evidence',
    'site': 'urban site/block grouping and land use',
    'site_access_location': 'candidate site/school access evidence',
    'site_routing_point': 'site routing reference point',
    'land': 'land-cover parcels and block/barrier representation',
    'structure': 'physical structures and barrier validation',
    'compound_structure': 'combined structure polygons and barriers',
    'road_track_or_path': 'road/path surface polygons adjoining reachable links',
    'path_link': 'OS path-network comparison only',
    'pavement_link': 'pedestrian pavement-network comparison only',
}

RELEVANT_TERMS = (
    'osid', 'toid', 'description', 'class', 'use', 'access', 'entrance',
    'building', 'site', 'uprn', 'address', 'floor', 'height', 'area', 'level',
    'obscured', 'status', 'physical', 'route', 'path', 'road', 'land', 'school',
)


def _safe_json(payload):
    """Remove any accidental key query parameter before saving a response."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str) and value.startswith(('http://', 'https://')):
            parts = urlsplit(value)
            query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != 'key']
            return urlunsplit((parts.scheme, parts.netloc, parts.path,
                               urlencode(query), parts.fragment))
        return value
    return clean(payload)


def _request(session, key, url, params=None):
    response = session.get(url, headers={'key': key}, params=params, timeout=120)
    return response, response.json() if response.ok else None


def _latest_collections(collections):
    by_id = {collection['id']: collection for collection in collections}
    selected = {}
    for label, prefix in TARGET_FAMILIES.items():
        candidates = []
        for collection_id in by_id:
            match = re.fullmatch(re.escape(prefix) + r'(\d+)', collection_id)
            if match:
                candidates.append((int(match.group(1)), collection_id))
        if candidates:
            selected[label] = by_id[max(candidates)[1]]
    return selected


def _geometry_type(schema, features):
    reference = schema.get('properties', {}).get('geometry', {}).get('$ref', '')
    if reference:
        return reference.rsplit('/', 1)[-1].replace('.json', '')
    types = sorted({feature.get('geometry', {}).get('type', '') for feature in features
                    if feature.get('geometry')})
    return '|'.join(filter(None, types)) or 'unknown'


def _normalise(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def run_discovery(buffer_m=350, limit=10):
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    key = os_project_key()
    if not key:
        raise RuntimeError('Add the existing OS project key as OS_API_KEY or OS_PLACES_API_KEY in .env.')
    DISCOVERY.mkdir(parents=True, exist_ok=True)
    INVENTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_CSV.parent.mkdir(parents=True, exist_ok=True)
    site = gpd.read_file(RAW / 'site_boundary.geojson').to_crs(CRS)
    query_area = site.geometry.union_all().buffer(buffer_m)
    bbox = ','.join(f'{value:.3f}' for value in query_area.bounds)
    session = requests.Session()
    collections_response, collections_payload = _request(session, key, f'{BASE}/collections')
    if not collections_response.ok:
        raise RuntimeError(f'OS NGD collections endpoint returned {collections_response.status_code}.')
    conformance_response, conformance_payload = _request(session, key, f'{BASE}/conformance')
    selected = _latest_collections(collections_payload.get('collections', []))
    missing_families = sorted(set(TARGET_FAMILIES) - set(selected))
    inventory_rows = []
    schema_rows = []
    sample_layers = []
    SAMPLES_GPKG.unlink(missing_ok=True)
    for label, collection in selected.items():
        collection_id = collection['id']
        root = f'{BASE}/collections/{collection_id}'
        schema_response, schema = _request(session, key, root + '/schema')
        query_response, queryables = _request(session, key, root + '/queryables')
        item_params = {'bbox': bbox, 'bbox-crs': CRS_URI, 'crs': CRS_URI, 'limit': limit}
        item_response, items = _request(session, key, root + '/items', item_params)
        accessible = schema_response.ok and query_response.ok and item_response.ok
        features = items.get('features', []) if items else []
        schema = schema or {}
        queryables = queryables or {}
        schema_properties = schema.get('properties', {})
        queryable_properties = queryables.get('properties', {})
        actual_fields = sorted({key_ for feature in features
                                for key_ in feature.get('properties', {})})
        non_null_fields = sorted({key_ for feature in features
                                  for key_, value in feature.get('properties', {}).items()
                                  if value not in (None, '', [], {})})
        relevant = sorted({field for field in schema_properties
                           if any(term in field.lower() for term in RELEVANT_TERMS)})
        valid = 0
        geometry_types = []
        for feature in features:
            geometry = feature.get('geometry')
            if geometry:
                geom = shape(geometry)
                valid += int(geom.is_valid)
                geometry_types.append(geom.geom_type)
        inventory_rows.append({
            'priority_family': label,
            'collection_id': collection_id,
            'collection_title': collection.get('title', ''),
            'purpose': PURPOSES[label],
            'access_status': 'accessible' if accessible else 'not_accessible',
            'schema_http': schema_response.status_code,
            'queryables_http': query_response.status_code,
            'sample_http': item_response.status_code,
            'geometry_type': _geometry_type(schema, features),
            'sample_returned': len(features),
            'valid_sample_geometries': valid,
            'invalid_sample_geometries': len(geometry_types) - valid,
            'sample_geometry_types': '|'.join(sorted(set(geometry_types))),
            'schema_field_count': len(schema_properties),
            'queryable_field_count': len(queryable_properties),
            'queryable_fields': '|'.join(queryable_properties),
            'relevant_fields': '|'.join(relevant),
            'sample_non_null_fields': '|'.join(non_null_fields),
            'spatial_filter_tested': 'bbox with bbox-crs EPSG:27700; output crs EPSG:27700',
            'storage_crs': collection.get('storageCrs', ''),
        })
        for field, definition in schema_properties.items():
            sample_values = []
            for feature in features:
                value = feature.get('properties', {}).get(field)
                if value not in (None, '', [], {}):
                    rendered = str(_normalise(value))
                    if rendered not in sample_values:
                        sample_values.append(rendered)
                if len(sample_values) == 3:
                    break
            schema_rows.append({
                'collection_id': collection_id,
                'field': field,
                'type': json.dumps(definition.get('type'), ensure_ascii=False),
                'required': field in schema.get('required', []),
                'queryable': field in queryable_properties,
                'description': definition.get('description', ''),
                'sample_values': ' | '.join(sample_values),
            })
        for name, payload in [('schema', schema), ('queryables', queryables), ('sample', items or {})]:
            (DISCOVERY / f'{collection_id}_{name}.json').write_text(
                json.dumps(_safe_json(payload), indent=2, ensure_ascii=False), encoding='utf-8')
        if features:
            rows = []
            for feature in features:
                properties = {key_: _normalise(value)
                              for key_, value in feature.get('properties', {}).items()}
                properties['geometry'] = shape(feature['geometry'])
                rows.append(properties)
            frame = gpd.GeoDataFrame(rows, geometry='geometry', crs=CRS)
            frame.to_file(SAMPLES_GPKG, layer=collection_id, driver='GPKG',
                          engine='pyogrio', append=SAMPLES_GPKG.exists())
            sample_layers.append(collection_id)
    inventory = pd.DataFrame(inventory_rows).sort_values('priority_family')
    inventory.to_csv(INVENTORY_CSV, index=False)
    pd.DataFrame(schema_rows).to_csv(SCHEMA_CSV, index=False)
    metadata = {
        'source': 'OS NGD API – Features live discovery',
        'base_endpoint': BASE,
        'run_at_utc': datetime.now(timezone.utc).isoformat(),
        'project_key_source': 'OS project key loaded from .env; value not recorded',
        'api_key_recorded': False,
        'collections_endpoint_http': collections_response.status_code,
        'available_collection_count': len(collections_payload.get('collections', [])),
        'selected_collection_count': len(selected),
        'missing_target_families': missing_families,
        'query_bbox_epsg27700': list(query_area.bounds),
        'site_buffer_m': buffer_m,
        'sample_limit_per_collection': limit,
        'saved_sample_layers': sample_layers,
        'conformance_http': conformance_response.status_code,
        'conformance_classes': (conformance_payload or {}).get('conformsTo', []),
        'existing_analysis_modified': False,
    }
    METADATA_JSON.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(f'Inspected {len(inventory)} selected collections; samples limited to {limit} each.')
    print(f'Inventory: {INVENTORY_CSV}')
    return inventory
