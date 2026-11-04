# ResearchDataHub

> Research Data Acquisition and Synchronization Hub — 面向气候科学与地球科学的
> 多源数据发现、采集、传输调度、完整性验证、元数据管理与统一归档框架。

**当前状态：M0（工程骨架与架构契约）。** M0 交付的是**契约、边界、状态机与验证机制**，
不是可用的下载器。本文件在「可以做什么 / 不可以做什么」两节里如实列出能力边界——
M0 没有任何数据源适配器、没有传输实现、不会联网下载一个字节。

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
- 不是 Web 应用：M0/M1 只有 CLI。
- 不做云端托管：本地优先，全部状态落在本地文件系统与 SQLite。

---

## 2. 现在能做什么 / 不能做什么

### ✅ M0 可用

| 命令 | 行为 |
|---|---|
| `rdh --help` | 显示命令与能力边界；未实现命令在独立的「未实现（M1）」面板中标出 |
| `rdh version` | 打印包版本、**数据契约版本**（`MODELS_VERSION`）、Python 与平台信息 |
| `rdh config validate` | 校验 `defaults.yaml` 与 `sources.yaml`（`env:` 展开、schema 严格校验、未知字段拒绝），通过退出 0，失败退出 1 并逐条列出错误 |
| `rdh source list` | 列出配置中声明的数据源（provider、访问路径、是否已实现）。M0 全部为 `implemented=False` |

已实现并可被外部依赖的 Python 契约：

- `researchdatahub.core`：10 个 Pydantic v2 数据模型、异常层次、12 态状态机、8 个 Protocol 接口。
- `researchdatahub.config`：YAML 加载、`env:VAR` 展开、结构化校验结果。

### ❌ M0 **不**具备

| 未实现 | 说明 |
|---|---|
| `rdh plan` / `rdh fetch` / `rdh status` | 真实命令，但执行即返回退出码 2 与「未实现（planned for M1）」。**不会假装成功** |
| 任何数据源适配器 | `sources/` 只有说明模块职责的 docstring |
| 任何传输实现 | `transfer/` 同上；不联网、不下载 |
| SQLite Manifest 实现 | `manifest/` 只有接口（`ManifestRepository`），表结构在 M1 落地 |
| 完整性检查的实现 | `integrity/` 同上；`IntegrityChecker` 接口与语义已定 |
| 断点续传、并发调度、重试执行 | M1 |

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

# 5) 查看声明的数据源（M0 全部 implemented=False）
rdh source list
```

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
├── docs/              # 架构、数据契约、模块边界、计划、测试策略、ADR
└── tests/             # unit / integration / fixtures
```

各模块的 M0 状态在其 `__init__.py` 的 docstring 中声明，与本文档一致。

---

## 6. 开发

```bash
# 格式与静态检查
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/mypy src

# 测试（默认不跑网络测试）
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

  子目录（`data_dir` / `quarantine_dir` / `tmp_dir`）**必须相对**，不得以 `/`、`~` 或 `env:` 开头。
- **密钥不进仓库**：配置文件中的密钥一律写成 `env:VAR` 引用（如 `credentials.key: env:CDSAPI_KEY`）；
  `configs/sources.yaml` 与 `*.local.yaml` 已在 `.gitignore` 中。
  CDS 的访问令牌只以 `env:CDSAPI_KEY` 形式出现，缺失时 `rdh config validate` 会点名报错（退出码 1），
  不会给出静默降级的"假通过"。
- **原始数据只读**：`StorageBackend.publish_original` 的契约要求原子移动并置为只读。
- **不默认转换数据**：任何加工产物都必须作为新对象并携带来源追踪字段。
- **不联网、不下载**：M0 阶段没有任何传输实现；`rdh fetch` 直接拒绝（退出码 2），
  不会在未告知的情况下访问网络。

---

## 8. 许可证

MIT，见 [LICENSE](LICENSE)。
