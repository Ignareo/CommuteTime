# AGENTS.md — 面向 LLM 协作指南

## 项目是什么

等时圈通勤工具，纯静态前端（无构建、无后端），基于高德地图 JS API 2.0。两个页面：

- `index.html` — 主工具：设起点、画等时圈、圈内检索候选地点、批量算真实通勤时间（调高德 API）。
- `metro.html` — 地铁可达性可视化：展示由 [shmetro-accessibility](https://github.com/Hecate2/shmetro-accessibility) 项目本地预计算的地铁 N×N 通勤时间矩阵。详见 `PLAN.md`。

## 代码约定

- 单文件页面：HTML + 内联 CSS/JS，除高德 JS API 外**零第三方依赖**，不要引入框架/构建工具。
  例外（仅 CDN script、无构建）：polygon-clipping（交差集几何裁剪）、lz-string（等时圈几何压缩缓存 `house_tool_circle_geom`、实时线网缓存 `house_tool_transit_line_cache`，均按签名 LRU/TTL，配额超限只 toast）。
- JS 用 `var` + `function` 风格，与 `index.html` 一致；UI 文案为中文。
- 密钥加载逻辑（localStorage → `config.yaml` → 弹窗兜底）在两个页面间保持一致的写法；密钥**不在页面展示**——无常驻设置面板、不回显，入口只有无密钥/失效时的弹窗与页脚「重新配置密钥」链接。`metro.html` 无密钥时可跳过弹窗纯数据浏览（地图区显示占位层，排行榜/单站列表可用），有密钥才初始化高德地图。
- 近似圈配速是开发者配置：`config.yaml` 的 `walk_speed`/`ride_speed`（页面无输入框），方案包仍携带速度以保证几何缓存签名命中；Mapbox Token 同样只能写在 `config.yaml`。下车后步行半径上限：默认值来自 `config.yaml` 的 `station_walk_max_m`（米，默认 900），但**用户可在公交/地铁数据源行输入 `#stationWalkMax` 修改**，用户值存 localStorage（`house_tool_station_walk_max`）并优先于 config；硬上限 `TP_STATION_RADIUS_HARD_MAX=2000`，`clampStationRadius` 按 `[下限 120, 硬上限]` 钳制。该值已进入 precise 圈几何签名，改后新建/刷新圈自动失效旧几何、已有存档圈按保存签名不重算；网页改动只提示「新建/刷新生效」，不自动重算已有圈。
- 公交/地铁圈有两种数据源，由 `#transitDataMode` 选择并写进圈的 `dataMode`（`legacy`/`precise`，参与 `circleSig`）。**时长上限按数据源区分**：`precise` 逐站推算没有高德到达圈的 API 限制，与步行/骑行同为 1-120 分钟；`legacy`（高德到达圈）与 Mapbox 仍为 1-60 分钟。`addCircleFrom` 的校验、`onRetimeSelected` 批量改时长、快捷时长 chips 以及切换数据源时的钳制都按此口径。
  - `legacy`：`AMap.ArrivalRange` 到达圈，1 次调用但线网数据滞后（实测上海 18 号线二期缺失）。
  - `precise`：实时线网逐站推算（`buildTransitPreciseRing`）。起点步行范围内 PlaceSearch 站点 POI 的 `address` 给出途经线路 → `LineSearch` 取站序/折线（接口只给一个方向，代码反向补一条以保证双向往返）→ 以到达时刻为优先级做松弛（上车计候车、站间步行换乘计步行+进出站、下车按剩余时间步行）。速度**不用全路网统一常数**（地铁 30/公交 15 只是兜底先验 `TP_SPEED`）：逐线差异达 3~7 倍（18 号线约 37km/h、11 号线约 43km/h、慢速公交不到 6），一律套常数必然一半偏快一半偏慢，边界动辄差一整站。故改为导航校准：①`AMap.Transfer` 乘车段的 `distance/time` 反算该线**含停站**有效速度，按 `城市|线路` 累进 `house_tool_transit_line_speed`，建图优先用（只学长区间样本，越界的脏样本丢弃）；②模型收敛后把**所有估值 ≤ 软地平线（1.18 倍目标）**的站列为候选，先按「是否会改变边界」分池：路径上所有线路都已学到速度、且估值 × 最大修正上限（`max(TP_NAV_BIAS_MAX, TP_LINE_BIAS_MAX)`）仍 ≤ 目标的站确定在内，只留**还没有单线样本的线路**各一个（最多 `TP_NAV_CAL_MAX` 个）作校准样本（已覆盖线路再查没有新信息），其余名额全给可能翻进/翻出的站；整体样本不足 6 个时才从确定站补足样本。边界池再按固定时间桶分层（远端桶名额递增），保证「模型 30 分钟、实际 90 分钟」的离群站仍在候选内。首轮上限仍是 `TP_NAV_CAP_MAX`（按分钟缩放，60 分钟约 88 次）。**最远一桶（模型已超目标 3% 以上）不随主波查询**：等主波样本算出偏差与逐线因子后再决定，校准后估值仍超目标、且该线已有单线样本的站直接跳过（两项系数 ≥1 时校准只会更远），模型偏悲观或样本 <6 时才继续查，省下信息量最低的一批请求。导航门到门时间覆盖该站，并把它们当 Dijkstra 锚点**清空重跑一遍**（第二遍只读内存线网、零 API）。③已复核站的「导航/模型」比值取 60% 分位（`TP_NAV_BIAS_*` 限幅）作为未复核站的接驳/候车系数（上限 `TP_NAV_BIAS_MAX=1.6` 与逐线因子对齐，实测 p60 约 1.52，1.5 会截断样本；按地铁/公交样本量分层，单类 ≥8 个样本才独立取分位；路径含未校准线路的站改用最大修正上限 `TP_LINE_BIAS_MAX` 保守化），分位序列 p50/p60/p75/p90 挂 `__tpDebug.nav.biasStats`，导航全失败时用 1.12 安全余量；否则没来得及复核的偏快站会原样漏进圈。**系数必须在第二遍松弛里乘到耗时上（`timeScale`），不能在 `finish()` 里对最终到达时间再乘一次**——锚点已用导航原始时间重写，再乘就是双重修正，会把临界站（如 18 号线御桥）无端推出圈；即使一个锚点都没命中也要跑第二遍，否则系数根本没生效。逐线修正：只用**单线路径**的复核样本反解 `导航 ≈ 全局系数×接驳 + 线路因子×乘车`，按乘车时长加权、向全局系数收缩后写入 `lineScales`，第二遍只缩放该线乘车时间；多线路径的误差分不清该记给哪条线，不参与线路因子估计。本轮导航学到的逐线速度会回填到已建线路，并先做一次不带锚点/不缩放的预跑刷新模型估值、再重新反推系数——旧系数按旧速度反推，直接叠加新速度等于双重修正。④第二轮**只补被系数挤出圈、且超时不超过 20% 的站**，按「最后乘坐线路」分组，每条线只查最接近边界的一个（`TP_NAV_REFINE_PER_LINE`），其余站用同线锚点的接驳比值外推（真实接驳 = 导航门到门 − 最后步行 − 缩放后乘车），已有锚点且外推明显超时的线直接跳过；多线换乘路径单独分组（误差来自站内步行，不能靠单线因子），每线 1 个且总量 ≤ `TP_NAV_REFINE_HUB_MAX`；方案内锚点已证明超时、模型却说在圈内的站单独补 ≤ `TP_NAV_MID_REFINE_MAX` 个，避免虚构的快速换乘路径漏进圈；「三不管」站（模型圈内、路径含未校准线路、无导航也无方案内锚点）按未校准线路每组补 1 个、总量 ≤ `TP_NAV_UNCAL_REFINE_MAX`，否则整条线会系统性低估漏进圈；`TP_NAV_REFINE_CAP` 只作异常情况下的硬上限。已经在圈内的站不占名额——查了只会缩小圈，解决不了遗漏。`__tpDebug.rejected` 记录圈外近失站的模型/导航/最终分钟与是否已复核，用来区分「导航确认到不了」和「被系数误伤」；导航一次返回多套方案时选其中符合圈类型且最快的那套，零额外调用；该方案的每个乘车段下车站还按「段用时累加」登记成免费锚点（`navMid`，作为第二遍种子与真实下车站时刻，不写 `navSec`），换乘站因此无需额外请求就有一个真实到达时刻；该值**无条件采用**（模型低估时也抬高 best），只当「更早到达的种子」会让模型低估的站永远抬不起来。导航方案同时也是「还有哪条线」的强证据：方案里出现、线网未加载的线路按累计乘车距离取前 `TP_MAX_NAV_LINES`（6）条补 LineSearch（线路名先去掉「(莘庄--富锦路)」这类方向后缀，否则会重复加载已在网线路），导航学到的逐线速度已缓存，第二遍沿新线传播；第二遍收敛后再对这些新线的临界站补一小批导航（每条线最多 2 个、总量同阶），否则一条从未校准的线只有全局限幅系数，像 3 号线友谊路「模型 58 / 真实 62」会原样漏进圈。注意：**绝不能只查 ±22% 窄带**——真实 80 分钟的站若被模型估成 45 分钟，窄带永远碰不到它。区间里程按 LineSearch 的**线路折线**累计，不是站点直线相连——弯折线路用直线会低估里程、把圈画大。几何只填「起点步行盘 + 各可达站的剩余步行盘」，**绝不填充线路折线走廊**：人只能在车站下车，区间任意一点不具备下车后继续步行的能力。步行用 `walk_speed` 并计 1.3 绕行。已查到站序的线路会**写回沿途站点**（`addLineTo`，对已出队的站点重新入队），两线相交处因此天然成为换乘点，不必再靠线路表检索发现；站点线路表只用来发现「新线路」。站点线路表检索以**地铁站**为主（公交站若全查，每条公交线都会带出几十个待查站）；一次 PlaceSearch 最多回 50 条 POI，而地铁站 POI 里八成是「X号口/出入口」——420m 半径实测一次只覆盖 1 个站，故查询半径取 1200m（`TP_AREA_RADIUS`），覆盖标记按**实际返回的最远距离**算（`TP_AREA_COVER_MIN/PAD`）而不是查询半径，否则「1200m 已查」会让后续中心全被跳过、整片站点漏检。地铁线路名可按编号枚举：已加载线路里最大编号以内的空号各查一次 `LineSearch`（`TP_MAX_METRO_GAPS`，一次拿整条线站序，命中缓存不耗额度）——实测 60 分钟圈区域检索 40 次仍整条 15 号线不出现（双柏路因此根本不在节点表里），编号补查 1 次即可补上；公交只做少量远端补查：首遍收敛后按方位 8 扇区、每区取「模型估值 ≤ 目标 80%、离家 ≥1800m、只挂一条线」的最远公交站，以 1000m 半径补查一次（`TP_MAX_BUS_AREA`，60 分钟圈 8 次；命中线路名的站点标记 `areaTried`，避免重复查），补到的新公交线走独立名额 `lineBusRescue`、不受 `busCap` 约束——冷启动时首轮线路常已把 busCap 用光，没有这条豁免 108 路这类横向换乘线永远加载不了。预算 `TP_MAX_AREA`（28）/`TP_MAX_LINE_*` 随时长缩放（只计真实请求，`house_tool_transit_line_cache` 命中的线路不占线路检索额度）。诊断：`__tpDebug.loadedLines` 是本次已加载的线路名，`areaLeft` 为剩余检索预算（为 0 说明预算用尽），`blindCount/blindSample` 列出没查过线路表且只挂一条线的站点（换乘可能不全），`metroGaps.tried/loaded` 记录编号缺口补查的线路。缓存：线路站序 `house_tool_transit_line_cache`、站点线路表 `house_tool_transit_stop_cache`、逐线速度 `house_tool_transit_line_speed`、导航复核结果 `house_tool_transit_nav_cache`（键含工作日/周末桶、**不含圈类型**：地铁圈与综合圈共用 LEAST_TIME 策略，条目按方案构成存各类最快时间（`b[0]` 混合 / `b[1]` 轨道 / `b[2]` 公交）供跨圈复用，省掉「同一站两个圈各查一次」；公交圈的 NO_SUBWAY 响应只贡献纯公交样本，不能给综合圈当最快；`m`（navMid）只在同圈类型命中时回放（混合方案的下车时刻对纯地铁圈不可比），`w`（最后步行）与构成无关、跨圈继承；「该构成无方案」写 12 小时短负缓存 `nq`，半天内不再重花配额），结果几何进 `house_tool_circle_geom`（签名含算法版本 `TRANSIT_PRECISE_VER`、步行配速、逐线速度指纹 `tpSpeedSig` 与算法指纹 `tpAlgoSig()`）。**`tpAlgoSig()` 哈希建圈函数源码与关键预算常量，代码一变签名就变、旧几何自动失效**，不再依赖人工递增版本号——曾因只改复核名额没动版本号，旧几何一直命中、新逻辑从未执行，`__tpDebug` 也不存在；`__tpDebug.nav` 里的 `calls/anchors/learnedLines` 用来判断复核是否真的跑过，`pruned/cal/refineGroups/refineSkipped/farSkipped/biasSamples/midAnchors/lineScales` 用来核对剪枝、校准样本、近失分组、远端跳过、方案内锚点与逐线因子，`nosolHits/nosolWrites` 核对负缓存复用与新增，`walk` 记录导航步行速度（n/avg/p50/p90 与配置配速，只诊断、暂不参与 `tpWalkSec`；AMap 2.0 的步行段用 `transit_mode='WALK'` 标识、没有 `walking` 对象，实测中位约 3.8km/h、明显低于 `walk_speed=4.5`），`extraLines/extraChecked/calNames/calSkip/midNames/lineSeen` 核对导航补线、新线临界站补核与方案线路统计。**新建/重建圈才用当前签名（改了算法的新圈走新逻辑）；恢复存档（`applyState`）与导入方案包时以圈保存的 `geomSig` 查缓存，缓存里有就装配、不自动重算**，方案包导出的几何键同样用保存签名，两端对齐——否则代码指纹/速度一变就把已缓存的圈排队重算，白耗配额。地铁圈只采信「全程轨道」的方案（Transfer 不能限定只坐地铁，混入公交会偏快），公交圈用 `NO_SUBWAY` 策略。运行时把本次探测摘要挂在 `window.__tpDebug`（起点、预算、调用次数、查询失败数、可达站到达分钟）便于排查；失败也会留摘要（带 `error` 字段）。城市以起点附近站点 POI 的 `cityname` 为准（LineSearch 按城市查，用错城市会取到外城同名线路）。
- 提交信息用中文、带 `feat:`/`fix:` 等前缀，正文列要点。
- 配色约定：通勤时间分档绿/黄/橙/红（index.html：≤30/45/60 分钟，为等时圈设定时长；metro.html 全城视图：≤40/50/60 全市平均通勤，单站视图：≤15/30/45/60 从该站出发）。两页阈值含义不同，靠图例/文案标注口径而非统一数值。黄/橙档徽标用深色文字（对比度）；metro 单站等值圈为凸包外扩近似，该口径随单站图例/数据文档说明（页脚不放口径文案，仅保留密钥入口）；出发口径文本渲染在「全市平均通勤时间」图例下方，取自 meta.json 的 departure 字段。
- 反馈组件分层：瞬时结果用 `#toast`（顶部居中，metro.html 中 z-index 1100 高于密钥弹窗）；进行中的任务（检索/导入/批量）一律用 `#progBar` 进度浮条（含取消按钮），不要用长显 toast 当进度条。index.html 中三者互斥，状态统一入口是 `updateActionStates()`。
- 附近车站线路（index.html ②）：PlaceSearch 取公交站(150700)/地铁站(150500)，站点 `address` 即途经线路名列表，`AMap.LineSearch`（extensions all）按线路名取 `path` 折线与 `via_stops` 沿途站点（零额外请求）；步行时间与线路走向分别缓存在 `house_tool_station_walk_cache` / `house_tool_station_line_cache_v2`；单次最多 24 个候选站、50 条线路，新增逻辑别把上限放开到配额失控。线路高亮用独立白色 halo Polyline（`hoveredStationLine` / `selectedStationLine`），不要改主折线颜色；详情窗用 `stationInfoWindow`（`autoMove:false`）并在点击位置打开，避免挪动地图；悬停名称用 `#mapTip` 固定定位小浮层，不走 InfoWindow。
- 破坏性操作（删起点级联删圈、清空候选）必须先 `confirm`；删等时圈按产品要求不二次确认；「刷新选中圈」是显式强制重算（清缓存、耗配额）也要 `confirm`；前置条件不满足的按钮用 disabled + title 提示，toast 校验只作兜底。

## 数据管线（metro.html 的数据从哪来）

```
朋友仓库 output/*.csv（根目录：全市三个 CSV + stations_all.csv）
  → python3 tools/build_metro_data.py --city <id> --city-name <中文名> --input <output目录> --output data/<id>
  → data/<id>/{meta.json, stations.json, rows/<组id>.json, lines.json} + 更新 data/index.json
```

前端数据契约（metro.html 依赖，改动需同步两侧）：

- 站点按**站名分组**（同站不同线的节点合并），组 id 为 `g001…`；`stations.json` 含每组坐标（GCJ-02）、线路、avg_minutes、rank。
- `rows/<组id>.json` = `{"t": {目的组id: 分钟整数}}`，按需 fetch，不含自身。
- `lines.json`（可选，旧城市数据可能没有）= `{"lines": [{label, color, groups: [组id 按线路顺序]}]}`，由 `stations_all.csv` 行序生成，前端画 Polyline；标志色在脚本的 `CITY_LINE_COLORS` 维护，未收录线路用灰。前端必须容忍 404（按无线处理）。`stations_all.csv` 有两种 schema：上海 root 输出 `{line, station_id, station_name}`，厦门等分城市输出 `{line_order, line_label, station_id, station_slug, station_name, source_key}`，脚本按列名自动识别（关联仍按 `(line_order, 站名)`）。
- 节点唯一键是 `{line_order}:{station_id}`（station_id 跨线路会复用，不能单独用）；`stations_all.csv` 的 station_id 是另一套格式，关联只能按 `(line, 站名)`。

已知坑：

- 爬虫输出的 CSV **可能带 BOM**——读 CSV 一律 `utf-8-sig`。
- 未解析站点（高德 POI 缺失，如厦门 6 号线角美段）会被排除并记入 `meta.json.unresolved_nodes`，属正常现象。
- `tools/fixtures/shanghai/` 是 12 站演示数据，仅供脚本测试；`data/shanghai/` 已由朋友仓库真实输出重建，别再拿 fixtures 覆盖。

## 边界与禁区

- **朋友仓库**（通常 clone 在 `../shmetro-accessibility`）的改动不进本仓库；本仓库只提交转换后的 `data/`。若需改爬虫，去朋友仓库改并提 PR。
- `config.yaml` 含真实高德密钥，工作区里的密钥改动一般属于用户本人，**不要代提交**；也不要在公开部署中泄露。
- 朋友项目的 `output/` 原始 CSV/SQLite 不要复制进本仓库。

## 验证方式

改 `metro.html` / 数据后：

```bash
python3 -m http.server 8931 --bind 127.0.0.1
# 浏览器打开 http://127.0.0.1:8931/metro.html
```

验证点：城市下拉框出现新城市、排行榜有数据、单击排行行末「›」（或散点 InfoWindow 里「查看单站视图 ›」）进单站视图（等值圈、可达列表正常），此时 URL 出现 `#origin=<组id>`，浏览器前进/后退可切换视图；无密钥时弹窗可跳过（或按 Esc）、纯数据浏览正常（图例隐藏）。两页移动端（≤768px）面板为底部抽屉，grabber 支持拖拽吸附三档高度（轻点仍是点击循环），收起态显示一行摘要。index.html 地图单击不再直接建起点，需先点确认层「设为起点」。注意底图瓦片要求高德 key 的域名白名单包含当前 origin，否则报 `INVALID_USER_DOMAIN`（站点数据层不受影响，属 key 配置问题而非代码问题）。
