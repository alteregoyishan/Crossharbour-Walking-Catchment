# Crossharbour Walking Catchment

最新分析交付：`outputs/gis/`（18 组服务区情景、人口相交、学校结果和 polygonisation review）、`outputs/maps/`（A3 地图与诊断输出）、`outputs/qa/`（敏感性图和 QA）、`outputs/methodology/`（方法、限制、architect note）。旧 `outputs/spatial/` 是第 08 步路网分析的中间输出。

已有 01–08 步结果时，后续分步运行：

```bash
python scripts/09_prepare_water.py
python scripts/10_analyse_service_areas.py
python scripts/11_export_analysis_maps.py
python scripts/12_google_isochrone_comparison.py
python scripts/14_compare_catchment_polygonisation.py
python scripts/15_review_catchment_barriers.py
```

第 09 步继续复用已经验证的 OSM water polygons。第 10 步验证已有可达路网，并完成 5/10/15 分钟 × 25/50/75 m × 标准/陆地面积加权；第 11 步通过现有 OS Data Hub project key 加入原生 EPSG:27700 的 OS Maps Light 底图；第 12 步把 Google 官方 walking isochrones 与 OSMnx 结果并列比较。主方案沿用 25 m buffer，人口为 **ONS Census 2021 baseline**，不代表当前实测人口。

## OS NGD 水体、OS Maps 底图和 Google Isochrones

OS Places、OS NGD API – Features 和 OS Maps API 使用同一个已经存在的 OS Data Hub project key，不需要重复填写。`.env` 中只需保留：

```text
OS_PLACES_API_KEY=你的 OS Data Hub project key
GOOGLE_MAPS_API_KEY=你的 Google Maps Platform key
WATER_SOURCE=osm
OS_MAPS_LAYER=Light_27700
```

该 OS Data Hub 项目需要启用 **OS Places API**、**OS NGD API – Features** 和 **OS Maps API**。代码通过 header 使用同一个 key，密钥不会写入 URL、输出或日志。当前水体与地图步骤为：

```bash
python scripts/09_prepare_water.py --source osm
python scripts/10_analyse_service_areas.py
python scripts/11_export_analysis_maps.py
```

OSM water polygons 继续作为分析层。`WATER_SOURCE=osm` 明确记录该选择；OS NGD Water 只保留为可选比较，不会因为 key 可用而自动替换当前水体。

OS Maps API 只用于第 11、12 步的地图底图。默认使用 `Light_27700` 和 WMTS `EPSG:27700:9`，与项目分析 CRS 一致，不再重投影 Web Mercator 底图。瓦片缓存在 `data/external/ordnance_survey/maps_tiles/`，不会参与最短路、服务区、水体裁切或人口计算。发布地图前应按你的 OS 许可确认 `.env` 中的 `OS_MAPS_ATTRIBUTION` 和 OS branding。

Google Cloud 项目需要启用 **Isochrones API**、关联 Billing，并允许密钥访问 `isochrones.googleapis.com`。之后运行：

```bash
python scripts/12_google_isochrone_comparison.py
```

脚本共请求 6 次：A、B 两个 origin 分别请求 5、10、15 分钟，参数为 `WALK`、`FROM`、`TRAFFIC_UNAWARE`、high fidelity、no smoothing；随后按相同时间合并 A/B，以对应 `min(T_A,T_B)`。输出为 `outputs/gis/google_walking_isochrones.gpkg`、`outputs/tables/google_isochrone_comparison.csv` 和 `outputs/maps/google_isochrone_comparison.png`。Google polygon 与 25 m OSMnx corridor 的面积重叠只是方法比较，不能解释为哪一个“正确”。

OS NGD collection discovery 单独运行，不修改现有分析：

```bash
python scripts/13_discover_os_ngd_features.py
```

它读取 live collections/schema/queryables，并在 site 350 m 范围内对每个候选 collection 最多取 10 条。报告在 `outputs/methodology/os_ngd_feature_inventory.md`，详细字段在 `outputs/qa/os_ngd_sample_schema.csv`，可视化样本在 `data/qa/os_ngd_discovery_samples.gpkg`。

学校地址坐标不等于入口坐标。`data/qa/school_destination_review.csv` 供填写已审核的路网目的地节点及证据；未审核学校的正式网络时间和可达结论保持空值，候选时间另列。学校数量为 pending 时不能解释为没有可达学校。完成审核后只需重跑 10、11。

当前代码按独立步骤完成数据 QA、人口清洗、学校清洗和学校坐标交叉验证。

## 运行

```bash
conda env create -f environment.yml
conda activate prior-gis
python scripts/01_validate_data.py
```

运行成功后生成两个简单表格：

```text
outputs/tables/qa_issues.csv    每条问题的记录和证据
outputs/tables/qa_summary.csv   按数据集和问题类型汇总，并列出示例记录
```

`qa_summary.csv` 用于快速查看问题出在哪个数据集、属于哪类检查、是什么问题，并显示最多三个示例记录。`qa_issues.csv` 列出全部需要复核或修正的记录、证据和建议的下一步。

## QA 方法

脚本按以下类别依次检查：

1. `field_structure`：空数据、重复字段、重复记录、URN/LSOA 缺失或重复；
2. `crs`：CRS 是否存在、坐标范围是否合理、能否转换到 EPSG:27700；
3. `spatial_location`：学校坐标是否可用、是否异常、是否重复；
4. `geometry_topology`：几何是否缺失、为空、自相交或相互重叠；
5. `attribute_consistency`：人口合计、学校容量、学生数、年龄和场地面积是否一致。

`config.yaml` 不再重复列出数据字段，只保存 CRS、距离和容差等需要人为确定的参数。如果某类字段存在，程序就执行对应检查；如果不存在，则跳过该项检查。

基础检查以后，各数据集只运行自身需要的专项检查：

- 场地：几何面积与源数据文字中声明的面积是否一致；
- 学校：坐标是否可用、是否需要外部来源核实、容量/学生数和年龄范围是否合理；
- 人口：人口值是否有效、分项之和是否等于总数、面之间是否异常重叠。

“坐标需要核实”只是筛查结果，不能据此判断学校位于海外。以后可在数据修正阶段使用 OS Places API 按地址核实，并补全缺失坐标。

## 人口数据清洗

QA 检查完成后，单独运行人口清洗：

```bash
python scripts/02_clean_population.py
```

该步骤只处理已经确认的两个问题：

- 完全重复的 `E01035705`：保留源数据第 66 条，删除第 109 条；
- 自相交的 `E01035698`：使用 `shapely.make_valid` 修复为有效的 `MultiPolygon`。

清洗结果写入 `data/cleaned/lsoa_2021_population_cleaned.gpkg`，修改依据写入 `outputs/tables/population_cleaning_log.csv`。脚本在输出前检查 LSOA 唯一性、几何有效性、记录数和 CRS。

`qa_issues.csv` 和 `qa_summary.csv` 保留对原始数据的审计结果，不会因清洗而被覆盖。第二步会对清洗后的人口图层重新运行人口 QA，只有零遗留问题时才发布输出文件。

## 学校数据清洗与 OS Places API

学校步骤使用 OS Places API 的地址搜索接口。先创建本地密钥文件：

```bash
cp .env.example .env
```

然后在 `.env` 中填写：

```text
OS_PLACES_API_KEY=你的密钥
GOOGLE_MAPS_API_KEY=你的 Google Maps Platform 密钥
```

`.env` 已被 Git 忽略，API key 不会写入代码、清洗结果或日志。填写后运行：

```bash
python scripts/03_clean_schools.py
```

该步骤会：

1. 排除 29 条 `Closed` 学校；
2. 再根据 `LA (name)` 排除剩余的 15 条 `Fieldwork Overseas Establishments`，这些记录不调用 OS Places；
3. 对完全重复的 URN `100898` 保留第一条、删除第二条；
4. 将全部 87 条源记录及 `keep/remove`、`Closed/Overseas/Duplicate` 原因写入 `data/qa/schools_qa.csv`；
5. 仅对剩余 42 所伦敦学校逐一调用 OS Places `/find`；
6. 使用 `config.yaml` 的 London BNG bounds 同时约束 API 请求和本地候选筛选；
7. 保存原始坐标，再用选中的 OS 坐标更新 `Easting`、`Northing` 和点几何；
8. 保存匹配地址、UPRN、OS match score 和 `high/medium/review/no_match` 置信度；
9. `high` 和 `medium` 直接采用 OS 坐标；`review` 自动调用 Google Geocoding API；
10. Google 返回伦敦范围内且邮编一致的结果时，使用 Google 坐标并将 `coordinate_source` 写为 `Google Geocoding API`；其余记录输出供人工核实。

当前 42 所伦敦学校的 OS API 结果为：23 条 `high`、13 条 `medium`、6 条 `review`。13 条 `medium` 都与源邮编一致；有原始有效坐标可比较的记录与 OS 坐标相差不超过 86 米，因此作为可接受的部分匹配。6 条 `review` 会在同一次脚本运行中自动交给 Google 验证。

最终数据为 `data/cleaned/schools_cleaned.csv` 和 `data/cleaned/schools_cleaned.gpkg`。全部 42 次查询保存在 `outputs/tables/os_places_school_matches.csv`，仅需人工复核的记录保存在 `outputs/tables/school_coordinate_review.csv`，所有筛选和查询动作保存在 `outputs/tables/school_cleaning_log.csv`。未填写 API key 时仍会生成学校 QA 表，然后在地理编码前停止，不会生成伪造坐标。

`schools_cleaned` 只保留学校分析需要的字段，以及 `api_returned_address`、`coordinate_source`、`coordinate_confidence` 和 `coordinate_status`。`api_returned_address` 与最终采用的坐标来源一致：采用 OS 坐标时写 OS Places 地址，采用 Google 坐标时写 Google 格式化地址。API 分数、UPRN 和比较距离保留在审计表中。

在 Google Cloud 中为密钥所属项目关联 Billing，并启用 [Geocoding API](https://developers.google.com/maps/documentation/geocoding/get-api-key-v4)。然后把 `GOOGLE_MAPS_API_KEY` 写入 `.env`。`03_clean_schools.py` 会自动查询低置信度学校，不需要再运行单独的脚本。Google 结果写入 `outputs/tables/google_school_validation.csv`；该表保存 Google Place ID、邮编/距离比较结论，不保存 Google 返回的完整地址或坐标。

## OpenStreetMap 步行网络

学校清洗完成后运行：

```bash
python scripts/04_download_pedestrian_network.py
```

脚本以全部清洗后学校点的凸包加 2 km 缓冲作为 Overpass 下载范围，并通过 OSMnx 的 `network_type=walk` 下载公共可步行道路和路径。网络投影到 EPSG:27700，每条边保留 `length` 并按 4.8 km/h 增加 `travel_time_s`，可直接作为 NetworkX 最短路径权重。

NetworkX 图保存为 `data/processed/OpenStreetMap/pedestrian_network.graphml`，GIS 检查图层保存为 `data/processed/OpenStreetMap/pedestrian_network.gpkg`。下载范围和学校到最近网络节点的检查结果分别保存为 `data/processed/OpenStreetMap/pedestrian_network_download_area.gpkg` 和 `data/qa/school_network_access.csv`。范围没有变化时会复用 GraphML；需要重新请求 Overpass 时运行 `python scripts/04_download_pedestrian_network.py --force`。

`data/processed/OpenStreetMap/pedestrian_network.gpkg` 可直接加入 QGIS，其中 `edges` 是用于展示和分析的步行道路/路径，`nodes` 是 NetworkX 节点。采用 GeoPackage 是因为它可在一个文件中保存多个图层，并完整保留字段名。

## Pedestrian network QA

下载后、选择 origin 前运行：

```bash
python scripts/05_validate_pedestrian_network.py
```

第 05 步只读取现有 GraphML，不修改边、补连接、拼接分量或运行 catchment。它检查图的元数据、弱/强连通分量、孤立点、物理 dead ends、局部疑似断点、平行边、步行权限标签、bridge/tunnel、几何有效性、节点与边端点一致性，以及 GraphML `length` 与 EPSG:27700 几何长度的差异。

当前图为 OSMnx 2.0.1 创建的 EPSG:27700 有向 `MultiDiGraph`，已经简化，包含 36,968 个节点和 91,692 条有向边。A、B 对应边都位于最大弱连通分量。几何有效性、零长度、端点匹配和长度差异检查通过；最大长度差约 3.21 m（约 0.30%）。

当前图确实使用 `network_type=walk` 下载。OSMnx 在 Overpass 查询时用 `foot` 排除 `foot=no`。下载代码另外把 `foot`、`layer` 和 sidewalk 系列加入保存字段，重新生成的 GraphML 现包含6,856条带 `foot` 的有向边、2,718条带 `layer` 的边，以及 sidewalk 属性。

基础图重新 QA 后，site 250 m 内没有未被 `foot` override 解释的 restricted edge；site 1 km 内有 10 条 restricted physical edges。基础图共有 711 个弱连通分量，A、B 都位于最大分量。

人工检查确认与 OSM ways `1192930895`、`1196724993`、`272697652` 相关的三处近距离断点实际连通。第 06 步把这三项批准修改写入基础图的副本，生成 reviewed graph；基础 GraphML 保持不变。修改后弱连通分量由 711 降为 708，三项 site 250 m 内的高优先级断点均已消失，A、B 仍位于最大分量。

结果保存为：

- `outputs/tables/network_qa_summary.csv`：检查值、状态、严重性和适用性结论；
- `outputs/spatial/network_qa_issues.gpkg`：`graph_issues`、`component_issues`、`edge_issues`、`node_issues` 四个图层；
- `outputs/figures/network_qa_map.png`：site 周边 1 km 的人工复核图；
- `outputs/tables/network_changes.csv`：三项人工批准的连接、证据、连接长度和修改结果；
- `data/qa/network_restricted_links_review.csv`：site 1 km 内剩余受限路段的 OSM 链接和人工决定栏。

批准的连接通过以下命令应用到独立副本，并随后对副本重新 QA：

```bash
python scripts/06_apply_reviewed_network_changes.py
python scripts/05_validate_pedestrian_network.py --reviewed
```

`network_restricted_links_review.csv` 中另有 10 条距 site 1 km 内的 restricted links，但从 A/B 沿路网到达它们至少需要 15.65 分钟，因此不会影响 5、10、15 分钟结果。它们保留为 `outside_analysis_horizon` 审计项；只有分析超过 15 分钟时才需要继续核实。Reviewed QA 对三个分析时段均给出 `SUITABLE_WITH_REVIEW`。

## Site origin points

路网 QA 问题处理并复核通过后运行：

```bash
python scripts/07_select_site_origins.py
```

Site 是 fictional development plot，原始资料没有指定未来建筑入口。Origin selection 先筛查地块边界与现有公共步行网络的接触位置，再进行人工图面复核。筛查排除 `service`、`parking_aisle`、private/no-access 道路，只接受真正穿越 Site boundary 的公共 pedestrian edge，并要求路径与边界的交叉角至少为 45°。不同 access points 之间至少相距 20 m，最多保留两个。

检查结果保留两个北侧接入：A 为 north-west access，交叉角约 87°；B 为 north access，交叉角约 82°。它们位于不同的 footway 接入位置，间距超过 20 m，因此同时保留可以避免任意舍弃一个真实入口。东南侧接触的交叉角只有约 8°，基本沿边界擦过，更可能是制图重叠，因此不作为 origin。A、B 是规划与可达性分析的 proxy origins，不代表已经确认的未来建筑入口。

`data/processed/site_origins.gpkg` 可直接加入 QGIS，包含：

- `site_access_points`：人工复核后保留的 A、B 两个 proxy origins，位置保持在对应 OSM footway 上。

地块边界始终使用原始 `data/raw/site_boundary.geojson`，不在 origin GeoPackage 中复制。原始边界采用 EPSG:4326，origin 和路网采用适合米制距离分析的 EPSG:27700；在 QGIS 中应启用按需坐标转换。

交叉角、最小间距等选择参数记录在 `config.yaml` 的 `site_origins` 部分；选择记录保存在 `data/qa/site_origin_selection.csv`。

## Walking catchments

Origin 复核完成后运行：

```bash
python scripts/08_calculate_walking_catchments.py
```

第 08 步不使用 nearest-node snapping，也不创建直线 connector。脚本将每个 origin 插入其所在的双向 OSM footway，把原 edge 拆成 `u → origin → v`，保留 OSM 属性并按投影几何重新计算两段长度。QA 要求两段长度之和与原 edge 几何长度相等，并确认 `highway=footway`、未设置 private/no access。

全路网按 4.8 km/h（1.333333 m/s）计算 `travel_time_s`，然后从 A、B 执行 multi-source Dijkstra：`T(v) = min(T_A(v), T_B(v))`。5、10、15 分钟阈值分别为 300、600、900 秒，10 分钟为主结果。阈值落在 edge 中间时，脚本按照剩余时间截断 edge，不会因为一个端点可达就纳入整条 edge。

`reachable_network_*` 是可达性的权威结果。`walking_catchment_*` 使用 `config.yaml` 中的 25 m network buffer 生成，只用于制图和 workshop 展示；它不是精确物理边界。沿每条可达线缓冲后 dissolve 会产生枝状边界、小孔洞和圆角，这种外观来自表示方法，不是最短路计算错误。原始层不裁水，另存的 `catchment_land_only` 扣除水体供敏感性分析；没有使用 convex hull。

旧 catchment 结果已经删除，并基于 reviewed graph 重新运行第 08 步。结果为：

| 时间 | 可达路网长度 | 展示用缓冲区面积 |
|---:|---:|---:|
| 5 分钟 | 8,179.7 m | 18.42 ha |
| 10 分钟 | 28,998.5 m | 69.44 ha |
| 15 分钟 | 57,265.0 m | 144.48 ha |

A、B 与所选 footway 的距离均为 0 m，edge 拆分长度检查通过；三个时段的路网包含关系和面积递增检查也通过。输出包括 `outputs/spatial/reachable_network_*min.gpkg`、`outputs/spatial/walking_catchment_*min.gpkg`、`outputs/tables/walking_catchment_summary.csv` 和 `outputs/figures/walking_catchment_qa.png`。该步骤停止在网络 catchment，不计算人口或学校可达性。

地图按 15、10、5 分钟的顺序绘制，较短阈值放在更高图层，所以 5 分钟橙色线路不会被包含它的 10/15 分钟线路覆盖。

学校清洗后的 42 条记录已经有位置，但这些地址坐标不等于经证实的步行入口。`data/qa/school_destination_review.csv` 中的 `pending`/地图上的 “School entrance not yet verified” 仅表示尚未为每所学校确认校门及其与步行路网的合法连接，并不表示学校记录无效。在入口证据完成前，学校可达数量保持 `Pending`。

Google 对比使用官方 Isochrones API，而不是用 Routes API 对大量网格点逐点试算。A、B 分别请求 5、10、15 分钟，因此首次运行共 6 次请求；默认复用已保存的原始响应。`google_walking_isochrones.gpkg` 保留 Google 原始面，`google_walking_isochrones_land_only.gpkg` 只为展示扣除已选 OSM 水体，不改变 Google 路由结果。比较表同时报告原始面积、去水体展示面积和水体重叠比例。

## 10 分钟 Catchment polygonisation review

第 14 步只比较 10 分钟 catchment 的面表达方式，不修改 reviewed OSMnx graph、A/B origins、multi-source Dijkstra、4.8 km/h 速度或部分可达 edge。运行：

```bash
python scripts/14_compare_catchment_polygonisation.py
```

比较的三种方法为：

- **Method A — Raw 25 m buffer**：第 08 步的原始 25 m 可达路网 buffer，作为不可改写的 baseline；
- **Method B — Controlled hole treatment**：保留 Method A 外边界，只填补有独立网格可达性及周边路网证据支持的内部 buffer 孔洞，并保留水体和证据不足的孔洞；
- **Method C — Experimental contour**：按网络 travel time 对 edge 中间点采样，以 600 秒等值线构面并扣除 OSM water。当前 Python 环境使用普通 Delaunay，因此这是受 Lin et al. (2020) 启发的实验代理，不等同于论文及参考 C++ 实现中的 constrained Delaunay triangulation。

10 m 验证网格通过“到最近 reviewed physical edge 的有向网络到达时间 + 离网步行时间”独立生成标签，不读取任何候选 polygon；precision、recall 和 F1 因此表示相对于该独立点接入模型的一致性，不是现场观测的绝对 ground truth。

当前复核建议使用 **Method B** 作为待确认的正式 polygonisation：面积 74.42 ha，估算人口 8,888，人口密度 119.4 人/ha；它填补 34 个有证据支持的 buffer artefact，保留 3 个待核实孔洞，并把 Method A 的外边界保持不变。Method C 的 F1 较高，但在没有 constrained triangulation 和显式 barrier constraints 的情况下不作为正式结果。现有 Figure 01 继续显示 Method A，只有审阅通过后才应替换。

主要输出为：

- `outputs/gis/catchment_hole_inventory.gpkg` 和 `outputs/tables/catchment_hole_inventory.csv`：37 个孔洞的分类、证据和处理决定；
- `outputs/gis/catchment_polygonisation_methods.gpkg`：A/B/C、独立验证网格、Method C 网络采样点及 barrier audit 图层；
- `outputs/tables/catchment_method_comparison.csv`：面积、人口、密度、孔洞、水体、网络覆盖和 components；
- `outputs/tables/catchment_method_validation.csv`：独立验证网格的 precision、recall 和 F1；
- `outputs/tables/catchment_polygonisation_population.csv`：标准原始 LSOA 面积加权与单列的 water-adjusted sensitivity；
- `outputs/maps/catchment_hole_diagnostic.pdf` 和 `outputs/maps/figure_04_catchment_method_comparison.pdf`：A3 孔洞诊断与三方法对比；
- `outputs/methodology/catchment_polygonisation_review.md`：研究依据、实现差异、结果解释和限制。

## 统一水体与桥梁政策

第 15 步保持路由结果不变，为 A2、B2、C1 和 C2 使用同一套水体与桥梁后处理：从分析面扣除经过几何验证的 OSM water；将有明确 `bridge`、`tunnel` 或非零 `layer` 证据的可达跨水 edge 单独保存为桥梁显示廊道；人口、密度和 land area 始终使用不含桥廊道的 `analytical_land_catchment`。

```bash
python scripts/15_review_catchment_barriers.py
```

当前识别出 11 条可达路段与水体相交。其中 7 条具有明确的 OSM 立体交叉证据，使用 1 m/侧、约 2 m 总宽的显示廊道；其余 4 条无标签的约 1–1.4 m 重叠保持 `requiring review`，不会自动当作桥梁。0.5、1 和 2 m/侧的完整敏感性记录在 `bridge_corridor_sensitivity_v2.csv`。选定廊道为所有处理方法增加约 142 m² 的显示面积，不计入人口或 land area。

公平比较结果为：

| 方法 | 分析陆地面积 | 估算人口 | 密度 | 孔洞 | Grid-model F1 |
|---|---:|---:|---:|---:|---:|
| A0 原始 25 m buffer | 69.44 ha | 8,161 | 117.5/ha | 37 | 0.887 |
| A2 统一水体政策 | 66.69 ha | 7,683 | 115.2/ha | 40 | 0.905 |
| **B2 受控填孔** | **71.66 ha** | **8,410** | **117.4/ha** | **6** | **0.943** |
| C1 普通 Delaunay | 67.93 ha | 8,006 | 117.9/ha | 3 | 0.962 |
| C2 barrier-aware proxy | 67.54 ha | 7,969 | 118.0/ha | 15 | 0.962 |

C1 的 10,855 个 Delaunay triangles 全部输出供审计。C2 对其中 568 个会参与 600 秒面的可疑三角形进行局部约束：跨越未确认水体障碍、网络距离相对欧氏距离异常大或跨网络分量时，只保留它与 25 m 可达路网 buffer 重合的部分。C2 仍是 ordinary-Delaunay heuristic，不是 Lin et al. 的 constrained Delaunay 实现。当前环境没有 CGAL Python bindings、Triangle 或 meshpy；C3 因依赖和 directed extended-tree 验证风险没有实施。

第 15 步比较阶段曾建议 B2 作为保守方案。项目作者随后明确选择 C1 作为最终展示方法；第 16 步记录并实施这一选择，不回写第 15 步的历史比较结果。

新增主要输出：

- `outputs/gis/catchment_polygonisation_barrier_review_v2.gpkg`：A0/A2/B2/C1/C2 分析面、显示面、桥廊道、验证网格和人口相交；
- `outputs/qa/reachable_water_crossings.gpkg` / `.csv`：11 条 crossing 的 OSM 属性、分类、证据和处理；
- `outputs/qa/method_c_triangles_v2.gpkg`：全部 Delaunay triangles 和距离/障碍指标；
- `outputs/qa/method_c_suspicious_triangles.gpkg`：需要复核的 triangle；
- `outputs/maps/figure_05_barrier_consistent_methods_v2.pdf`：A2/B2/C1/C2 公平比较；
- `outputs/maps/figure_06_bridge_barrier_qa_v2.pdf`：每条自动接受 crossing 的局部 QA；
- `outputs/methodology/catchment_barrier_policy_review_v2.md`：完整方法、C3 可行性和最终建议。

## 最终 C1 图件

运行：

```bash
python scripts/16_export_final_c1_maps.py
```

最终 C1 仍使用同一组网络 travel-time 样本、普通 Delaunay 线性插值和 150 m 最大三角边限制。完全封闭且不与 mapped water 重叠的内部空白属于插值覆盖缺口；第 16 步填补这些空白后再次扣除 water，因此 Millwall Park 不再错误显示为空洞，docks 和其他水体仍被排除。为避免三角网稀疏位置的短路段伸出面外，analytical land 在陆地范围内加入可达路网两侧各 2 m 的最小包络；5、10、15 分钟未被面覆盖的非水体可达路网长度均为 0 m。制图层另用 10 m 拓扑简化和 close/open 处理边界，并在最后恢复路网两侧各 6 m 的可见包络；这个较宽包络只用于地图，不参与人口或面积计算。

5、10、15 分钟单图使用完全相同的地图范围和比例尺。参考圆采用 4.8 km/h × 15 分钟，即半径 1,200 m、直径 2.4 km；代码同时验证它完整包含 15 分钟 cartographic catchment。Development site 使用单层、较细的黑色虚线空心符号，不再绘制白色 casing；A/B origins 使用较小圆点和无背景文字，学校使用较大的紫色圆点，地图和图例中的实际点径一致。5、10、15 分钟路网分别使用对应 catchment 颜色的深色版本；组合图先画 15 分钟路网，再叠加 10 和 5 分钟路网。

主要输出：

- `outputs/maps/figure_00a_site_origin_selection.png` / `.pdf`：使用弱化的 OS Maps `Outdoor_27700` 底图和更细一级瓦片矩阵，显示 reviewed OSM pedestrian network、A/B 所在 footway 和交叉角；底图文字为栅格内容，不能单独调整字号；
- `outputs/maps/figure_00b_all_cleaned_schools.png` / `.pdf`：全部具有可用 geometry 的清洗后学校；
- `outputs/maps/figure_01_c1_05min.png` / `.pdf`；
- `outputs/maps/figure_02_c1_10min.png` / `.pdf`；
- `outputs/maps/figure_03_c1_15min.png` / `.pdf`；
- `outputs/maps/figure_04_c1_combined.png` / `.pdf`；
- `outputs/maps/figure_05_c1_combined_2400m_diameter.png` / `.pdf`；
- `outputs/gis/c1_walking_catchments_final.gpkg`：analytical、display、cartographic display、unmasked contour、network samples 和 bridge corridor 图层；
- `outputs/tables/c1_walking_catchment_summary.csv`；
- `outputs/methodology/final_c1_map_series.md`。

## 人口分配敏感性

运行：

```bash
python scripts/17_analyse_population_sensitivity.py
```

P1 保持为主结果：使用 ONS Census 2021 `usual_residents_total`，在 EPSG:27700 中按原始完整 LSOA 面积加权；analytical C1 catchment 已扣除水体且不包含制图桥廊道，但 LSOA 分母不扣水。10 分钟结果为 **8,016 人、117.8 人/ha**。

OS NGD 只下载与最终 catchments 相交 LSOA 的有界范围。10 分钟敏感性结果为：P2 residential Site v2 9,739 人（+21.5%）；P3 residential-address-share building footprint 9,065 人（+13.1%）；P4 使用直接 `numberoffloors`，在 mixed-use residential share 0.25 / 0.50 / 0.75 下分别得到 8,997 / 8,962 / 8,938 人（+12.2% / +11.8% / +11.5%）。情景名称表示 mixed-use 权重大小，不保证最终人口数同方向排列；这里较高 mixed-use 权重会把更多权重放到 catchment 外侧的 mixed-use 楼面，因此估算总量略降。直接楼层字段覆盖 99.4% 的住宅加权 footprint；没有从高度猜测楼层。Building Access Location 对分类住宅建筑的直接覆盖仅 0.03%，因此 P5 没有实施，也没有自动跨水体或障碍吸附入口。

Crossharbour 高层住宅很多，footprint 本身不能代表住宅楼面面积，因此 P2–P4 都是敏感性分析，不能替代透明的 P1。主要输出：

- `outputs/tables/population_sensitivity_c1.csv`：P1–P4 汇总与共同密度分母；
- `outputs/tables/population_allocation_methods_by_lsoa.csv`：每种方法的逐 LSOA 分配过程；
- `outputs/tables/population_spatial_diagnostics_by_lsoa.csv`：逐 LSOA 支撑面积、重叠比例、估算差异及排序；
- `outputs/tables/population_accessibility_p5.csv`：P5 覆盖不足和 `ACCESS_UNKNOWN` 决定；
- `outputs/tables/population_method_feasibility.csv`；
- `outputs/qa/population_spatial_qa_issues.csv` 和 `population_ngd_coverage_qa.csv`；
- `outputs/gis/population_sensitivity_c1.gpkg`；
- `outputs/maps/figure_06_population_sensitivity_qa.png` / `.pdf`；
- `outputs/methodology/population_calculation_summary.md`：公式、假设、全部结果和最终推荐集中说明；
- `outputs/methodology/population_sensitivity.md`；
- `data/processed/OrdnanceSurvey/os_ngd_population_support.gpkg`（API key 不写入数据或报告）。

## 代码结构

```text
config.yaml                    CRS、距离和容差参数
environment.yml               Conda 环境与 Python 依赖
scripts/01_validate_data.py    QA 命令入口
scripts/02_clean_population.py 人口数据清洗入口
scripts/03_clean_schools.py    学校清洗和 OS Places 查询入口
scripts/04_download_pedestrian_network.py OSM 步行网络下载入口
scripts/05_validate_pedestrian_network.py OSM 路网只读 QA 入口
scripts/06_apply_reviewed_network_changes.py 应用人工批准的路网连接
scripts/07_select_site_origins.py Site 路网起点选择入口
scripts/08_calculate_walking_catchments.py 5/10/15 分钟路网 catchment
scripts/09_prepare_water.py    OS NGD 水体下载与 OSM fallback
scripts/10_analyse_service_areas.py 人口、密度、学校和敏感性分析
scripts/11_export_analysis_maps.py A3 PDF/PNG 地图与方法说明
scripts/12_google_isochrone_comparison.py Google walking isochrone 对比
scripts/13_discover_os_ngd_features.py OS NGD collections 与小范围样本调查
scripts/14_compare_catchment_polygonisation.py 10 分钟孔洞诊断与 A/B/C polygonisation 比较
scripts/15_review_catchment_barriers.py 统一水体/桥梁政策及 A0/A2/B2/C1/C2 比较
scripts/16_export_final_c1_maps.py 最终 C1 5/10/15 分钟地图系列
scripts/17_analyse_population_sensitivity.py P1 与 OS NGD P2–P5 可行性/敏感性
scripts/18_build_web_report.py GitHub Pages 静态报告图片与 PDF 资源
src/data_validation.py         分类检查函数
src/population_cleaning.py     人口去重、几何修复和输出验证
src/school_cleaning.py         学校筛选、去重、地址匹配和坐标输出
src/google_school_validation.py Google Geocoding 交叉验证
src/pedestrian_network.py      下载、验证和保存 NetworkX 步行图
src/pedestrian_network_qa.py   路网元数据、拓扑、权限和几何 QA
src/network_changes.py         复制基础图并实施批准的路网修改
src/site_origins.py            Site 候选入口和路由节点选择
src/walking_catchment.py       Edge 拆分、Dijkstra、部分边和服务区
src/os_data_services.py        OS NGD Features 与 OS Maps raster 接口
src/google_isochrones.py       Google Isochrones 请求、合并与比较图
src/os_ngd_discovery.py        OS NGD collection/schema/queryables 只读调查
src/catchment_polygonisation.py 孔洞分类、控制填孔、实验轮廓、验证和人口敏感性
src/catchment_barrier_review.py 水体政策、桥梁 crossing、triangle audit 和 barrier-aware C2
src/c1_final_maps.py          最终 C1 几何后处理、统一图幅和地图输出
src/population_sensitivity.py OS NGD 有界下载、P1–P4 分配、P5 可行性和 QA 图
outputs/tables/qa_issues.csv   问题明细
outputs/tables/qa_summary.csv  分类汇总
outputs/tables/population_cleaning_log.csv  人口修改记录
outputs/tables/school_cleaning_log.csv      学校修改和匹配记录
outputs/tables/os_places_school_matches.csv 全部 OS Places 查询结果
outputs/tables/school_coordinate_review.csv 需要人工核实的坐标
outputs/tables/google_school_validation.csv  Google 交叉验证结果
data/processed/OpenStreetMap/pedestrian_network.graphml NetworkX 步行图
data/processed/OpenStreetMap/pedestrian_network.gpkg    GIS 节点和边图层
data/processed/OpenStreetMap/pedestrian_network_reviewed.graphml 人工复核后的 NetworkX 图
data/processed/OpenStreetMap/pedestrian_network_reviewed.gpkg    人工复核后的 GIS 图层
data/processed/site_origins.gpkg              Site 多入口起点图层
data/processed/OpenStreetMap/pedestrian_network_reviewed_with_origins.graphml 将 origins 插入 reviewed edges 后的路由图；公开仓库可使用同名 `.graphml.gz` 快照，加载函数会自动回退
data/qa/site_origin_selection.csv             Site 候选入口选择记录
data/qa/network_restricted_links_review.csv   剩余受限路段人工审核表
data/qa/school_network_access.csv             学校到最近网络节点检查
data/qa/schools_qa.csv                       学校保留/排除决定及原因
outputs/spatial/reachable_network_*min.gpkg   5/10/15 分钟权威可达路网
outputs/spatial/walking_catchment_*min.gpkg   展示用 25 m 路网缓冲区
outputs/tables/walking_catchment_summary.csv  Catchment 数量与 QA 摘要
outputs/figures/walking_catchment_qa.png      Catchment 诊断图
outputs/gis/catchment_hole_inventory.gpkg     37 个内部孔洞及填补/保留依据
outputs/gis/catchment_polygonisation_methods.gpkg  A/B/C 方法、验证网格和 barrier crossings
outputs/tables/catchment_method_comparison.csv 面积、人口、孔洞、水体、路网覆盖和验证指标
outputs/maps/figure_00a_site_origin_selection.pdf 至 figure_05_c1_combined_2400m_diameter.pdf 最终说明图与 C1 地图
outputs/maps/figure_06_population_sensitivity_qa.pdf OS NGD 人口分配 QA
outputs/methodology/AI_USAGE.md AI 辅助范围与人工责任说明
data/raw/                      原始数据，QA 不修改
data/cleaned/                  修正后的数据
tests/test_qa.py               QA 规则测试
```

## 测试

```bash
python -m unittest discover -s tests -v
```

项目采用分步脚本，不使用统一的 end-to-end 入口。人口数据由第二步处理；学校筛选和坐标验证由第三步完成；OSM 步行网络由第四步下载；路网 QA 为第五步；人工批准的连接由第六步应用；Site origin 为第七步；5/10/15 分钟步行 catchment 为第八步。当前第八步已基于 reviewed graph 完成；下一步可进行人口和学校可达性汇总。

## GitHub Pages 报告

网站源文件位于 `docs/`，本地更新网页图片和下载版地图：

```bash
conda activate prior-gis
python scripts/18_build_web_report.py
```

GitHub Pages 从 `main` 分支的 `/docs` 文件夹发布静态网站。本项目仓库名为 `Crossharbour-Walking-Catchment`，因此项目网站地址是 `https://alteregoyishan.github.io/Crossharbour-Walking-Catchment/`，源代码地址是 `https://github.com/alteregoyishan/Crossharbour-Walking-Catchment`。

第一次发布时，在 GitHub 仓库进入 **Settings → Pages → Build and deployment**，选择 **Deploy from a branch**，分支选择 `main`、文件夹选择 `/docs`。随后对 `docs/` 的更新会自动部署。`.env`、API key、OS Maps tile cache、临时数据库 WAL/SHM 和测试缓存均由 `.gitignore` 排除。
