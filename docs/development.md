# 开发者指南

本工具是**纯静态前端**：无构建、无后端、无框架，除高德 JS API 与两个 CDN 库外零第三方依赖。克隆下来用任意静态服务器打开即可。

## 本地运行

```bash
python3 -m http.server 8931 --bind 127.0.0.1
# 打开 http://127.0.0.1:8931/index.html    主工具
# 打开 http://127.0.0.1:8931/metro.html    地铁可达性
```

改动后建议按 [验证清单](#测试与验证) 逐项自查（含移动端抽屉、无密钥降级、深链接等）。

## 目录结构

```
index.html                  主工具：起点、等时圈、圈内检索、批量算通勤、导出
metro.html                  地铁可达性可视化（本地预计算矩阵）
config.yaml                 运行时配置（密钥、初始视角、配速），浏览器可直接读取
data/                       地铁静态数据（每城市一个子目录 + index.json 城市清单）
  <city>/{meta.json, stations.json, rows/<组id>.json, lines.json}
tools/build_metro_data.py   CSV → 前端 JSON 的转换脚本
tools/fixtures/shanghai/    12 站演示数据，仅供脚本测试
assets/                     图片与看板
docs/                       本目录，开发者文档
PLAN.md                     地铁功能的设计决策与待办
AGENTS.md                   面向 LLM 协作的速查（代码约定、数据契约、已知坑）
```

## 高德密钥配置

调用高德 JS API 需要 `Key`（JS API）与 `安全密钥（securityJsCode）`。**本仓库 `config.yaml` 已默认预配置好一份可用密钥，克隆后开箱即用**，无需任何设置。

密钥**不在页面展示**：两个页面都没有常驻的密钥设置面板，也不回显已保存的密钥。入口只有两个——无密钥首访/密钥失效（如 `INVALID_USER_DOMAIN`）时自动弹出的引导弹窗，以及页脚不显眼的「重新配置密钥」链接（弹窗输入框不回显已有值）。

运行时的密钥解析优先级为：

1. `localStorage`（用户在弹窗里保存过的）
2. `config.yaml`（静态文件，页面加载时 fetch）
3. 弹窗引导输入（兜底，仅当上面都没有时）

> ⚠️ `config.yaml` 是静态文件，部署后浏览器可以直接下载；且 JS 调用高德时 key 必然出现在网络请求里，**纯静态站无法真正隐藏密钥**。真正的防盗手段是高德后台的「域名白名单」+ 配额限制。公开仓库**不要**提交真实密钥；公开部署建议通过 CI 注入或让用户自备。

### 自行申请密钥

1. 前往 [console.amap.com](https://console.amap.com) 注册登录。
2. 「应用管理」→「创建 Key」，服务平台选 **「Web端(JS API)」**。
3. 复制 Key；在 Key 的「安全设置」里获取 **安全密钥**。
4. 填入首访弹窗点「保存并开始使用」（存 `localStorage`），或写入 `config.yaml`。

`config.yaml` 还包含地图初始视角（GCJ-02，与高德一致）与近似圈配速：

```yaml
amap_key: "你的 JS API Key"
amap_code: "你的安全密钥"
mapbox_token: ""                 # 仅 Mapbox 圈用，公开 pk token 即可，可留空
walk_speed: 4.5                  # 步行近似圈配速 km/h（页面无配置入口）
ride_speed: 9                    # 骑行近似圈配速 km/h（页面无配置入口）
map_center: "121.4997,31.2397"   # 经度,纬度（默认上海陆家嘴）
map_zoom: 12
```

**Mapbox Token**（仅驾车/骑行/步行(Mapbox)圈用）只能写入 `config.yaml` 的 `mapbox_token`，页面无配置入口；旧版本存过 `localStorage.mapbox_token` 的仍会被读取。配速同理：`walk_speed` / `ride_speed` 是开发者配置（缺省/越界回落 4.5 / 9），方案包仍会携带速度以保证几何缓存命中。

`metro.html` 在无密钥时可跳过弹窗纯数据浏览（地图区显示占位层，排行榜/单站列表可用）。

## 技术栈与约束

- 单文件页面：HTML + 内联 CSS/JS；JS 用 `var` + `function` 风格；UI 文案为中文。
- 两个页面共享同一套密钥加载、`#toast`、`#progBar`、配色与深链接写法，改动时注意两侧一致。
- 例外依赖（仅 CDN script，无构建）：
  - 高德地图 JS API 2.0；
  - `polygon-clipping` —— 交集/差集几何裁剪；
  - `lz-string` —— 等时圈几何压缩缓存。

## 底层算法与机制

### index.html（实时计算）

等时圈按交通方式走不同分支：

| 方式 | 实现 | 成本 |
|---|---|---|
| 地铁 / 公交 / 地铁+公交 | 高德真实路网**到达圈**（ArrivalRange） | 1 次/圈，上限 60 分钟 |
| 步行 / 骑行（默认） | 按 config.yaml 配速画**直线近似圈**（圆环 72 点） | 0 次，即时 |
| 步行 / 骑行（勾选「精确路网圈」） | 真实路径**逐方向二分**边界 | ≈200 次/圈，约 30~60 秒 |
| 驾车 / 骑行 / 步行(Mapbox) | Mapbox 等时圈接口 | 需 Mapbox Token，上限 60 分钟 |

- **精确路网圈**（`buildPreciseRing`）：以起点为中心取 `24` 条方向射线，每条射线在 `[0, maxR]` 内做 `8` 步二分，逼近「耗时 ≈ 目标时长」的边界点；`4` 路并发，扫描上限按 1.8 倍绕行系数估计。凑够 ≥6 个边界点后闭合平滑。
- **交集/差集**：勾选若干圈后用 `polygon-clipping` 对多个多边形做布尔裁剪。交集以灰色叠加，差集保留各圈颜色，两个开关可叠加；候选列表与批量通勤跟随显示区域（`regionsActive`）。
- **几何缓存**（`lz-string`）：每圈几何按签名 `circleSig(起点, 方式, 时长, 是否精确)` 压缩写入 `localStorage`（键 `house_tool_circle_geom`），按签名做 LRU/TTL，上限 80 条、30 天。命中则零 API 消耗秒装配；配额超限只 toast 提示，不影响使用。
- **通勤计算**：结果缓存相同起点+候选不重复请求；高德接口统一经 `amapRequest` 令牌桶限流，遇限流自动退避重试，配额耗尽明确报错（不会误判为「无公交方案」）。
- **附近车站线路**（`searchNearbyStations`）：PlaceSearch 找公交站(150700)/地铁站(150500)，`AMap.Walking` 逐站算真实步行时间并按设定时长过滤；站点 POI 的 `address` 给出途经线路名（公交「167路;18路」、地铁「1号线;2号线」），再用 `AMap.LineSearch`（extensions all）按名取 `path` 折线与 `via_stops`（含每站名称/坐标，沿途站点零额外请求）。单次最多 24 个候选站（先按直线距离粗筛）、50 条线路（按步行时间由近到远），步行时间与线路走向分别缓存于 `house_tool_station_walk_cache` / `house_tool_station_line_cache_v2`。线路高亮（悬停/点击）通过独立的白色 halo Polyline 实现，沿途站点用去重后的 CircleMarker 绘制；悬停名称用 `#mapTip` 固定定位浮层（不触发 InfoWindow），线路详情用 `stationInfoWindow`（`autoMove:false`）在点击位置打开，避免视野偏移。
- **面积**：环面积按参考纬度做球面近似换算（`ringAreaM2`）。
- **方案包/分享链接**：把起点与圈几何序列化为纯文本（lz-string 压缩），可跨设备导入导出；导入为追加语义，重复项自动跳过。

### metro.html（本地预计算）

数据是离线算好的地铁 N×N 通勤时间矩阵，前端纯静态加载，零 API 消耗、秒开：

- **站点按站名分组**（同站不同线的节点合并），组 id 形如 `g001`。
- **全城视图**：所有站点按「到其他各站的平均通勤时间」上色，附排行榜。
- **单站视图**：点击任一站点或排行行末「›」，用真实站间时间给全城上色，并画 15/30/45/60 分钟等值圈。
- **等值圈为凸包外扩近似**（`convexHull` + `expandHull`）：对同档站点取凸包后按质心外扩作为缓冲，非真实到达面（页脚保留口径声明）。
- **按需分片加载**：`rows/<组id>.json` 只在进入单站视图时 fetch，首屏不加载矩阵。
- **线路折线**：`lines.json`（可选，旧城市数据可能没有）提供每条线的标志色与有序组 id，前端画 Polyline；404 时按无线处理。

## 地铁数据管线

`data/` 由 [shmetro-accessibility](https://github.com/Hecate2/shmetro-accessibility) 的爬虫输出 CSV 转换而来。完整的同步步骤、数据契约与已知坑见 **[metro-data.md](metro-data.md)**。

## 测试与验证

```bash
python3 -m http.server 8931 --bind 127.0.0.1
```

需人工核对的关键点：

- 城市下拉框出现新城市、排行榜有数据、点排行行末「›」（或散点 InfoWindow 的「查看单站视图 ›」）进单站视图，等值圈与可达列表正常。
- 进单站视图后 URL 出现 `#origin=<组id>`，浏览器前进/后退可切换视图。
- 无密钥弹窗可跳过（或按 Esc），纯数据浏览正常、图例隐藏。
- 两页移动端（≤768px）面板为底部抽屉，grabber 拖拽吸附三档高度（轻点仍是点击循环），收起态显示一行摘要。
- 主工具地图单击不再直接建起点，需先点确认层「设为起点」。
- 「② 附近车站线路」：选起点与步行时长后查询，车站点、线路与沿途站点应出现；公交/地铁/途经站复选框可分别显隐；悬停或点击线路出现白色描边高亮，点击站点可看经过线路；同参数重复查询应明显更快（命中本地缓存）。
- 底图瓦片报 `INVALID_USER_DOMAIN` 属 key 域名白名单未含当前 origin（数据层不受影响，是 key 配置问题而非代码问题）。

## 提交约定

- 提交信息用中文、带 `feat:`/`fix:` 等前缀，正文列要点。
- 不提交真实密钥；`data/` 只提交转换后的结果，不复制上游 `output/` 原始 CSV/SQLite。
