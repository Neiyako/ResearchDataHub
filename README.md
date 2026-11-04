# ResearchDataHub

> Research Data Acquisition and Synchronization Hub — 面向气候科学与地球科学的
> 多源数据发现、采集、传输调度、完整性验证、元数据管理与统一归档框架。

**当前状态：M1–M5 已完成（可用的采集链路）；M6 的「轻量下载效率基准」子项已完成。** SQLite Manifest、HTTP/S3 传输
（含安全断点续传）、CDS/NCAR 数据源适配、计划与调度（租约 + 心跳 + 崩溃恢复）、
完整性校验与 staging→raw 原子提交、交付清单与报告均已实现并有测试；真实 ERA5 产物
（CDS 小样本子集 + NCAR 月度全域文件 1.13 GB）已于 2026-11-04 实际下载并校验，
CDS 3 天请求因服务端保护性限流未完成，降量到最小后实测 30.2 s 联通（如实记录），效率对比见
[`docs/M6-efficiency-benchmark.md`](docs/M6-efficiency-benchmark.md)。

本文件如实区分「已验证」「未验证」「未实现」三类状态（见 §2 与 §7）。
最近一次工程验收：**979 passed / 14 deselected（network）/ 0 failed**，
`ruff` 与 `mypy --strict` 全绿；真实外网链路已执行：NCAR GDEX 目录发现 → HTTPS 下载 →
SHA-256 → raw 提交 → 交付清单，以及 M6 的 CDS 子集 / NCAR 1.13 GB 月度文件真实下载
（2026-11-04，见 [`docs/M6-efficiency-benchmark.md`](docs/M6-efficiency-benchmark.md)）。

---

## 1. 项目定位

ResearchDataHub 要解决的是一类长期存在的问题：科研数据采集脚本通常一次性写完、
与某个数据源（ERA5 / CMIP6 / CloudSat / 地面观测）强耦合，几年后无法维护、无法复现、
无法证明"本地这份文件和远端那份是同一份科学数据"。

本项目把这件事拆成有明确边界的模块：

```
需求(DatasetRequest) → 计划(AcquisitionPlan) → 任务(AcquisitionTask)
   → 传输(TransferResult) → 校验(IntegrityReport) → 落盘(FileRecord)
   → 持久化状态(Manifest, SQLite) → 交付清单(DeliveryManifest)
```

四条不可让步的架构约束（详见 [`docs/architecture.md`](docs/architecture.md) 与 ADR）：

| 约束 | 含义 |
|---|---|
| **Source ≠ Transport** | 数据源适配器负责"发现数据、解析元数据"，传输适配器负责"搬字节、重试、超时"。NCAR 的 S3 逻辑不得写进通用 S3 Worker（[ADR-0002](docs/adr/ADR-0002-source-transport-separation.md)） |
| **Manifest First** | SQLite 是任务与文件状态的持久化事实来源，不靠扫描文件夹猜进度（[ADR-0003](docs/adr/ADR-0003-sqlite-manifest-source-of-truth.md)） |
| **Integrity Before Delivery** | 未通过完整性检查的文件不得标记为完成；状态机里 `RUNNING → COMPLETED` 这条边**不存在** |
| **Preserve Original Data** | 原始数据默认只读，不做覆盖式清洗、转换、重命名 |

### 非目标（明确不做）

- 不是分布式的：不引入 Kafka / Celery / Redis / Kubernetes / 微服务（[ADR-0001](docs/adr/ADR-0001-modular-monolith.md)）。
- 不是数据加工平台：不默认做重网格化、单位换算、插值。需要时另行产出**新**产物并保留来源。
- 不是 Web 应用：只有 CLI（M1–M5 无前端）。
- 不做云端托管：本地优先，全部状态落在本地文件系统与 SQLite。

---

## 2. 现在能做什么 / 不能做什么

### ✅ 已实现并验证

| 命令 | 行为 | 验证方式 |
|---|---|---|
| `rdh version` / `rdh config validate` / `rdh source list` | 版本、配置校验（严格 schema + `env:` 展开）、数据源清单（`implemented` 如实） | 单元 + 集成测试 |
| `rdh db init/status/migrate` | SQLite Manifest 初始化（WAL + 外键 + 版本化迁移）、状态快照、幂等迁移 | 82 项 manifest 测试（含真实多线程并发领取） |
| `rdh request validate <yaml>` | §6.2 分段格式 / 扁平格式；错误定位到 `文件:行` + 字段路径；`units` 不猜 | 39 项 planner 测试 |
| `rdh plan create <yaml>` / `plan show <plan_id>` | 源匹配、资源发现、按 file/year/month/request 拆分、去重、未满足变量登记；计划落库 | 同上 + 真实 NCAR 目录实测 |
| `rdh task run [plan_id]` / `task status/pause/resume/retry/cancel/reconcile` | Manifest 原子领取、有限并发、心跳续约、失败分类与退避、暂停恢复取消、启动恢复与崩溃对账 | 24 项 scheduler 测试 + 5 项 e2e（含 4 个故障注入） |
| `rdh file verify <path>` | 任意文件完整性体检（无 Manifest 也可用）；`PASSED` 才退出 0 | 14 项 check_path + 9 项 CLI |
| `rdh report generate/show <id>` | 采集报告（成功/失败/跳过/隔离全量）+ 交付清单 JSON（相对路径） | 37 项 reporting + 9 项 CLI |

传输与数据源（Python API，已被 CLI/调度器集成）：

- `transfer`：httpx 流式下载（`.part` 原子提交、三重自证断点续传、取消、进度）、
  boto3 S3（流式、ClientError 分类、**ETag 不作为 MD5**）、CDS 结果签名 URL 下载
  （`cds_api` 路由，复用 HTTP 实现）、统一重试策略、有界并发。
- `sources`：CDS（真实 retrieve/v1 schema、参数翻译、服务端异步生命周期建模）、
  NCAR（GDEX/RDA JSON 目录 API、真实响应解析）、Source Registry（工厂注册，无按源 if/else）。

### ❌ 尚未实现 / 未验证（如实列出）

| 项 | 状态 |
|---|---|
| **真实 ERA5 数据下载**（CDS 或 RDA 的 ERA5 产物） | ✅ **部分实测（2026-11-04）**：NCAR GDEX 月度全域文件（1.13 GB，SHA-256 校验）与 CDS 小样本子集（1 天 × 1 时次，34 KB，xarray 校验）真实下载；**CDS 3 天请求因服务端限流 45 min 未就绪；降量到 2°×2°×1 时次后实测 30.2 s 联通**。效率对比见 [`docs/M6-efficiency-benchmark.md`](docs/M6-efficiency-benchmark.md) |
| CDS 端到端执行（submit → poll → 下载结果文件） | **传输层已实现（2026-11-04）**：新增 `CdsApiTransport`（`access_route=cds_api`，复用 HTTP 流式/`.part`/续传/重试语义，本地真实 HTTP socket 单测覆盖）；**调度器内真实 CDS 端到端仍 NOT RUN**（需 CDS 凭证 + 实际排队，未造假）。脚本层此前已实测（M6 基准：排队→生成→下载，含队列上限拒绝的真实错误） |
| S3 端到端下载 | **NOT RUN**（无 MinIO/S3 环境）；无 mock 冒充，代码覆盖 URI/ETag 规则/错误分类 |
| S3 multipart 并行下载 | 未实现（单条 `get_object` 顺序流式，`s3.part_size_mb` 仅作块上限） |
| NetCDF 科学语义校验（变量/时间覆盖的正确性） | 只校验结构与声明一致性，**不做科学结论** |
| 多进程/多机调度、数据库分布式锁 | 不支持（local-first 单机定位，ADR-0001） |
| `plan show --request-id`、优先级老化提升 | 未实现（需要 `list_plans` API / 额外调度策略） |

---

## 3. 安装

要求：**Python ≥ 3.12**。开发环境为 macOS (Apple Silicon) / Linux。
（本文所有命令在 macOS 27.0.1 / arm64 / Python 3.13.13 上实测通过。）

```bash
git clone <repo> ResearchDataHub && cd ResearchDataHub

# 推荐：uv（Apple Silicon 原生）
uv venv --python 3.12
uv pip install -e ".[dev]"

# 或 pip
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

验证安装：

```bash
.venv/bin/python -c "import researchdatahub; print(researchdatahub.__version__)"   # 0.1.0
.venv/bin/rdh version          # 打印版本表，退出码 0
```

> **若 `rdh` 报 `ModuleNotFoundError: No module named 'researchdatahub'`**：
> 这**不是**包本身的问题，而是该 venv 的可编辑安装状态已损坏（常见于多次失败的
> `pip install` 之后，或 venv 目录被部分删除）。**推荐修复：删掉重建 venv**：
>
> ```bash
> rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
> ```
>
> 重建后 `.venv/bin/rdh version` 应正常。临时绕过可用
> `.venv/bin/python -m researchdatahub.cli.main <命令>` 或加 `PYTHONPATH=src`。
> 排查记录见 [`docs/development-plan.md` §7](docs/development-plan.md)。

可选依赖组：

| extra | 内容 | 何时需要 |
|---|---|---|
| `cds` | `cdsapi` | 走 Copernicus CDS 路径（M1 起） |
| `sci` | `xarray`、`netCDF4` | 科学内容检查（NetCDF 可打开性、变量存在性） |
| `dev` | `pytest`、`pytest-cov`、`ruff`、`mypy` | 开发与验收 |

> **Apple Silicon 提示**：`netCDF4` 走 wheel，通常无需编译。若在 M1/M2 上遇到
> HDF5 链接错误，优先改用 `sci` 之外的纯 Python 校验，或安装 `h5netcdf` 替代后端。
> 8GB 内存机器上不建议同时启用多 worker 的大文件校验。

---

## 4. 快速开始

```bash
# 1) 看能力边界
rdh --help

# 2) 版本与数据契约版本
rdh version

# 3) 校验配置（CDS 密钥通过环境变量提供，不写进仓库）
export CDSAPI_KEY="<your-cds-token>"
rdh config validate
# PASS 配置校验通过：2 个文件，0 条错误。

# 4) 未提供密钥时会如实失败（退出码 1），并点名缺失变量
unset CDSAPI_KEY
rdh config validate
# 错误 ... environment variable 'CDSAPI_KEY' is not set ...

# 5) 查看声明的数据源（cds/ncar 已实现；s3/globus/zarr 路由仍为 False）
rdh source list
```

### 4.1 完整采集链路（M1–M5）

```bash
# 0) 准备配置：把示例复制成本地配置（configs/sources.yaml 已被 .gitignore 忽略）
cp configs/sources.example.yaml configs/sources.yaml
export CDSAPI_KEY="<your-cds-token>"     # 只有走 CDS 时才需要

# 1) 初始化 Manifest（幂等；同时创建 raw/ staging/ quarantine/ temp/ archive/）
rdh db init
rdh db status                            # 看 schema 版本、WAL/外键、任务计数

# 2) 写一份需求 YAML（见下文 4.2 与 examples/），先校验（不联网）
rdh request validate my-request.yaml

# 3) 生成采集计划（会联网查询数据源目录，但**不下载任何字节**）
rdh plan create my-request.yaml
rdh plan show <plan_id>                  # 看任务拆分、数据源、未满足变量

# 4) 执行（从 Manifest 领取任务；Ctrl-C 或崩溃后重跑同一条命令即可续跑）
rdh task run <plan_id> --max-workers 2
rdh task status                          # 任务列表；单个任务看租约/心跳/事件

# 5) 校验与交付
rdh file verify /path/to/file.nc --expected-size 123456
rdh report generate <request_id>         # 采集报告 + 交付清单 JSON
rdh report show <report_id>
```

产物位置（全部由 `storage.root` 决定，默认 `~/.rdh`）：

```
~/.rdh/
├── manifest.db               # SQLite 事实来源（ADR-0003）
├── raw/<dataset>/<file>      # 正式数据（0444 只读，默认禁止覆盖）
├── staging/                  # 下载中（.part + sidecar）
├── quarantine/               # 校验失败（保留 + .reason.json）
├── deliveries/<id>.json      # 交付清单（相对路径）
└── reports/<id>.json         # 采集报告
```

### 4.2 需求 YAML（分段格式）

```yaml
request:
  name: era5_cloud_water_2011
  preferred_sources: [ncar, cds]
dataset:
  provider: cds
  name: reanalysis-era5-pressure-levels
  product: reanalysis
  product_family: ERA5
variables:
  - name: clwc
    units: kg kg**-1
  - name: t
    units: K
temporal:
  start: "2011-01-01"
  end: "2011-12-31"
  frequency: hourly
spatial:
  type: global
vertical:
  levels: [1000, 925, 850, 700, 500]
storage:
  preserve_original: true
validation:
  verify_size: true
  calculate_sha256: true
```

`end: 2011-12-31` 表示"到 2011-12-31 结束"（内部解释为 `2012-01-01T00:00Z` 的闭开区间）；
变量可以只写名字，但那样 `units` 记为 `unknown` 并给出警告——系统**不猜**单位。
扁平格式（`examples/era5-request.yaml`）同样支持。

示例需求文件：[`examples/era5-request.yaml`](examples/era5-request.yaml)
（ERA5 气压层 `clwc/ciwc/t/q`，2011–2020，全球，1000–900 hPa，候选源 NCAR 与 CDS）。
它是一份 `DatasetRequest`，可以直接用模型校验：

```bash
python -c "import yaml; from researchdatahub.core import DatasetRequest; \
           DatasetRequest.model_validate(yaml.safe_load(open('examples/era5-request.yaml')))"
```

---

## 5. 目录结构

```
ResearchDataHub/
├── src/researchdatahub/
│   ├── core/          # 契约层：models / errors / state / ports（无上层依赖）
│   ├── config/        # 配置加载与校验（依赖 core）
│   ├── cli/           # Typer 命令（依赖 core + config）
│   ├── sources/       # 数据源适配器        —— M1
│   ├── catalog/       # 数据集目录          —— M1
│   ├── planner/       # 需求 → 计划         —— M1
│   ├── scheduler/     # 调度与并发          —— M1
│   ├── transfer/      # 传输协议实现        —— M1
│   ├── integrity/     # 完整性校验          —— M1
│   ├── storage/       # 临时/原始/隔离/归档 —— M1
│   ├── manifest/      # SQLite 持久化       —— M1
│   └── reporting/     # 报告                —— M1
├── configs/           # defaults.yaml / sources.example.yaml
├── examples/          # era5-request.yaml
├── scripts/           # M6 效率基准与结果合并（scripts/m6_*.py）
├── docs/              # 架构、数据契约、模块边界、计划、测试策略、ADR
└── tests/             # unit / integration / fixtures
```

各模块的实现状态在其 `__init__.py` 的 docstring 中声明，与本文档一致。

---

## 6. 开发

```bash
# 格式与静态检查
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/mypy src

# 测试（默认不跑网络测试；pytest addopts 已 -m 'not network'）
PYTHONPATH=src .venv/bin/python -m pytest
PYTHONPATH=src .venv/bin/python -m pytest --cov=researchdatahub --cov-report=term-missing
```

测试分层、覆盖率目标与"为什么不用 mock 掩盖核心逻辑"见
[`docs/testing-strategy.md`](docs/testing-strategy.md)。

---

## 7. 安全与数据约定

**仓库里不含任何本地绝对路径与真实凭证**（已用
`grep -nE "/Users/|/home/" configs/*.yaml examples/*.yaml` 验证，仅命中一条说明性注释）。

- **不写死用户本地路径**：`storage.root` 默认 `~/.rdh`（开箱即用、可移植）。
  需要自定义时把该行改成 `root: "env:RDH_HOME"`，由环境变量提供；
  也支持内嵌插值 `${RDH_HOME}/data`。三种写法均已实测：

  | 写法 | 解析结果（`RDH_HOME=/tmp/rdh-demo`） |
  |---|---|
  | `~/.rdh` | `~/.rdh`（默认值） |
  | `env:RDH_HOME` | `/tmp/rdh-demo` |
  | `${RDH_HOME}/state` | `/tmp/rdh-demo/state` |

  子目录（`raw_dir` / `staging_dir` / `quarantine_dir` / `temp_dir` / `archive_dir`）**必须相对**，
  不得以 `/`、`~` 或 `env:` 开头，且五者不得重叠。
- **密钥不进仓库**：配置文件中的密钥一律写成 `env:VAR` 引用（如 `credentials.key: env:CDSAPI_KEY`）；
  `configs/sources.yaml` 与 `*.local.yaml` 已在 `.gitignore` 中。
  CDS 的访问令牌只以 `env:CDSAPI_KEY` 形式出现，缺失时 `rdh config validate` 会点名报错（退出码 1），
  不会给出静默降级的"假通过"。
- **原始数据只读**：提交到 `raw/` 后默认 `chmod 0444`；目标已存在时比较大小+SHA-256，
  相同则跳过、不同则报错，**绝不静默覆盖**。
- **不默认转换数据**：任何加工产物都必须作为新对象并携带来源追踪字段。
- **联网范围可控**：只有 `plan create`（目录查询）与 `task run`（下载）会访问网络；
  `db` / `report` / `task status` 等命令完全不联网。默认测试套件排除真实外网用例。
- **不记录密钥**：日志与异常只打印凭证的**变量名**，不打印值（`logging.redact_env`）。
- **路径穿越防护**：下载文件名经清洗，所有落盘路径必须位于配置的存储根内。

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [architecture.md](docs/architecture.md) | 主数据流、线程/事务边界、模块职责 |
| [data-contract.md](docs/data-contract.md) | 契约模型、科学等价性、版本策略 |
| [module-boundaries.md](docs/module-boundaries.md) | 允许的 import 方向与检查脚本 |
| [configuration.md](docs/configuration.md) | 每个配置字段的含义与约束 |
| [recovery.md](docs/recovery.md) | 租约/心跳/崩溃恢复/对账 |
| [data-integrity.md](docs/data-integrity.md) | 六层校验、checksum 策略、原子提交 |
| [operations.md](docs/operations.md) | 部署、日常流程、故障处置、备份 |
| [testing-strategy.md](docs/testing-strategy.md) | 测试分层、覆盖率、盲区 |
| [development-plan.md](docs/development-plan.md) | 里程碑与逐项证据 |
| [engineering-report.md](docs/engineering-report.md) | **M1–M5 验收报告**（含 NOT RUN 项） |
| [M6-efficiency-benchmark.md](docs/M6-efficiency-benchmark.md) | **M6 轻量下载效率基准**（纯 CDS / 纯 NCAR / 混合制，实测数据与结论） |
| [M6-verification.md](docs/M6-verification.md) | **M6 子项 1 验收记录**（五态判定 + 证据 + 遗留） |
| [ADR-0001…0006](docs/adr/) | 关键架构决策 |

## 9. 许可证

MIT，见 [LICENSE](LICENSE)。
