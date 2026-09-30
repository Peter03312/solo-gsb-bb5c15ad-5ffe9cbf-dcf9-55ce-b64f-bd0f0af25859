# 海洋保护区穿越判定服务（MPA Crossing API）

为海洋测绘团队提供的 HTTP 服务：提交一批**保护区**（编号、UTC 生效区间、GeoJSON
`Polygon` / `MultiPolygon`）与一条**按时间排列的测线**，返回测线在每个保护区内的
进入时刻、离开时刻与累计停留秒数。

海上巡查场景另提供 `POST /api/v1/patrol`：提交**多条带唯一船舶编号的测线**与阈值
`N`，按保护区返回**同时停留船舶数不少于 N 的最大连续时段**、每个时段的**峰值船数**
与**累计并发秒数**。沿用同一套短弧插值、边界归属与保护区生效区间规则。

## 判定规则

- **相邻测点间的运动模型**：时间匀速、纬度线性、经度沿较短方向
  （经度增量规整到 `(-180, 180]`）线性插值，因此正确处理穿越 180° 经线（日界线）。
- **边界计入**：外环边界属于保护区；内环（洞）的边界也属于保护区；
  洞的内部严格区域不属于保护区。
- **左闭右开**：穿越结果与保护区生效区间均按 `[enter, leave)` 裁切
  （`valid_from` 含、`valid_to` 不含）。
- **片段合并**：裁切后端点相接（`[a,b)` 与 `[b,c)`）或相交的有效片段合并为一次停留。
- **单点触碰不计时长**：测线仅在某一时刻接触保护区（如擦过顶点）时，
  `total_duration_seconds` 为 0，且不产生停留条目。
- **整体拒绝**：经纬度越界、时间不严格递增、或几何无效时，整次计算返回
  `400 Bad Request` 并给出中文原因，不产生任何部分结果。巡查接口还会因
  船舶编号重复、`threshold` 非正整数而整体拒绝。
- **几何校验内容**：坐标必须为有限数字且在范围内；每个环至少 4 点且首尾闭合；
  环不得自交、不得有相邻重复点；洞必须完全位于外环内；洞之间不得横截交叉、
  重叠或包含（顶点相切允许）；MultiPolygon 各部分的内部不得相交
  （顶点相切、以及 RFC 7946 在 ±180° 接缝处切开的共边表示允许）。
- 时间均以 **UTC** 输出（`...Z`）；输入支持 `Z`、`+08:00` 等偏移，
  无时区的朴素时间按 UTC 处理。

## 目录结构

```
app/
  main.py        FastAPI 路由与错误处理（HTTP 层；/analyze 与 /patrol）
  schemas.py     请求模型（Pydantic 字段校验）
  analysis.py    编排：插值、逐段求交、合并、生效区间裁切、多船并发扫描
  geopoly.py     几何核心：精确线段/环求交、洞规则、日界线处理、几何校验
tests/           pytest 测试（80 个用例）
examples/request.json         /analyze 请求示例
examples/patrol_request.json  /patrol 请求示例
Dockerfile
docker-compose.yml
docker/entrypoint.sh
```

## 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
```

服务监听：

- 地址：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/health`
- 接口文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI：`http://localhost:8000/openapi.json`

停止：`Ctrl+C`，或 `docker compose down`。

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8000` | 宿主机映射端口（容器内固定 8000） |
| `UVICORN_WORKERS` | `1` | uvicorn worker 进程数 |
| `LOG_LEVEL` | `info` | 日志级别 |
| `RUN_TESTS_ON_START` | `0` | 设为 `1` 时容器启动前先跑 pytest 自检 |

示例：

```bash
APP_PORT=9000 UVICORN_WORKERS=2 docker compose up --build
RUN_TESTS_ON_START=1 docker compose up --build
```

## 直接运行（不用 Docker）

需要 Python 3.11+。

```bash
pip install -r requirements.txt        # fastapi / uvicorn / pydantic
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt    # 额外安装 pytest、httpx
pytest
```

## 接口

### `POST /api/v1/analyze`

请求体（JSON 对象）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `areas` | 数组（≥1） | 保护区列表 |
| `areas[].id` | 字符串或数字 | 保护区编号，原样返回 |
| `areas[].valid_from` | ISO 8601 字符串 | UTC 生效起点（含） |
| `areas[].valid_to` | ISO 8601 字符串，可空 | UTC 生效终点（不含）；省略/`null` 表示无限 |
| `areas[].geometry` | GeoJSON 对象 | `Polygon` 或 `MultiPolygon`（也接受 `Feature` 包装） |
| `track` | 数组（≥2） | 按时间严格递增排列的测线点 |
| `track[].time` | ISO 8601 字符串 | 该点时刻 |
| `track[].lon` | 数字 | 经度，`[-180, 180]` |
| `track[].lat` | 数字 | 纬度，`[-90, 90]` |

额外字段一律拒绝（HTTP 400）。

响应 `200`：

```json
{
  "results": [
    {
      "id": "MPA-001",
      "inside": true,
      "total_duration_seconds": 5200.0,
      "entries": [
        {
          "enter": "2026-06-15T01:00:00Z",
          "leave": "2026-06-15T02:00:00Z",
          "duration_seconds": 3600.0
        }
      ]
    }
  ]
}
```

- `inside`：是否存在任何有效停留；仅单点触碰时为 `false`。
- `entries` 按时间升序，片段已合并；`duration_seconds = leave - enter`（秒）。
- `total_duration_seconds` 为所有片段时长之和。
- 结果数组与请求 `areas` 顺序一致、每个保护区都有一条结果。

错误响应 `400`：

```json
{"error": "track[1].time 必须严格晚于 track[0].time（时间必须严格递增）"}
```

字段级错误会带 `details`，例如：

```json
{
  "error": "请求参数无效",
  "details": [
    {"field": "track.1.lon", "type": "value_error",
     "message": "经度越界: 200.0（允许范围 [-180, 180]）"}
  ]
}
```

### 请求示例

curl：

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

Python：

```python
import requests

payload = {
    "areas": [{
        "id": "A1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [121.0, 30.0], [123.0, 30.0],
                [123.0, 32.0], [121.0, 32.0], [121.0, 30.0]
            ]],
        },
    }],
    "track": [
        {"time": "2026-06-15T00:00:00Z", "lon": 120.0, "lat": 31.0},
        {"time": "2026-06-15T03:00:00Z", "lon": 124.0, "lat": 31.0},
    ],
}
r = requests.post("http://localhost:8000/api/v1/analyze", json=payload, timeout=30)
r.raise_for_status()
print(r.json())
```

### `POST /api/v1/patrol`（多船舶并发巡查）

在单测线规则之上，先分别计算每条船舶测线在每个保护区内、且处于保护区生效区间内的
有效停留（左闭右开，单点触碰不计），再按时间统计同一保护区内的**不同船舶数**。

请求体（JSON 对象）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `areas` | 数组（≥1） | 保护区列表，结构同 `/analyze` |
| `vessels` | 数组（≥1） | 船舶测线列表，每条测线至少 2 点 |
| `vessels[].vessel_id` | 字符串或数字 | **唯一**船舶编号，重复整体报错 |
| `vessels[].track` | 数组（≥2） | 该船舶按时间严格递增的测线点（字段同 `track`） |
| `threshold` | 正整数 | 阈值 `N`；只接受数学意义上的正整数（`1.5`、`2.0`、`true` 均拒绝） |

不同船舶的测线时间范围可以不一致；全部时间统一到 UTC 时间轴统计。

并发时段规则：

- 只保留不同船舶数 `>= N` 的时间段；结果按 `areas` 输入顺序返回。
- 船数在某时刻变化（如 2→3→2）但始终达标时**不断开**，`peak_vessel_count`
  取该时段内的最大船数。
- 端点相接按左闭右开处理：一艘在 `t` 离开、另一艘在 `t` 进入时，
  `t` 时刻前后的达标状态连续，不断开。
- 单点触碰不产生停留，因此不产生并发时段。
- `N` 大于船舶总数时不报错，每个保护区返回空 `periods`。
- 任一测线/保护区无效、船舶编号重复或 `threshold` 非正整数时整体 `400`。

响应 `200`：

```json
{
  "results": [
    {
      "id": "MPA-001",
      "threshold": 2,
      "vessel_count": 3,
      "periods": [
        {
          "start": "2026-06-15T01:30:00Z",
          "end": "2026-06-15T03:00:00Z",
          "peak_vessel_count": 3,
          "duration_seconds": 5400.0
        }
      ],
      "total_concurrent_seconds": 5400.0
    },
    {
      "id": "MPA-DATELINE",
      "threshold": 2,
      "vessel_count": 3,
      "periods": [],
      "total_concurrent_seconds": 0.0
    }
  ]
}
```

- `periods` 按时间升序，是船数达标的**最大连续时段**（船数变化不断开）。
- `duration_seconds = end - start`；`total_concurrent_seconds`
  为该保护区所有达标时段时长之和。
- 每个保护区都有一条结果（包括空时段）；结果数组与请求 `areas` 顺序一致。

错误响应 `400` 形如：

```json
{"error": "vessels[1].vessel_id 重复: 'X'"}
```

```json
{"error": "vessels[0].track[1].time 必须严格晚于 vessels[0].track[0].time（时间必须严格递增）"}
```

curl：

```bash
curl -X POST http://localhost:8000/api/v1/patrol \
  -H 'Content-Type: application/json' \
  --data @examples/patrol_request.json
```

Python：

```python
import requests

payload = {
    "areas": [{
        "id": "A1",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [121.0, 30.0], [123.0, 30.0],
                [123.0, 32.0], [121.0, 32.0], [121.0, 30.0]
            ]],
        },
    }],
    "threshold": 2,
    "vessels": [
        {"vessel_id": "HULL-01", "track": [
            {"time": "2026-06-15T00:00:00Z", "lon": 120.0, "lat": 31.0},
            {"time": "2026-06-15T04:00:00Z", "lon": 124.0, "lat": 31.0},
        ]},
        {"vessel_id": "HULL-02", "track": [
            {"time": "2026-06-15T01:00:00Z", "lon": 120.0, "lat": 31.0},
            {"time": "2026-06-15T03:00:00Z", "lon": 124.0, "lat": 31.0},
        ]},
    ],
}
r = requests.post("http://localhost:8000/api/v1/patrol", json=payload, timeout=30)
r.raise_for_status()
print(r.json())
```

## 算法说明

1. 每个多边形环沿顶点做**经度一致展开**（逐点取短弧增量），
   再整体平移 `±360°` 得到三个坐标框；测线线段同样按短弧展开。
   于是日界线两侧的求交等价于普通平面线段求交，无需球面投影。
2. 对每条测线线段与多边形边做精确求交（含共线重叠），交点参数把 `[0,1]`
   切成若干开区间；用区间中点做绕数（winding number）内外判定。
3. 点的保护区归属 = 在外环内或外环边界上，且不在任一洞的内部严格区域
   （落在洞边界上仍计入）。
4. 线段内的闭参数区间映射到绝对时间，丢弃零长（单点触碰），转左闭右开，
   跨线段合并，再与 `[valid_from, valid_to)` 裁切并再次合并。
5. 巡查接口对每艘船各自执行上述步骤，把所有船舶的有效停留映射到同一 UTC
   时间轴后做扫描线统计：起止事件按时间排序（同时刻先合再算），切出
   船数恒定的片段，保留船数 `>= N` 的片段，再把首尾相接的达标片段合并为
   最大连续时段（峰值船数取片段内最大值）。`N` 大于船舶总数时扫描结果必空。
