# ResearchDataHub

> **Agent-native Scientific Data Acquisition Infrastructure**
> 面向 AI Coding Agent 的科研数据获取基础设施 —— 自然语言 → 自动获取 → 验证 → 交付。

[![CI](https://github.com/Neiyako/ResearchDataHub/actions/workflows/ci.yml/badge.svg)](https://github.com/Neiyako/ResearchDataHub/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1735%20passed-brightgreen)](tests/)
[![Chain Benchmark](https://img.shields.io/badge/chain%20benchmark-3%20platforms-blue)](docs/chain-speed-benchmark.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 1. 它解决什么问题

用户想拿到科研数据，通常要：查文档 → 读平台 API → 申请凭据 → 写脚本处理参数顺序/日期边界/变量映射 →
等排队 → 断点续传 → 核对文件是否完整 → 手工整理清单。每一步都容易出错，且过一年无法复现。

ResearchDataHub 把这件事变成**一句话**：

> 帮我获取 2015—2024 年中国区域的 ERA5 小时数据，包含温度、风场、云液态水和云冰水，保存到当前项目的 `data/raw`。

Coding Agent（Codex / Claude Code）通过 **MCP** 调用本系统，系统负责：

```
理解需求 → 发现数据源 → 检查数据集真实能力 → 检查环境与凭据 → 引导配置
        → 构建并校验请求 → 选择平台/节点/链路 → 真实下载 → 监控与重试
        → 完整性验证（六层）→ 交付到用户指定目录 → Manifest 与校验结果
```

**边界很清楚**：ResearchDataHub 只负责"把正确的数据取到正确的位置，并证明取数过程与结果可靠"。
拿到数据之后的 EDA、清洗、建模、绘图、论文撰写，**不属于本项目**，交给外部工具与 Agent。

---

## 1.5 本轮新增（v1.1.0）

> 从"CLI 数据采集框架"升级为 **Agent-native 数据获取基础设施**。以下每一项都已落地并有测试。

### 新增能力

| 能力 | 说明 | 位置 |
|---|---|---|
| **MCP Server + 18 工具** | 外部 Coding Agent 的唯一正式执行接口（stdio） | [`interfaces/mcp/`](src/researchdatahub/interfaces/mcp/) · [`docs/mcp.md`](docs/mcp.md) |
| **Agent Skills × 4** | `data-discovery` / `environment-setup` / `data-acquisition` / `data-validation` | [`skills/`](skills/) · [`docs/skills.md`](docs/skills.md) |
| **环境配置系统** | 5 个 Provider 的依赖/凭据/网络/磁盘检查 + 可操作引导 | [`environment/`](src/researchdatahub/environment/) · [`docs/environment-setup.md`](docs/environment-setup.md) |
| **多节点 Worker 体系** | Worker 注册/心跳/健康、**真实 Remote Worker 协议**（7 端点，含 Range 回传） | [`network/`](src/researchdatahub/network/) · [`docs/workers.md`](docs/workers.md) |
| **多链路 Route 选择** | 只凭**实测**性能打分；冷启动明说"无测量"，**禁止按地域推断速度** | [ADR-0013](docs/decisions/ADR-0013-route-selection-from-measurement.md) |
| **Provider 契约 v1** | NASA Earthdata（CMR + EDL）、Copernicus Dataspace（STAC/OData + OAuth2） | [`providers/`](src/researchdatahub/providers/) |
| **需求级状态机** | 17 态覆盖 发现→评估→配置→计划→审批→执行→交付 | [`core/acquisition.py`](src/researchdatahub/core/acquisition.py) · [ADR-0007](docs/decisions/ADR-0007-two-level-state-machines.md) |
| **请求契约 v1.2** | 坐标顺序/日期边界/时区/频率/语义变量名→原生 ID 映射 | [`planner/request_v12.py`](src/researchdatahub/planner/request_v12.py) · [ADR-0012](docs/decisions/ADR-0012-request-contract-v1-2.md) |
| **交付到用户目录** | 数据落到用户指定的 `./data/raw`，默认**不覆盖** | [`delivery/`](src/researchdatahub/delivery/) · [ADR-0015](docs/decisions/ADR-0015-delivery-to-user-path.md) |
| **HMAC 审批令牌** | `submit_acquisition` 必须携带一次性、有期限、绑定计划指纹的令牌 | [`interfaces/mcp/auth.py`](src/researchdatahub/interfaces/mcp/auth.py) · [ADR-0011](docs/decisions/ADR-0011-approval-token-model.md) |

### 修复的真实缺陷（都是在审计与真实跑测中暴露的）

| # | 缺陷 | 严重性 |
|---|---|---|
| 1 | **交付路径缺失**：数据只到 `~/.rdh/raw/`，用户要求的 `./data/raw` **从未被使用** | 🔴 功能性 |
| 2 | **签名凭据泄露到日志**：CloudFront/S3 预签名 URL 的 `Signature`/`token` 被原样写入 `transfer_logs` 落盘 | 🔴 安全 |
| 3 | **认证头无法透传**：RDH 无法下载任何需要 Bearer token 的科研数据（Earthdata 全部受保护资源） | 🔴 功能性 |
| 4 | **跨域重定向丢凭据**：EDL 303 到签名地址后 401 | 🟠 功能性 |
| 5 | **审批闸门可绕过**：`PAUSED → QUEUED` 能跳过 `AWAITING_APPROVAL` 直接入队 | 🔴 安全 |
| 6 | **科学校验从未生效**：`variable_present`/`time_coverage` 因期望值不传而永远 `SKIPPED` | 🔴 可靠性 |
| 7 | **语义名↔原生名未桥接**：声明 `temperature` 而文件是 `t` 时永远失败 | 🟠 功能性 |
| 8 | **规划器改写签名 URL**：已完成的结果地址被改成 `cds://` 模板 → 任务必然失败 | 🟠 功能性 |
| 9 | **MCP 层 3 个 bug**：协议帧写进 stderr / 令牌因 JSON 键序失效 / 幂等判断位置错误 | 🟠 可靠性 |

> 全部有回归测试锁定。逐条细节见 [CHANGELOG](CHANGELOG.md)。

---

## 1.6 效率对比：三条链路实测

> 完整报告：[`docs/chain-speed-benchmark.md`](docs/chain-speed-benchmark.md) · 原始数据：[`benchmarks/speed/`](benchmarks/speed/)
> 测于 2026-11-06，macOS arm64 / Python 3.13。对比对象是**用户自己写的朴素下载脚本**（不重试、不续传、不校验、不交付）。

| 链路 | 路径 | 样本 | 响应率 | 中位耗时 | P95 | 中位吞吐 |
|---|---|---:|---:|---:|---:|---:|
| **Earthdata**（0.17 MB 受保护 granule） | baseline | 8 | 100% | **3.50 s** | 7.62 s | 0.05 MB/s |
| | **RDH** | 8 | **100%** | 6.04 s | **6.77 s** | 0.03 MB/s |
| **GEE**（云端裁剪→Export→Drive） | baseline | 5 | 100% | **46.56 s** | 75.34 s | 0.02 MB/s |
| | **RDH** | 5 | **100%** | 48.28 s | 85.49 s | 0.02 MB/s |
| **NCAR GDEX**（58.3 MB NetCDF） | baseline | 8 | **88%** | 15.75 s | 20.34 s | 3.53 MB/s |
| | **RDH** | 8 | **100%** ✅ | **14.18 s** | **19.60 s** | **3.92 MB/s** |

### 怎么读这张表（不夸大）

- **NCAR（大文件、CDN 有间歇断连）**：RDH **100% vs 88%** —— 重试 + `.part` 三重自证续传把"从头再来"
  变成"从断点继续"，**中位耗时反而更快**。这是本项目最可复现、最有价值的提升。
- **Earthdata（小文件）**：两者都 100%，RDH 慢 **+2.5 s**。这 2.5 s 是六层校验 + 原子提交 + 交付的
  固定成本；换来的是"文件真的完整、有 SHA-256、有 Manifest 可回溯"。**P95 反而更低**（6.77 < 7.62），
  说明框架开销稳定，而基线高分位由网络抖动主导。
- **GEE（云端导出任务）**：瓶颈是 GEE 自己的排队（中位 46 s），RDH 只多 **1.7 s（3.7%）**，
  没有重复云端计算。

**因此本项目不宣称"让下载变快"。它保证的是三件事：**
**① 失败能恢复**（88%→100%）**② 成功的数据经过验证**（六层校验 + Manifest）
**③ 固定成本可预期且随文件增大被摊薄。**

### 测速过程中修复的 4 个缺陷

签名凭据泄露到日志、认证头无法透传、跨域重定向丢凭据、规划器改写签名 URL —— 全部在真实链路跑测时暴露并修复（见 §1.5 表格 2/3/4/8）。

---

---

## 3. 快速开始

### 3.1 装好后直接给 Agent 发提示词：

**把下面这段整段复制给 Codex / Claude Code，它会自己完成安装和配置：**

```text
请帮我安装并配置 ResearchDataHub（科研数据获取基础设施）：
仓库 https://github.com/Neiyako/ResearchDataHub

步骤：
1. git clone 到 ~/ResearchDataHub（若已存在则跳过），cd 进去
2. python3 -m venv .venv && .venv/bin/pip install -e ".[mcp,sci]"
3. 把 MCP Server 注册到你自己：
   - Claude Code: claude mcp add researchdatahub -- <仓库绝对路径>/.venv/bin/rdh-mcp
   - Codex: 在 ~/.codex/config.toml 加
       [mcp_servers.researchdatahub]
       command = "<仓库绝对路径>/.venv/bin/rdh-mcp"
4. bash scripts/install_skills.sh
5. 用 `.venv/bin/rdh version` 验证退出码为 0，然后告诉我 MCP 工具是否可见

注意：不要向我索要任何密码或 token；需要凭据时教我自己在终端 export。
```

装完后直接说需求即可，例如：

> 帮我获取 2015—2024 年中国区域的 ERA5 小时数据，包含温度、风场、云液态水和云冰水，保存到当前项目的 `data/raw`。

Agent 会按 Skill 自动走完：**搜索数据源 → 检查数据集 → 检查环境 → 引导配置凭据 → 校验请求 → 生成计划 → 等你确认 → 下载 → 校验 → 交付路径与 Manifest**。

### 3.2 手动安装（想自己控制时）

```bash
git clone https://github.com/Neiyako/ResearchDataHub.git
cd ResearchDataHub
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp,sci]"     # mcp=Agent 接口, sci=NetCDF 校验
.venv/bin/rdh version                     # 退出码 0 即成功
bash scripts/install_skills.sh            # 装 4 个 Skill
```

> 需要 **Python 3.12+**（见 [ADR-0010](docs/decisions/ADR-0010-python-version-and-deps.md)）。
> `cds` 依赖只在需要 CDS 取数时安装：`pip install -e ".[cds]"`。

### 3.3 用 CLI（不用 Agent 时）

```bash
cp configs/sources.example.yaml configs/sources.yaml
export CDSAPI_KEY="<你的 CDS 个人访问令牌>"     # 凭据只以 env 引用出现，不写进 YAML
.venv/bin/rdh config validate                  # PASS，退出码 0

.venv/bin/rdh request validate examples/era5-request.yaml
.venv/bin/rdh plan create examples/era5-request.yaml
.venv/bin/rdh task run <plan_id> --max-tasks 1 --max-workers 1
.venv/bin/rdh report generate <request_id>
```

需要凭据时，`rdh` 与 MCP 都会给出**结构化、可操作**的引导（配什么/为什么/去哪拿/存到哪/怎么验证），
见 [docs/environment-setup.md](docs/environment-setup.md)。

---

## 4. 真实能力（如实标注）

> 本表是本项目**唯一的能力口径**。未验证的一律写 `UNVERIFIED` / `NOT_RUN`，不写进"已完成"。

### 3.1 Agent 接入层

| 能力 | 状态 | 证据 |
|---|---|---|
| MCP Server（stdio）+ 18 个工具 | ✅ | `src/researchdatahub/interfaces/mcp/`；`tests/unit/test_mcp/` |
| 副作用授权（HMAC 审批令牌、一次性、防重放、指纹绑定） | ✅ | `interfaces/mcp/auth.py`；[ADR-0011](docs/decisions/ADR-0011-approval-token-model.md) |
| 结构化错误信封（`code`/`hint`/`retryable`） | ✅ | 同上 |
| Agent Skills × 4（discovery / env-setup / acquisition / validation） | ✅ | `skills/`；[docs/skills.md](docs/skills.md) |
| 在真实 Codex / Claude Code 会话中端到端验证 | `NOT_RUN` | 需用户本地安装后验证；工具协议层已由集成测试覆盖 |

### 3.2 数据平台

| 平台 | 覆盖数据 | 状态 |
|---|---|---|
| **Copernicus CDS** | ERA5 / ERA5-Land / 气压层 / 单层 / 其他 CDS 集合 | ✅ 真实取数实测（2026-11-04，见 [`benchmarks/m6/`](benchmarks/m6/)）；**本轮未重测**（[ADR-0014](docs/decisions/ADR-0014-no-cdsapi-live-tests.md)：用户要求不做 cdsapi 抓取测试） |
| **NSF NCAR GDEX (RDA)** | 大气/气候/数值预报/地球系统科学 | ✅ 真实取数实测（1.13 GB 文件，10.5 MB/s） |
| **NASA Earthdata (CMR)** | MODIS / VIIRS / 对地观测 | 🟡 目录查询与能力声明已实现；**真实下载 `UNVERIFIED`**（需 EDL 账户） |
| **Copernicus Data Space** | Sentinel 系列（STAC / OData） | 🟡 目录查询与 OAuth2 已实现；**真实下载 `UNVERIFIED`**（需 CDSE 账户） |
| **Google Earth Engine** | 遥感影像 / 土地覆盖 / 植被指数 | ⬜ **未实现**（需 GEE 项目权限；且只做筛选/裁剪/导出，不做分析） |
| NOAA / USGS / PANGAEA / Zenodo / AWS Open Data | — | ⬜ 未实现（Provider 架构已预留） |

### 3.3 执行与网络

| 能力 | 状态 | 说明 |
|---|---|---|
| 本地 Worker | ✅ | 适配既有调度器执行路径 |
| 远程 Worker 协议（7 端点，含 Range 回传） | ✅ 本机环回验证 | `tests/integration/test_network_remote.py`：真实子进程 + 真实 HTTP + 真实 sha256 |
| 真实公网远程节点 | `NOT_RUN` | 需要一台可部署的服务器 |
| 多链路 Route 选择（**仅凭实测**打分） | ✅ | [ADR-0013](docs/decisions/ADR-0013-route-selection-from-measurement.md)；**禁止按地域推断速度** |
| 故障转移 / 重试 / 取消 / 超时 | ✅ | `network/routes/failover.py` |
| HTTP / HTTP Range / S3 / CDS API 传输 | ✅（S3 端到端 `NOT_RUN`） | `transfer/` |
| FTP / SFTP / Globus / Zarr | ⬜ 未实现 | 不虚构路径规则 |

### 3.4 完整性与交付

| 能力 | 状态 |
|---|---|
| 六层校验（存在/大小/SHA-256/格式/NetCDF/科学语义） | ✅ |
| `time_coverage`（时间轴覆盖）与 `variable_present`（含语义名→原生名映射） | ✅ 本轮实现 |
| 断点续传（`.part` + 大小/校验器/来源身份三重自证） | ✅ |
| staging → raw 原子提交 + 隔离区（不删除坏文件） | ✅ |
| **交付到用户指定目录**（默认不覆盖，冲突返回结构化选项） | ✅ 本轮实现，[ADR-0015](docs/decisions/ADR-0015-delivery-to-user-path.md) |
| Manifest / 交付清单 / 采集报告 | ✅ |

### 3.5 端到端场景（`tests/integration/test_m7_scenarios.py`，10 passed）

| 场景 | 结果 |
|---|---|
| A：ERA5 全链路（契约→计划→审批→下载→校验→交付 `./data/raw`） | ✅ |
| B：NCAR 全链路（免认证→下载→验证→交付） | ✅ |
| C：缺凭据 → 引导配置 → 复验 → 继续 | ✅ |
| D：节点失败 → 明确失败（不产生文件记录、不冒充成功） | ✅ |
| E：传输中断 → 续传（断言真实 `Range` 请求）→ 最终一致 | ✅ |
| F：目标已存在 → 不覆盖 → 用户选择 | ✅ |

---

## 5. 架构

```
┌─ Layer 1  Agent Integration ─────────────────────────────────┐
│  MCP Server (18 tools) · Tool Schemas · Agent Skills × 4     │
│  Environment Setup Guidance · Approval (HMAC)                │
└───────────────────────────┬──────────────────────────────────┘
                            │ 只做入参校验 + 调 Core service
┌─ Layer 2  Acquisition Core ──────────────────────────────────┐
│  Discovery · Metadata · Assessment · Request Builder         │
│  Validator · Planner · Provider/Route Selection · Verify     │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌─ Layer 3  Providers ────────┴────────────────────────────────┐
│  CDS · NCAR GDEX · NASA Earthdata · Copernicus Dataspace     │
│  （每个 Provider 如实声明能力：支持什么、不支持什么）           │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌─ Layer 4  Execution Network ─────────────────────────────────┐
│  Worker Registry · Local/Remote Worker · Route Manager        │
│  Scheduler · Transfer Engine · Retry · Task Queue             │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌─ Layer 5  Storage & Integrity ───────────────────────────────┐
│  SQLite Manifest · Task DB · Checksum · Manifest · Logs       │
│  Credential References（只存引用，不存值）                     │
└──────────────────────────────────────────────────────────────┘
```

**四条不可让步的约束**：

| 约束 | 含义 | 决策 |
|---|---|---|
| **Source ≠ Transport** | 数据源知道"哪里有数据"，传输层知道"怎么搬字节" | [ADR-0002](docs/adr/ADR-0002-source-transport-separation.md) |
| **Manifest First** | SQLite 是唯一事实来源，不靠扫目录判断进度 | [ADR-0003](docs/adr/ADR-0003-sqlite-manifest-source-of-truth.md) |
| **Integrity Before Delivery** | 未经校验不得标记完成 | [ADR-0006](docs/adr/ADR-0006-integrity-check-layering.md) |
| **Preserve Original Data** | 原始数据只读，坏文件进隔离区而不是删除 | [ADR-0005](docs/adr/ADR-0005-staging-raw-quarantine-storage.md) |

**Core 不依赖 LLM**：Agent 负责理解自然语言，Core 只做真实执行。MCP 是唯一执行接口，Skill 是流程知识分发机制。

---

## 6. 两套状态机（重要）

| 状态机 | 位置 | 描述什么 | 状态数 |
|---|---|---|---|
| **传输任务态** | `core/state.py` | 一个远端文件 → 一次字节搬运 + 校验 | 12 |
| **需求生命周期态** | `core/acquisition.py` | 一次用户请求：发现→评估→配置→计划→审批→执行→交付 | 17 |

二者**并列不合并**（[ADR-0007](docs/decisions/ADR-0007-two-level-state-machines.md)）：
一次需求在 `PLANNING` 后展开为 N 个传输任务，`EXECUTING/TRANSFERRING/VERIFYING` 段是**聚合态**。

```
需求级：CREATED → DISCOVERING → ASSESSING → VALIDATING → PLANNING
        → AWAITING_APPROVAL → QUEUED → EXECUTING → TRANSFERRING → VERIFYING → COMPLETED
异常态：NEEDS_CLARIFICATION / NEEDS_CONFIGURATION / PARTIALLY_SUPPORTED / FAILED / CANCELLED / PAUSED

任务级：PENDING → PLANNED → QUEUED → RUNNING → VERIFYING → COMPLETED
                             ↘ RETRY_WAIT ↗        ↘ QUARANTINED
```

---

## 7. 请求契约 v1.2（三个最容易静默出错的语义）

```yaml
schema_version: "1.2"
dataset:
  provider: cds
  dataset_id: reanalysis-era5-pressure-levels
  product_family: ERA5
selection:
  variables:
    - name: cloud_liquid_water        # 语义名（CF 风格），不是平台变量 ID
      units: kg m**-2
      provider_native: {cds: clwc}    # 显式映射槽；Provider 负责真实映射
  temporal:
    start: "2015-01-01"
    end: "2024-12-31"                 # 纯日期 → 次日 00:00 UTC（闭开区间，包含 12-31 全天）
    frequency: hourly
  spatial:
    type: bounding_box
    bounds: [73, 18, 135, 54]         # 固定顺序 W, S, E, N（CDS 是 N,W,S,E，转序集中在一处并强制测试）
output:
  format: netcdf
  destination: local
  path: ./data/raw                    # 数据必须落在这里（ADR-0015）
```

详见 [ADR-0012](docs/decisions/ADR-0012-request-contract-v1-2.md) 与 [`planner/request_v12.py`](src/researchdatahub/planner/request_v12.py)。

---

## 8. 安全与凭据

- **凭据只以引用形式出现**：`env:VAR` / `file:~/.cdsapirc` / `keyring:service/user`。
- **任何 MCP 返回、日志、Manifest 都不含 secret**：`environment` 包只返回 `present: true/false`，
  有 7 个 sentinel 专项用例断言 secret 不出现在任何序列化形式里。
- **不要求用户把 token 发到聊天窗口**：Agent 引导用户在终端自行配置。
- **有副作用的操作需要审批令牌**：`submit_acquisition` 必须携带由 `create_execution_plan` 签发的
  HMAC 令牌（一次性、有期限、绑定计划指纹）。
- **默认不覆盖用户文件**：冲突时返回 `overwrite` / `skip` / `suffix` 三个选项由用户决定。

**已知局限（如实声明）**：令牌机制保证"计划未被篡改且只执行一次"，但**无法在协议层证明
"用户确实点了确认"**——真正的用户确认由 Skill 在提示词层强制。见 [ADR-0011](docs/decisions/ADR-0011-approval-token-model.md)。

---

## 9. 测试与质量门

```bash
.venv/bin/python -m pytest -q              # 1735 passed, 30 deselected（默认离线、可重复）
.venv/bin/python -m pytest -m network -q   # 真实外网（需凭据，默认不跑）
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
```

**30 个 `network` 用例默认不跑**：外网抖动算进 CI 红/绿会让"测试通过"失去意义。

---

## 10. 明确不做

EDA / 清洗 / 分析 / 可视化 / 机器学习 / 模型训练 / 科研结论推断 / 论文生成 /
独立聊天 Agent / 内置 LLM 推理 / 通用 Python 分析 SDK / 面向普通用户的 Web 下载平台。

也不引入 Redis / Kafka / Kubernetes / PostgreSQL —— 本地运行保持轻量，适配低内存设备。

---

## 11. 文档索引

| 文档 | 内容 |
|---|---|
| [INSTALL_PROMPT.md](INSTALL_PROMPT.md) | **给 Agent 的安装提示词（整段复制即可）** |
| [roadmap.md](docs/roadmap.md) | **里程碑真实状态 + 未完成项与阻塞原因** |
| [chain-speed-benchmark.md](docs/chain-speed-benchmark.md) | **三条链路效率实测对比（Earthdata / GEE / NCAR）** |
| [repository-audit.md](docs/repository-audit.md) | M0 仓库审计：保留/修改/删除/新增判定 |
| [decisions/](docs/decisions/) | 15 份 ADR（两层状态机、审批模型、请求契约、链路选择、交付路径…） |
| [architecture.md](docs/architecture.md) | 主数据流、线程/事务边界、模块职责 |
| [mcp.md](docs/mcp.md) | MCP Server 安装、18 个工具、错误码、审批流程 |
| [skills.md](docs/skills.md) | 四个 Agent Skills 的安装与使用 |
| [environment-setup.md](docs/environment-setup.md) | 环境检查与配置引导 |
| [workers.md](docs/workers.md) | Worker/Route 部署、远程 Worker 协议 |
| [data-contract.md](docs/data-contract.md) | 契约模型、科学等价性、版本策略 |
| [data-integrity.md](docs/data-integrity.md) | 六层校验、checksum 策略、原子提交 |
| [recovery.md](docs/recovery.md) | 租约/心跳/崩溃恢复/对账 |
| [operations.md](docs/operations.md) | 部署、日常流程、故障处置 |
| [testing-strategy.md](docs/testing-strategy.md) | 测试分层、覆盖率、盲区 |
| [M6-efficiency-benchmark.md](docs/M6-efficiency-benchmark.md) | 效率基准（CDS / NCAR / 混合，实测） |
| [adr/](docs/adr/) | M0–M6 历史 ADR（0001–0006，仍然有效） |

---

## 12. 目录结构

```
ResearchDataHub/
├── src/researchdatahub/
│   ├── core/          # 契约层：models / errors / state（任务态）/ acquisition（需求态）/ ports
│   ├── interfaces/mcp/# MCP Server + 18 tools + 审批令牌
│   ├── environment/   # 依赖/凭据/网络/磁盘检查与配置引导
│   ├── providers/     # 数据集目录与能力层（Earthdata / Copernicus …）
│   ├── network/       # Worker 注册/心跳/健康、Remote Worker 协议、Route 选择
│   ├── planner/       # 需求 → 计划（含版本化请求契约 v1.2）
│   ├── sources/       # CDS / NCAR 资产发现适配器
│   ├── scheduler/     # 调度、租约、心跳、恢复
│   ├── transfer/      # http / s3 / cds_api 传输与续传
│   ├── integrity/     # 六层校验
│   ├── delivery/      # 交付到用户目录（不覆盖）
│   ├── storage/       # staging / raw / quarantine / archive
│   ├── manifest/      # SQLite 持久化
│   └── reporting/     # 交付清单与报告
├── skills/            # 四个 Agent Skills
├── configs/ examples/ scripts/ benchmarks/
├── docs/              # 架构、契约、决策、路线图
└── tests/             # unit / integration（1735 项）
```

---

## 13. 许可与引用

- 许可证：[MIT](LICENSE)
- 引用：[CITATION.cff](CITATION.cff)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)

> 本项目如实区分「已验证 / 未验证 / 未实现」三类状态；未测试的能力不会出现在"已完成"里。
