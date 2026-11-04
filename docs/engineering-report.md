# 工程验收报告：M1–M5

| 项 | 值 |
|---|---|
| 报告日期 | 2026-11-04 |
| 基线 commit | `3f149a3`（M0）→ `e5e1365`（M4 完成） |
| 执行方式 | Agent Teams（Lead + 5 名模块负责人，写作用域互不重叠） |
| 验证口径 | 只有实测 PASS 才算通过；NOT RUN ≠ PASS；SKIPPED 单独计数 |
| 环境 | macOS / Apple Silicon (arm64) / 8 GB / Python 3.13.13 / .venv |

---

## A. 实际完成情况

| 里程碑 | 状态 | 关键证据 |
|---|---|---|
| **M0**（基线） | ✅ 已完成（本次未改动其契约语义） | 360 tests / ruff / mypy strict 全绿，工作区干净 |
| **M1** SQLite Manifest | ✅ 完成 | `dc533ab`；82 项测试；`rdh db init/status/migrate` 可用 |
| **M2** 传输引擎 | ✅ 完成（S3 端到端 **NOT RUN**） | `4fdb191`、`88e8761`；104 项真实 HTTP 测试 |
| **M3** 数据源适配 | ✅ 完成（CDS 有凭证链路 **NOT RUN**） | `dfa02be`；82 项 + 11 项 network（实测 10 passed / 1 skip） |
| **M4** 计划与调度 | ✅ 完成 | `e5e1365`；63 项 + 5 项 e2e（含 4 个故障注入） |
| **M5** 完整性与存储 | ✅ 完成 | `dc533ab`、`ec32590`；193 项（storage 98 / integrity 58 / reporting 37） |
| 真实 ERA5 小规模端到端 | ❌ **NOT RUN** | 需 CDS 许可 / RDA 数据集权限；已用 GDEX 公开目录文件做过真实链路（见 §I.4） |

**一句话结论**：从"YAML 需求"到"可交付清单"的链路已经可用、可恢复、可审计；
**核心科学场景（真实 ERA5 产物）尚未在真实凭证下验证**，这是 M6 的第一优先。

## B. 架构变化

**新增模块实现**（M0 时只有 Protocol）：`manifest`、`transfer`、`sources`、`planner`、
`scheduler`、`integrity`、`storage`、`reporting`。

**修改的核心契约**（向后兼容，契约版本仍 `1.0`）：

| 位置 | 变更 | 原因 |
|---|---|---|
| `core/models.AcquisitionTask` | +`heartbeat_at`、+`lease_expires_at` | 单时间线无法同时满足"抢占保护"与"崩溃接管"（ADR-0004） |
| `core/models.TransferResult` | +`source_uri`/`expected_size`/`actual_size`/`started_at`/`retryable`/`resumed` | 传输必须自证"下了多少、续传没有"（§4.2） |
| `core/models` | +`DatabaseStatus`、`RecoveryResult`、`TransferLogEvent` | `db status`、恢复审计、传输事件的结构化载体 |
| `core/state` | +`lease_expired`、`renew_lease`；`transition` 签发/释放租约 | 双时间线（ADR-0004） |
| `core/ports.ManifestRepository` | 8 → 31 个成员（目录/任务/租约/文件/校验/交付/状态） | M1 需要领域完整性 |
| `config/models` | `transfer`/`manifest`/`planner` 三个新段；`storage` 改为 `raw/staging/quarantine/temp/archive`；`scheduler` +租约/心跳 | 存储布局与调度参数必须有配置来源（ADR-0005） |
| `cli` | 删除 M0 占位命令 `_stubs.py`；新增 7 个命令组；新增组合根 `cli/_services.py` | M4 落地后占位命令不再成立 |

**依赖方向**（与 M0 一致，未破坏）：

```
cli ──► {config, manifest, sources, planner, scheduler, storage, integrity, reporting, transfer}
执行层 ──► core（只依赖模型与 Protocol）
执行层 ✗ 横向 import（scheduler 通过 Protocol 接收 transfer/integrity/storage）
```

**数据流**：

```
请求 YAML → planner/request.py → DatasetRequest
        → planner/planner.py   → AcquisitionPlan（PLANNED 任务，资产已绑定）
        → scheduler.submit()    → QUEUED（数据库里，不是内存队列）
        → claim_tasks()         → RUNNING（租约+心跳）
        → transport.transfer()  → staging/.part
        → integrity.check()     → IntegrityReport
        → storage.commit_staging() → raw/（原子、只读、禁覆盖）
        → save_file_record + save_task_and_event → COMPLETED
        → reporting.build_*()   → 报告 JSON + 交付清单 JSON
```

## C. 数据契约（新增/关键字段）

| 模型 | 关键字段 |
|---|---|
| `AcquisitionTask` | `state`（12 态）、`attempt`/`max_attempts`、`worker_id`（=SQLite `lease_owner`）、`heartbeat_at`、`lease_expires_at`、`last_error`、`notes` |
| `TransferResult` | `status`(succeeded/failed/skipped)、`bytes_transferred`、`expected_size`、`actual_size`、`resumed`、`retryable`、`error_code`、`destination_path` |
| `IntegrityReport` | `status`(passed/failed/skipped/unknown)、`checks`、`failures`、`expected`/`observed`；`permits_completion` 仅 PASSED |
| `FileRecord` | `local_path`、`size_bytes`、`checksum`（`sha256:...`）、`origin_uri`、`remote_etag`、`retrieved_at` |
| `DeliveryManifest` | `files`(FileRecord)、`integrity`、`sources`、`total_bytes`、`complete`、`tool_version`；不变式：complete ⇒ 全部 PASSED |
| `DatabaseStatus` | `db_schema_version`、`journal_mode`、`foreign_keys`、`task_counts`、`file_count`、`integrity_failures` |
| `RecoveryResult` | `task_id`、`previous_state`、`new_state`、`lease_owner`、`reason` |
| `TransferLogEvent` | `task_id`、`event_type`、`message`、`bytes_transferred`、`metadata`、`timestamp` |

契约版本：`MODELS_VERSION = "1.0"`（只做加法，符合 1.x 规则）；数据库 schema 版本 `2`。

## D. 数据库

**10 张表**：`schema_version`、`sources`、`datasets`、`requests`、`assets`、`tasks`、`files`、
`transfer_logs`、`integrity_reports`、`plans`、`plan_tasks`。

**关键索引**：`idx_tasks_dispatch(state, priority, scheduled_at)`、`idx_tasks_lease(state, lease_expires_at)`、
`idx_tasks_request`、`idx_tasks_plan`、`idx_files_task`、`idx_assets_dataset`、
`idx_transfer_logs_task`、`idx_integrity_file`、`idx_integrity_status`、`idx_plan_tasks_order`。

**迁移**：`schema_version` 表记录 `version/description/applied_at`；MIGRATIONS 元组
（v1 = 业务表与索引；v2 = plans/plan_tasks + 任务租约列）。`initialize()`/`migrate()` 幂等，
失败回滚，v1 老库升级数据不丢。WAL + `foreign_keys=ON` + `busy_timeout` 每连接强制。

**Repository**（`ManifestRepository` 31 个成员，单一实现 `SqliteManifestRepository`）：
生命周期（`initialize/close/schema_version/status`）、任务与租约
（`save_task`/`save_task_and_event`/`load_task`/`list_tasks`/`claim_tasks`/`heartbeat`/`release_expired`/`append_event`/`list_events`）、
目录（`upsert_source`/`list_sources`/`get_source`/`save_dataset`/`get_dataset`/`save_request`/`get_request`/`save_plan`/`load_plan`/`save_asset`/`list_assets`）、
文件与校验（`save_file_record`/`load_file_record`/`list_file_records`/`save_integrity_report`/`list_integrity_reports`）、
交付（`build_delivery_manifest`/`aiter_tasks`）。

**并发模型**：每线程独立连接（`check_same_thread=True`），`BEGIN IMMEDIATE` + 指数退避重试；
领取用单条 `UPDATE ... RETURNING`，数据库层保证不重复。实测：双线程 100 轮零重复、
4 线程 100 任务零 `database is locked`。

## E. 状态机

12 态、30 条合法边、5 个终态。M1 增加的两条业务不变式：

* `RUNNING` **不能**直接到 `COMPLETED`（必须经 `VERIFYING`）；
* 进入任何终态时清空租约（`lease_expires_at = NULL`）。

关键边：

```
PENDING→PLANNED→QUEUED→RUNNING→VERIFYING→COMPLETED
                     │        ├→RETRY_WAIT→QUEUED（退避后，受 attempt 预算约束）
                     │        └→FAILED（永久错误/预算耗尽）
                     ├→PAUSED→QUEUED（用户恢复）
                     └→CANCELLED（任意非终态可取消）
VERIFYING→QUARANTINED（校验失败，终态）
FAILED→QUARANTINED 仅 after_integrity=True（事后审计，非成功路径）
```

`COMPLETED` 只能从 `VERIFYING` 进入由 `transition()` 二次强制，即使篡改转换表也会被拦。

## F. 传输能力

| 能力 | HTTP/HTTPS | S3 |
|---|---|---|
| 实现状态 | ✅ 已实现、**真实 HTTP 服务器测试 104 项** | ⚠️ 已实现、**无网络单测 20 项**；端到端 **NOT RUN** |
| 流式下载 | ✅ `iter_bytes` + 1 MiB 块 | ✅ `get_object` 顺序流式 |
| 原子落盘 | ✅ `<dest>.part` → `os.replace` | ✅ 同 |
| 断点续传 | ✅ 三重自证（206+Content-Range、ETag/Last-Modified、大小） | ✅ 同一套自证（依赖 ETag/Last-Modified） |
| 资源变化检测 | ✅ ETag/大小/Last-Modified 变化 → 整下 | ✅ 同 |
| 重试分类 | ✅ 408/425/429/5xx 可重试；401/403/404/410 永久 | ✅ ClientError 分类（403 永久、5xx/Throttling 可重试） |
| 取消 | ✅ `threading.Event`，保留可续传 `.part` | ✅ 同 |
| 有界并发 | ✅ `BoundedTransferRunner`（信号量，默认 2） | ✅ 同 |
| 超时 | ✅ connect/read 始终生效；总时长可选（默认不限） | ✅ boto3 配置超时 |
| 校验和 | ✅ 本地 SHA-256 | ✅ 本地 SHA-256；**ETag 不当 MD5** |
| multipart 并行 | — | ❌ 未实现（如实标注在 docstring） |

## G. 数据源

| | CDS | NCAR / GDEX |
|---|---|---|
| 真实连接 | ✅ 实测 200（`/api/retrieve/v1/processes/reanalysis-era5-pressure-levels`） | ✅ 实测 200（GDEX OpenAPI `/api/schema/`、1731 数据集） |
| 数据集元数据 | ✅ 真实 schema：inputs 字段、variable/pressure_level 枚举、CC-BY-4.0 | ✅ 目录/分组/webfiles（size/format/date_modified），CC-BY-4.0 |
| 资源发现 | ⚠️ 生命周期已建模（submit→poll→results），有凭证发现 **NOT RUN** | ✅ 真实 6 文件（gindex=-2）；一次真实下载 39 382 B |
| 变量→资源映射 | ✅ 按远端 long_name 匹配变量 | ❌ 无机器可读映射：要求显式 `rda.gindex`（不猜） |
| 元数据标准化 | ✅ SourceMetadata/DatasetMetadata/SourceAsset，原始值进命名空间 extensions | ✅ 同 |
| 未验证项（capabilities 显式） | area 顺序 `[north,west,south,east]` 未经官方文档复核；结果 checksum 格式；签名 URL 过期 | 目录大小未核验（实测 106 391 vs 39 382 B）；无 checksum；S3/Globus/Zarr 路由未发布 |
| 与传输解耦 | ✅ `sources` 不 import `transfer`；传输层无源字面量（AST 断言） | ✅ |

## H. 完整性验证

**六个层级**（每条报告带 `level`）：`existence` / `size` / `checksum` / `protocol` /
`format` / `scientific`。**SKIPPED 与 UNKNOWN 都不算通过**。

**checksum 策略**：
1. 数据源可信 checksum（→ `remote_checksum_verified`）；
2. 传输协议可信校验（protocol 层判定）；
3. 本地 SHA-256（→ `local_sha256_recorded`，只自证"此后未变"）。

**ETag 纪律**：S3 ETag ≠ MD5（multipart/加密场景）；默认 `verify_etag_as_md5=false`，
若 MD5 恰等于 ETag 或来源标 `remote_etag`，直接判 protocol 级 FAILED。

**失败隔离**：`quarantine/<file>.<ts>` + `.reason.json`，**不删除**；任务进 `QUARANTINED`（终态）。

**原子提交**：同文件系统 `os.replace`；EXDEV 走 `copy → fsync → 同目录 rename → size+sha256 复核 → 删源`。
提交日志 `<file>.commit.json`（`O_EXCL` 兼作同目标锁）支撑四分支恢复决策；目标已存在时
相同 → `SKIPPED_IDENTICAL`（不碰目标），不同 → `StorageError`（绝不覆盖）。

**交付清单**：`complete` 仅当纳入文件全部 PASSED；相对路径；失败/跳过/隔离保留在报告中。

## I. 测试结果

### I.1 实际命令与结果（2026-11-04 实测）

```bash
PYTHONPATH=src .venv/bin/python -m pytest
#  955 passed, 14 deselected, 1 warning in ~69s
PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" -q -m network
#  10 passed, 4 skipped  （S3 3 + CDS 有凭证 1；均 NOT RUN，无 mock 冒充）
.venv/bin/python -m ruff check .
#  All checks passed!
.venv/bin/python -m ruff format --check src tests
#  136 files already formatted
.venv/bin/python -m mypy
#  Success: no issues found in 64 source files
PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" --cov=researchdatahub
#  TOTAL 85%（8819 statements / 1137 miss / 2416 branches）
```

默认套件口径：**955 passed / 14 deselected / 0 failed**（`pytest` 一条命令即可复现）。

### I.2 分层计数

| 层/包 | 用例数 |
|---|---|
| M0 契约/状态机/配置/序列化 | 约 130 |
| `manifest` | 82 |
| `transfer` | 104 |
| `sources` | 82（+11 network） |
| `planner` | 39 |
| `scheduler` | 24 |
| `storage` | 98 |
| `integrity` | 58 |
| `reporting` | 37 |
| CLI（db/request/plan/task/file/report） | 48 |
| e2e | 5（含 4 故障注入） |
| 合计 collected | **969**（含 network）/ 955 默认运行 |

### I.3 五态统计

| 状态 | 数量 | 说明 |
|---|---|---|
| PASS | 955（默认）+ 10（network） | 全部实测通过 |
| FAIL | 0 | 最终门禁无失败 |
| SKIPPED | 4 | S3×3、CDS 有凭证×1 —— 均因环境缺失，**不计入 PASS** |
| BLOCKED | 0 | — |
| NOT RUN | 真实 ERA5 下载、S3 端到端、CDS 有凭证生命周期 | 见 §K |

> 开发期间 network 套件曾出现 1 次偶发失败（外网 502），因此把 network 从默认套件中排除
> （`addopts = "-m 'not network'"`），默认套件的红/绿只反映代码。

### I.4 真实外网链路（手工执行）

见 [`development-plan.md` §3.6](development-plan.md) 的完整命令与输出。结论：
GDEX 目录发现 6 资产 → 计划 6 任务 → 真实 HTTPS 下载 1 个文件（39 382 B）→ SHA-256
`25896d45…` → 原子提交 raw（0444）→ 报告 `completed=1 / unfinished=5 / total_bytes=39382`
→ 交付清单 `complete=true` 且注明"1/6 文件，5 任务未完成"。

## J. 性能与资源

**已测量**：

| 项 | 数值 | 方法 |
|---|---|---|
| 默认测试套件时长 | ~70 s | 真实运行（含 104 项 HTTP 传输测试） |
| `list_tasks` 1 万任务峰值内存 | **0.05 MB** | `test_list_tasks_is_lazy_and_memory_bounded`（tracemalloc） |
| 传输内存占用 | 常量级 ≈ 2 × `chunk_size_bytes`（默认 2 MiB/下载） | 流式实现 + 分块断言 |
| 并发上限 | 默认 2（`DEFAULT_WORKER_LIMIT`） | `test_bounded_concurrency_never_exceeds_max_workers`（服务端观测） |
| 真实下载吞吐 | 39 KB / ~1.7 s（含目录请求、校验、提交） | 手工 e2e，样本过小，不具代表性 |

**未测量（不编造）**：大文件（GB 级）RSS、持续吞吐、SQLite 写入延迟分布、
长时间运行稳定性。这些是 M6 的工作。

## K. 未完成事项

**未实现**
1. `CdsApiTransport`（submit→poll→download）：CDS 资产当前无法被现有传输层领取。
2. S3 multipart 并行下载。
3. `catalog` 模块（仍只有 Protocol）。
4. `priority_aging_seconds` 老化提升；`list_plans` / `plan show --request-id`。
5. FileRecord/报告/状态三写的严格单事务（现由 reconcile 修复中间窗口）。

**未经过真实服务验证（NOT RUN）**
1. **真实 ERA5 产物下载**（CDS 或 RDA 的 netCDF/GRIB），需许可与权限。
2. CDS 有凭证的完整生命周期。
3. S3 端到端（无 MinIO/S3 环境；3 项集成测试 skip）。

**已知问题**
1. CDS area 顺序（`[north, west, south, east]`）未经官方文档复核（已标 `area_order_verified=False`）。
2. NCAR 目录声明大小与实际大小可能不一致（实测 106 391 vs 39 382 B）；系统以实测字节
   与 Content-Length 为准，目录大小仅作参考。
3. `unmatched_variables` 在源未声明变量覆盖时只能标 `assumed_from_discovery`（非伪造）。
4. 单机单写者语义：多进程同写一个库不支持。

**技术债务**
1. `manifest/rows.py` 覆盖率 72%、`connection.py` 77% —— 编解码分支较多，测试可再补。
2. `transfer/base.py` 的部分防御分支不可达（覆盖率低但非缺陷）。
3. `reporting` 的 CLI 组合逻辑放在 `cli/report_cmd.py`（组合根），若报告需求变多应下沉 service。

**风险**
1. 8 GB 设备上并发 2 + NetCDF 校验同时进行时的峰值内存未实测。
2. 真实 CDS 排队时间可能远超租约（15 min）：需依赖心跳续约，但未在真实长任务上验证。
3. 外置 SSD 的 EXDEV 提交路径已测试（模拟），未在真实外置盘验证。

## L. 后续计划（M6）

1. **真实 ERA5 小规模端到端**（第一优先）：CDS 单变量 × 单日 × 全球，或 RDA 单文件，
   总下载量 < 1 GB；验证 CDS 生命周期 + 交付清单。
2. **`CdsApiTransport`**：把 CDS 的 submit/poll/download 变成传输层能力，
   同时保持 ADR-0002 边界（源提供请求参数，传输执行取数）。
3. **S3 真实集成**：MinIO 或真实桶；按需补 multipart。
4. **性能与资源**：1–10 GB 文件测 RSS/吞吐/SQLite 延迟；并发 1/2/4 对比；8 GB 机器基线。
5. **恢复与长期稳定性**：故障注入升级为真实 `kill -9` 循环；连续 6–24 小时稳定性。
6. **契约补强**：`SourceAsset.variables`（变量覆盖真实来源）、`ManifestRepository.save_completion`
   （单事务完成）。
7. **不做**（保持边界）：Web 前端、分布式调度、云端托管、科学 EDA。

---

## 附：外部参照

本次实现严格依据仓库内的 `docs/architecture.md`、`docs/data-contract.md`、ADR 与 M0 契约，
未引入外部方案。唯一的外部信息是**真实服务端点**的实测（CDS retrieve/v1 schema、
GDEX OpenAPI），用于避免硬编码过时 API。
