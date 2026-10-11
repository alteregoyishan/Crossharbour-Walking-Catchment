"""Reproducible service-area and population analysis of existing network reach."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from shapely import box, make_valid, Point
from shapely.ops import unary_union

from .paths import ROOT, RAW, CLEANED, PROCESSED, OPENSTREETMAP, OUTPUTS
from .config import load_config
from .pedestrian_network import load_pedestrian_graph
from .walking_catchment import reachable_network, _edge_geometry
from .pedestrian_network_qa import classify_pedestrian_access
from .api_credentials import env_value
from .os_data_services import (OSM_WATER, download_os_ngd_water,
                               selected_water_path, water_metadata_path)

GIS = OUTPUTS / 'gis'
TABLES = OUTPUTS / 'tables'
QA_OUT = OUTPUTS / 'qa'
METHOD = OUTPUTS / 'methodology'
WATER = OSM_WATER  # compatibility name for the documented OSM fallback
CRS = 'EPSG:27700'
BUFFERS = (25, 50, 75)
MINUTES = (5, 10, 15)
PRIMARY_BUFFER = 25  # retained pre-existing corridor assumption, not fitted to population


def polygonal(geometry):
    """Retain every polygon part, without smoothing or minimum-area filtering."""
    if geometry.geom_type in ('Polygon', 'MultiPolygon'):
        return geometry
    if hasattr(geometry, 'geoms'):
        return unary_union([polygonal(g) for g in geometry.geoms
                            if g.geom_type in ('Polygon', 'MultiPolygon', 'GeometryCollection')])
    return unary_union([])


def write_gpkg(frame, path, layer, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not append:
        path.unlink(missing_ok=True)
    frame.to_file(path, layer=layer, driver='GPKG', engine='pyogrio')


def prepare_osm_water(force=False):
    """Download the OSM fallback once; OSMnx caches Overpass responses too."""
    if WATER.exists():
        if force:
            WATER.unlink()
            WATER.with_suffix('.json').unlink(missing_ok=True)
        else:
            print(f'Reusing OSM water: {WATER}')
            return WATER
    population = gpd.read_file(CLEANED / 'lsoa_2021_population_cleaned.gpkg').to_crs(CRS)
    reach = gpd.read_file(OUTPUTS / 'spatial/reachable_network_15min.gpkg').to_crs(CRS)
    area = box(*unary_union([population.geometry.union_all(), reach.geometry.union_all()]).bounds).buffer(300)
    wgs = gpd.GeoSeries([area], crs=CRS).to_crs(4326).iloc[0]
    ox.settings.use_cache = True
    ox.settings.cache_folder = ROOT / 'data/external/osm/water_cache'
    ox.settings.requests_timeout = 180
    tags = {'natural': 'water', 'waterway': ['riverbank', 'dock'], 'landuse': 'reservoir'}
    features = ox.features_from_polygon(wgs, tags)
    features = features[features.geom_type.isin(['Polygon', 'MultiPolygon'])].to_crs(CRS)
    rows, repair = [], []
    for (kind, osmid), row in features.iterrows():
        before = row.geometry
        geom = polygonal(make_valid(before)) if not before.is_valid else before
        repair.append({'element_type': kind, 'osmid': osmid, 'valid_before': before.is_valid,
                       'area_before_m2': before.area, 'area_after_m2': geom.area,
                       'area_change_m2': geom.area-before.area})
        rows.append({'element_type': kind, 'osmid': str(osmid), 'name': str(row.get('name', '')),
                     'natural': str(row.get('natural', '')), 'water': str(row.get('water', '')),
                     'waterway': str(row.get('waterway', '')), 'geometry': geom})
    water = gpd.GeoDataFrame(rows, crs=CRS)
    if water.empty or not water.is_valid.all():
        raise RuntimeError('No valid water polygons obtained')
    write_gpkg(water, WATER, 'water_polygons')
    QA_OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(repair).to_csv(QA_OUT / 'water_geometry_qa.csv', index=False)
    meta = {'source': 'OpenStreetMap via Overpass / OSMnx features_from_polygon',
            'source_url': 'https://www.openstreetmap.org/copyright',
            'downloaded_at_utc': datetime.now(timezone.utc).isoformat(),
            'osmnx_version': ox.__version__, 'endpoint': ox.settings.overpass_url,
            'query_tags_union': tags, 'query_bounds_epsg27700': list(area.bounds),
            'crs': CRS, 'polygon_count': len(water),
            'processing': 'Polygon features; documented make_valid where necessary; retain full returned polygons; dissolve only during overlay.',
            'coverage': 'Complete supplied population-zone bounds plus 300 m; also includes 15-minute reach.',
            'sha256': sha256(WATER.read_bytes()).hexdigest()}
    WATER.with_suffix('.json').write_text(json.dumps(meta, indent=2))
    print(f'Cached {len(water)} water polygons at {WATER}')
    return WATER


def prepare_water(source='auto', force=False):
    """Prepare the explicitly selected water source; OSM is the default."""
    if source not in {'auto', 'os-ngd', 'osm'}:
        raise ValueError('source must be auto, os-ngd or osm')
    if source == 'auto':
        source = env_value('WATER_SOURCE', 'osm').lower()
    if source == 'os-ngd':
        return download_os_ngd_water(force=force)
    path = prepare_osm_water(force=force)
    print('Using OSM water as the selected analysis source.')
    return path


def population_qa(population):
    raw = gpd.read_file(RAW / 'lsoa_2021_population.gpkg')
    unique = raw.drop_duplicates('LSOA21CD').set_index('LSOA21CD')
    clean = population.set_index('LSOA21CD')
    pop = pd.to_numeric(population['usual_residents_total'], errors='coerce')
    same = (set(clean.index) == set(unique.index) and
            clean['usual_residents_total'].sort_index().equals(
                unique['usual_residents_total'].sort_index()))
    overlap = population.area.sum() - population.geometry.union_all().area
    checks = [
        ('crs', str(population.crs), population.crs.to_epsg() == 27700),
        ('population_field', 'usual_residents_total', True),
        ('baseline', 'ONS Census 2021 population baseline', True),
        ('missing_population', int(pop.isna().sum()), not pop.isna().any()),
        ('negative_population', int((pop < 0).sum()), (pop >= 0).all()),
        ('duplicate_geographies', int(population.LSOA21CD.duplicated().sum()), not population.LSOA21CD.duplicated().any()),
        ('invalid_geometries', int((~population.is_valid).sum()), population.is_valid.all()),
        ('missing_geometries', int(population.geometry.isna().sum()), not population.geometry.isna().any()),
        ('unique_source_population_preserved', bool(same), same),
        ('raw_total_including_duplicate', int(raw.usual_residents_total.sum()), True),
        ('unique_population_total', int(pop.sum()), True),
        ('overlap_area_m2', float(overlap), overlap <= 1),
        ('repairs_this_stage', 0, True),
    ]
    qa = pd.DataFrame([{'check': k, 'value': v, 'status': 'pass' if ok else 'fail'} for k,v,ok in checks])
    qa.to_csv(TABLES / 'population_qa_summary.csv', index=False)
    # Preserve the existing repair's before/after-area evidence in the final QA directory.
    shutil.copy2(TABLES / 'population_cleaning_log.csv', QA_OUT / 'population_prior_corrections.csv')
    if (qa.status == 'fail').any():
        raise RuntimeError('Population QA failed; see population_qa_summary.csv')
    return pop


def area_weight(population, catchment, water=None):
    """Full-zone denominators; optional land subtraction in both numerator and denominator."""
    rows = []
    for zone in population.itertuples():
        source = zone.geometry if water is None else polygonal(zone.geometry.difference(water))
        denom = float(source.area)
        if denom <= 0:
            raise ValueError(f'Zero-area zone: {zone.LSOA21CD}')
        intersection = polygonal(source.intersection(catchment))
        if intersection.is_empty or intersection.area <= 0:
            continue
        fraction = float(intersection.area / denom)
        if not -1e-10 <= fraction <= 1 + 1e-10:
            raise ValueError(f'Invalid area fraction: {zone.LSOA21CD} {fraction}')
        fraction = min(1.0, max(0.0, fraction))
        rows.append({'geography_id': zone.LSOA21CD,
                     'original_population': float(zone.usual_residents_total),
                     'zone_area_m2': denom, 'intersection_area_m2': float(intersection.area),
                     'area_fraction': fraction,
                     'estimated_population': float(zone.usual_residents_total) * fraction,
                     'geometry': intersection})
    return gpd.GeoDataFrame(rows, columns=['geography_id','original_population','zone_area_m2',
        'intersection_area_m2','area_fraction','estimated_population','geometry'], crs=CRS)


def verify_network(cfg):
    graph_path = OPENSTREETMAP / 'pedestrian_network_reviewed_with_origins.graphml'
    graph = load_pedestrian_graph(graph_path)
    origins = gpd.read_file(PROCESSED / 'site_origins.gpkg').to_crs(CRS)
    if set(origins.origin_id) != {'A','B'}:
        raise ValueError('Expected reviewed origins A and B')
    for row in origins.itertuples():
        node = graph.nodes[int(row.site_access_node)]
        if Point(node['x'], node['y']).distance(row.geometry) > .001:
            raise ValueError('Routing graph origin differs from reviewed origin')
    times = nx.multi_source_dijkstra_path_length(graph, origins.site_access_node.astype(int).tolist(),
                                                weight='travel_time_s')
    speed = cfg['walking']['speed_kmh'] / 3.6
    for *_, edge in graph.edges(data=True):
        if not np.isclose(edge['length']/speed, float(edge['travel_time_s']), atol=1e-7):
            raise ValueError('Routing graph weights do not match configured speed')
    networks, checks = {}, []
    for minutes in MINUTES:
        source = OUTPUTS / f'spatial/reachable_network_{minutes}min.gpkg'
        saved = gpd.read_file(source).to_crs(CRS)
        actual, partial = reachable_network(graph, times, minutes*60, cfg['walking']['speed_kmh'])
        geom = saved.geometry.union_all()
        # Tiny buffer accommodates representation roundoff, never changes saved geometry.
        difference = actual.difference(geom.buffer(.0001)).length + geom.difference(actual.buffer(.0001)).length
        if difference > .01:
            raise ValueError(f'{minutes}-minute saved network differs from routed partial-edge geometry')
        networks[minutes] = geom
        shutil.copy2(source, GIS / source.name)
        checks.append({'check':'reachable_network_matches_recomputed_partial_edges', 'minutes':minutes,
                       'value':difference, 'unit':'m', 'status':'pass', 'partial_directed_edges':partial})
    restricted = []
    for u,v,k,edge in graph.edges(keys=True,data=True):
        if classify_pedestrian_access(edge.get('access'),edge.get('foot'),edge.get('highway')) == 'restricted_pedestrian_access':
            arrival = times.get(u, np.inf)
            if arrival < 900:
                restricted.append({'u':u,'v':v,'key':k,'osmid':str(edge.get('osmid')),
                                   'arrival_seconds':arrival})
    pd.DataFrame(restricted, columns=['u','v','key','osmid','arrival_seconds']).to_csv(
        QA_OUT/'restricted_links_within_15min.csv',index=False)
    if restricted:
        raise RuntimeError('Restricted links reachable within 15 minutes require resolution')
    checks.append({'check':'restricted_links_reachable_full_graph_15min','value':0,'status':'pass'})
    return graph, origins, times, networks, checks


def build_scenarios(population, networks, water, checks):
    scenarios, results, intersections = {}, [], {}
    coverage = population.geometry.union_all()
    land_population = population.copy()
    land_population.geometry = population.geometry.apply(lambda geom: polygonal(geom.difference(water)))
    for geom in land_population.geometry:
        if not geom.is_valid or geom.area <= 0:
            raise ValueError('Invalid land-only zone denominator')
    for minutes in MINUTES:
        for width in BUFFERS:
            raw = networks[minutes].buffer(width)
            if not raw.is_valid:
                raise ValueError('Unexpected invalid buffered network; review before repair')
            if minutes > 5:
                smaller = scenarios[minutes-5,width,'standard']
                fragment = smaller.difference(raw)
                outside = float(fragment.area)
                # Polygonised circular caps can create sub-metre boundary slivers.
                # Permit only tiny area AND a 0.1 m boundary neighbourhood.
                if outside > 0:
                    near_boundary = fragment.difference(raw.buffer(.1)).area <= 1e-8
                    if outside > .5 or outside/smaller.area > 1e-6 or not near_boundary:
                        raise ValueError(f'Fundamentally non-nested polygon: {outside} m2')
                    raw = raw.union(smaller)
                    checks.append({'check':'buffer_arc_discretisation_repair', 'minutes':minutes,
                        'buffer_m':width,'value':outside,'unit':'m2','status':'documented_repair',
                        'notes':'Union only sub-0.5 m2 slivers within 0.1 m boundary; <1 ppm smaller area.'})
            land = polygonal(raw.difference(water))
            for method, geom in [('standard', raw), ('land_only', land)]:
                if geom.is_empty or not geom.is_valid:
                    raise ValueError('Invalid service area')
                scenarios[minutes,width,method] = geom
                weighted = area_weight(land_population if method == 'land_only' else population, geom)
                intersections[minutes,width,method] = weighted
                estimated = float(weighted.estimated_population.sum())
                missing = float(geom.difference(coverage).area)
                if missing > 1:
                    raise ValueError(f'Population coverage gap: {missing} m2')
                results.append({'walking_threshold_min':minutes,'network_buffer_m':width,
                    'area_method':method,'catchment_area_ha':geom.area/10000,
                    'estimated_population':estimated,'population_density_per_ha':estimated/(geom.area/10000),
                    'population_method':'standard_areal_weighting' if method=='standard' else 'land_only_areal_weighting',
                    'population_baseline':'ONS Census 2021','uncovered_area_m2':missing})
                checks.append({'check':'polygon_valid', 'minutes':minutes,'buffer_m':width,
                               'area_method':method,'value':True,'status':'pass'})
                layer = f'catchment_{"raw" if method=="standard" else "land_only"}_{minutes}min_{width}m'
                frame = gpd.GeoDataFrame([{'minutes':minutes,'buffer_m':width,'area_method':method,
                                          'area_m2':geom.area,'geometry':geom}], crs=CRS)
                write_gpkg(frame, GIS/'catchment_scenarios.gpkg',layer,
                           append=(minutes,width,method)!=(5,25,'standard'))
    for width in BUFFERS:
        for method in ['standard','land_only']:
            for lo,hi in [(5,10),(10,15)]:
                non_nested = scenarios[lo,width,method].difference(scenarios[hi,width,method]).area
                checks.append({'check':f'containment_{lo}_in_{hi}','buffer_m':width,
                               'area_method':method,'value':non_nested,'unit':'m2',
                               'status':'pass' if non_nested<=.01 else 'fail'})
                if non_nested > .01:
                    raise ValueError('Non-nested service areas')
    for minutes in MINUTES:
        path = GIS/f'walking_catchment_{minutes}min.gpkg'
        for method in ['standard','land_only']:
            geom = scenarios[minutes,PRIMARY_BUFFER,method]
            write_gpkg(gpd.GeoDataFrame([{'minutes':minutes,'buffer_m':PRIMARY_BUFFER,
                'area_m2':geom.area,'geometry':geom}],crs=CRS), path,
                'catchment_raw' if method=='standard' else 'catchment_land_only', append=method=='land_only')
    band_rows=[]
    for method in ['standard','land_only']:
        prev = unary_union([])
        for lo,hi in [(0,5),(5,10),(10,15)]:
            geom = scenarios[hi,PRIMARY_BUFFER,method]
            band = polygonal(geom.difference(prev))
            band_rows.append({'band':f'{lo}-{hi} min','area_method':method,
                              'buffer_m':PRIMARY_BUFFER,'area_m2':band.area,'geometry':band})
            prev=geom
    bands=gpd.GeoDataFrame(band_rows,crs=CRS)
    for method, frame in bands.groupby('area_method'):
        overlap = frame.area.sum()-frame.geometry.union_all().area
        if overlap > .01:
            raise ValueError('Isochrone bands overlap')
        checks.append({'check':'non_overlapping_bands','area_method':method,'value':overlap,'status':'pass'})
    write_gpkg(bands,GIS/'walking_isochrone_bands.gpkg','isochrone_bands')
    for method in ['standard','land_only']:
        write_gpkg(intersections[10,PRIMARY_BUFFER,method],GIS/'population_intersections_10min.gpkg',
                   method,append=method=='land_only')
    sensitivity=pd.DataFrame(results)
    sensitivity.to_csv(TABLES/'catchment_population_sensitivity.csv',index=False)
    changes=[]
    for method in ['standard','land_only']:
        frame=sensitivity[(sensitivity.walking_threshold_min==10)&(sensitivity.area_method==method)].set_index('network_buffer_m')
        for start,end in [(25,50),(50,75),(25,75)]:
            row={'area_method':method,'from_buffer_m':start,'to_buffer_m':end}
            for field in ['catchment_area_ha','estimated_population','population_density_per_ha']:
                row[field+'_change_pct']=100*(frame.loc[end,field]/frame.loc[start,field]-1)
            changes.append(row)
    pd.DataFrame(changes).to_csv(TABLES/'catchment_sensitivity_changes_10min.csv',index=False)
    return scenarios, sensitivity, intersections


def school_results(graph, times, polygon, water):
    """Unreviewed nearest-edge projections are diagnostics, never verified destinations."""
    schools=gpd.read_file(CLEANED/'schools_cleaned.gpkg').to_crs(CRS)
    source_qa=pd.read_csv(ROOT/'data/qa/schools_qa.csv',dtype={'URN':str})
    excluded=source_qa[source_qa.qa_action=='remove']
    excluded.to_csv(QA_OUT/'schools_excluded.csv',index=False)
    kept=source_qa[source_qa.qa_action=='keep'].set_index('URN')
    statuses=kept['EstablishmentStatus (name)'].to_dict()
    schools['status']=schools.URN.astype(str).map(statuses)
    if schools.URN.duplicated().any() or schools.geometry.is_empty.any() or schools.geometry.isna().any():
        raise ValueError('School core QA failed')
    if schools.status.str.contains('Closed',case=False,na=False).any():
        raise ValueError('Closed schools remain')
    write_gpkg(schools,GIS/'schools_cleaned.gpkg','schools_cleaned')
    qa=[{'check':'retained_schools','count':len(schools),'status':'pass'},
        {'check':'duplicate_URN','count':int(schools.URN.duplicated().sum()),'status':'pass'},
        {'check':'missing_coordinate','count':int(schools.geometry.isna().sum()),'status':'pass'}]
    for reason,frame in excluded.groupby('qa_reason'):
        qa.append({'check':'excluded_'+reason,'count':len(frame),'status':'documented'})
    for field in ['TypeOfEstablishment (name)','StatutoryLowAge','StatutoryHighAge','SchoolCapacity','NumberOfPupils']:
        qa.append({'check':'missing_'+field,'count':int(schools[field].isna().sum()),'status':'review_attributes_only'})
    lo=pd.to_numeric(schools.StatutoryLowAge,errors='coerce');hi=pd.to_numeric(schools.StatutoryHighAge,errors='coerce')
    qa.append({'check':'inconsistent_age_range','count':int((lo>hi).sum()),'status':'review_attributes_only'})
    cap=pd.to_numeric(schools.SchoolCapacity,errors='coerce'); pupils=pd.to_numeric(schools.NumberOfPupils,errors='coerce')
    qa.append({'check':'pupils_exceed_capacity','count':int((pupils>cap).sum()),'status':'review_attributes_only'})
    # A reviewed destination can be supplied later, with explicit evidence and a graph node ID.
    review_path=ROOT/'data/qa/school_destination_review.csv'
    if not review_path.exists():
        pd.DataFrame({'URN':schools.URN.astype(str), 'school_name':schools.EstablishmentName,
            'review_status':'pending','destination_node':'','destination_method':'',
            'evidence':'','reviewer':'','review_date':''}).to_csv(review_path,index=False)
    reviews=pd.read_csv(review_path,dtype=str).fillna('').set_index('URN')
    # Restricted edges are excluded from candidate destinations, without changing graph connectivity.
    edges=ox.graph_to_gdfs(graph,nodes=False).reset_index()
    allowed=[]
    for row in edges.itertuples():
        d=graph[row.u][row.v][row.key]
        allowed.append(classify_pedestrian_access(d.get('access'),d.get('foot'),d.get('highway')) is None)
    edges=edges[allowed].reset_index(drop=True)
    rows=[]
    candidate_geoms=[]
    for school in schools.itertuples():
        urn=str(school.URN); point=school.geometry
        idx=int(edges.sindex.nearest(point,return_all=False)[1][0]);edge=edges.iloc[idx]
        u,v,k=int(edge.u),int(edge.v),int(edge.key)
        geom=_edge_geometry(graph,u,v,graph[u][v][k]);dest=geom.interpolate(geom.project(point))
        candidate_times=[]
        for a,b in [(u,v),(v,u)]:
            for _,data in graph.get_edge_data(a,b,default={}).items():
                line=_edge_geometry(graph,a,b,data)
                if line.distance(dest)<=.01:
                    candidate_times.append(times.get(a,np.inf)+line.project(dest)/line.length*float(data['travel_time_s']))
        candidate_time=min(candidate_times,default=np.inf)
        review=reviews.loc[urn] if urn in reviews.index else None
        verified=review is not None and review.review_status=='approved'
        t=np.nan;method='unresolved';verification='manual_review_required';accessible=None
        notes='Geocoded address point is not an entrance. Candidate projection has no assumed connector; verify gates, buildings, water and fences.'
        if verified:
            if not all(review[x].strip() for x in ['destination_node','destination_method','evidence','reviewer','review_date']):
                raise ValueError(f'Incomplete school destination evidence: {urn}')
            node=int(review.destination_node)
            if node not in graph:
                raise ValueError(f'School destination node missing: {urn}')
            t=times.get(node,np.inf);method=review.destination_method;verification='reviewed_proxy_or_entrance'
            accessible=bool(t<=600);notes=review.evidence
        rows.append({'URN':urn,'school_name':school.EstablishmentName,'status':statuses.get(urn,''),
            'school_type':'',
            'network_destination_method':method,'network_walk_time_seconds':t,
            'network_walk_time_minutes':t/60,'network_accessible_10min':accessible,
            'within_service_area_polygon':bool(polygon.covers(point)),
            'verification_status':verification,'notes':notes,
            'candidate_projection_walk_minutes':candidate_time/60,
            'candidate_point_to_edge_m':point.distance(dest),
            'candidate_edge':f'{u}-{v}-{k}',
            'candidate_easting':dest.x,'candidate_northing':dest.y,
            'candidate_near_10min_threshold':bool(abs(candidate_time-600)<=120),
            'geometry':point})
        candidate_geoms.append({'URN':urn,'school_name':school.EstablishmentName,
            'purpose':'unverified review candidate only','geometry':dest})
    result=gpd.GeoDataFrame(rows,crs=CRS)
    result['school_type']=schools['TypeOfEstablishment (name)'].to_numpy()
    result.drop(columns='geometry').to_csv(TABLES/'school_accessibility_results.csv',index=False)
    write_gpkg(result,GIS/'school_accessibility_results.gpkg','school_accessibility')
    write_gpkg(gpd.GeoDataFrame(candidate_geoms,crs=CRS),GIS/'school_accessibility_results.gpkg',
               'unverified_destination_candidates',append=True)
    qa.append({'check':'unverified_destinations','count':int((result.verification_status=='manual_review_required').sum()),'status':'review'})
    qa.append({'check':'candidate_within_2min_of_threshold','count':int(result.candidate_near_10min_threshold.sum()),'status':'review'})
    pd.DataFrame(qa).to_csv(QA_OUT/'schools_qa_summary.csv',index=False)
    return result


def run_analysis():
    for folder in [GIS,TABLES,QA_OUT,METHOD,OUTPUTS/'maps']:
        folder.mkdir(parents=True,exist_ok=True)
    cfg=load_config()
    try:
        water_path = selected_water_path()
    except FileNotFoundError as exc:
        raise FileNotFoundError('Run scripts/09_prepare_water.py first') from exc
    protected=[*RAW.glob('*'),CLEANED/'lsoa_2021_population_cleaned.gpkg',CLEANED/'schools_cleaned.gpkg',
               OPENSTREETMAP/'pedestrian_network.graphml',OPENSTREETMAP/'pedestrian_network_reviewed.graphml',
               OPENSTREETMAP/'pedestrian_network_reviewed_with_origins.graphml',PROCESSED/'site_origins.gpkg']
    hashes={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in protected if p.is_file()}
    population=gpd.read_file(CLEANED/'lsoa_2021_population_cleaned.gpkg').to_crs(CRS)
    population_qa(population)
    water=gpd.read_file(water_path).to_crs(CRS).geometry.union_all()
    graph,origins,times,networks,checks=verify_network(cfg)
    scenarios,sensitivity,intersections=build_scenarios(population,networks,water,checks)
    schools=school_results(graph,times,scenarios[10,PRIMARY_BUFFER,'land_only'],water)
    primary=sensitivity[(sensitivity.walking_threshold_min==10)&(sensitivity.network_buffer_m==PRIMARY_BUFFER)&(sensitivity.area_method=='standard')].iloc[0]
    land=sensitivity[(sensitivity.walking_threshold_min==10)&(sensitivity.network_buffer_m==PRIMARY_BUFFER)&(sensitivity.area_method=='land_only')].iloc[0]
    unresolved=int((schools.verification_status=='manual_review_required').sum())
    confirmed=int((schools.network_accessible_10min==True).sum())
    metrics={'walking_threshold_min':10,'walking_speed_kmh':cfg['walking']['speed_kmh'],
        'selected_network_buffer_m':PRIMARY_BUFFER,'primary_population_method':'standard_areal_weighting',
        'primary_area_method':'raw_service_area','catchment_area_ha':round(float(primary.catchment_area_ha),1),
        'estimated_population':int(round(float(primary.estimated_population)/10)*10),
        'population_density_per_ha':round(float(primary.population_density_per_ha),1),
        'population_baseline':'ONS Census 2021 population baseline',
        'land_only_area_ha':round(float(land.catchment_area_ha),1),
        'land_only_estimated_population':int(round(float(land.estimated_population)/10)*10),
        'land_only_density_per_ha':round(float(land.population_density_per_ha),1),
        'number_network_accessible_schools':confirmed if unresolved==0 else None,
        'confirmed_network_accessible_schools':confirmed,'school_destinations_unverified':unresolved,
        'school_result_status':'pending_destination_review' if unresolved else 'complete',
        'within_land_service_polygon_schools':int(schools.within_service_area_polygon.sum()),
        'water_source_file':str(water_path.relative_to(ROOT)),
        'water_metadata_file':str(water_metadata_path(water_path).relative_to(ROOT)),
        'analysis_date':datetime.now(timezone.utc).date().isoformat()}
    (TABLES/'primary_metrics.json').write_text(json.dumps(metrics,indent=2,allow_nan=False))
    pd.DataFrame([metrics]).to_csv(TABLES/'primary_metrics.csv',index=False)
    pd.DataFrame(checks).to_csv(QA_OUT/'catchment_geometry_qa.csv',index=False)
    for name,digest in hashes.items():
        if sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError(f'Protected input changed: {name}')
    (METHOD/'input_hashes.json').write_text(json.dumps(hashes,indent=2))
    print(json.dumps(metrics,indent=2))
    return metrics
