# 博物馆借展运输环境风险核验系统

面向博物馆**文物借展运输**全过程的中文 Web 应用：以运输批次为单位，管理文物、有序运输节点、
每件文物的**温度 / 湿度上下限**与**约定采样间隔**，采集绑定「文物 + 节点」的**带时间戳读数**，
并按时间自动核验越限风险、标出缺测区间、闭环处置风险事件；补录 / 更正读数生成**新的评估版本**，
旧版读数与关闭结论永久可查。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · SQLite（可换 PostgreSQL 等）· Jinja2 服务端页面
- 交付：`Dockerfile` + `docker-compose.yml` 一键启动，内置 HTTP API（自带 Swagger 交互文档）与中文页面

---

## 一、启动

### 方式 1：Docker Compose 一键启动（推荐）

```bash
docker compose up -d --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| Web 页面（批次风险摘要 / 详情 / 版本 / 差异 / 处置） | http://localhost:8000/ |
| HTTP API 交互文档（Swagger UI） | http://localhost:8000/api/docs |
| ReDoc API 文档 | http://localhost:8000/api/redoc |
| OpenAPI 描述 | http://localhost:8000/api/openapi.json |
| 健康检查 | http://localhost:8000/healthz |

数据持久化在 Docker 卷 `museum_data`（容器内 `/data/museum_transport.db`），容器重建数据不丢。

常用命令：

```bash
docker compose logs -f          # 查看日志
docker compose down             # 停止（保留数据卷）
docker compose down -v          # 停止并删除数据（清空重来）
```

### 方式 2：Docker 直接运行

```bash
docker build -t museum-transport-risk:1.0 .
docker run -d --name museum-transport-risk \
  -p 8000:8000 \
  -v museum_data:/data \
  museum-transport-risk:1.0
```

### 方式 3：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动建表；默认写入一个演示批次 **B-2026-001「青铜鼎赴伦敦借展运输」**
（含 3 个版本：初始录入 → 补录 → 更正，演示跨节点合并、缺测中断、旧版关闭留痕、新版不继承关闭）。

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:////data/museum_transport.db` | SQLAlchemy 连接串；如 PostgreSQL：`postgresql+psycopg://user:pass@host:5432/museum`（需另装驱动） |
| `DATA_DIR` | `/data` | SQLite 文件与数据目录（容器内），本地运行可改为 `./data` |
| `SEED_DEMO` | `true` | 是否在首次启动写入演示批次；置 `false` 得到空系统 |
| `APP_HOST` | `0.0.0.0` | 监听地址 |
| `APP_PORT` | `8000` | 监听端口（容器内固定暴露 8000；修改 compose 端口映射即可换宿主机端口） |

修改宿主机端口示例（改为 18000）：

```yaml
ports:
  - "18000:8000"
```

---

## 三、业务规则

1. **持久化对象**：批次、有序运输节点（`seq` 唯一）、文物（温/湿度上下限、约定采样间隔秒数）、
   评估版本、读数（文物 + 节点 + 时间戳 + 指标 + 数值）、风险事件、缺测区间。
2. **按时间核验**：同一文物、同一指标的读数按时间排序；
   **连续越限读数合并为同一个风险事件**，即使相邻读数位于不同运输节点（事件标记「跨节点」，
   记录起止节点、峰值与超出量、越限读数条数）。
3. **缺测中断**：相邻两条读数的时间间隔 **大于约定采样间隔** 时，生成一条「缺测区间」
   （起止时间、实际间隔、约定间隔、是否跨节点）；若当时存在进行中的越限事件，
   事件在缺口前一条读数处**中断**（标记「缺测中断」），缺口之后再次越限另起新事件。
   读数回到正常区间则事件自然结束。
4. **事件关闭闭环**：事件初始为「待处置」；工作人员必须同时填写**处置说明**与**证据编号**
   才能关闭；关闭后记录处置人与关闭时间，且不可重复关闭。
5. **评估版本留痕**：首次提交读数生成 V1（初始录入）；之后每次提交：
   - 出现新时间点 → 新版本记为**补录**（supplement）；
   - 已存在时间点给出新值/新节点 → 新版本记为**更正**（correction），新读数引用被替换的旧读数。

   旧版本**只读归档**，旧版读数与关闭结论保持可查；新版事件全部重新计算，
   **不会自动继承旧版的关闭状态**，必须重新处置；版本差异页/API 展示读数的新增、移除、更正及事件对比。
6. **拒绝保存并说明原因**（返回 400 与中文原因，整批不写入）：
   - 同一文物同一指标同一时间戳重复（包括本批提交内部与已存在时间戳的完全重复）；
   - 温度或湿度下限 ≥ 上限（无序上下限）；
   - 读数 `value` 非数值（字符串、布尔、NaN、无穷大等）；
   - 时间戳非法、指标非法、节点/文物不属于本批次、采样间隔非正整数等。
7. **批次风险摘要**：当前版本读数总数、待处置/已关闭事件数、跨节点事件数、缺测中断数、
   缺测区间数，并按文物汇总，页面与 API 均可查看。

---

## 四、HTTP API 与调用示例

所有接口前缀 `/api`，请求 / 响应均为 JSON（`Content-Type: application/json`）。
时间戳使用 ISO 8601，建议带时区：`2026-10-01T08:00:00Z` 或 `2026-10-01T16:00:00+08:00`。
指标 `metric` 仅允许 `temperature`（温度℃）/ `humidity`（湿度%RH）。
错误响应统一为：`{"error": "中文原因"}`，HTTP 400/404。

> 以下示例假设服务运行在 `http://localhost:8000`，用 `curl` 调用。

### 1. 创建批次

```bash
curl -X POST http://localhost:8000/api/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_no": "B-2026-002",
    "name": "玉器赴东京借展运输",
    "borrower": "东京国立博物馆",
    "description": "含公路与空运两段"
  }'
```

### 2. 登记文物（温湿度上下限 + 采样间隔）

```bash
curl -X POST http://localhost:8000/api/batches/1/artifacts \
  -H 'Content-Type: application/json' \
  -d '{
    "code": "YQ-007",
    "name": "汉代玉璧",
    "temp_low": 18.0, "temp_high": 22.0,
    "humidity_low": 45.0, "humidity_high": 55.0,
    "sampling_interval_seconds": 3600
  }'
# 下限 >= 上限时返回 400：
# {"error":"温度上下限非法：下限 22.0 必须严格小于上限 18.0"}
```

### 3. 新增有序运输节点

```bash
curl -X POST http://localhost:8000/api/batches/1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "name": "出库装车", "location": "北京"}'
curl -X POST http://localhost:8000/api/batches/1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"seq": 2, "name": "空运抵东京", "location": "东京"}'
```

### 4. 提交读数（首次=初始版本，之后=补录/更正新版本并重新核验）

```bash
curl -X POST http://localhost:8000/api/batches/1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "created_by": "押运员王磊",
    "remark": "初始运输读数",
    "readings": [
      {"artifact_id": 1, "node_id": 1, "ts": "2026-10-01T00:00:00Z", "metric": "temperature", "value": 20.5},
      {"artifact_id": 1, "node_id": 1, "ts": "2026-10-01T01:00:00Z", "metric": "temperature", "value": 22.6},
      {"artifact_id": 1, "node_id": 2, "ts": "2026-10-01T02:00:00Z", "metric": "temperature", "value": 23.1},
      {"artifact_id": 1, "node_id": 2, "ts": "2026-10-01T03:00:00Z", "metric": "temperature", "value": 20.9},
      {"artifact_id": 1, "node_id": 1, "ts": "2026-10-01T05:00:00Z", "metric": "humidity",    "value": 56.2},
      {"artifact_id": 1, "node_id": 2, "ts": "2026-10-01T08:00:00Z", "metric": "humidity",    "value": 56.5}
    ]
  }'
```

响应给出新版本号、检出的事件（上面温度 01–02 时跨节点合并为 1 个事件）与缺测区间
（湿度 05→08 间隔 3 小时 > 1 小时）。

**补录**（新时间点）与**更正**（同时间点给新值，自动识别为 correction）：

```bash
# 补录一个新时间点
curl -X POST http://localhost:8000/api/batches/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"created_by":"李静","readings":[
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T09:00:00Z","metric":"humidity","value":55.8}]}'

# 更正既有时间点（V1 中 01:00 的温度应为 21.9）
curl -X POST http://localhost:8000/api/batches/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"created_by":"李静","remark":"依据校准证书更正","readings":[
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T01:00:00Z","metric":"temperature","value":21.9}]}'
```

非法输入示例（均返回 400，数据不写入）：

```bash
# 非数值
curl -X POST http://localhost:8000/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T00:00:00Z","metric":"temperature","value":"22度"}]}'
# -> {"error":"第 1 条读数 的读数 value 必须是 JSON 数值（数字），收到的是 str：'22度'"}

# 同文物同指标重复时间戳
curl -X POST http://localhost:8000/api/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"readings":[
    {"artifact_id":1,"node_id":1,"ts":"2026-10-02T00:00:00Z","metric":"temperature","value":20},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-02T00:00:00Z","metric":"temperature","value":21}]}'
# -> {"error":"重复时间戳：文物 1 的温度(℃) 在 2026-10-02T00:00:00Z 出现了多条读数……"}
```

### 5. 关闭风险事件（处置说明 + 证据编号，缺一不可）

```bash
# 事件 id 来自读数提交响应、版本详情或批次摘要
curl -X POST http://localhost:8000/api/events/4/close \
  -H 'Content-Type: application/json' \
  -d '{
    "disposition": "途中恒温箱空调短时故障，已重启并加隔热层，复查文物无损。",
    "evidence_no": "EV-20261001-07",
    "closed_by": "文保员李静"
  }'
# 缺少证据编号 -> 400 {"error":"证据编号不能为空……"}
# 已关闭再关闭 -> 400 {"error":"该事件已关闭……"}
```

### 6. 版本、差异、摘要查询

```bash
# 批次列表（每个批次带当前版本风险摘要）
curl -s http://localhost:8000/api/batches | python3 -m json.tool

# 批次详情：文物 / 节点 / 版本 / 风险摘要
curl -s http://localhost:8000/api/batches/1 | python3 -m json.tool

# 全部版本
curl -s http://localhost:8000/api/batches/1/versions

# 某一版本完整数据（读数、事件、缺测）
curl -s http://localhost:8000/api/versions/3 | python3 -m json.tool

# 版本间差异：默认与上一版对比，也可用 base_no 指定
curl -s "http://localhost:8000/api/batches/1/versions/3/diff?base_no=2" | python3 -m json.tool
# 包含 readings_added / readings_removed / readings_changed(旧值→新值)
# 以及 events_added / events_removed / events_common（并注明关闭状态不继承）
```

### 接口速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/batches` | 批次列表（含风险摘要） |
| POST | `/api/batches` | 创建批次 |
| GET | `/api/batches/{id}` | 批次详情（文物/节点/版本/摘要） |
| POST | `/api/batches/{id}/artifacts` | 登记文物 |
| GET | `/api/batches/{id}/artifacts` | 文物清单 |
| POST | `/api/batches/{id}/nodes` | 新增运输节点 |
| GET | `/api/batches/{id}/nodes` | 节点清单 |
| POST | `/api/batches/{id}/readings` | 提交读数，生成评估版本并核验 |
| GET | `/api/batches/{id}/versions` | 版本列表 |
| GET | `/api/versions/{id}` | 版本完整数据 |
| GET | `/api/versions/{id}/readings` | 版本读数明细 |
| GET | `/api/batches/{id}/versions/{n}/diff?base_no=` | 版本间差异 |
| GET | `/api/events/{id}` | 事件详情（含构成读数） |
| POST | `/api/events/{id}/close` | 关闭事件 |
| GET | `/healthz` | 健康检查 |

页面操作路径：首页 `/` → 进入批次（登记文物/节点、粘贴读数 JSON 提交、查看摘要）→
版本页 `/versions/{id}`（查看事件、缺测、读数，对待处置事件填写处置说明与证据编号后关闭）→
差异页 `/batches/{id}/diff`（选择任意两个版本对比）。

---

## 五、目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口、异常处理、启动建表/演示数据
│   ├── config.py        # 环境变量配置
│   ├── database.py      # 引擎/会话/建表（SQLite 外键开启）
│   ├── models.py        # ORM 模型（含时区安全的 TZDateTime）
│   ├── services.py      # 核心领域逻辑：校验、版本、按时间核验、缺测中断、关闭、差异、摘要
│   ├── serializers.py   # JSON 序列化
│   ├── api.py           # /api HTTP 路由
│   ├── web.py           # 中文页面路由
│   ├── seed.py          # 演示批次（B-2026-001，V1→V2→V3）
│   ├── templates/       # Jinja2 页面
│   └── static/style.css
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 六、核验算法说明

对每个版本、按 `(文物, 指标)` 分组后按时间升序遍历：

1. 计算与上一条读数的时间差；差值 **严格大于** 该文物约定采样间隔时记录缺测区间；
   若存在进行中的越限事件，则把事件结束点截到缺口前一条读数并标记 `interrupted`。
2. 当前读数越上限/低于下限时：若无进行中事件则新开事件（跨节点自然延续），
   否则追加到当前事件并更新峰值、结束点与跨节点标记；读数回到范围内则结束事件。
3. 每个版本独立重算全部事件，事件编号在 `(版本, 文物, 指标)` 内从 1 开始；
   关闭状态仅属于该版本的该事件，不做任何跨版本继承。
