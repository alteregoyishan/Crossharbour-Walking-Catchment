"""A3 planning maps and concise methodology from calculated service-area results."""
import json
import textwrap

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import pandas as pd

from .paths import RAW, PROCESSED, OPENSTREETMAP, OUTPUTS
from .service_area_analysis import GIS, TABLES, QA_OUT, METHOD, CRS, PRIMARY_BUFFER
from .os_data_services import selected_water_path, water_metadata_path, os_maps_basemap

INK='#253842'
TEAL='#176b72'
WATER_COLOUR='#d8e9ef'
SITE='#bd542a'
PURPLE='#746398'


def _plot_geom(geometry, ax, **kwargs):
    if not geometry.is_empty:
        gpd.GeoSeries([geometry],crs=CRS).plot(ax=ax,**kwargs)


def _base_map(ax,bounds,edges,water,site,basemap=None):
    xmin,ymin,xmax,ymax=bounds
    ax.set_facecolor('#fafaf7')
    if basemap:
        ax.imshow(basemap['image'],extent=basemap['extent'],zorder=0)
    else:
        local_edges=edges.cx[xmin:xmax,ymin:ymax]
        if not local_edges.empty:
            local_edges.plot(ax=ax,color='#dedfd9',linewidth=.45,zorder=1)
    local_water=water.cx[xmin:xmax,ymin:ymax]
    if not local_water.empty:
        local_water.plot(ax=ax,color=WATER_COLOUR,edgecolor='#7ba0af',linewidth=.35,
                         alpha=.62 if basemap else 1,zorder=2)
    ax.set_xlim(xmin,xmax); ax.set_ylim(ymin,ymax)
    ax.set_aspect('equal');ax.set_xticks([]);ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor('#dedfd9')
    # BNG grid north, labelled explicitly rather than pretending true north.
    ax.annotate('N',xy=(.95,.94),xytext=(.95,.87),xycoords='axes fraction',
                ha='center',fontsize=11,color=INK,
                arrowprops={'arrowstyle':'-|>','lw':1,'color':INK})
    ax.text(.95,.855,'grid',transform=ax.transAxes,ha='center',fontsize=7,color=INK)
    length=500 if xmax-xmin>2000 else 250
    x=xmin+.055*(xmax-xmin);y=ymin+.045*(ymax-ymin)
    ax.plot([x,x+length],[y,y],color=INK,lw=2,zorder=20)
    for sx in [x,x+length]:
        ax.plot([sx,sx],[y-12,y+12],color=INK,lw=1,zorder=20)
    ax.text(x+length/2,y+25,f'{length} m',ha='center',fontsize=9,color=INK,zorder=20,
            bbox={'facecolor':'white','edgecolor':'none','alpha':.85,'pad':2})


def _site(ax,site):
    site.plot(ax=ax,facecolor=SITE,edgecolor='white',linewidth=.9,zorder=12)
    centre=site.geometry.union_all().representative_point()
    ax.annotate('Development site',xy=(centre.x,centre.y),xytext=(55,-35),
        textcoords='offset points',fontsize=10,color=INK,fontweight='bold',
        bbox={'facecolor':'white','alpha':.94,'edgecolor':'none','pad':3},
        arrowprops={'arrowstyle':'-','color':INK,'lw':.7},zorder=20)


def _layout(number,title,subtitle,date,basemap=None):
    fig=plt.figure(figsize=(420/25.4,297/25.4),facecolor='white')
    fig.text(.04,.955,f'CROSSHARBOUR  /  {number}',fontsize=10,color=TEAL,weight='bold')
    fig.text(.04,.918,title,fontsize=22,color=INK,weight='bold')
    fig.text(.04,.889,subtitle,fontsize=10,color='#586970')
    ax=fig.add_axes([.04,.12,.65,.74]); panel=fig.add_axes([.73,.12,.23,.73]);panel.axis('off')
    fig.text(.04,.075,'Walking accessibility calculated on the OpenStreetMap pedestrian network at 4.8 km/h.',fontsize=9,color=INK)
    fig.text(.04,.053,'Sources: supplied site, DfE school records, ONS Census 2021 baseline and OpenStreetMap pedestrian network.',fontsize=8,color='#586970')
    context=(f"Basemap: OS Maps API {basemap['layer']}; {basemap['attribution']}"
             if basemap else 'Context: local OSM roads and selected water polygons; © OpenStreetMap contributors')
    fig.text(.04,.035,f'{context}  |  Analysis: {date}  |  EPSG:27700',fontsize=8,color='#586970')
    return fig,ax,panel


def _text(panel,y,text,size=10,colour=INK,width=39):
    wrapped='\n'.join(textwrap.fill(line,width=width) for line in text.split('\n'))
    panel.text(0,y,wrapped,va='top',fontsize=size,color=colour,linespacing=1.45)
    return y - (wrapped.count('\n')+1)*.025 - .025


def _export(fig,name):
    folder=OUTPUTS/'maps';folder.mkdir(parents=True,exist_ok=True)
    fig.savefig(folder/f'{name}.png',dpi=220,facecolor='white')
    fig.savefig(folder/f'{name}.pdf',facecolor='white',metadata={
        'Title':name,'Author':'Crossharbour GIS analysis',
        'Subject':'EPSG:27700; ONS Census 2021 baseline; OSM walking network; A3 landscape'})
    plt.close(fig)


def make_maps_and_notes():
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'pdf.fonttype':42})
    metrics=json.loads((TABLES/'primary_metrics.json').read_text())
    sensitivity=pd.read_csv(TABLES/'catchment_population_sensitivity.csv')
    site=gpd.read_file(RAW/'site_boundary.geojson').to_crs(CRS)
    water_path=selected_water_path()
    water=gpd.read_file(water_path).to_crs(CRS)
    edges=gpd.read_file(OPENSTREETMAP/'pedestrian_network_reviewed.gpkg',layer='edges').to_crs(CRS)
    schools=gpd.read_file(GIS/'school_accessibility_results.gpkg',layer='school_accessibility')
    raw=gpd.read_file(GIS/'walking_catchment_10min.gpkg',layer='catchment_raw').geometry.union_all()
    land=gpd.read_file(GIS/'walking_catchment_10min.gpkg',layer='catchment_land_only').geometry.union_all()
    xmin,ymin,xmax,ymax=raw.bounds
    # Fit a landscape map panel while preserving the spatial aspect ratio.
    cx=(xmin+xmax)/2;cy=(ymin+ymax)/2;h=(ymax-ymin)+500;w=max((xmax-xmin)+600,h*1.23)
    bounds=(cx-w/2,cy-h/2,cx+w/2,cy+h/2)
    basemap1=os_maps_basemap(bounds)
    fig,ax,panel=_layout('01','10-Minute Walking Catchment and Accessible Schools',
        'Early-stage masterplanning | 25 m network corridor representation | School entrances await validation',metrics['analysis_date'],basemap1)
    _base_map(ax,bounds,edges,water,site,basemap1)
    _plot_geom(land,ax,facecolor=TEAL,edgecolor='none',alpha=.17,zorder=3)
    _plot_geom(raw.boundary,ax,color=TEAL,linewidth=.85,zorder=4)
    _site(ax,site)
    nearby=schools.cx[bounds[0]:bounds[2],bounds[1]:bounds[3]]
    for status,marker,face,edge,label in [
        ('confirmed','o',TEAL,'white','Verified <=10 min'),
        ('outside','o','none','#7b858a','Verified >10 min'),
        ('unverified','D','#fff8ec','#9e7947','School entrance not yet verified')]:
        if status=='confirmed': subset=nearby[nearby.network_accessible_10min==True]
        elif status=='outside': subset=nearby[nearby.network_accessible_10min==False]
        else: subset=nearby[nearby.verification_status=='manual_review_required']
        if not subset.empty:
            subset.plot(ax=ax,marker=marker,color=face,edgecolor=edge,markersize=46,zorder=10,linewidth=1)
    ax.legend(handles=[Patch(facecolor='#d7e6e6',edgecolor=TEAL,label='10-min derived service area'),
        Patch(facecolor=SITE,label='Development site'),Patch(facecolor=WATER_COLOUR,label='Water'),
        Line2D([],[],marker='D',lw=0,markerfacecolor='#fff8ec',markeredgecolor='#9e7947',label='School entrance not yet verified')],
        loc='upper left',fontsize=9,framealpha=.96)
    y=.99
    metric_rows=[('10 MIN','Walking catchment'),(f"{metrics['catchment_area_ha']:.1f} ha",'Raw service-area footprint'),
        (f"~{metrics['estimated_population']:,}",'Estimated residents | Census 2021'),
        (f"{metrics['population_density_per_ha']:.1f}",'Residents per hectare | raw area'),
        ('Pending' if metrics['number_network_accessible_schools'] is None else str(metrics['number_network_accessible_schools']),
         'Network-accessible schools')]
    for value,label in metric_rows:
        panel.text(0,y,value,va='top',fontsize=26,weight='bold',color=TEAL)
        panel.text(0,y-.056,label,va='top',fontsize=10,color=INK)
        y-=.132
    y=_text(panel,y-.015,
        f"{metrics['school_destinations_unverified']} school entrance destinations require access-point review. Address coordinates do not prove school access.",size=10)
    _text(panel,y,'Population estimated by area-weighted interpolation of the supplied ONS Census 2021 small-area population data.',size=9)
    _export(fig,'figure_01_workshop_map')

    networks={m:gpd.read_file(GIS/f'reachable_network_{m}min.gpkg') for m in [5,10,15]}
    bb=networks[15].total_bounds;cx=(bb[0]+bb[2])/2;cy=(bb[1]+bb[3])/2;h=bb[3]-bb[1]+400;w=max(bb[2]-bb[0]+400,h*1.23)
    bounds2=(cx-w/2,cy-h/2,cx+w/2,cy+h/2)
    basemap2=os_maps_basemap(bounds2)
    fig,ax,panel=_layout('02','Pedestrian Network Reach: 5, 10 and 15 Minutes',
        'Authoritative network reach | Partial edges clipped at each time threshold',metrics['analysis_date'],basemap2)
    _base_map(ax,bounds2,edges,water,site,basemap2)
    colours={15:'#aaa5bd',10:TEAL,5:'#c16b38'}
    # Draw the broadest time threshold first. Shorter-time lines are subsets
    # of the longer-time network and otherwise disappear beneath it.
    widths={15:1.0,10:1.5,5:2.0}
    zorders={15:3,10:4,5:5}
    for m in [15,10,5]:
        networks[m].plot(ax=ax,color=colours[m],linewidth=widths[m],zorder=zorders[m])
    _site(ax,site)
    origins=gpd.read_file(PROCESSED/'site_origins.gpkg')
    origins.plot(ax=ax,color='#fff7c6',edgecolor=INK,markersize=40,zorder=15)
    for row in origins.itertuples():
        ax.annotate(row.origin_id,(row.geometry.x,row.geometry.y),xytext=(-15,12) if row.origin_id=='A' else (12,12),
            textcoords='offset points',fontsize=10,weight='bold',zorder=20,
            bbox={'facecolor':'white','edgecolor':'none','pad':1})
    ax.legend(handles=[Line2D([],[],color=colours[m],lw=2,label=f'{m} minutes'+(' (primary)' if m==10 else '')) for m in [5,10,15]],loc='upper left',fontsize=10)
    panel.text(0,.99,'HOW TO READ THIS MAP',fontsize=11,weight='bold',color=TEAL,va='top')
    y=_text(panel,.93,'Walking times calculated using multi-source shortest paths from two reviewed network-based proxy access locations.')
    y=_text(panel,y,'At each location, use the shorter time from A or B. All edge times use projected length divided by 1.333 m/s.')
    y=_text(panel,y,'Origins represent plausible external pedestrian access locations for analysis and are not confirmed future site entrances.')
    y=_text(panel,y,'A partially reachable edge is cut at the remaining time budget. Lines crossing in 2D do not automatically share a network node.')
    y=_text(panel,y,'Three manually confirmed local connections are included in the reviewed graph. The downloaded GraphML is preserved.')
    _text(panel,y,'Network QA retains unresolved wider-area topology and conditional-access findings. No restricted-tag edge is reachable within 15 minutes.')
    _export(fig,'figure_02_network_accessibility')

    population=gpd.read_file(PROCESSED.parent/'cleaned/lsoa_2021_population_cleaned.gpkg')
    portions=gpd.read_file(GIS/'population_intersections_10min.gpkg',layer='standard')
    fig,ax,panel=_layout('03','Population Estimation within the 10-Minute Walking Catchment',
        'ONS Census 2021 baseline | Standard area weighting | 25 m network corridor',metrics['analysis_date'],basemap1)
    _base_map(ax,bounds,edges,water,site,basemap1)
    local=population.cx[bounds[0]:bounds[2],bounds[1]:bounds[3]]
    local.boundary.plot(ax=ax,color='#929ba0',linewidth=.55,zorder=3)
    portions.plot(ax=ax,facecolor=TEAL,edgecolor='white',linewidth=.3,alpha=.32,zorder=4)
    _plot_geom(raw.boundary,ax,color=TEAL,linewidth=1,zorder=5)
    _site(ax,site)
    ax.legend(handles=[Patch(facecolor='none',edgecolor='#929ba0',label='Complete census zones'),
        Patch(facecolor='#9bbabc',edgecolor='white',label='Zone portions inside catchment'),
        Patch(facecolor='none',edgecolor=TEAL,label='10-min derived service area'),Patch(facecolor=WATER_COLOUR,label='Water')],
        loc='upper left',fontsize=9,framealpha=.96)
    panel.text(0,.99,'ESTIMATION METHOD',fontsize=11,weight='bold',color=TEAL,va='top')
    panel.text(0,.91,r'$P_C=\sum_i P_i\,\frac{\mathrm{Area}(Z_i\cap C)}{\mathrm{Area}(Z_i)}$',fontsize=16,color=INK)
    y=_text(panel,.84,'Multiply each complete zone population by its area fraction inside the catchment, then sum the contributions.')
    y=_text(panel,y,'Density = estimated population / catchment area (ha). One hectare equals 10,000 square metres.')
    y=_text(panel,y,'Area-weighted interpolation assumes population is uniformly distributed within each source census zone.')
    y=_text(panel,y,f"STANDARD\n~{metrics['estimated_population']:,} residents\n{metrics['population_density_per_ha']:.1f} residents/ha")
    y=_text(panel,y,f"LAND-ONLY SENSITIVITY\n~{metrics['land_only_estimated_population']:,} residents\n{metrics['land_only_density_per_ha']:.1f} residents/ha")
    _text(panel,y,'Land-only weighting removes mapped water from both zone denominators and intersections. It remains a sensitivity scenario, not a measured population count.',size=9)
    _export(fig,'figure_03_population_method')
    sensitivity_charts(sensitivity)
    write_notes(metrics,sensitivity,water_path,basemap1 is not None)
    print('Exported three A3 PDF/PNG maps, sensitivity charts and methodology.')


def sensitivity_charts(sensitivity):
    primary=sensitivity[sensitivity.walking_threshold_min==10]
    for field,name,label in [('catchment_area_ha','area','Catchment area (ha)'),
        ('estimated_population','population','Estimated residents (Census 2021 baseline)'),
        ('population_density_per_ha','density','Estimated residents per hectare')]:
        fig,ax=plt.subplots(figsize=(8,4.8))
        for method,colour,marker in [('standard',TEAL,'o'),('land_only',PURPLE,'s')]:
            frame=primary[primary.area_method==method].sort_values('network_buffer_m')
            ax.plot(frame.network_buffer_m,frame[field],marker=marker,color=colour,lw=2,
                    label='Standard area weighting' if method=='standard' else 'Land-only area weighting')
        ax.set(xticks=[25,50,75],xlabel='Network buffer (m)',ylabel=label,ylim=(0,None),
               title='10-minute catchment: polygon sensitivity')
        ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.2);ax.legend(frameon=False)
        fig.tight_layout();fig.savefig(QA_OUT/f'catchment_sensitivity_{name}.png',dpi=180);plt.close(fig)


def write_notes(metrics,sensitivity,water_path,uses_os_basemap):
    selected=sensitivity[(sensitivity.walking_threshold_min==10)&(sensitivity.network_buffer_m==25)].set_index('area_method')
    pct=100*(selected.loc['land_only','estimated_population']/selected.loc['standard','estimated_population']-1)
    changes=pd.read_csv(TABLES/'catchment_sensitivity_changes_10min.csv')
    change=changes[(changes.area_method=='standard')&(changes.from_buffer_m==25)&(changes.to_buffer_m==75)].iloc[0]
    pchange=change.estimated_population_change_pct
    water_meta=json.loads(water_metadata_path(water_path).read_text())
    water_source=water_meta.get('source','Recorded water polygon source')
    water_file=str(water_path.relative_to(PROCESSED.parent))
    map_context=('OS Maps API Light raster basemap is requested at export time and cached locally. '
                 'The API key is not stored in the outputs.' if uses_os_basemap else
                 'No OS Maps API key was configured for this export; local vector context was used.')
    text=f'''# Methodology

## Inputs and provenance
Reuse the supplied site, cleaned DfE schools and 109 unique ONS Census 2021 population zones. This is a **Census 2021 population baseline**, not measured {metrics['analysis_date'][:4]} population. The input inventory and SHA-256 manifest identify the files used. The existing population correction log documents one exact duplicate removed and a self-intersection repaired; the latter changed area by approximately 0.000569 m². No second correction was applied. Original zone population totals, excluding the known duplicate, were preserved.

## Pedestrian network and time
OSMnx 2.0.1 acquired the network with `network_type="walk"`, retained all components and simplified edges. Analysis uses the reviewed EPSG:27700 MultiDiGraph and the saved routing graph with A/B inserted. Three user-confirmed connections are documented in `network_changes.csv`; the downloaded GraphML is preserved. Foot/layer attribute omissions in the initial export were an auditability limitation; the retained-tag graph is used here.

Each edge time is projected geometry length / (4.8 km/h), equivalent to 80 m/min. Multi-source Dijkstra gives the minimum cumulative edge time from A or B. These reviewed external proxy accesses are not confirmed future building entrances; travel starts at the accesses and omits internal-site travel. Thresholds are 300, 600 and 900 seconds, with 600 seconds primary. A partially reachable edge is clipped along its oriented geometry using remaining seconds multiplied by speed. Saved reachable geometries were checked against recalculated partial edges before reuse. No change to the shortest-path algorithm was needed. A full-graph check found no restricted-tag edge reachable within 15 minutes; conditional permissions and wider topology findings remain limitations.

## Two-dimensional service areas
Network reach is the authoritative time result. Buffer all reachable lines by 25, 50 and 75 m and dissolve, producing nine raw geometries. Retain all polygon parts, without smoothing, convex hulls or silently filling gaps. Also subtract the selected water-polygon union to create nine land-only geometries. Verify validity and 5/10/15 nesting; derive non-overlapping bands by differences. These polygons are representations, not exact travel-time boundaries. Buffering can cross unmodelled barriers or narrow gaps; water subtraction does not prove access to individual parcels and also removes water beneath genuine bridge routes from the polygon (the network routes remain).

Water source: **{water_source}**. The selected file is `data/{water_file}` and its adjacent JSON records query, download timestamp, bounds, CRS and hash. Full returned polygons are retained; the query covers complete supplied census zones, avoiding truncated land denominators. Invalid geometry repairs, if any, are logged. OSM Overpass remains the selected analysis water source. OS NGD features are investigated separately for building, site, land and barrier refinement. {map_context}

## Population and density
For each complete zone i, standard weighting estimates P_i * Area(zone_i intersection raw catchment) / Area(zone_i). Retain zone ID, original population, denominator, intersection area, fraction and contribution. Denominators are full zone areas, never clipped zone areas. Population QA checks identifiers, values, geometries, overlap and preservation of unique source totals. All areas use EPSG:27700 metres, divided by 10,000 for hectares. No intermediate rounding is applied. Density uses the corresponding raw area.

Land-only sensitivity subtracts water from both each full zone and each catchment intersection, then applies the same formula. Its density uses land-only catchment area. This redistributes each full zone population over mapped land, which is still not a residential-area model. At 10 minutes / 25 m, this changes estimated population by {pct:+.1f}% relative to standard weighting.

## Primary choice and sensitivity
Retain the pre-existing 25 m corridor as the primary 10-minute representation: it is transparent, reproducible and keeps the polygon close to the dense pedestrian network, reducing unsupported spread across docks, yards and enclosed parcels. It is a conservative corridor convention, not an empirically measured walking-distance allowance or an optimised buffer. Use the raw footprint for primary standard population/density metrics; display water explicitly and provide the land-only alternative. This decision retains the original parameter rather than selecting a preferred population result.

Standard population changes by {pchange:+.1f}% between 25 and 75 m. This is material: population and area results are strongly dependent on polygon construction and should be presented as scenario estimates, not precise catchment counts. Increasing width fills more space between dense network lines; narrow buffers may omit nearby residents, while wider buffers can include inaccessible land. The six 10-minute standard/land-only results and percentage comparisons must accompany the main map.

## School accessibility
Reuse documented Closed/Overseas/Duplicate exclusions and retain unusual attributes for review. Address geocoding does not establish a pedestrian entrance. The present inputs provide no reviewed school destination evidence, so {metrics['school_destinations_unverified']} destinations are unverified. The number of network-accessible schools is pending, not zero. Candidate projections and travel times are diagnostic only and omit any unverified school-to-network connection. No connector is added through water, buildings or fences. Records within two minutes of the 10-minute threshold are flagged. Polygon inclusion is a secondary comparison only, using the water-subtracted primary representation.

`data/qa/school_destination_review.csv` accepts a reviewed existing graph node, destination method, evidence, reviewer and date. Approve only a verified entrance on the network or a defensible public-network proxy. Approved destination times use the existing Dijkstra results; unresolved destinations remain null. If a verified entrance lies inside an edge, a separate documented edge-insertion step is required before assigning its node. Rerun steps 10 and 11 after review. No candidate destination is silently accepted.

## Reproduction
Activate the project's GIS environment, then run:

```bash
python scripts/09_prepare_water.py
python scripts/10_analyse_service_areas.py
python scripts/11_export_analysis_maps.py
python scripts/12_google_isochrone_comparison.py
python -m unittest discover -s tests -v
```

Step 09 reuses OSM water by default; step 10 verifies and reuses saved network reach, writes 18 scenarios and school QA; step 11 exports A3 landscape PDF/PNG maps, charts and notes using the native EPSG:27700 OS Maps layer when the OS project key is available. Step 12 performs the six-request Google walking-isochrone comparison (A/B × 5/10/15 minutes). Step 13 is a read-only OS NGD collection and small-sample discovery. Existing steps 01-08 need not be rerun unless their inputs or assumptions change. Analysis outputs are under `outputs/gis`, `tables`, `qa`, `maps` and `methodology`.
'''
    (METHOD/'methodology.md').write_text(text)
    limits='''# Assumptions and limitations

- Fixed walking speed 4.8 km/h; no slope, stairs, crossing-delay, congestion or mobility variation. Sensitivity varies time and buffer width, not walking speed.
- A/B are plausible reviewed external accesses, not confirmed future entrances. No internal-site walk is included.
- Three manual network connectors model confirmed physical connections; their geometry and lengths are modelling assumptions documented in the change log.
- OSM completeness, permissions and grade separation remain imperfect. Wider local gap, disconnected-component and conditional-access findings are retained, not silently repaired. A zero count of restricted links does not establish that every remaining route is publicly usable.
- The 1D network is authoritative. A buffer can overlap unwalkable land, merge across narrow gaps or cross unmodelled barriers. Water subtraction removes mapped water only; no claim of parcel-level access or complete barrier treatment is made.
- Census 2021 baseline population is uniformly allocated within complete zones (or complete mapped land for sensitivity). Neither method measures residential occupancy at building level or current population.
- Population and area are materially sensitive to 25/50/75 m representation; present the range alongside the primary 25 m result.
- All school destination classifications remain pending unless evidence is entered in the review file. Address matches and polygon inclusion are not entrance verification. Candidate times are not school accessibility results.
- Water completeness follows the selected OS NGD or OSM source and its recorded acquisition date. OS Maps raster tiles, when configured, provide visual context only and never enter the analysis.
- Presentation rounds population to the nearest ten, area/density to one decimal; analytical CSV values retain precision.
'''
    (METHOD/'assumptions_and_limitations.md').write_text(limits)
    note=f'''The indicative 10-minute walking corridor around the Crossharbour site contains approximately {metrics['estimated_population']:,} residents at {metrics['population_density_per_ha']:.1f} residents per hectare, using the supplied Census 2021 baseline. Its raw footprint is {metrics['catchment_area_ha']:.1f} hectares.

The analysis assumes a walking speed of 4.8 km/h from either of two reviewed external access locations. These are planning proxies, not confirmed future building entrances. A 25 m corridor around reachable paths provides the primary map representation.

The number of accessible schools cannot yet be confirmed: school address coordinates are available, but pedestrian entrances or defensible network destinations still require review. Schools inside the shaded area must not be counted as accessible on that basis alone.

Population is estimated by distributing each census zone's population uniformly across its area. Different corridor widths materially change the estimate; the land-only sensitivity gives approximately {metrics['land_only_estimated_population']:,} residents. These are scenario estimates, not current headcounts. OSM completeness, conditional permissions and unmodelled barriers remain limitations. Use the results to frame workshop discussion, with the sensitivity charts and school-review status visible.
'''
    if len(note.split())>200:raise ValueError('Architect note exceeds 200 words')
    (METHOD/'architect_note.md').write_text(note)
