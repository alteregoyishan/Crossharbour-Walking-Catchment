"""Presentation-only delivery build from the existing validated analytical outputs."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .paths import ROOT, RAW, CLEANED, PROCESSED, OUTPUTS

SCHOOL_TABLE = OUTPUTS / 'tables/schools_within_10min_catchment.csv'
REPO = 'https://github.com/alteregoyishan/Crossharbour-Walking-Catchment'


def delivery_results():
    """Point-in-polygon identification; this does not calculate school gate times."""
    catchments = gpd.read_file(OUTPUTS / 'gis/c1_walking_catchments_final.gpkg',
                               layer='analytical_land_catchments')
    polygon = catchments.loc[catchments.time_minutes.eq(10)].geometry.iloc[0]
    schools = gpd.read_file(CLEANED / 'schools_cleaned.gpkg').to_crs(catchments.crs)
    selected = schools[schools.geometry.notna() & ~schools.geometry.is_empty].copy()
    selected = selected[selected.geometry.apply(polygon.covers)].sort_values(
        ['EstablishmentName', 'URN']).reset_index(drop=True)
    if selected.URN.duplicated().any():
        raise ValueError('Duplicate school URNs in final location result')
    selected['map_number'] = range(1, len(selected) + 1)
    selected['identification_method'] = 'Cleaned location covered by 10-minute analytical land catchment'
    selected.drop(columns='geometry').to_csv(SCHOOL_TABLE, index=False)
    pop = pd.read_csv(OUTPUTS / 'tables/population_sensitivity_c1.csv')
    pop = pop[pop.time_minutes.eq(10)].set_index('method')
    summary = pd.read_csv(OUTPUTS / 'tables/c1_walking_catchment_summary.csv')
    area = float(summary.loc[summary.time_minutes.eq(10), 'analytical_land_area_ha'].iloc[0])
    baseline = pop.loc['P1']
    if abs(float(baseline.estimated_residents) / area - float(baseline.residents_per_hectare)) > .001:
        raise ValueError('Population, density and analytical land area disagree')
    return selected, schools, pop, summary, area


def architect_note(selected, pop, area):
    phases = selected['PhaseOfEducation (name)'].value_counts()
    return (
        f'The 10-minute walking catchment around Crossharbour contains an estimated '
        f'{pop.loc["P1", "estimated_residents"]:,.0f} residents, with an average density of '
        f'{pop.loc["P1", "residents_per_hectare"]:.1f} residents per hectare across approximately {area:.0f} '
        f'hectares of analytical land. {len(selected)} schools are identified within the catchment, including '
        f'{int(phases.get("Primary", 0))} primary and {int(phases.get("Secondary", 0))} secondary schools. '
        'The catchment follows the pedestrian network rather than extending evenly from the site. '
        'Docks and the River Thames constrain east–west movement, while connections extend primarily '
        'north–south through the Isle of Dogs. This highlights the importance '
        'of connecting the site to surrounding neighbourhoods, schools and community infrastructure. '
        'Five- and 15-minute catchments provide reference scenarios. Alternative population allocations '
        'test sensitivity to where residents are located within census areas. '
        'Population is estimated from Census 2021, not a present-day headcount. The model represents '
        'existing walking connections from two proxy access points, not confirmed future entrances. '
        'School locations indicate spatial proximity, not capacity or admissions; pedestrian gates remain unverified.')


def render_delivery_maps(selected, schools, summary):
    """Render saved polygons/networks only; never recompute routing or catchments."""
    from . import c1_final_maps as maps
    from .os_data_services import selected_water_path
    results = {}
    cartographic = gpd.read_file(maps.OUT_GPKG, layer='cartographic_display_catchments').to_crs(maps.CRS)
    for minute in (5, 10, 15):
        results[minute] = {
            'cartographic_display': cartographic.loc[cartographic.time_minutes.eq(minute)].geometry.iloc[0],
            'network': gpd.read_file(OUTPUTS / f'gis/reachable_network_{minute}min.gpkg').to_crs(maps.CRS).geometry.union_all(),
        }
    site = gpd.read_file(RAW / 'site_boundary.geojson').to_crs(maps.CRS)
    origins = gpd.read_file(PROCESSED / 'site_origins.gpkg').to_crs(maps.CRS)
    water = gpd.read_file(selected_water_path()).to_crs(maps.CRS)
    from .paths import OPENSTREETMAP
    network_edges = gpd.read_file(OPENSTREETMAP / 'pedestrian_network_reviewed.gpkg',
                                  layer='edges').to_crs(maps.CRS)
    maps._save_context_map_pair(maps.MAP_BASENAMES['origin_selection'],
        lambda ax: maps._draw_origin_selection_map(ax, site, origins, network_edges, water),
        'Crossharbour Site Origin Selection',
        'Two existing public footways intersect the site boundary at distinct northern access locations',
        'A and B are planning proxy origins at distinct existing northern boundary crossings; private/service links and tangential contacts are excluded.')
    metadata = json.loads(maps.OUT_METADATA.read_text())
    bounds = tuple(metadata['individual_map_bounds_epsg27700'])
    for minutes, base in [([5], maps.MAP_BASENAMES[5]), ([10], maps.MAP_BASENAMES[10]),
                          ([15], maps.MAP_BASENAMES[15]), ([5, 10, 15], maps.MAP_BASENAMES['combined'])]:
        displayed_schools = selected if minutes == [10] else schools
        maps._save_clean_png(maps.MAPS / f'{base}.png', results, minutes, water, site, origins, displayed_schools, bounds)
        maps._save_a3_pdf(maps.MAPS / f'{base}.pdf', results, minutes, water, site, origins, displayed_schools, bounds, summary)
    from shapely import Point
    from shapely.ops import unary_union
    circle = Point(float(origins.geometry.x.mean()), float(origins.geometry.y.mean())).buffer(
        maps.THEORETICAL_RADIUS_M, resolution=256)
    circle_bounds = maps._fit_bounds(unary_union([results[15]['cartographic_display'], circle]).bounds)
    base = maps.MAP_BASENAMES['circle']
    maps._save_clean_png(maps.MAPS / f'{base}.png', results, [5, 10, 15], water,
                        site, origins, schools, circle_bounds, circle)
    maps._save_a3_pdf(maps.MAPS / f'{base}.pdf', results, [5, 10, 15], water,
                     site, origins, schools, circle_bounds, summary, circle)
    def draw_school_map(ax):
        maps._draw_map(ax, results, [10], water, site, origins, selected, bounds)
        for row in selected.itertuples():
            ax.annotate(str(row.map_number), (row.geometry.x, row.geometry.y),
                        xytext=(7, 7), textcoords='offset points', fontsize=10,
                        color=maps.SCHOOL_COLOUR, weight='bold', zorder=110)
    maps._save_context_map_pair('figure_07_schools_within_10min', draw_school_map,
        'Schools within the 10-minute walking catchment',
        f'{len(selected)} cleaned school locations within the analytical land catchment; numbers match the report table',
        'School locations use cleaned address/location coordinates rather than verified pedestrian gate locations.')


def build_report():
    selected, schools, pop, summary, area = delivery_results()
    note = architect_note(selected, pop, area)
    if not 130 <= len(note.split()) <= 200:
        raise ValueError('Architect note must be between 130 and 200 words')
    (OUTPUTS / 'methodology/architect_note.md').write_text(note + '\n', encoding='utf-8')
    escape = html.escape
    school_rows = ''.join(
        f'<tr><td>{r.map_number}</td><td>{escape(str(r["EstablishmentName"]))}</td>'
        f'<td>{escape(str(r.URN))}</td><td>{escape(str(r["PhaseOfEducation (name)"]))}</td>'
        f'<td>{escape(str(r["TypeOfEstablishment (name)"]))}</td></tr>'
        for _, r in selected.iterrows())
    methods = [
        ('P1', 'Census area-weighted baseline', 'Allocates each LSOA total by the proportion of its full area inside the catchment.'),
        ('P2', 'Residential land weighting', 'Allocates Census residents by residential-site area inside the catchment.'),
        ('P3', 'Residential building-footprint weighting', 'Weights residential-capable buildings by ground-floor footprint; building height is not included.'),
        ('P4_central', 'Residential floor-space weighting (central)', 'Weights footprint × recorded floor count × residential share: a floor-space proxy, not measured occupancy.'),
    ]
    population_rows = ''.join(
        f'<tr><td><strong>{name}</strong><span class="method-description">{description}</span></td><td>{"Baseline" if key == "P1" else "Sensitivity"}</td>'
        f'<td class="numeric">{pop.loc[key,"estimated_residents"]:,.0f}</td>'
        f'<td class="numeric">{pop.loc[key,"residents_per_hectare"]:.1f}</td></tr>' for key, name, description in methods)
    template = (ROOT / 'report/web_report_template.html').read_text(encoding='utf-8')
    school_log = pd.read_csv(OUTPUTS / 'tables/school_cleaning_log.csv')
    population_log = pd.read_csv(OUTPUTS / 'tables/population_cleaning_log.csv')
    def count(log, action):
        return int(log.action.eq(action).sum())
    cleaning_items = [
        ('Population', f'{count(population_log, "remove_exact_duplicate")} duplicate LSOA record',
         'Removed the exact duplicate; retained one record and its population attributes.'),
        ('Population', f'{count(population_log, "repair_invalid_geometry")} self-intersecting polygon',
         'Repaired with make_valid; checked validity and area change, preserving attributes.'),
        ('Schools', f'{count(school_log, "exclude_closed_school")} closed establishments',
         'Excluded before geocoding and school accessibility analysis.'),
        ('Schools', f'{count(school_log, "exclude_overseas_school")} overseas establishments',
         'Excluded using the local-authority classification before geocoding.'),
        ('Schools', f'{count(school_log, "remove_exact_duplicate")} duplicate school record',
         'Removed duplicate records by school identifier; retained the cleaning audit.'),
        ('Schools', 'Coordinate confidence and address consistency',
         'Checked OS Places candidates; used Google geocoding for lower-confidence cases and retained matched addresses.'),
    ]
    cleaning_rows = ''.join('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in item) + '</tr>'
                            for item in cleaning_items)
    replacements = {'{{SCHOOL_COUNT}}': str(len(selected)), '{{SCHOOL_ROWS}}': school_rows,
                    '{{CLEANED_SCHOOL_COUNT}}': str(len(schools)), '{{CLEANING_ROWS}}': cleaning_rows,
                    '{{POPULATION_ROWS}}': population_rows, '{{ARCHITECT_NOTE}}': escape(note),
                    '{{NOTE_WORDS}}': str(len(note.split())), '{{AREA}}': f'{area:.1f}',
                    '{{AREA_ROUND}}': f'{area:.0f}',
                    '{{FLOOR_LOW}}': f'{pop.loc[["P4_low", "P4_central", "P4_high"], "estimated_residents"].min():,.0f}',
                    '{{FLOOR_HIGH}}': f'{pop.loc[["P4_low", "P4_central", "P4_high"], "estimated_residents"].max():,.0f}',
                    '{{LAND_POPULATION}}': f'{pop.loc["P2", "estimated_residents"]:,.0f}',
                    '{{POPULATION}}': f'{pop.loc["P1","estimated_residents"]:,.0f}',
                    '{{DENSITY}}': f'{pop.loc["P1","residents_per_hectare"]:.1f}'}
    for token, value in replacements.items():
        template = template.replace(token, value)
    if '{{' in template:
        raise ValueError('Unresolved report placeholders')
    (ROOT / 'docs/index.html').write_text(template, encoding='utf-8')
    note_path = ROOT / 'docs/assets/downloads/methodology.md'
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        '# Crossharbour walking catchment — methodology\n\n' + note + '\n\n'
        '## Methods\n\nReviewed OpenStreetMap walking network; two boundary-crossing access proxies A/B; '
        '4.8 km/h; multi-source Dijkstra and partial-edge clipping. The final workshop polygon is an '
        'ordinary-Delaunay travel-time contour with mapped water removed from analytical land. '
        'Cartographic regularisation and bridge display corridors do not enter the population-area denominator. '
        'The original 25 m network buffer remains a comparison baseline. The independent validation grid '
        'is a model-based consistency check, not observed ground truth.\n\n'
        'Census 2021 residents are allocated using population × intersection area / original full LSOA area. '
        f'Density uses {area:.6f} ha analytical land. School locations are identified using polygon covers, '
        'including points on the boundary; no school gate-to-gate time is implied. '
        'Land, footprint and direct floor-space allocations are sensitivity scenarios, not confidence intervals. '
        'Analysis uses British National Grid (EPSG:27700).\n\n'
        '## Limitations\n\nAccess proxies are not future entrances. Census 2021 is not a present-day headcount. '
        'The baseline assumes uniform distribution within LSOAs. School locations are not verified gates. '
        'Results depend on OSM completeness and fixed walking speed, excluding delays and mobility variation. '
        'Separately mapped footways are retained, but pavements on both sides of every road and all '
        'crossing locations are not systematically modelled; crossing detours and boundary travel times may differ. '
        'The polygon is a derived representation rather than an exact physical boundary.\n\n'
        f'## Technical evidence\n\nCleaning logs, network QA, polygonisation comparisons and population '
        f'sensitivity are preserved in [{REPO}]({REPO}).\n', encoding='utf-8')
    shutil.copy2(SCHOOL_TABLE, ROOT / 'docs/assets/downloads/schools-within-10min.csv')
    return selected, schools, pop, summary, area
