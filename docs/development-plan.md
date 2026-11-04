# 开发计划（Development Plan）

本文列出 **M0 已完成什么（附证据）**、**M1 要做什么（附验收标准与工时）**、**M2 展望**、
**风险与缓解**，以及**明确不做的事**。

原则：已完成项必须给出可复核的证据（文件路径或可运行命令）；未完成项必须标注所属里程碑，
不写"下一步将支持 X"而不标期。

---

## 1. 里程碑概览

| 里程碑 | 主题 | 状态 | 证据 |
|---|---|---|---|
| **M0** | 工程骨架、数据契约、状态机、模块边界、配置子系统、CLI 骨架 | ✅ 已完成 | commit `3f149a3`；§2 |
| **M1** | SQLite Manifest：schema/迁移/Repository/租约/CLI `db` | ✅ 已完成 | commit `dc533ab`；82 项测试 |
| **M2** | 传输引擎：HTTP/S3、安全续传、重试、有界并发 | ✅ 已完成（S3 集成 NOT RUN） | commit `4fdb191`、`88e8761`；104 项测试 |
| **M3** | 数据源适配：CDS + NCAR + Registry + 元数据标准化 | ✅ 已完成（CDS 有凭证链路 NOT RUN） | commit `dfa02be`；82 项 + 11 network |
| **M4** | 计划与调度：YAML 需求、计划生成、原子调度、心跳恢复 | ✅ 已完成 | commit `e5e1365`；63 项 + 5 e2e |
| **M5** | 完整性与存储：校验链、staging→raw 原子提交、交付清单/报告 | ✅ 已完成 | commit `dc533ab`、`ec32590`；193 项 |
| **M6** | 真实 ERA5 小规模端到端、性能与长期稳定性验证 | 🟡 进行中（子项 1：轻量下载效率基准 ✅） | [`M6-efficiency-benchmark.md`](M6-efficiency-benchmark.md)、[`M6-verification.md`](M6-verification.md)、[`benchmarks/m6/results.json`](../benchmarks/m6/results.json) |

---

## 2. M0 已完成清单与证据

### 2.1 数据契约层（`core`）

| # | 交付项 | 证据（文件 / 命令） | 验证结果 |
|---|---|---|---|
| 1 | 10 个 Pydantic v2 持久化模型 | [`core/models.py`](../src/researchdatahub/core/models.py)（679 行） | `pytest tests/unit/test_models.py` → 24 passed |
| 2 | 全部模型 `frozen=True` + `extra="forbid"` | [`models.py:180`](../src/researchdatahub/core/models.py#L180) | `test_all_ten_models_are_frozen` |
| 3 | `schema_version` 契约与版本守卫 | [`models.py:66`](../src/researchdatahub/core/models.py#L66)、[`models.py:102`](../src/researchdatahub/core/models.py#L102) | `test_forward_compatibility_requires_migration` |
| 4 | `scientific_identity()` 科学等价判定 | [`models.py:369`](../src/researchdatahub/core/models.py#L369) | 3 个专项测试；4 个维度实测判不等 |
| 5 | 13 个异常类 + 稳定 `code` | [`core/errors.py`](../src/researchdatahub/core/errors.py)（275 行） | 异常层次与 `to_dict()` 均有测试 |
| 6 | `NotImplementedFeatureError` 明确拒绝未实现功能 | [`errors.py:261`](../src/researchdatahub/core/errors.py#L261) | `rdh plan` 退出码 2（见 2.4） |

### 2.2 状态机（`core/state.py`）

| # | 交付项 | 证据 | 验证结果 |
|---|---|---|---|
| 7 | 12 态 `TaskState` | [`state.py`](../src/researchdatahub/core/state.py)（371 行） | 实测 `len(list(TaskState)) == 12` |
| 8 | `TRANSITIONS` 表：30 条合法边，5 个终态 | [`state.py:116`](../src/researchdatahub/core/state.py#L116) | `describe_transitions()` 实测输出 |
| 9 | `transition()` 返回新对象并维护审计字段 | [`state.py:211`](../src/researchdatahub/core/state.py#L211) | `notes` 为 `list[str]`；`attempt` 自增 |
| 10 | 业务前提检查（不只是表驱动） | `COMPLETED` 仅经 `VERIFYING`；`RETRY_WAIT→QUEUED` 预算耗尽即拒 | `test_requeue_refused_when_budget_exhausted` |
| 11 | `is_stale()` / `recover_stale()` 崩溃恢复 | [`state.py:310`](../src/researchdatahub/core/state.py#L310) / [`state.py:331`](../src/researchdatahub/core/state.py#L331) | 6 个恢复相关测试 |
| 12 | **修正**：`FAILED` 改为纯终态，`FAILED→QUARANTINED` 需 `after_integrity=True` | [`state.py:169`](../src/researchdatahub/core/state.py#L169)、[`state.py:188`](../src/researchdatahub/core/state.py#L188) | `test_failed_is_a_pure_terminal_state`、`test_failed_to_quarantined_requires_integrity_context` |
| 13 | 144 组合穷举 | [`test_state_machine_illegal.py`](../tests/unit/test_state_machine_illegal.py) | `test_matrix_is_fully_partitioned`：30 + 114 = 144 |
| 14 | `describe_transitions()` 供文档与 CLI 复用 | [`state.py`](../src/researchdatahub/core/state.py) | 本文档转换表即由它生成 |

### 2.3 模块接口（`core/ports.py`）

| # | 交付项 | 证据 | 验证结果 |
|---|---|---|---|
| 15 | 8 个 `@runtime_checkable` Protocol | [`ports.py`](../src/researchdatahub/core/ports.py)（429 行） | `test_all_eight_protocols_exist_and_are_runtime_checkable` |
| 16 | 每个 Protocol 声明并发约定 | 各 docstring | `test_protocol_docs_mention_concurrency` |
| 17 | `assert_implements()` 契约自检 | [`ports.py:407`](../src/researchdatahub/core/ports.py#L407) | 3 个测试（拒绝非 Protocol、检出缺失方法、接受真实实现） |
| 18 | 8 个真实算法的参考实现（非 mock） | [`tests/fixtures/contract_fakes.py`](../tests/fixtures/contract_fakes.py) | 16 个契约行为测试 |

### 2.4 配置子系统（`config`）

| # | 交付项 | 证据 | 验证结果 |
|---|---|---|---|
| 19 | YAML 加载 + 严格 schema | [`config/loader.py`](../src/researchdatahub/config/loader.py)（544 行）、[`config/models.py`](../src/researchdatahub/config/models.py)（452 行） | `pytest tests/unit/test_config.py` → 15 passed |
| 20 | `env:` / `${VAR}` 展开，缺失即报错并点名 | `resolve_env_refs` | 实测：缺 `CDSAPI_KEY` 时逐条列出 |
| 21 | 结构化校验结果 `ValidationResult` | [`config/models.py`](../src/researchdatahub/config/models.py) | `rdh config validate` 直接消费 |
| 22 | 配置文件 | [`configs/defaults.yaml`](../configs/defaults.yaml)、[`configs/sources.example.yaml`](../configs/sources.example.yaml) | `rdh config validate` → PASS，退出码 0 |
| 23 | 示例请求 | [`examples/era5-request.yaml`](../examples/era5-request.yaml) | `test_era5_example_yaml_parses_to_dataset_request` |

### 2.5 CLI 骨架（`cli`）

| # | 交付项 | 证据 | 验证结果 |
|---|---|---|---|
| 24 | `rdh` 入口（`researchdatahub.cli.main:app`） | [`pyproject.toml:47`](../pyproject.toml#L47)、[`cli/main.py`](../src/researchdatahub/cli/main.py) | `rdh --help` 退出码 0 |
| 25 | `rdh version` | [`cli/version_cmd.py`](../src/researchdatahub/cli/version_cmd.py) | 打印 0.1.0 / `MODELS_VERSION` 1.0 / Python / 平台 |
| 26 | `rdh config validate` | [`cli/config_cmd.py`](../src/researchdatahub/cli/config_cmd.py) | 无密钥 → 退出码 1；有密钥 → `PASS`，退出码 0 |
| 27 | `rdh source list` | [`cli/source_cmd.py`](../src/researchdatahub/cli/source_cmd.py) | 列出 cds/ncar，`implemented=False` |
| 28 | `plan`/`fetch`/`status` 显式拒绝 | [`cli/_stubs.py`](../src/researchdatahub/cli/_stubs.py) | 退出码 **2**，输出「未实现（planned for M1）」 |
| 29 | 退出码约定 0/1/2 | [`cli/_console.py`](../src/researchdatahub/cli/_console.py) | `pytest tests/unit/test_cli.py` → 12 passed |

### 2.6 文档与工程设施

| # | 交付项 | 证据 |
|---|---|---|
| 30 | 数据契约文档 | [`docs/data-contract.md`](data-contract.md) |
| 31 | 架构文档（含 mermaid + 文本降级） | [`docs/architecture.md`](architecture.md) |
| 32 | 模块边界（API 表 + import 矩阵 + 检查脚本） | [`docs/module-boundaries.md`](module-boundaries.md) |
| 33 | 测试策略 | [`docs/testing-strategy.md`](testing-strategy.md) |
| 34 | 3 篇 ADR | [`docs/adr/`](adr/) |
| 35 | README | [`README.md`](../README.md) |
| 36 | 静态检查配置（ruff / mypy strict / pytest markers） | [`pyproject.toml`](../pyproject.toml) |

### 2.7 M0 质量基线（实测命令）

```bash
# 测试：360 passed
PYTHONPATH=src .venv/bin/python -m pytest

# 覆盖率：整体 90%，core 95%
PYTHONPATH=src .venv/bin/python -m pytest --cov=researchdatahub --cov-report=term-missing

# 静态检查：All checks passed!
.venv/bin/ruff check src tests

# 类型检查：Success: no issues found in 25 source files
.venv/bin/python -m mypy src
```

| 指标 | 实测值 |
|---|---|
| 测试用例数 | **360 passed** |
| 整体覆盖率 | **90%** |
| `core` 覆盖率 | **95%** |
| mypy | strict，25 个文件无问题 |
| 源码规模 | 3602 行（`src/` 下 25 个 `.py`） |
| 测试规模 | 2334 行 |

---

## 3. M1–M5 完成情况与证据

> 与 M0 时代的"任务清单"不同，本节只写**已经做完并有实测证据**的事。
> 每条证据都能用命令复现；未验证项单独列在 §4。

### 3.1 M1 SQLite Manifest（`manifest/`）

| 能力 | 证据 |
|---|---|
| 10 张表（sources/datasets/requests/assets/tasks/files/transfer_logs/integrity_reports/plans/plan_tasks）+ schema_version | `manifest/schema.py`；`test_manifest_schema.py` |
| 版本化迁移 v1→v2，幂等、失败回滚、数据不丢 | `test_migrate_v1_database_upgrades_and_keeps_rows` |
| WAL + `foreign_keys=ON` + `busy_timeout` 每连接强制 | `test_initialize_sets_wal_foreign_keys_and_busy_timeout` |
| 每线程独立连接（跨线程使用真实报错） | `test_connection_cannot_be_used_from_another_thread` |
| 原子领取：双线程 100 轮无重复 | `test_concurrent_claim_never_assigns_same_task_twice` |
| 4 线程写 100 任务零 `database is locked` | `test_four_threads_write_one_hundred_tasks_without_lock_errors` |
| 租约过期三分支（续租/接管/失败） | `test_manifest_leases.py`（6 项） |
| 状态+事件同事务（TRIGGER 注入故障后同时回滚） | `test_save_task_and_event_is_atomic_under_injected_failure` |
| 1 万任务惰性迭代峰值内存 0.05 MB | `test_list_tasks_is_lazy_and_memory_bounded` |
| CLI `db init/status/migrate`（幂等、建存储目录、缺库报错） | `tests/unit/test_cli_db.py`（13 项） |

### 3.2 M2 传输引擎（`transfer/`）

| 能力 | 证据 |
|---|---|
| httpx 流式下载 + `.part` 原子替换 | `test_destination_only_appears_after_atomic_replace` |
| 断点续传三重自证（Range/206/Content-Range + ETag 或 Last-Modified + 大小） | `test_resume*.py`（33 项） |
| 资源变化（同 URL 但 ETag/大小变）→ 拒绝续传、整下 | `test_etag_change_forces_full_redownload` 等 3 项 |
| 状态码分类：401/403/404/410 permanent；408/425/429/5xx retryable | `test_*_is_permanent_*`、`test_429_honours_retry_after_header` |
| 重试次数与 `retries_used` 真实计数 | `test_500_is_retried_then_succeeds` 等 |
| 取消保留可续传的 `.part` | `test_cancel_mid_stream_keeps_resumable_partial` |
| 有界并发（服务端观测 max_concurrent==2） | `test_bounded_concurrency_never_exceeds_max_workers` |
| 总超时可选（`null`=不限制），读超时始终兜底 | `test_large_stream_not_aborted_by_total_deadline`、`test_read_timeout_still_active_without_total_timeout` |
| S3：ETag 不当 MD5、ClientError 分类、流式取对象 | `test_s3.py`（20 项，无网络） |
| S3 端到端 | **NOT RUN**（无 MinIO/S3，未用 mock 冒充） |

### 3.3 M3 数据源适配（`sources/`）

| 能力 | 证据 |
|---|---|
| CDS 真实 retrieve/v1 schema（实测 2026-11-04：inputs 字段、variable enum、pressure_level enum） | `tests/integration/test_sources_cds_network.py`（network） |
| 请求翻译（变量/层/日期/area/format），units 不猜 | `test_cds_translation.py` |
| 服务端异步生命周期独立建模（submit → poll → results） | `CdsRequestLifecycle` + `test_cds_adapter.py` |
| 认证缺失 → 明确 unavailable，不泄密 | `test_cds_adapter.py`（密钥不出现在日志/异常/capabilities） |
| NCAR/GDEX 真实目录 API（1731 数据集、分组、webfiles） | `test_sources_ncar_network.py` + 真实 fixtures 快照 |
| 未验证能力在 `capabilities()` 显式列出 | `ncar.py::capabilities`、`cds.py::capabilities` |
| Source/Transport 解耦（AST 断言 + 无源字面量） | `test_decoupling.py` |
| Registry 工厂（无按源 if/else） | `test_registry.py` |

### 3.4 M4 计划与调度（`planner/`、`scheduler/`）

| 能力 | 证据 |
|---|---|
| §6.2 分段 YAML + 扁平格式兼容；`文件:行` 定位 | `test_request.py`（16 项） |
| 日期区间闭开语义（`end=2011-12-31` → 次日 00:00Z） | `test_end_date_is_expanded_to_next_midnight_utc` |
| units 缺失 → `unknown` + 警告，不猜测 | `test_string_variable_units_are_not_guessed` |
| 源匹配顺序、`max_sources`、部分覆盖补源 | `test_plan.py` |
| 拆分 file/year/month/request + `max_tasks_per_plan` | `test_generated_source_splits_by_year_and_keeps_request_lifecycle` 等 |
| 去重、`unmatched_variables`、`allow_partial=false` | 3 项 |
| 计划确定性（plan_id/task_id 可复现） | `test_plan_id_and_task_ids_are_deterministic_and_serializable` |
| 调度：原子领取、默认并发 2、失败分类、退避 | `test_scheduler/`（24 项） |
| 心跳续约；租约丢失立即停机且不再写状态 | `test_heartbeat_renews_lease_during_long_transfer`、`test_lease_loss_stops_worker_without_writing_state` |
| 崩溃恢复：`release_expired`（续租 vs 接管）、重启后续跑 | `test_scheduler_recovery.py` |
| 崩溃窗口对账：文件已提交/DB 未更新、孤儿文件、坏 checksum | `test_committed_file_without_db_record_is_reconciled` 等 4 项 |
| 暂停/恢复/取消/重试（状态机强制） | `test_control.py`（7 项） |
| 端到端（真实 HTTP + 真实 SQLite + tmp 存储）+ 4 个故障注入 | `tests/integration/test_e2e_pipeline.py`（5 项） |

### 3.5 M5 完整性与存储（`integrity/`、`storage/`、`reporting/`）

| 能力 | 证据 |
|---|---|
| 六层检查链（existence/size/checksum/protocol/format/scientific） | `test_checks.py`、`test_checksum_provenance.py` |
| 正常/损坏/截断 NetCDF 真实检查；依赖缺席 → SKIPPED | `test_netcdf.py` |
| ETag 当 MD5 → protocol 级 FAILED | `test_checksum_provenance.py` |
| 同文件系统 `os.replace`；EXDEV copy+fsync+rename+复核 | `test_atomic_commit.py` |
| 目标已存在：相同跳过（不碰目标）、不同拒绝覆盖 | `test_publish.py` |
| 提交日志 + 四分支恢复决策表 | `test_recovery.py`（storage 侧） |
| 路径穿越/非法文件名/符号链接防护 | `test_path_safety.py`（48 项） |
| 隔离保留文件并写 `.reason.json` | `test_quarantine_archive.py` |
| 交付清单：`complete` 仅全 PASSED；相对路径；失败/跳过/隔离全留 | `test_delivery.py` |
| 报告：成功/失败/跳过/隔离计数与 JSON 导出 | `test_report.py` |
| CLI `file verify` / `report generate|show` | `test_cli_file.py`(9)、`test_cli_report.py`(9) |

### 3.6 真实外网链路（手工执行，2026-11-04）

在 `/tmp/rdh-live`（临时 state，不写 `~/.rdh`）执行，命令与结果：

```bash
rdh request validate /tmp/rdh-live/request.yaml          # 0：分段格式校验通过
rdh plan create ... --sources ncar                     # 0：真实 GDEX API 发现 6 个资产
rdh plan show plan-b86645b29fe41656                    # 0：6 个 PLANNED 任务
rdh task run <plan> --max-tasks 1 --max-workers 1      # 0：completed=1
rdh db status                                          # COMPLETED=1, QUEUED=5, 可交付请求
rdh report generate ncar-live-smoke                    # 0：报告 + 交付清单
```

结果：`raw/era5-pressure-levels/d633000-osdf.csv`（39 382 B，`-r--r--r--`），
SHA-256 `25896d45…`，交付清单 `complete=true` 且注明"1/6 个文件、5 个任务未完成"，
报告计数 `discovered=6 / completed=1 / unfinished=5 / total_bytes=39382`。

**边界必须说清**：这是 GDEX 的**公开目录 CSV 文件**，不是 ERA5 产物；真实 ERA5
（CDS 或 RDA 的 netCDF/GRIB 产物）下载 **NOT RUN**，需账号许可与访问权限。

### 3.7 质量门（实测）

```bash
PYTHONPATH=src .venv/bin/python -m pytest
# 955 passed, 14 deselected (network), 0 failed, 1 warning
PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" -q -m network
# 10 passed, 4 skipped（S3 3 + CDS 有凭证 1；均 NOT RUN，无 mock 冒充）
.venv/bin/python -m ruff check .        # All checks passed!
.venv/bin/python -m ruff format --check src tests   # 136 files already formatted
.venv/bin/python -m mypy                # Success: no issues found in 64 source files
PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" --cov=researchdatahub
# TOTAL 85%（8819 statements, 1137 miss, 2416 branches）
```

规模：`src/` 64 个 Python 文件 24 034 行；`tests/` 15 407 行；测试用例 969 个（含 network）。

### 3.8 M6 轻量下载效率基准（2026-11-04 实测）

三条件均为真实网络执行（CDS 个人令牌 + NCAR GDEX 公开访问），同一逻辑需求：
ERA5 2t，2024-01-01..03 逐时，30–45°N/100–125°E，NetCDF。

| 条件 | 结果 | 关键证据 |
|---|---|---|
| 纯 CDS API | 3 天请求 45 min 未就绪（拥塞期，下界 ≥2 700 s）；**降量到 2°×2°×1 时次后 30.2 s 完成**（25 116 B，含生成 28.71 s + 下载 1.49 s） | `benchmarks/m6/results.json`；产物 xarray 校验 |
| 纯 NCAR GDEX | 端到端 113.6 s，下载 1 134 576 716 B，10.5 MB/s | 目录声明与 HEAD 一致；SHA-256 `e619fc57…9b04` |
| 混合制（双源对冲） | 先就绪 112.55 s（winner=NCAR）；CDS 侧未就绪 | `results.json` `runs[mode=mixed].validation` |

产物校验确认拿到的是真 ERA5（`VAR_2T`/2t，2024-01 全月逐时，全球网格；CDS 侧区域裁剪正确），
并顺带**消除了一个历史未验证项**：CDS `area` 数组顺序 `[N,W,S,E]` 由真实产物反证
（非对称 bbox `[45,100,30,120]` → `latitude 30..45`、`longitude 100..120`），
`sources/cds.py` 的 `area_order_verified` 已据此置为 `True`。

报告：[`M6-efficiency-benchmark.md`](M6-efficiency-benchmark.md)；验收：[`M6-verification.md`](M6-verification.md)。

## 4. 遗留问题与 M6 建议

| # | 遗留 | 影响 | 建议 |
|---|---|---|---|
| 1 | 真实 ERA5 产物下载 NOT RUN | 核心场景未端到端验证 | M6 第一优先：CDS 小样本（1 变量 × 1 天 × 全球）或 RDA 单文件，限制 < 1 GB |
| 2 | ~~CDS 执行断点~~ **已实现（2026-11-04）**：新增传输适配器 `CdsApiTransport`（`access_route=cds_api`，复用 `HttpTransportAdapter` 的流式/`.part`/续传/校验/重试语义，只覆写路由判别与 `transport_id`；`build_transports()` 已装配）。剩余：调度器内真实 CDS 端到端 **NOT RUN**（需凭证 + 实际排队）；临时签名 URL 过期不自动 re-resolve（401/403 → 不可重试 FAILED，需重新 plan 换新 URL） | 原 "no transport" 失败已消除；真实链路仍未端到端验证 | 有 CDS 凭证时跑一次 `rdh plan create` + `rdh task run` 做真实端到端；若产品上需要过期自动续签，再在调度层加 re-resolve |
| 3 | S3 端到端 NOT RUN、multipart 未实现 | 大文件 S3 下载只有单流 | M6 用真实 S3/MinIO 跑集成；需要时补 multipart（复用同一 `.part` 自证语义） |
| 4 | `unmatched_variables` 依赖源声明变量覆盖（现标注 `assumed_from_discovery`） | 变量级覆盖率不精确 | 契约增加 `SourceAsset.variables`，由 adapter 真实填充 |
| 5 | FileRecord/报告/状态非严格单事务（三次写） | 中间窗口靠 reconcile 修复 | 给 `ManifestRepository` 加复合写方法（`save_completion`） |
| 6 | `priority_aging_seconds` 未实现 | 低优先级可能长期等待 | M6 调度策略补老化提升 |
| 7 | `plan show --request-id`、`task run --plan 之外按请求聚合` | 运维便利性 | 增加 `list_plans` |
| 8 | 性能未测（并发/内存/大文件） | 8 GB 设备实际表现未知 | M6 用 1–10 GB 文件测 RSS、吞吐、SQLite 写入延迟 |
| 9 | 多进程共享一个 `~/.rdh` 未支持（单写者语义） | 误用会得到 locked 错误 | 文档已声明；M6 可加单实例锁 |

## 5. 风险与缓解

| # | 风险 | 影响 | 可能性 | 缓解措施 |
|---|---|---|---|---|
| R1 | **CDS 请求排队不可控**（几分钟到数小时） | `fetch` 长时间无进展，用户以为卡死 | 高 | 显式状态 `RETRY_WAIT` + 进度输出；文档写明预期时长；M1 引入心跳日志 |
| R2 | **远端限流**（RDA 30 次/分，CDS 有配额） | 大量任务时被拒 | 高 | `per_source_limit` 配置已预留；429 → `RetryableTransportError` + 尊重 `Retry-After` |
| R3 | **`netCDF4` 在 Apple Silicon 上的 wheel/链接问题** | 科学校验不可用 | 中 | `[sci]` 设为可选；未装时 `netcdf_openable` 返回 `UNKNOWN` 而非 PASSED；文档给出 `h5netcdf` 备选 |
| R4 | **`FAILED` 语义再次漂移**（终态 vs 有出边） | 状态机自相矛盾，文档与代码脱节 | 中 | 已用 `after_integrity=True` 收口；`test_failed_is_a_pure_terminal_state` 守护；文档表由 `describe_transitions()` 生成 |
| R5 | **模块边界漂移**（跨模块 import） | 变成隐式状态机，难以测试 | 中 | 依赖矩阵 + `grep` + 检查脚本；M1 接入 CI |
| R6 | **SQLite 并发写** | `database is locked`，任务卡住 | 中 | 单写者队列；M1-1 验收要求 4 线程 100 任务零冲突 |
| R7 | **`cell_methods` 未纳入科学判等** | 统计含义不同的数据被判等价 | 低 | 已在 [`data-contract.md` §9.4](data-contract.md) 显式标注为已知局限；由 `time_definition` 承载主要约定 |
| R8 | **旧 venv 的可编辑安装损坏** | `rdh` 报 `ModuleNotFoundError`，但 `pip show` 显示已安装 | 低 | 根因已证伪并修正（详见 §7）：不是 `.pth` 命名问题，是 venv 自身损坏；重建 venv 即修复，已实测 |
| R9 | **大文件内存占用** | 校验时 OOM | 低 | 校验按块流式计算（`Checksum.of_file` 已是 1 MB 分块）；M1-4 验收含 2 GB 文件用例 |
| R10 | **文档与代码漂移** | 用户按文档操作失败 | 中 | 每次改 `TRANSITIONS` 跑 `describe_transitions()` 比对；文档命令均在写作时实测 |

---

## 6. 明确不做的事

以下项目**不是**"以后再做"，而是**设计上排除**：

| 不做 | 理由 |
|---|---|
| 微服务拆分 | 无可独立伸缩的组件；引入分布式事务（[ADR-0001](adr/ADR-0001-modular-monolith.md)） |
| Kafka / RabbitMQ / Redis 队列 | 量级不匹配；至少一次语义会制造重复下载（ADR-0001） |
| Celery | 单机 I/O 密集，线程池足够；与状态机重试语义冲突（ADR-0001） |
| PostgreSQL | 部署门槛违背"pip install 即用"（ADR-0001） |
| Kubernetes / 容器编排 | 同上 |
| Web UI / HTTP API | M0/M1 只有 CLI；无托管服务计划 |
| 云同步 / 账号体系 | 本地优先 |
| 默认做重网格化 / 单位换算 / 插值 | 任何加工产物必须是**新**对象并保留来源（README §1） |
| 覆盖式修改原始数据 | `Preserve Original Data` |
| Windows 平台支持 | 不在当前支持范围 |
| 多进程同时写同一个 Manifest | 明确不支持；跨进程并发需另设计 |
| 在 M0 假装支持 `plan`/`fetch`/`status` | 必须以退出码 2 明确拒绝 |

---

## 7. 已知环境问题与排查记录

### 7.1 现象

开发过程中曾出现：`pip install -e .` 退出码 0、`pip show researchdatahub` 正常、
`.venv/bin/rdh` 存在，但执行时报 `ModuleNotFoundError: No module named 'researchdatahub'`。

### 7.2 排查与结论（**结论已被实验修正**）

初次假设（**错误，已被证伪**）：go editable install 生成的 `.pth` 文件名以 `_` 开头
（`_editable_impl_researchdatahub.pth`），被 `site` 模块跳过。

证伪实验：彻底删除并重建 `.venv` 后重新 `pip install -e ".[dev]"`，生成的
**仍是同名** `_editable_impl_researchdatahub.pth`，而 `import researchdatahub` 与
`.venv/bin/rdh version` **均正常**。

| 实验 | 结果 |
|---|---|
| 损坏的旧 venv（同名 `.pth`，内容正确） | `import researchdatahub` → `ModuleNotFoundError` |
| 全新 venv（同名 `.pth`，同样内容） | `import researchdatahub` → 成功，`rdh version` 退出码 0 |

两者 `.pth` 文件名与内容**完全一致**，唯一差异是 venv 本身的状态 →
**根因是旧的 venv 损坏**，与 `.pth` 命名无关。

**损坏来源**（可复现，已确认）：M0 早期 `pip install -e .` 因 `README.md` 缺失而
在 metadata 阶段失败；多次失败/中断的安装会留下不完整的可编辑安装状态。此外该 venv
内混入了 Finder 生成的 `.DS_Store`（导致 `rm -rf .venv` 报 `Directory not empty`），
进一步说明该目录已被非包管理工具污染。

### 7.3 修复方式

```bash
# 推荐：删掉重建（幂等，不影响源码与 git 状态）
chmod -R u+w .venv && rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/rdh version   # 应为退出码 0
```

临时绕过（不做安装修复时）：

```bash
PYTHONPATH=src .venv/bin/python -m researchdatahub.cli.main --help
```

### 7.4 给验收者的建议

验收「项目能正常安装」时，**不要只看 `pip install` 的退出码**，必须实际执行
`rdh version` 与 `python -c "import researchdatahub"`。前者的 0 只代表 wheel 装上了，
不代表可导入。这也是 `tests/integration/test_cli_install.py` 用真实子进程而非
`CliRunner` 的原因。

---

## 8. 相关文档

- 架构与数据流：[`docs/architecture.md`](architecture.md)
- 数据契约：[`docs/data-contract.md`](data-contract.md)
- 模块边界：[`docs/module-boundaries.md`](module-boundaries.md)
- 测试策略：[`docs/testing-strategy.md`](testing-strategy.md)
