# 仓库审计（M0）

> 审计时间：2026-11-05　审计人：Lead（首席架构师）
> 审计对象：`https://github.com/Neiyako/ResearchDataHub` @ `b78fe55`（main，工作区干净）
> 目的：对照《完整工程实现总指令》，判定**保留 / 修改 / 删除 / 新增**，输出功能差距清单。
> 原则：先审计再重构（指令 §16 M0），已完成项必须给出可复跑的证据。

---

## 0. 一句话结论

仓库**不是**空项目。它已经是一个**工程质量相当高、测试很扎实的本地 CLI 数据采集框架**
（979 通过 / 14 network 跳过 / 0 失败，rc=0；65 个源文件 24,109 行；74 个测试文件）。
但它距离指令要求的产品形态还有**结构性差距**，且这些差距是"缺一整层"，不是"补几个函数"：

| 差距 | 严重程度 | 说明 |
|---|---|---|
| **没有 MCP Server** | 🔴 阻断 | 全仓 `grep -rn "mcp"` = 0 命中。产品正式入口完全缺失 |
| **没有 Agent Skills** | 🔴 阻断 | 无 `skills/` 目录 |
| **没有环境配置系统** | 🔴 阻断 | 无 `environment/` 包，凭据缺失只能靠 config 报错，无引导 |
| **没有 Provider 契约层** | 🔴 阻断 | 只有 2 个 `SourceAdapter`（CDS/NCAR），缺 NASA Earthdata / Copernicus Dataspace / GEE |
| **没有 Worker/Route 体系** | 🟠 重要 | 只有 `scheduler` 内的本地线程池；无 Worker 注册/心跳/健康、无 Route 选择、无链路性能记录 |
| **状态机与指令不一致** | 🟠 重要 | 现有 12 态是**传输任务**状态机；指令要求的是**需求生命周期**状态机（CREATED→DISCOVERING→…→COMPLETED） |
| **README 定位与指令不符** | 🟡 文档 | README 自称"多源科研数据采集与同步框架、CLI 优先"，指令要求"Agent-native 数据获取基础设施、MCP 优先" |

**结论：保留执行层（质量高、实测过），补齐契约层与 Agent 接入层，不重写。**

---

## 1. 环境探针（审计先验证工具链可用）

| 项 | 值 | 证据 |
|---|---|---|
| Python | 3.13.13（`.venv`，基于 `/opt/miniconda3`） | `.venv/bin/python -V` |
| 依赖 | `[dev,cds,sci]` 已装（pydantic / typer / rich / httpx / boto3 / cdsapi / xarray / netCDF4） | `.venv/bin/pip show` |
| 基线测试 | **979 passed, 14 deselected, 0 failed, rc=0** | `.venv/bin/python -m pytest --collect-only` → `979/993 tests collected (14 deselected)`；`pytest --tb=no -q` → rc=0 |
| Git | 分支 `main`，远端 `origin = github.com/Neiyako/ResearchDataHub.git`，工作区干净 | `git status --short` 空 |

### 1.1 审计中发现并修复的环境缺陷（与产品代码无关）

**现象**：初始基线 6 个 `tests/integration/test_cli_install.py` 失败，报
`ModuleNotFoundError: No module named 'researchdatahub'`，但 `.venv/bin/python -c "import researchdatahub"` 在仓库根目录能成功。

**根因**（用 `python -v` 定位）：该 conda Python 的 `site.py` 会
`Skipping hidden .pth file: '_editable_impl_researchdatahub.pth'` — **跳过所有以 `_` 开头的 `.pth`**，
这是 conda 的补丁行为。因此 `pip install -e .` 生成的 editable hook 永远不生效；
"在仓库根目录能 import" 只是因为 cwd 在 `sys.path` 里。

**修复**：改为安装真实 wheel + 把 site-packages 里的 `researchdatahub` 换成指向 `src/researchdatahub` 的符号链接
（开发期改源码立即生效，且子进程/console script 都能 import）。修复后基线全绿。
> 这是**本机 venv** 的问题，不是仓库缺陷；CI（GitHub Actions）用标准 CPython，不受影响。
> 已记录，避免后续成员误判为代码 bug 重复排查。

---

## 2. 现有资产清单（逐模块）

### 2.1 已实现且质量可接受 —— **保留，不重写**

| 模块 | 行数 | 职责 | 审计结论 |
|---|---:|---|---|
| `core/models.py` | ~700 | 10 个 Pydantic v2 契约模型，`frozen` + `extra="forbid"` + `schema_version` | ✅ 保留。质量高，是本项目最值钱的资产 |
| `core/errors.py` | 275 | 16 个异常类，结构化 `code`/`detail`/`to_dict()` | ✅ 保留，可扩展 |
| `core/state.py` | 371 | 12 态**传输任务**状态机 + 144 组合穷举测试 + 租约/心跳/崩溃恢复 | ✅ 保留，但需**并列新增**需求生命周期状态机（见 §4） |
| `core/ports.py` | 429 | 8 个 `@runtime_checkable` Protocol | ✅ 保留，新增层复用其风格 |
| `config/` | 1,129 | YAML 加载、`env:` 展开、严格校验、`FileValidation` 定位到行 | ✅ 保留（M2 环境系统要复用其 `env:` 引用语义） |
| `manifest/` | ~1,600 | SQLite（WAL + foreign_keys）、单写者、租约、心跳、原子领取、恢复 | ✅ 保留。ADR-0003 决策正确 |
| `transfer/` | ~3,000 | httpx 流式下载、`.part` 三重自证续传、重试分类、S3、CDS API 传输 | ✅ 保留。有真实实测数据支撑 |
| `sources/` | ~2,300 | CDS + NCAR GDEX 适配器、Registry、元数据标准化（"远端没给的不推断"） | ✅ 保留。**真实下载实测过**（1.13 GB NCAR / CDS 30.2 s） |
| `planner/` | ~1,800 | YAML 需求 → 计划、源匹配、拆分、去重、未满足变量登记 | ✅ 保留 |
| `scheduler/` | ~2,700 | 原子领取、线程池、优先级、退避、恢复、对账 | ✅ 保留（M4 的 Local Worker 将**适配**它而非重写） |
| `integrity/` | 1,325 | 六层校验链（存在/大小/SHA-256/格式/NetCDF/科学语义） | ✅ 保留。直接服务 M5 `verify_dataset` |
| `storage/` | ~600 | staging→raw 原子提交、隔离区、只读保护、跨卷处理 | ✅ 保留 |
| `reporting/` | ~900 | 交付清单 + 采集报告 + JSON 导出 | ✅ 保留。直接服务 M5 `get_manifest` |
| `cli/` | ~1,900 | `rdh version/config/db/request/plan/task/file/report/source` | ✅ 保留（作为运维面，MCP 是 Agent 面，两者共存） |

**判断依据**：这些模块有 144 组合穷举的状态机测试、979 个通过的测试、真实网络实测记录、
以及对"不得推断远端事实"这类关键不变量有明确编码。重写它们违反指令 §17.12。

### 2.2 需要修改

| 对象 | 问题 | 处置 |
|---|---|---|
| `README.md` | 定位写成"多源科研数据采集与同步框架 · CLI 优先"，与指令要求的产品定位不符；且"979 passed / 85% coverage"等数字需在新增模块后重新核实 | 重写第 1 节定位与能力表，**只写已实现能力** |
| `docs/development-plan.md` | 只有 M0–M6，无 M0–M8 全景、无差距清单、无 roadmap | 新增 `docs/roadmap.md`，保留原文件作历史记录 |
| `pyproject.toml` | `requires-python = ">=3.12"`，指令要求 3.11+ | **保留 3.12**（理由：现有代码大量使用 PEP 695 `type` 语句与 `StrEnum`，3.11 需改代码换收益；ADR 记录） |
| `docs/adr/` | 6 个 ADR 都在 | 新增 `docs/decisions/` 目录（指令要求 `docs/decisions/`），并把新增决策写进去 |

### 2.3 明确**未**实现（= 本次要补的）

- MCP Server 与 18 个 MCP Tools（零）
- 4 个 Agent Skills（零）
- `environment/` 环境检查与配置引导（零）
- `providers/` Provider 契约 + NASA Earthdata + Copernicus Dataspace + GEE（零）
- `network/` Worker 注册/心跳/健康 + Route 契约 + 选路 + 链路性能记录（零）
- 需求生命周期状态机（`CREATED`→`DISCOVERING`→…→`COMPLETED` + 5 个异常态）
- 数据请求契约 v1.1（指令 §10 的 `schema_version` + `selection.temporal.frequency` + `bounds` 顺序）
- M7 六个端到端场景（A–F）

---

## 3. 功能差距清单（对照指令逐条）

| 指令条款 | 要求 | 现状 | 差距 | 责任里程碑 |
|---|---|---|---|---|
| §4 L1 | MCP Server / Tools / Schemas / Skills / 环境引导 / 认证 | 仅 CLI | **全缺** | M2, M5, M6 |
| §4 L2 | Discovery / Metadata / Assessment / Request Builder / Validator / Planner / Provider Selection / Route Selection / Orchestration / Verification | `planner`+`scheduler` 有部分；Discovery/Assessment/Route Selection 缺 | 部分 | M1, M3 |
| §4 L3 | Provider Contract + 能力声明（10 项能力） | 仅 `AdapterCapabilities`（7 字段，且是 sources 私有） | 部分 | M3 |
| §4 L4 | Worker Registry/Manager、Route Manager、Scheduler、Transfer、Retry、Queue、Remote Worker | 仅本地线程池 | **大部分缺** | M4 |
| §4 L5 | Metadata Registry / SQLite Index / Task DB / Manifest / Checksum / File Registry / Logs / Config / Credential Refs | 基本齐（manifest + integrity + config）；Credential Refs 缺 | 小 | M1, M2 |
| §5.1-5.2 | CDS、NCAR GDEX 真实端到端 | ✅ 已实测（`benchmarks/m6/`） | 无 | 已完成 |
| §5.3-5.5 | NASA Earthdata、Copernicus Dataspace、GEE | **全缺** | 大 | M3 |
| §6 | 多节点、多链路、真实链路性能记录 | 缺 | 大 | M4 |
| §7 | 10 步自然语言工作流 | 只有 CLI 5 步 | 大 | M5, M6 |
| §8 | 18 个 MCP Tool + 副作用授权 | 缺 | 大 | M5 |
| §9 | 4 个 Skills | 缺 | 中 | M6 |
| §10 | 版本化 Data Request 契约（含坐标顺序、日期边界、时区、频率语义、原生变量映射） | 现有 YAML 契约已覆盖大部分，**缺** `schema_version` 前缀与 `selection.*` 结构、`frequency` 已支持、坐标顺序已文档化 | 小 | M1 |
| §11 | 需求生命周期状态机 + 5 个异常态 | 有传输任务状态机（12 态），无需求态 | 中 | M1 |
| §12 | 环境配置系统（6 个组件 + 结构化结果 + 跨 OS + 不泄露） | 缺 | 大 | M2 |
| §13 | 完整性与安全（checksum/manifest/重复任务/不静默覆盖/原子交付/任务恢复） | 大部分已实现（`storage` 原子提交、隔离区、`.part` 自证） | 小：需补"目标已存在"的**用户决策**流程 | M5, M7 |
| §15 | 技术栈（Pydantic/SQLite/httpx/MCP SDK/pytest） | 已用（除 MCP SDK） | 小 | M5 |
| §16 | M0–M8 里程碑 | M0–M6（旧口径）已完成 | M2–M8 需按新口径重排 | 全部 |
| §18 | 最终验收：用户对 Codex 说一句话完成全链路 | 缺 | 大 | M5, M6, M7 |

---

## 4. 关键架构冲突与裁决

### 4.1 两套状态机的冲突（重要，必须显式裁决）

- **现有** `core/state.py::TaskState`（12 态）：`PENDING / PLANNED / QUEUED / RUNNING / VERIFYING / RETRY_WAIT / COMPLETED / FAILED / PAUSED / CANCELLED / SKIPPED / QUARANTINED`。
  它的语义是**"一个传输任务"的生命周期**，已被 manifest 表、调度器、恢复流程、144 组穷举测试依赖。
- **指令 §11 要求**：`CREATED → DISCOVERING → ASSESSING → VALIDATING → PLANNING → AWAITING_APPROVAL → QUEUED → EXECUTING → TRANSFERRING → VERIFYING → COMPLETED` + `NEEDS_CLARIFICATION / NEEDS_CONFIGURATION / PARTIALLY_SUPPORTED / FAILED / CANCELLED / PAUSED`。
  它的语义是**"一次数据获取需求"的端到端生命周期**（含发现、评估、配置引导、审批）。

**这两者不是同一个东西**：一次"需求"包含 N 次"发现/评估"，最后生成 N 个"传输任务"，每个任务再走现有状态机。
强行合并会把「需求级推进」（发现/规划/审批）塞进传输状态机，污染 144 组测试依赖的既有不变量。

**裁决（ADR-0007）**：**并列两层，不合并**。
- 新增 `core/acquisition.py::AcquisitionState`（需求级 17 态）+ `transition_acquisition()`，与 `TaskState` 各自独立、各有穷举测试。
- 需求级状态机在 `EXECUTING/TRANSFERRING/VERIFYING` 段**聚合**其下任务的状态（任一失败→`PARTIALLY_SUPPORTED` 或 `FAILED`，取决于是否允许部分成功）。
- 持久化到 manifest 库的**新表** `acquisitions`（不改已有 `tasks` 表）。

### 4.2 `sources/` 与 `providers/` 的关系

- `sources/`（现有）：**资产发现层** —— 给定结构化 `DatasetRequest`，产出 `SourceAsset`（远端文件 + 大小 + 校验和）。偏"执行"。
- `providers/`（新增，M3）：**数据集目录与能力层** —— 给 Agent 用的 `search/inspect/compare/get_capabilities`。偏"发现与评估"，服务 §7 Step 2–3。

**裁决（ADR-0008）**：两层并存，`providers/` 是 Agent 面，`sources/` 是执行面；
桥接方式：`providers.inspect()` 的结果用于构造 `DatasetRequest`，再交给 `planner/sources` 执行。不合并。

### 4.3 CLI 与 MCP 的关系

**裁决（ADR-0009）**：两者都是入口，共同调用同一套 Core service，**不各自实现业务逻辑**。
CLI 面向人类运维（`rdh`），MCP 面向 Agent（`rdh-mcp`）。Core service 层（`planner/service.py`、`scheduler/service.py` 模式）是唯一业务实现点。

---

## 5. 保留 / 修改 / 删除 / 新增（最终判定）

| 处置 | 对象 |
|---|---|
| **保留（不动）** | `core/models.py`、`core/errors.py`（仅扩展）、`core/ports.py`、`config/`、`manifest/`、`transfer/`、`sources/`、`planner/`、`scheduler/`、`integrity/`、`storage/`、`reporting/`、`cli/`、全部 979 个现有测试 |
| **修改（最小侵入）** | `README.md`（定位与能力表）、`docs/development-plan.md`（标注旧口径）、`pyproject.toml`（仅加 `mcp` 可选依赖与 `rdh-mcp` script）、`core/state.py`（**只加文档交叉引用**，不改转换表） |
| **删除** | 仓库根的误提交产物：`.coverage`、`.coverage 2`、`.coverage 3`、`.DS_Store` 若干 → 加入 `.gitignore`（不删除用户文件，仅忽略） |
| **新增** | `core/acquisition.py`（需求级状态机）、`planner/request_v11.py`（§10 版本化请求契约）、`planner/planner_permissions.py`（授权/审批模型）、`environment/`、`providers/`、`network/`、`interfaces/mcp/`、`skills/`、`docs/decisions/`、`docs/roadmap.md`、M7 端到端测试 |

---

## 6. 对"不要做什么"的审计（指令 §17 纪律自查）

| 纪律 | 现有仓库是否违反 | 证据 |
|---|---|---|
| 不创建空模块 | ❌ 未违反 | 每个模块都有实现与测试；`catalog/` 只有 Protocol（README 已如实标注 ⬜） |
| 不实现虚假 Provider | ❌ 未违反 | 只有 CDS/NCAR 两个真实适配器，且有实测记录 |
| 不使用随机模拟冒充真实数据 | ❌ 未违反 | `benchmarks/m6/*/results.json` 是真实测量 |
| 不把数据源包装成相同 API | ❌ 未违反 | `sources/base.py` 明确"远端没给的值不得推断" |
| 不把 LLM 作为 Core 依赖 | ❌ 未违反 | 全仓无 LLM 依赖 |
| 不扩展到分析/ML | ❌ 未违反 | 无 pandas/sklearn 依赖 |
| 不默认覆盖用户文件 | ❌ 未违反 | `storage` 有隔离区；但"目标已存在"的用户决策流程缺失，M5/M7 补 |
| 不泄露凭据 | ❌ 未违反 | `config` 用 `env:` 引用；需在 M2 加强并加测试 |
| 不在无测试时宣称完成 | ❌ 未违反 | 979 个测试 |
| 不把仅有接口的标为已完成 | ❌ 未违反 | `catalog/`、S3 端到端均如实标 NOT RUN |

**审计结论：现有仓库的工程纪律良好，可以作为地基；本项目的风险不在"改坏"，而在"新增层是否同样严格"。**
因此本次实施对新增层提出同等要求：可复跑测试 + 真实证据 + 如实标注未验证项。

---

## 7. 执行计划（与此审计配套）

见 [`docs/roadmap.md`](roadmap.md) 与 [`docs/decisions/`](decisions/)。
五个并行实施单元，写作用域互不重叠：

| 单元 | 主题 | 负责人 | 写作用域 |
|---|---|---|---|
| Lead | M0 审计 + 契约冻结（需求状态机、请求契约 v1.1、审批模型）+ 终验 | lead | `docs/`、`core/acquisition.py`、`planner/request_v11.py`、`planner/planner_permissions.py` |
| A | M2 环境配置系统 | engineer-env | `environment/` |
| B | M4 Worker/Route 体系 | engineer-network | `network/` |
| C | M3 Provider 契约 + Earthdata + Copernicus | engineer-providers | `providers/` |
| D | M5 MCP Server + 18 工具 | engineer | `interfaces/mcp/` |
| E | M6 Skills + 调研 | researcher | `skills/`、`docs/research/` |
