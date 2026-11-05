---
name: data-acquisition
description: 在 ResearchDataHub 中把已确认的数据需求估量、生成执行计划、向用户展示计划并取得明确确认（计划带一次性 approval_token），然后提交采集任务并轮询状态，失败时按错误码做重试或取消。当用户已确认数据集与参数、要真正开始下载/采集，或要"估算这次要下载多少""把任务跑起来""查进度""取消任务"时使用。
metadata:
  type: workflow
  version: "1.0.0"
  stage: "3/4"
  next-skill: data-validation
---

# data-acquisition —— 估量、计划、审批、提交、轮询

> 四段式流程的**第 3 段**：`data-discovery` → `environment-setup` → `data-acquisition` → `data-validation`
> 本段是**唯一**会产生副作用（写库、入队、下载文件）的阶段，因此设计了**两阶段确认**：
> `create_execution_plan` 落库并签发**一次性 approval_token** → 用户确认 → `submit_acquisition` 校验令牌后执行。

## 1. 何时使用

- 用户已确认数据集、变量、时间、区域、输出路径（或已走完 `data-discovery`）；
- 上一次采集失败，需要按错误码决定"重试 / 换源 / 取消 / 修配置"；
- 用户问「这次大概要下多少」「跑起来了没」「怎么取消」。

**不适用**：还没确认科学参数（回 `data-discovery`）；环境未就绪（回 `environment-setup`）。

## 2. 前置条件（硬性）

1. `validate_environment(provider).usable == true`（否则不允许进入本段）；
2. 需求 8 字段齐全，关键科学参数**没有猜测成分**；
3. `output.path` 指向用户确实想写入的目录。

## 3. 工具调用序列

```text
① validate_request(request | request_path, units_catalog?)      # 规范化 + 可满足性预判
② estimate_acquisition(request | request_path, split_granularity?)   # 规模估算
③ create_execution_plan(request | request_path, split_granularity?, approval_ttl_seconds?)  # 落库 + 发令牌
④ （把计划展示给用户，取得明确确认）
⑤ submit_acquisition(plan_id, approval_token, max_workers?, max_tasks?)   # 唯一真正执行入口
⑥ get_task_status(task_ids)                                     # 轮询
⑦ （按需）cancel_task(task_ids, reason?) / get_execution_plan(plan_id)
```

### 3.1 `validate_request`

| 参数 | 类型 | 说明 |
|---|---|---|
| `request` | object（`additionalProperties: true`） | 需求对象：**分段格式**（`request`/`dataset`/`variables`/`temporal`/`spatial`/`vertical`/`storage`/`validation`/`extensions`）或**扁平 DatasetRequest 格式**；未知顶层段直接报错 |
| `request_path` | string | 改为从 YAML 文件读（与 `request` 二选一） |
| `units_catalog` | object<string,string> | 变量→单位的权威映射；**未提供时不得猜测单位** |

返回：`request`（规范化后的完整需求）、`request_id`、`dataset_id`、`variables[]`、`warnings[]`、`satisfiable_now`
（`candidate_providers` / `preferred_available` / `note`）。**不落库、不访问远端。**

### 3.2 `estimate_acquisition`

| 参数 | 类型 | 说明 |
|---|---|---|
| `request` / `request_path` | 同上 | 二选一 |
| `split_granularity` | `auto` \| `file` \| `year` \| `month` \| `request` | 默认 `auto`（由数据源能力决定） |

返回：`task_count`、`tasks_by_source`、`tasks_by_route`、`total_bytes`、`per_task[]`、`unmatched_variables[]`、`persisted:false`。

> **`unmatched_variables` 非空必须显式报告用户**：「以下变量无法被任何候选源满足：…」，并让用户在
> 「换数据集 / 缩小变量 / 允许部分满足（`allow_partial`）」之间选择。**禁止静默删除或改写变量。**

### 3.3 `create_execution_plan`

| 参数 | 类型 | 说明 |
|---|---|---|
| `request` / `request_path` | 同上 | 二选一 |
| `split_granularity` | 同上 | 默认 `auto` |
| `approval_ttl_seconds` | int 60–86400 | 令牌有效期，默认 `3600` |

返回（**关键**）：

```json
{
  "plan_id": "plan-...",
  "request_id": "req-...",
  "task_count": 120,
  "plan": { ...任务清单... },
  "approval_token": "<一次性令牌>",
  "approval": { ...摘要... },
  "approval_usage": {"submit_tool": "submit_acquisition", "one_shot": true, "note": "..."},
  "warnings": [],
  "persisted": true
}
```

- **令牌只对这一个 plan 生效，提交一次即失效；过期需重新 `create_execution_plan`**。
- 令牌要原样传给 `submit_acquisition` 的 `approval_token`；**不要展示给用户全文**（它是审批凭据，
  截断显示前 8 位 + `…` 即可），也**不要**写入任何文件。

### 3.4 `submit_acquisition`（**唯一**执行入口）

| 参数 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `plan_id` | ✅ | string | `create_execution_plan` 返回的 plan_id |
| `approval_token` | ✅ | string(16–4096) | 一次性审批令牌 |
| `max_workers` | — | int 1–8 | 本次并发；**省略=只入队不执行** |
| `max_tasks` | — | int 1–1000 | `max_workers` 生效时本次最多执行多少任务 |

返回：`plan_id`、`submitted`、`queued_tasks`、`task_count`、`approval:{consumed:true,one_shot:true}`、
`executed`、`run`（同步执行时的运行摘要：`counts` / `completed` / `unfinished` / `records[]` / `errors[]`）。

**幂等**：同一 plan 重复提交会返回既有任务组，**不会重复入队**，也不会误报 `approval_invalid`。
**令牌无效**（缺失/伪造/过期/已用/计划已变）→ `approval_invalid`，且 `detail.side_effects_performed=false`
——即什么也没发生。

### 3.5 `get_task_status`（必填 `task_ids`，1–200 个）

返回 `tasks[]`（每项：`task_id`、`plan_id`、`request_id`、`state`、`attempt` / `max_attempts`、`worker_id`、
`heartbeat_at`、`lease_expires_at`、`last_error`、`source_id`、`file_name`、`origin_uri`、`access_route`、
`file_count`、`latest_report_status`、`events[]`）、`errors[]`、`found`、`missing`、`source:"sqlite"`。

**状态只从 SQLite 读**——服务重启后仍能查到同一结果。

任务状态机（全部取值）：
`PENDING → PLANNED → QUEUED → RUNNING → VERIFYING → COMPLETED`；
可重试失败 → `RETRY_WAIT` → 回到 `QUEUED`；终态：`COMPLETED` / `FAILED` / `CANCELLED` / `SKIPPED` / `QUARANTINED`；
用户暂停：`PAUSED`。

### 3.6 `cancel_task`（必填 `task_ids`，可选 `reason` ≤512）

返回 `cancelled[]`、`skipped[]`（含 `reason`：`terminal` = 终态不可改 / `not_found` / `transition_rejected`）、
`cancelled_count`、`skipped_count`。**终态任务不会被改写。**

### 3.7 `list_workers` / `get_route_status`（可选，节点与链路）

- `list_workers(status?, limit?)` → 分布式 worker 健康与能力；依赖 `researchdatahub.network` 包，
  不可用时返回 `not_available`（**不伪造 worker 列表**）。
- `get_route_status(dataset_id?, probe?, timeout_seconds?)` → 各访问路线（`https` / `s3` / `cds_api` …）
  可用性与不可用原因；`probe:true` 会发真实 HEAD。**计划展示里的"风险"一栏就来自这里。**

## 4. 计划展示模板（提交前必须让用户看到）

```markdown
## 执行计划（待你确认）

| 项 | 值 |
|---|---|
| plan_id | plan-7f3a…（令牌有效期至 15:20） |
| 数据源 / 数据集 | cds → reanalysis-era5-pressure-levels |
| 变量 | clwc, ciwc, t, u, v（+ 原生名映射 clwc/ciwc/t/u/v） |
| 时间 / 频率 | 2015-01-01 → 2025-01-01（UTC，闭开），逐时 |
| 区域 / 网格 | [73,18,135,54]（W,S,E,N），0.25° |
| 垂直层 | 1000 / 850 / 500 hPa |
| worker / route | 2 workers，cds_api |
| 预计文件数 | 120 |
| 预计体积 | 48.3 GB |
| 风险 | CDS 拥塞期可能排队 30 min+；剩余配额未知 |
| 费用 / 配额 | 免费（CDS 无按量计费），但受队列与日配额约束 |
| 输出 | netcdf → ./data/raw |

**注意**：本次会向 CDS 提交 120 个作业并写入 Manifest，属于外部副作用。
回复「确认执行」即开始；回复「改…」我会重新生成计划（旧令牌作废）。
```

**必须包含**：平台、数据集、参数、worker、route、预计文件数、预计体积、风险、费用或配额。

## 5. 输出给用户的运行中/结束后模板

```text
正在采集（plan-7f3a…）：已完成 37/120，运行中 2，排队 81，失败 0
最新事件：task-0042 RUNNING（cds_api，clwc 2019-03）
—— 需要我继续盯着，还是到 50% 再报一次？
```

```text
采集结束：COMPLETED 118 / FAILED 2 / QUARANTINED 0
失败任务：task-0091（transport_error，已耗尽 3 次重试）、task-0107（integrity_error）
下一步：进入 data-validation 生成交付清单并逐文件校验。
```

## 6. 轮询策略（避免把 Agent 轮次烧在等待上）

1. 提交用 `max_workers` 让执行在服务端推进（默认 2，符合"单机不打满远端"）；
2. 用 `get_task_status` **批量**查（一次最多 200 个 id），**不要**逐个查；
3. 有任务处于 `RETRY_WAIT` 时，先算下一次重试时间（看 `events[]` / `attempt`）再决定何时再查；
4. **禁止**忙等：单次对话里最多查 2–3 次，中间把控制权交还用户；
5. 全部任务进入终态（`COMPLETED`/`FAILED`/`CANCELLED`/`SKIPPED`/`QUARANTINED`）才进入 `data-validation`。

## 7. 错误码表（与 MCP 层一致，按《《错误码 → Agent 动作》使用）

| 错误码 | 含义 | `retryable` | Agent 动作 |
|---|---|---|---|
| `invalid_arguments` | 参数名/类型/取值不符合 schema（顶层未知字段一律拒绝） | 否 | 按 `hint` 修正参数，**不要**原样重试 |
| `not_found` | plan / task / dataset 不存在 | 否 | 用 `get_execution_plan` / `get_task_status` 确认 ID |
| `conflict` | 目标已存在（如 `get_manifest` 的 `output_path`） | 否 | 换路径，或先与用户确认是否覆盖（本服务不覆盖） |
| `approval_invalid` | 令牌缺失/伪造/过期/已用/计划已变 | **否** | 重新 `create_execution_plan` 拿新令牌，在有效期内提交 |
| `approval_required` | 未提供审批子系统/未带令牌 | 否 | 检查 server 是否配置了审批；必须走两阶段流程 |
| `not_available` | 可选组件未注入（`environment` / `providers` / `network`） | 否 | 看 `data.capability`，如实告诉用户"该能力不可用" |
| `invalid_state` | 当前状态不允许该动作（如取消终态任务） | 否 | 先查状态再决定 |
| `config_error` | 配置/状态根有问题 | 否 | 提示 `rdh config validate` 与 `--root` |
| `model_invalid` | 需求模型字段不合法 | 否 | 按 `data.detail` 修字段（常见：坐标顺序、日期、层序） |
| `source_not_found` | 数据源未在配置中声明 | 否 | 换源或补配置 |
| `source_unavailable` | 源暂时不可用（凭证/配额/网络） | **是** | 稍后重试或换源；先跑 `get_route_status(probe:true)` |
| `metadata_error` | 远端元数据与声明不一致 | 否 | 检查需求文件；不要盲改 |
| `storage_error` | 磁盘/权限问题 | 否 | 让用户清空间或改 `output.path` |
| `manifest_error` | SQLite 操作失败（含 `SQLITE_BUSY`） | **是**（建议） | 重试一次；连续失败则让用户检查库文件 |
| `schedule_error` | 计划规模/调度约束不满足 | 否 | 缩小时间范围或降低并发 |
| `integrity_error` | 完整性检查无法进行（缺失/不可读） | 否 | **不得当成通过**；进入 `data-validation` 标 unverified |
| `transport_error` | 传输失败 | **是** | 看 `retryable`；服务端已按分类重试，耗尽后建议换源 |
| `not_implemented` | 功能未实现 | 否 | 如实告知"未实现"，**不要**假设成功 |
| `internal_error` | 服务端内部错误（详情见 stderr） | 否 | 停止重试，报告维护者 |

> 成功/失败信封统一为：`{ok, data, error, meta:{tool, schema_version, duration_ms}}`；
> `error` 含 `code` / `message` / `hint` / `retryable` / `detail`（已脱敏）。

## 8. 失败分支与恢复动作（流程级）

| 现象 | 恢复动作 |
|---|---|
| `estimate_acquisition` 报 `unmatched_variables` 非空 | 显式告诉用户哪些变量不满足；给"换数据集/删变量/允许部分满足"三选一 |
| `create_execution_plan` 后用户改主意 | 修改需求后**重新** `create_execution_plan`（旧 plan 的令牌自然作废），不要复用旧令牌 |
| `submit_acquisition` 返回 `approval_invalid` | 若原因是**过期** → 重新生成计划；若原因是**已使用** → 用 `get_task_status` 查是否已入队（幂等） |
| 任务长时间 `QUEUED` | CDS 是数据集级队列；用 `get_route_status` 看是否可转 NCAR，问用户是否换源 |
| 任务反复 `RETRY_WAIT` → `FAILED` | 报告 `last_error` 与 `access_route`；建议换源或缩小范围，**不要**无限自动重试 |
| 用户要求取消 | `cancel_task(task_ids, reason)`，把 `skipped[]` 的原因如实转述 |
| 服务重启后继续 | `get_execution_plan(plan_id)` + `get_task_status(task_ids)` 即可续接（状态在 SQLite） |

## 9. 黄金路径：10 步完整调用序列

> 用户原话：「我需要 2015—2024 年中国区域 ERA5 小时数据，变量包括云液态水、云冰水、温度和风场，保存到当前项目的 data/raw」
> 解析后：变量 `clwc / ciwc / t / u / v`（气压层）；时间 `2015-01-01 → 2025-01-01`（UTC 闭开，逐时）；
> 区域 `[73,18,135,54]`（W,S,E,N）；层 `1000/850/500 hPa`；输出 `netcdf → ./data/raw`。

| 步 | Skill | 工具 | 关键参数（形状） | 期望返回 | 用户看到 |
|---|---|---|---|---|---|
| 1 | discovery | `search_datasets` | `{"query":"era5","variables":["clwc","ciwc","t"]}` | 候选数据集清单 | 「找到 2 个候选源」 |
| 2 | discovery | `inspect_dataset` | `{"dataset_id":"reanalysis-era5-pressure-levels"}` | 覆盖范围 + `cds_api` 路线 + verified | 数据集详情 |
| 3 | discovery | `compare_datasets` | `{"dataset_ids":["reanalysis-era5-pressure-levels","ds633.0"],"dimensions":["variables","temporal_coverage","vertical_levels","providers"]}` | 逐维度对比 | 对比表 |
| 4 | discovery | `get_provider_capabilities` | `{"provider":"cds"}` | 路线 + 缺失依赖/凭证**名称** | 「需要 CDSAPI_KEY」 |
| 5 | — | （向用户确认 8 字段与方案） | — | 用户确认 | 确认框 |
| 6 | env | `check_environment` | `{"provider":"cds","checks":["dependencies","credentials","network","disk"]}` | `status` / `usable` / `requirements[]` | 环境检查结果 |
| 7 | env | `get_environment_requirements` | `{"provider":"cds"}`（仅当 `usable=false`） | `guide.steps[]`（action/why/where/verify） | 四要素配置引导 |
| 8 | env | `validate_environment` | `{"provider":"cds","require_network":true}` | `usable:true` | ✅ 环境就绪 |
| 9 | acquisition | `validate_request` → `estimate_acquisition` → `create_execution_plan` | 见下方 JSON | 规范化需求 → 规模估算 → `plan_id` + `approval_token` | 执行计划表（含预计文件数/体积/风险） |
| 10 | acquisition | `submit_acquisition` → `get_task_status` → `verify_dataset` → `get_manifest` | 见下方 JSON | 入队/运行 → 任务状态 → 校验结论 → 交付清单 | 采集进度 → 交付清单 |

**第 9 步的实际请求体**（分段格式，`validate_request` / `estimate_acquisition` / `create_execution_plan` 共用同一个 `request` 对象）：

```json
{
  "request": {
    "request": {"name": "era5-clouds-2015-2024", "preferred_sources": ["cds"], "max_sources": 1},
    "dataset": {
      "provider": "cds",
      "name": "reanalysis-era5-pressure-levels",
      "product": "reanalysis",
      "product_family": "ERA5"
    },
    "variables": [
      {"name": "clwc", "units": "kg kg**-1", "long_name": "Specific cloud liquid water content"},
      {"name": "ciwc", "units": "kg kg**-1", "long_name": "Specific cloud ice water content"},
      {"name": "t",    "units": "K",         "long_name": "Temperature"},
      {"name": "u",    "units": "m s**-1",   "long_name": "Eastward wind"},
      {"name": "v",    "units": "m s**-1",   "long_name": "Northward wind"}
    ],
    "temporal": {"start": "2015-01-01", "end": "2024-12-31", "frequency": "hourly"},
    "spatial": {
      "bbox": {"west": 73.0, "south": 18.0, "east": 135.0, "north": 54.0, "grid_degrees": 0.25}
    },
    "vertical": {"kind": "pressure", "levels": [1000.0, 850.0, 500.0], "units": "hPa"},
    "storage": {"preserve_original": true},
    "validation": {"verify_size": true, "calculate_sha256": true, "netcdf_openable": true},
    "extensions": {"era5.dataset_note": "CDS collection reanalysis-era5-pressure-levels"}
  },
  "split_granularity": "auto"
}
```

**字段约束（照抄即可，别自己发明字段名）**：

| 段 | 允许的键 | 必需 | 说明 |
|---|---|---|---|
| `request` | `name` `priority` `preferred_sources` `max_sources` `extensions` | `name` 建议填 | `name` 即 `request_id` |
| `dataset` | `provider` `name` `product` `product_family` `version` | `name` | 这里用 `name`，**不是** `dataset_id` |
| `variables` | 字符串，或映射 `{name, units, long_name, standard_name, cell_methods}` | ≥1 | **不能为空**；`units` 强烈建议显式写 |
| `temporal` | `start` `end` `frequency` | 三者全必填 | `end` 纯日期 → 展开到次日 00:00 UTC（含当天） |
| `spatial` | `type`（`global`）或 `bbox{west,east,south,north,grid_degrees}` | 二选一 | **不要**写 `bounds: [W,S,E,N]` 数组 |
| `vertical` | `kind` `levels` `units` | 可选 | `pressure` 的 `levels` 必须**严格递减**（hPa） |
| `storage` | `preserve_original` | 可选 | 默认 `true` |
| `validation` | `verify_size` `calculate_sha256` `netcdf_openable` | **至少开一项** | 全 `false` 会让校验状态成 `UNKNOWN`、任务永远无法 `COMPLETED` |
| 顶层 `extensions` | 任意，键建议带命名空间点号 | 可选 | 如 `era5.dataset_note` |

> 未知的顶层段/键**直接报错**（不会静默忽略）。上面这份请求体已用仓库的真实加载器
> `researchdatahub.planner.request.load_request_text_detailed` 实测通过（2026-11-05）：
> 解析出 `variables=['clwc','ciwc','t','u','v']`、`temporal=2015-01-01T00:00:00Z→2025-01-01T00:00:00Z`、
> `spatial=[73,18,135,54]`、`vertical=pressure[1000,850,500] hPa`、`checks=[size_match, checksum_match, netcdf_openable]`、`warnings=[]`。
>
> **语义名与平台变量 ID 的区别**：本格式的 `variables[].name` 直接就是规划/路由使用的变量标识；
> 「语义名 → Provider 原生 ID」的映射槽（`provider_native`）属于**版本化请求契约 v1.2**
> （`schema_version: "1.2"`，见 `src/researchdatahub/planner/request_v12.py`）。注意：**当前 MCP 的 `request`
> 参数走分段/扁平格式，不经过 v1.2 解析**，所以先写分段格式；v1.2 的 `selection` / `output`
> 顶层段会被加载器以「未知的顶层段」拒绝（本机实测 2026-11-05）。若需要单位权威映射，用
> `validate_request` 的 `units_catalog` 参数。

**第 10 步的实际调用**：

```json
submit_acquisition: {"plan_id": "<第9步返回>", "approval_token": "<第9步返回>", "max_workers": 2, "max_tasks": 200}
get_task_status:    {"task_ids": ["task-0001", "..."]}          // 来自 submit 返回或 get_execution_plan
```

## 10. 禁止事项

1. **禁止跳过 `create_execution_plan` 直接提交**——没有令牌 `submit_acquisition` 必然 `approval_invalid`。
2. **禁止编造或猜测 `approval_token`**——它是 HMAC 签名的一次性凭据。
3. **禁止未经用户明确确认就 `submit_acquisition`**——涉及外部副作用（提交作业、写盘、占配额）。
4. **禁止静默删除/改写不支持的变量**——必须通过 `unmatched_variables` 显式上报。
5. 禁止把错误码翻译成模糊说法（`approval_invalid` 不能报成"网络问题"）。
6. 禁止在用户没要求时自行切换数据源；换源必须问。
7. 禁止在正文里回显令牌全文或任何凭证。
