# 多节点 Worker 与多链路 Route（M4）

> 适用版本：`researchdatahub` 0.1.0 · 模块：`researchdatahub.network`
> 本文档描述 **Worker 注册/健康、Local Worker、Remote Worker HTTP 协议（服务端 + 客户端）、
> Route 选路、实测链路性能与故障转移**的完整规格。

## 0. 一句话结论

- 节点健康来自**真实心跳/探活**（不是配置文件）；
- 链路性能来自**真实传输样本**（`~/.rdh/workers.db` 的 `route_stats`），没有样本时选路结果
  明确标注 `basis: "no_measurement"`，**不按国家/地区猜速度**；
- 远程节点是一个**可独立部署的 HTTP 服务进程**（`python -m researchdatahub.network.workers.agent`），
  主进程用真实 HTTP 下发、轮询、回传并用 SHA-256 复核；
- 首选链路失败/节点失联 → 记录样本 → 选下一条；全部不可用 → 结构化错误
  `NoRouteAvailable` / `FailoverExhausted`。

## 1. 现有模块如何被复用（而不是被重写）

| 能力 | 复用来源 | 本模块新增的部分 |
| --- | --- | --- |
| 下载 + 断点续传 + 重试 + 原子落盘 | `transfer.HttpTransportAdapter` / `TransportAdapterBase`（`transfer_outcome`） | 本地 worker 适配器、远程 worker 的 agent 侧同样调用它 |
| 12 态状态机 / 租约 | `core.state`（主进程仍由 `scheduler.TaskExecutor` 推进） | — **完全不改** |
| SQLite 连接与事务风格 | `manifest.connection` 的 WAL / `BEGIN IMMEDIATE` / busy 退避风格 | 独立库 `workers.db`（`network/workers/store.py`），不改 `manifest/schema.py` |
| 校验 | `storage.naming.sha256_file` | 回传后再算一次，与声明值比对 |

`network` **不修改** `scheduler/`、`transfer/`、`manifest/`、`core/`、`planner/`、`cli/`。

## 2. 数据模型（`network/workers/models.py`）

```text
WorkerRecord{
  worker_id, kind(local|remote|regional|cloud), region: str|None,
  capabilities{datasets[], providers[], protocols[], max_bytes, max_concurrent},
  health{status(healthy|degraded|offline), last_heartbeat, uptime_s, consecutive_failures},
  version, endpoint, registered_at, updated_at
}
```

不变式（由 pydantic 模型强制）：

- `kind=local` ⇒ 必须没有 `endpoint`；其它 kind ⇒ 必须有 `endpoint`；
- `offline` 节点不得上报非零 `uptime_s`；
- 能力白名单**空列表 = 不限制**；要表达"不支持"必须显式列出白名单。

## 3. Worker 注册表（`network/workers/registry.py` + `store.py`）

SQLite 文件默认 `~/.rdh/workers.db`（独立于 `manifest.db`），表：

| 表 | 用途 |
| --- | --- |
| `workers` | 节点身份 / 能力 / 最近心跳 / 连续失败次数 |
| `worker_events` | 追加式审计：`registered` / `heartbeat` / `marked_offline` / `deregistered` / `transferred` / `transport_failed` / `route_sample` |
| `route_stats` | 按 `(route_id, source_id)` 聚合的实测样本 |
| `worker_schema_version` | 自带迁移账本（当前 v1） |

**健康判定**（读路径按当前时间重算）：

| 距上次心跳 | 状态 |
| --- | --- |
| ≤ `heartbeat_timeout_s`（默认 30s） | 库中记录的状态（默认 `healthy`） |
| > 30s 且 ≤ `offline_after_s`（默认 120s） | `degraded`（仍可用，排序靠后） |
| > 120s | `offline`（写回库 + `marked_offline` 事件） |
| 连续失败 ≥ 3 次 | `degraded`（成功一次或心跳即清零） |

**重启恢复**：`workers` 是持久事实；进程重启后 `list_workers()` 直接读库并按当前时间重算，
`offline` 节点在下一次心跳后恢复 `healthy`。

## 4. Local Worker（`network/workers/local.py`）

- 只做"把资产搬到本机路径 + 报告字节数/SHA-256"，**不推进任务状态机**；
- 后台线程执行、`prepare()` 立即返回句柄，`poll()`/`fetch()`/`cancel()` 语义与远程 worker 一致；
- 并发受 `max_concurrent` 限制，超出返回 `WorkerTaskError(retryable=True, code="worker_busy")`；
- 路径只允许 `storage_root` 下的相对路径（`..`/绝对路径拒绝）；
- 产物按 `sha256_file` 重新计算并与资产声明比对（大小 + 摘要）。

## 5. Remote Worker HTTP 协议（v1.0）

### 5.1 部署

```bash
export RDH_WORKER_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
python -m researchdatahub.network.workers.agent \
    --host 127.0.0.1 --port 8765 \
    --worker-id remote-a \
    --storage-root /srv/rdh-worker \
    --max-concurrent 2 \
    --max-bytes 0 \
    --task-lease-seconds 300
# stdout: [agent remote-a] listening on http://127.0.0.1:8765 (storage=/srv/rdh-worker)
```

- 令牌只从 `Authorization: Bearer <token>` 读取（不接受查询串）；`hmac.compare_digest` 比较；
- **未配置令牌时只允许绑定回环地址**，否则启动即失败；
- 生产部署请在反向代理做 TLS 终结，并且只监听回环/内网。

### 5.2 端点

| 方法 | 路径 | 请求体 | 响应 |
| --- | --- | --- | --- |
| GET | `/v1/health` | — | `AgentHealth{ok, agent_version, protocol_version, worker_id, uptime_s, capabilities, storage_root, free_bytes, active_tasks, known_tasks, server_time}` |
| POST | `/v1/register` | `RegisterRequest{protocol_version, worker_id, capabilities, caller}` | `RegisterResponse{accepted, worker_id, reason, agent_version, effective_capabilities, registered_at}`（拒绝时 403 + `accepted=false`） |
| POST | `/v1/heartbeat` | `HeartbeatRequest{protocol_version, worker_id}` | `HeartbeatResponse{worker_id, ok, uptime_s, active_tasks, known_tasks, queue_depth, server_time}` |
| POST | `/v1/tasks` | `RemoteTaskRequest`（见 5.3） | `RemoteSubmitResponse{task_id, state, created}`；新建 201 / 幂等命中 200 |
| GET | `/v1/tasks/{id}` | — | `RemoteStatusResponse{task_id, state, created_at, updated_at, started_at, finished_at, request, result{path,size_bytes,sha256,duration_s}, error, retryable, error_code, bytes_received}` |
| POST | `/v1/tasks/{id}/cancel` | 空对象 | `CancelResponse{task_id, state, cancelled}`；终态再取消返回 409 |
| GET | `/v1/files/{id}` | — | 产物字节流；`Accept-Ranges: bytes`、`ETag: "<sha256>"`；`Range` ⇒ 206 + `Content-Range` |

### 5.3 `RemoteTaskRequest`

```json
{
  "protocol_version": "1.0",
  "task_id": "task-0001",
  "asset_id": "asset-ncar-e5-2020-01-nc",
  "dataset_id": "era5-pressure-levels",
  "source_id": "ncar",
  "provider_id": "ncar",
  "access_route": "https",
  "protocol": "https",
  "origin_uri": "https://rda.ucar.edu/.../e5.1-pressure-2020-01.nc",
  "file_name": "e5.1-pressure-2020-01.nc",
  "size_bytes": 3145728,
  "expected_checksum": "sha256:<64 hex>",
  "target_relative_path": "era5-pressure-levels/e5.1-pressure-2020-01.nc",
  "timeout_seconds": 1800.0,
  "metadata": {}
}
```

`target_relative_path` 只允许干净相对路径：`..`、绝对路径、以及"规范化后才合法"的
`a/../b` 一律拒绝（400），避免审计记录里出现看似合法实为越界的路径。

### 5.4 状态与错误码

远程任务状态：`queued | executing | completed | failed | cancelled`（与本地 12 态状态机
是不同层次，不要混用）。`failed` 携带 `retryable` 与 `error_code`
（`transfer_failed` / `size_mismatch` / `checksum_mismatch` / `worker_internal_error` …）。

| HTTP | `error` | 场景 |
| --- | --- | --- |
| 400 | `invalid_json` / `invalid_task` / `invalid_register` / `invalid_heartbeat` / `invalid_range` / `path_escape` / `invalid_task_id` | 请求不合法 |
| 401 | `unauthorized` | 令牌缺失/错误 |
| 403 | `capability_mismatch` / `worker_mismatch` | 能力不匹配 / 冒名心跳 |
| 404 | `unknown_task` / `not_found` | 未知任务 / 未知路由 |
| 409 | `task_id_conflict` / `task_finished` / `task_not_completed` | 状态冲突 |
| 410 | `product_missing` | 产物丢失 |
| 413 | `payload_too_large` | 请求体 > 8 MiB |
| 416 | `range_not_satisfiable` | Range 越界 |
| 500 | `internal_error` | 服务端缺陷 |

### 5.5 幂等 / 超时 / 失联语义

| 场景 | 语义 |
| --- | --- |
| 同一 `task_id` 重复 `POST /v1/tasks`，内容相同 | `200` + 原状态，`created=false`，**不会第二次下载** |
| 同一 `task_id`，内容不同 | `409 task_id_conflict`（不静默覆盖） |
| 客户端超时/崩溃 | 服务端按 `task_lease_seconds` 判定孤儿：轮询与心跳会续租，超过即取消 |
| 对终态任务取消 | `409 task_finished`（不假装成功） |
| 主进程侧超时 | `FailoverCoordinator` 的总超时（默认 1800s/链路）触发取消并转移链路 |
| 主进程侧失联判定 | 连续失败达到 `worker_offline_after_failures`（默认 3）→ 注册表 `offline`，后续选路立即跳过 |
| 回传 | `GET /v1/files/{id}`；本地已有 `.part` 时发 `Range` 续传，服务端回 200 则丢弃前缀整文件重下，**绝不拼接** |

## 6. Route 与实测选路（`network/routes/`）

### 6.1 Route 契约

```text
Route{route_id, protocol(https|rest|http_range|s3|ftp|sftp|provider_api|provider_async|cloud_export),
      worker_id, provider_id, supports_resume, max_concurrent, auth_ref, base_url,
      dataset_ids[], enabled, notes}
```

- 协议必须是枚举之一（拼错立即失败，不会被当成"能用"）；
- `auth_ref` 只能是引用（`env:` / `secret:` / `keychain:` / `file:`），**内联密钥直接拒绝**；
- "平台不支持某协议"是**显式拒绝**：`RouteRejection{code: protocol_not_supported_by_worker | unsupported_protocol}`，
  被拒原因始终回传给调用方。

### 6.2 选路流程

```python
selection = route_manager.select(provider_id="ncar", dataset_id="era5-pressure-levels", size_bytes=3_145_728)
selection.basis         # MeasurementBasis.MEASURED | NO_MEASUREMENT
selection.chosen        # RouteOption | None
selection.options       # 按分数降序的候选
selection.rejected      # 每条被拒链路的结构化原因
```

过滤顺序：`enabled` → 本次已失败（`exclude`）→ 协议已实现 → 数据集白名单 → 提供方
→ 节点已登记 → 节点能力协议白名单 → 节点健康（`offline` 排除，`degraded` 可关）
→ 节点 `max_bytes` → 链路并发预算。

打分（只由实测样本驱动）：

```text
scale       = 1 - exp(-samples / 5)
score       = scale * (5.0*success_rate + 2.0*min(throughput/50MiBps, 1)
                       + 1.0*(1-latency_penalty) + 1.0*(1-size_penalty) + 0.5*resume)
degraded 节点: score *= 0.7
无样本:        score = None, basis = "no_measurement"
```

冷启动时排序退化为"可用性 + 稳定字典序"，**地区标签不参与任何计算**。

### 6.3 `route_stats` 字段

`samples / successes / failures / retry_count / transferred_bytes / transfer_duration_s /
latency_ms_total / last_outcome / last_success_at / last_failure_at`；派生
`success_rate / failure_rate / throughput_bps / latency_ms`，**无样本时为 `None` 而不是 0**。

## 7. 故障转移（`network/routes/failover.py`）

```python
delivery = coordinator.run(task, provider_id="ncar")
# TransferDelivery{task_id, route, file{path,size_bytes,sha256,resumed}, attempts[]}
```

- 每次尝试的成败都写一条 `route_stats` 样本（失败也计入 `samples`/`failure_rate`）；
- 失败且 `retryable` → 下一条链路；不可转移（取消、凭据错误、路径越界）→ 立即停止；
- 回传后对落盘文件重新计算 SHA-256，与任务声明值比对（不一致即换链路）；
- 全部失败 → `FailoverExhausted{attempts[], retryable}`；无候选 → `NoRouteAvailable{rejected[]}`。

## 8. 公开 API（供 MCP 层 `list_workers` / `get_route_status` 使用）

### 8.1 函数签名

```python
# list_workers
registry.list_workers(kind: WorkerKind | None = None) -> Sequence[WorkerRecord]
registry.candidates(dataset_id, provider_id, protocol, size_bytes,
                    include_degraded=True, kind=None) -> Sequence[WorkerRecord]

# get_route_status
route_manager.status() -> dict[str, object]

# 选路/观测
route_manager.select(provider_id, dataset_id, size_bytes, target=None,
                     exclude=frozenset(), include_degraded=True) -> RouteSelection
route_manager.stats_for(route_id, provider_id=None) -> RouteStats
route_manager.record_attempt(route, success, latency_ms, duration_s,
                             bytes_transferred, retried, outcome) -> RouteRecord

# 节点接入（真实探活后登记）
coordinator.ensure_remote_worker(worker_id, endpoint, token=None, region=None) -> WorkerRecord

# 健康维护
registry.heartbeat(worker_id) -> WorkerRecord
registry.mark_offline(worker_id, reason=...) -> WorkerRecord
registry.deregister(worker_id) -> bool
```

### 8.2 `list_workers` 数据形状

```python
[WorkerSummary.of(r).model_dump(mode="json") for r in registry.list_workers()]
```

真实输出（`WorkerSummary` 字段恒全量，不随节点类型缺字段）：

```json
[
  {
    "worker_id": "remote-a",
    "kind": "remote",
    "region": "cn-east",
    "status": "healthy",
    "last_heartbeat": "2026-11-05T12:00:00Z",
    "uptime_s": 0.0,
    "version": "0.1.0",
    "endpoint": "http://127.0.0.1:8765",
    "capabilities": {
      "datasets": [], "providers": [], "protocols": ["https"],
      "max_bytes": 0, "max_concurrent": 2
    }
  }
]
```

字段语义：`status ∈ {healthy, degraded, offline}`；`last_heartbeat` 为 `null` 表示注册表从未
观测到心跳；`uptime_s` 为 `last_heartbeat - registered_at` 的**实测差**（注册后尚未心跳即为
`0.0`），不是进程启动时长。

### 8.3 `get_route_status` 数据形状

```python
route_manager.status()  # -> {"routes": [RouteStatus, ...]}
```

真实输出：

```json
{
  "routes": [
    {
      "route_id": "remote-a:ncar:https",
      "protocol": "https",
      "worker_id": "remote-a",
      "provider_id": "ncar",
      "worker_status": "healthy",
      "supports_resume": true,
      "max_concurrent": 1,
      "in_flight": 0,
      "measurement": "measured",
      "stats": {
        "route_id": "remote-a:ncar:https",
        "provider_id": "",
        "samples": 1,
        "successes": 1,
        "failures": 0,
        "success_rate": 1.0,
        "failure_rate": 0.0,
        "retry_count": 0,
        "transferred_bytes": 1048576,
        "transfer_duration_s": 0.094,
        "latency_ms": 41.7,
        "throughput_bps": 11155063.829787234,
        "last_outcome": "succeeded"
      }
    }
  ]
}
```

约定（MCP 层可直接依赖）：

- `measurement ∈ {"measured", "no_measurement"}`——`no_measurement` 表示该链路**没有任何实测
  样本**，此时 `stats.samples == 0` 且所有派生指标为 `null`（**不是 0**）；
- `worker_status` 为 `null` 表示链路指向的节点未登记（孤儿链路，不参与选路）；
- `stats.provider_id == ""` 表示"跨提供方聚合视图"；`route_stats` 明细按 `(route_id, source_id)`
  存储，需要单提供方数据请用 `route_manager.stats_for(route_id, provider_id="ncar")`。

## 9. 真实测量样本（本机 loopback，2026-11-05）

| 链路 | 样本 | 文件 | 单次传输 | 备注 |
| --- | --- | --- | --- | --- |
| `remote-perf:ncar:https`（子进程 agent） | 6 | 4 MiB | 0.0266 / 0.0276 / 0.0671 s | loopback + 回传 + 双次 SHA-256 |
| `remote-perf:ncar:https` | 12 | 4 MiB | 同上（累计） | `throughput_bps=88_345_580`、`latency_ms=1.77` |
| `local-perf:ncar:https`（进程内） | 3 | 4 MiB | 0.0260 / 0.0305 / 0.0302 s | 同一源，无回传段 |

> 这些数字只说明**本机 loopback** 的性能，不代表任何真实远端节点的速度；真实部署应以
> 生产库里的 `route_stats` 为准（每完成一次传输就会累计一条样本）。

### 9.1 一条真实样本的完整往返（`curl` 驱动独立进程，2026-11-05）

```text
$ python -m researchdatahub.network.workers.agent --host 127.0.0.1 --port 8791 \
      --worker-id demo-a --storage-root /tmp/agent-demo/storage --max-concurrent 2
[agent demo-a] listening on http://127.0.0.1:8791 (storage=/private/tmp/agent-demo/storage)

$ curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8791/v1/health
{"ok":true,"agent_version":"0.1.0","protocol_version":"1.0","worker_id":"demo-a",
 "uptime_s":1.4276,"capabilities":{"datasets":[],"providers":[],"protocols":["http","https"],
 "max_bytes":0,"max_concurrent":2},"storage_root":"/private/tmp/agent-demo/storage",
 "free_bytes":252778532864,"active_tasks":0,"known_tasks":0,
 "server_time":"2026-11-05T15:18:35.039447Z"}

$ curl -s -X POST .../v1/tasks   # 2048 位随机 1 MiB 文件，local http.server 提供
{"task_id":"task-demo-1","state":"queued","created":true}              # HTTP 201

$ curl -s .../v1/tasks/task-demo-1
{"task_id":"task-demo-1","state":"completed","bytes_received":1048576,
 "result":{"path":".../storage/era5/payload.bin","size_bytes":1048576,
           "sha256":"c2e236112b8e94576d002d8f735e3a0bb4932d480fec3789f2ae55c4812f088f",
           "duration_s":0.06223358301213011}}

$ curl -s -o returned.bin .../v1/files/task-demo-1      # HTTP 200 bytes=1048576
source   sha256 = c2e236112b8e94576d002d8f735e3a0bb4932d480fec3789f2ae55c4812f088f
returned sha256 = c2e236112b8e94576d002d8f735e3a0bb4932d480fec3789f2ae55c4812f088f   match=TRUE

$ curl -s -X POST .../v1/tasks   # 同一 task_id 再提交一次
{"task_id":"task-demo-1","state":"completed","created":false}          # HTTP 200，未重复下载

$ cat agent.log
[agent demo-a] listening on http://127.0.0.1:8791 ...
[agent demo-a] GET /v1/health -> 200
[agent demo-a] GET /v1/health -> 401        # 不带令牌被拒
[agent demo-a] POST /v1/tasks -> 201
[agent demo-a] GET /v1/tasks/task-demo-1 -> 200
[agent demo-a] GET /v1/files/task-demo-1 -> 200
[agent demo-a] POST /v1/tasks -> 200        # 幂等命中
```

## 10. 为什么选路"不按地域猜速度"

`grep -n "region\|country" src/researchdatahub/network/routes/*.py` 的全部命中（2 行，均在
`failover.py`）：

```text
src/researchdatahub/network/routes/failover.py:283:        region: str | None = None,
src/researchdatahub/network/routes/failover.py:317:            region=region,
```

两者的含义：

| 行 | 作用 | 是否进入打分 |
| --- | --- | --- |
| `failover.py:283` | `ensure_remote_worker(..., region=...)` 的**入参**，供运维标注部署区域 | 否 |
| `failover.py:317` | 把该参数原样写进 `WorkerRecord.region`（展示字段） | 否 |

证据：

- `routes/models.py` / `routes/stats.py` 中**没有** `region` / `country` 任何引用；
- 打分函数 `score_route(stats, *, supports_resume, size_bytes)` 的入参只有**实测统计**与
  "是否支持续传 / 文件多大"，签名里根本没有节点身份或地理位置；
- `RouteManager.select()` 的排序键是
  `(是否有实测样本, -score, worker 健康度, route_id)`——最后一项是**稳定字典序**，
  仅用于让无样本链路的结果可复现（见 `tests/unit/test_network/test_routes.py::test_select_does_not_guess_by_region`
  的双向断言：`cn-east` 与 `us-west` 节点在无样本时按 `route_id` 字典序排列，与地区无关）。

## 11. 已知限制（如实标注）

1. **agent 任务状态只在内存**：进程重启后未完成任务丢失，需要主进程重新下发
   （主进程侧会把该节点判为失败并转移，不会误判为成功）。
2. **HTTP Range 只支持单段**；多段范围返回 400。
3. **未做 TLS**：协议本身是 JSON over HTTP，TLS 需由反向代理终结（文档明确要求）。
4. **worker 侧不做跨提供方限流**：节点级并发上限由 `max_concurrent` 控制，但同一提供方的
   速率限制没有单独建模（属 sources 层职责）。
5. **选路打分未使用时间衰减**：历史样本永久累计；若要反映"最近变慢"，需要额外的滑动窗口
   （当前 `stats.reset(route_id)` 提供手动重新校准）。
6. **远程节点回传会占用主进程磁盘**（`<workers.db 目录>/remote-artifacts/<worker_id>/`），
   没有自动清理策略。
