# 开发计划（Development Plan）

本文列出 **M0 已完成什么（附证据）**、**M1 要做什么（附验收标准与工时）**、**M2 展望**、
**风险与缓解**，以及**明确不做的事**。

原则：已完成项必须给出可复核的证据（文件路径或可运行命令）；未完成项必须标注所属里程碑，
不写"下一步将支持 X"而不标期。

---

## 1. 里程碑概览

| 里程碑 | 主题 | 状态 |
|---|---|---|
| **M0** | 工程骨架、数据契约、状态机、模块边界、配置子系统、CLI 骨架 | ✅ 已完成 |
| **M1** | 首个端到端可用链路：SQLite Manifest、S3/HTTPS 传输、NCAR 发现、完整性检查、`plan`/`fetch`/`status` | ⬜ 未开始 |
| **M2** | CDS 异步源、多源降级、报告生成、并发调度优化 | ⬜ 未开始 |

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

## 3. M1 任务清单

M1 目标：**一条真实可用的端到端链路**——用户能用 `rdh fetch` 真的把一份 NCAR 的 ERA5 数据
下载下来、校验通过、并得到一个可移植的交付清单。

> 工时按 1 名熟悉本仓库的开发者估算，单位为"人日"。含自测，不含评审与发布。

### M1-1 SQLite Manifest 实现（估 3 人日）

**做什么**：实现 `ManifestRepository`，表结构、迁移、单写者队列、WAL。

**验收标准**：
- [ ] `initialize()` 幂等：连续调用两次不报错，表结构不重复创建
- [ ] 每 worker 独立连接；并发 4 线程写 100 个任务，**零** `database is locked`
- [ ] `save_task(expected_state=...)` 冲突时抛 `ManifestError`
- [ ] `save_task` + `append_event` 在同一事务：注入故障后二者要么都生效要么都不生效
- [ ] `list_tasks(state=FAILED)` 惰性迭代，1 万个任务时内存增量 < 10 MB
- [ ] `build_delivery_manifest` 在存在非 `PASSED` 报告时抛 `ManifestError`
- [ ] 迁移测试：v1 schema 的库能被 `initialize()` 升级，数据不丢

### M1-2 传输层：S3 + HTTPS（估 4 人日）

**做什么**：`S3Transport`、`HttpsTransport`，含断点续传、重试、原子落盘。

**验收标准**：
- [ ] 原子落盘：下载中途 kill 进程，目标路径**不出现**半成品文件（只有 `.part`）
- [ ] 断点续传：中断后重启，从已有字节继续，最终校验和正确
- [ ] 429/5xx/超时 → `RetryableTransportError`；404/403 → `PermanentTransportError`
- [ ] `supports()` 只依据 `access_route` 与 URI scheme
- [ ] **反向验证**：`grep -rniE "ncar|cds|pangeo" src/researchdatahub/transfer/` 无输出
- [ ] 离线测试全部通过（用 `LocalCopyTransport` 或本地 `http.server`）

### M1-3 数据源发现：NCAR（估 4 人日）

**做什么**：`NcarRdaSource`，实现目录查询与 `SourceAsset` 生成。

**验收标准**：
- [ ] `discover_assets(request)` 能把 `DatasetRequest` 映射为文件列表，带 `origin_uri`/`size_bytes`
- [ ] 远端元数据不可解析时抛 `MetadataError`，不返回半成品资产
- [ ] 变量无法匹配时记入 `unmatched_variables`，不静默丢弃
- [ ] `health_check()` 不下载数据，超时返回 `False` 而非抛异常
- [ ] 并发调用 `discover_assets` 无竞争（4 线程 × 20 次）

### M1-4 完整性校验（估 3 人日）

**做什么**：`DefaultIntegrityChecker`，实现 7 种检查中的前 5 种。

**验收标准**：
- [ ] `size_match`、`checksum_match`、`format_sniff` 全覆盖
- [ ] `netcdf_openable`、`variable_present` 在装 `[sci]` 时生效，未装时返回 `UNKNOWN`（**不是 PASSED**）
- [ ] 校验失败返回 `FAILED` 报告（不抛异常）；文件缺失才抛 `IntegrityError`
- [ ] `PASSED` 报告不含 `failures`；`FAILED` 报告至少一条 `failures`
- [ ] 构造一个 2 GB 截断文件，`size_match` 必失败

### M1-5 存储后端（估 2 人日）

**做什么**：`LocalStorageBackend`，四区（tmp/data/quarantine/archive）+ 原子移动。

**验收标准**：
- [ ] `publish_original` 原子，跨设备时要么成功要么抛 `StorageError`（不留半成品）
- [ ] 原始区文件权限为只读
- [ ] `quarantine` **保留字节**（不删除），文件名带原因摘要
- [ ] `free_bytes()` 反映真实卷容量，低于 `free_space_floor_gb` 时调度器能感知

### M1-6 计划器与调度器（估 5 人日）

**做什么**：`DefaultPlanner`、`ThreadPoolScheduler`。

**验收标准**：
- [ ] 计划生成的任务初始状态为 `PLANNED`，`plan_id` 全部一致
- [ ] 按 `Priority.rank` 出队；同优先级 FIFO
- [ ] `per_source_limit` 生效：单源并发不超过配置值
- [ ] 状态推进全经 `transition()`；`grep` 无 `task.state = ` 直接赋值
- [ ] 重试：`attempt < max_attempts` 进 `RETRY_WAIT`，退避时间符合 `backoff_seconds * 2^(n-1)` 且不超 `backoff_max_seconds`
- [ ] 崩溃恢复：kill 进程后重启，过期任务被 `recover_stale()` 重排

### M1-7 CLI：`plan` / `fetch` / `status`（估 3 人日）

**做什么**：三个命令的真实实现，替换 `_stubs.py`。

**验收标准**：
- [ ] `rdh plan --request examples/era5-request.yaml` 输出计划摘要（任务数、总字节估算、未满足变量），退出码 0
- [ ] `rdh fetch --request ...` 执行并落库，进度可见，失败任务列出原因
- [ ] `rdh status --request-id ...` 从 SQLite 读状态，不扫描目录
- [ ] 三命令在无配置/无密钥时给出可操作的中文错误，退出码 1
- [ ] 删除 `NotImplementedFeatureError` 的对应注册（保持"未实现即拒绝"的语义不被削弱）

### M1-8 交付清单与最小报告（估 2 人日）

**验收标准**：
- [ ] `build_delivery_manifest` 产出的清单 `complete=True` 时全部报告为 `PASSED`
- [ ] `total_bytes` 与文件之和一致
- [ ] 输出一份 Markdown 报告：请求、源、文件清单、校验结果、失败原因
- [ ] 清单可被 `DeliveryManifest.model_validate()` 无损反序列化

### M1 汇总

| 任务 | 工时 |
|---|---|
| M1-1 SQLite Manifest | 3 |
| M1-2 S3 + HTTPS 传输 | 4 |
| M1-3 NCAR 发现 | 4 |
| M1-4 完整性校验 | 3 |
| M1-5 存储后端 | 2 |
| M1-6 计划器与调度器 | 5 |
| M1-7 CLI 三命令 | 3 |
| M1-8 交付清单与报告 | 2 |
| **合计** | **26 人日** |

依赖顺序：M1-1 → M1-5 → M1-2 →（M1-3 ∥ M1-4）→ M1-6 → M1-7 → M1-8。

---

## 4. M2 展望

以下为方向性规划，**未做详细分解**，不应作为承诺。

| 方向 | 内容 | 前置条件 |
|---|---|---|
| CDS 异步源 | `CdsApiSource` + 请求排队轮询 + 临时 URL 过期重试 | M1 完成；`[cds]` extras |
| 多源降级 | 一个源失败时自动切到另一个源的等价资产（依赖 `scientific_identity()` 判等） | M1 的 NCAR 与 CDS 均可用 |
| 科学内容校验 | `time_coverage`、`metadata_consistent`，需要 `[sci]` | M1-4 落地 |
| 报告增强 | HTML / 机器可读（JSON Schema）报告 | M1-8 |
| 缓存与去重 | 基于 `scientific_identity()` + 校验和避免重复下载 | M1-1 |
| 归档与清理 | 归档区策略、保留期、审计日志 | M1-5 |

---

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
