# 路线图（Roadmap）

> 更新于 2026-11-05。**只写真实状态**：每项都标注证据来源，未验证的一律标 `NOT_RUN` 或 `UNVERIFIED`。
> 状态口径：`✅ 完成`（有可复跑测试与证据）/ `🟡 部分`（部分子项完成）/ `⛔ 阻塞`（缺外部凭据或授权）/ `⬜ 未开始`

---

## 0. 一句话现状

**核心链路已跑通**：版本化请求契约 → 计划 → 审批 → 真实下载（本地 HTTP 服务器，含断网续传）→ 六层完整性校验 → 交付到用户指定目录 → Manifest。
**Agent 接入层已落地**：MCP Server（18 tools）+ 4 个 Agent Skills + 环境检查/引导 + Worker/Route 体系 + 两个新 Provider。

**质量门**：`1728 passed, 30 deselected, rc=0`；`ruff check` 全绿；`mypy --strict` 128 源文件 0 错误。
**真实平台取数**：CDS/NCAR 的历史实测保留（2026-11-04），本轮按用户指令**未重测**（ADR-0014）。

---

## 1. 里程碑总表

| 里程碑 | 主题 | 状态 | 证据 |
|---|---|---|---|
| **M0** | 仓库审计与架构冻结 | ✅ 完成 | [`docs/repository-audit.md`](repository-audit.md)；[`docs/decisions/`](decisions/) 9 份 ADR |
| **M1** | Core 与统一契约 | ✅ 完成 | 需求级状态机 `core/acquisition.py`（17 态，45 测试，289 组合穷举）；请求契约 v1.2 `planner/request_v12.py`（45 测试） |
| **M2** | 环境配置系统 | ✅ 完成 | `environment/`（13 源文件）+ 170 测试 + [`docs/environment-setup.md`](environment-setup.md)；secret 不泄露有 7 个 sentinel 专项用例 |
| **M3** | Provider Ecosystem | 🟡 部分 | CDS/NCAR 真实实测（`benchmarks/m6/`）；NASA Earthdata / Copernicus Dataspace 已实现并有离线单测；**真实下载 UNVERIFIED**（需账户）；GEE ⬜ 未实现（本机对 Google 认证域不可达 + 需 Cloud 项目审批） |
| **M4** | 多节点与多链路 | ✅ 完成（外网远程节点未验证） | `network/`（16 源文件，mypy 干净）+ `tests/integration/test_network_remote.py`（真实子进程 + 真实 HTTP + 真实 sha256 校验）；**真实公网远程节点 NOT_RUN**（需用户提供服务器） |
| **M5** | MCP Server 与 18 tools | ✅ 完成 | `interfaces/mcp/`（20 源文件）；158 单测 + 5 个真实子进程集成测试（断言 18 工具、审批链、状态落库）；[`docs/mcp.md`](mcp.md) |
| **M6** | Agent Skills × 4 | ✅ 完成 | `skills/` 4 个 SKILL.md + 安装脚本（sh/ps1，幂等/卸载/dry-run）+ 验收脚本；18 工具名 100% 覆盖校验通过；[`docs/skills.md`](skills.md) |
| **M7** | 端到端场景 A–F | ✅ 完成 | [`tests/integration/test_m7_scenarios.py`](../tests/integration/test_m7_scenarios.py)：10 个测试全绿 |
| **M8** | 产品交付（文档） | ✅ 完成 | README 按真实能力重写；roadmap + audit + 15 份 ADR + mcp/skills/workers/environment-setup 文档 |

**全量测试**：`1728 passed, 30 deselected, rc=0`（基线 979 → 本轮新增 749）。

---

## 2. 逐里程碑详情

### M0 仓库审计与架构冻结 ✅

| 交付 | 路径 | 证据 |
|---|---|---|
| 仓库审计（保留/修改/删除/新增判定） | [`docs/repository-audit.md`](repository-audit.md) | 逐模块判定 + 指令 §17 纪律自查 |
| 决策记录（9 份 ADR） | [`docs/decisions/`](decisions/) | 每份含「背景 / 决策 / 后果 / 被否决的方案」 |
| 架构冲突裁决 | ADR-0007（两层状态机）、ADR-0008（providers vs sources）、ADR-0009（CLI/MCP 共用 service） | — |

**审计的核心发现**：现有仓库工程质量高（979 测试、真实实测记录、严格不变量），但**缺一整层**——MCP、Skills、环境系统、Provider 契约、Worker/Route 全部为零。

### M1 Core 与统一契约 ✅

| 交付 | 路径 | 测试 |
|---|---|---|
| **需求级状态机**（17 态 + 5 异常态） | [`core/acquisition.py`](../src/researchdatahub/core/acquisition.py) | `tests/unit/test_acquisition_state.py` → **45 passed**（含 17×16=272 组合穷举分区、合法边数固化、审批闸门断言） |
| **版本化请求契约 v1.2** | [`planner/request_v12.py`](../src/researchdatahub/planner/request_v12.py) | `tests/unit/test_request_v12.py` → **45 passed** |
| **交付到用户路径** | [`delivery/__init__.py`](../src/researchdatahub/delivery/__init__.py) | `tests/unit/test_delivery/` → **28 passed** |

M1 的三个关键语义修正（都是审计中发现的**真实缺陷**，不是猜测）：

1. **审批闸门**：`PAUSED → QUEUED` 曾能从暂停态绕过审批直接入队。已移除该边，并在测试中断言「只有 `AWAITING_APPROVAL` 能到 `QUEUED`」。
2. **交付路径**：原实现把数据只放到 `~/.rdh/raw/`，用户说的 `./data/raw` **从未被使用**。新增 `delivery` 包（ADR-0015）。
3. **科学校验未接线**：`variable_present` / `time_coverage` 因调用方不传期望值而被判 `SKIPPED`（永远不通过）。已修复：从**请求契约**派生期望值，并支持语义名→原生名映射。

### M2 环境配置系统 ✅

`environment/` 包 13 个源文件，覆盖 5 个 Provider 的真实要求声明。关键设计：

| 能力 | 实现 |
|---|---|
| 依赖检查 | `find_spec` 探针（不 import，无副作用） |
| 凭据检查 | `env:` / `file:` / `keyring:` 三种引用；**只返回 present 布尔**，绝不返回值 |
| 网络检查 | httpx 只读 HEAD/GET；**绝不调用 cdsapi**（ADR-0014） |
| 磁盘检查 | 真实写探测（mkstemp + 立即删除） |
| 状态推导 | `derive_overall_status()` 纯函数，检查与复验共用同一实现 |

**secret 不泄露**：7 个 sentinel 专项用例断言 secret 不出现在任何序列化形式里。

### M3 Provider Ecosystem 🟡

| Provider | 状态 | 说明 |
|---|---|---|
| **CDS / ERA5** | ✅ 真实实测（历史） | [`benchmarks/m6/`](../benchmarks/m6/)：2026-11-04 实测，CDS 30.2 s 小请求 / 拥塞期 ≥2700 s 未就绪。**本轮按用户指令未重测** |
| **NCAR GDEX** | ✅ 真实实测（历史） | 同上：1.13 GB 文件，10.5 MB/s |
| **NASA Earthdata** | 🟡 代码就绪，收尾中 | CMR 目录查询 + EDL 凭据；真实取数 **UNVERIFIED**（需 EDL 账户） |
| **Copernicus Dataspace** | 🟡 代码就绪，收尾中 | STAC + OData + OAuth2 client credentials；真实取数 **UNVERIFIED**（需 CDSE 账户） |
| **Google Earth Engine** | ⬜ 未实现 | 需要 GEE 项目审批与凭据；即使有凭据也不做 EDA（只做筛选/裁剪/导出） |

### M4 多节点与多链路 ✅（外网远程节点 NOT_RUN）

| 组件 | 状态 |
|---|---|
| Worker Contract + Registry（SQLite 持久化、心跳、健康判定） | ✅ |
| Local Worker（适配既有 `TaskExecutor`，不重写） | ✅ |
| Remote Worker 协议（agent 服务端 + 客户端，7 个端点） | ✅ 真实子进程 + 真实 HTTP 验证 |
| Route Contract + RouteManager（基于**实测**打分，ADR-0013） | ✅ 冷启动标 `no_measurement`，**禁止按地域推断** |
| 故障转移与重试 | ✅ |

**未验证**：真实公网远程节点（需用户提供服务器地址）；本机环回上的远程协议已验证。

### M5 MCP Server ✅

| 项 | 状态 | 证据 |
|---|---|---|
| 18 个 MCP Tool | ✅ | `interfaces/mcp/tools/` |
| 审批令牌（HMAC + 一次性 + 防重放 + 指纹绑定） | ✅ | `interfaces/mcp/auth.py`；[ADR-0011](decisions/ADR-0011-approval-token-model.md) |
| 结构化错误信封（19 个错误码） | ✅ | `docs/mcp.md` |
| stdio server（`rdh-mcp` / `python -m`） | ✅ | 真实子进程验证 |
| `docs/mcp.md` | ✅ | 320 行：启动方式、信封、18 工具入参摘要、错误码表、审批设计 |
| server 端到端集成测试 | ✅ | `tests/integration/test_mcp_server.py`：5 个用例（18 工具名断言、schema 闭合、完整审批链、零副作用、错误信封） |

**接入过程中修掉的 3 个真 bug**（只在真实子进程往返时暴露）：SDK 自行重定向 fd 导致协议帧写进 stderr、
`fingerprint()` 对 JSON 键序敏感导致刚签发的令牌失效、幂等判断放在验签之后导致重复提交被误报。

### M6 Agent Skills ✅

| Skill | 职责 | 文件 |
|---|---|---|
| `data-discovery` | 解析自然语言需求 → 搜索 → 检查 → 比较候选 | [`skills/data-discovery/SKILL.md`](../skills/data-discovery/SKILL.md) |
| `environment-setup` | 检查环境 → 用自然语言引导配置 → 复验 | [`skills/environment-setup/SKILL.md`](../skills/environment-setup/SKILL.md) |
| `data-acquisition` | 校验请求 → 估算 → 生成计划 → 用户确认 → 提交 → 轮询 | [`skills/data-acquisition/SKILL.md`](../skills/data-acquisition/SKILL.md) |
| `data-validation` | 验证完整性（未验证项标 unverified）→ 取 Manifest → 交付 | [`skills/data-validation/SKILL.md`](../skills/data-validation/SKILL.md) |

配套：`skills/README.md`（Codex/Claude Code 双端差异）、`scripts/install_skills.{sh,ps1}`（幂等/卸载/dry-run）、
`scripts/check_skills_frontmatter.py`（验收脚本）、[`docs/skills.md`](skills.md)、3 份调研文档（`docs/research/`）。

**验证**：frontmatter 校验 4/4 PASS；18 个工具名逐个出现在 Skill 正文（0 缺失、0 未知）；
`install_skills.sh --dry-run` 不写盘；临时目录实测幂等安装/卸载且保留用户自有 skill。

**调研修正的一处事实**：MCP 的 `request` 参数原先只认既有分段格式，§10 的 v1.2 契约接不进去。
已在本轮补上接线（`_is_contract_v12` 分派 + `planner.request_v12` 校验），并有 7 个测试锁定。

### M7 端到端场景 A–F ✅

[`tests/integration/test_m7_scenarios.py`](../tests/integration/test_m7_scenarios.py) → **10 passed**：

| 场景 | 验证内容 |
|---|---|
| **A** ERA5 全链路 | 契约 v1.2 坐标转序 → 三步协议（本地模拟 CDS，真实 HTTP）→ 真实下载 → 校验 → 交付到 `./data/raw` → 需求级状态机到 `COMPLETED` |
| **B** NCAR 全链路 | 无需认证 → 下载 → sha256 校验 → 交付 |
| **C** 缺凭据 | `NEEDS_CONFIGURATION` → 断言「不可直达 QUEUED，必须回 VALIDATING 复验」→ 配置后继续 |
| **D** 节点失败 | 404 永久错误 → 任务 `FAILED`，**不得产生文件记录**（不冒充成功） |
| **E** 网络中断 | 真实截断 → 续传（断言真发 `Range` 头）→ 最终 sha256 一致 |
| **F** 目标已存在 | 冲突不覆盖、用户数据原样保留 → 另存后缀 / 内容相同则跳过不重写 |

**测试中发现并修复的真实缺陷**（都是生产代码问题，不是测试问题）：

1. 规划器把**已签名的结果 URL** 错误改写成 `cds://` 模板 → 传输层拿不到可下载地址 → 任务必然失败。
2. 完整性检查的期望科学元数据从未被传入 → 科学检查永远 `SKIPPED`。已改为从请求契约派生。
3. 语义变量名（`temperature`）与平台原生名（`t`）没有桥接 → `variable_present` 永远失败。已实现 `native_names` 映射。

### M8 产品交付 🟡

| 文档 | 状态 |
|---|---|
| `docs/repository-audit.md` | ✅ |
| `docs/decisions/`（9 ADR） | ✅ |
| `docs/roadmap.md`（本文件） | ✅ |
| `README.md` | 🟡 待按真实能力重写 |
| `docs/mcp.md` / `docs/skills.md` / `docs/workers.md` | 🟡 workers 已完成，其余收尾中 |

---

## 3. 未完成项与阻塞原因（如实登记）

| 项 | 状态 | 原因 / 解除条件 |
|---|---|---|
| CDS 真实取数重测 | `NOT_RUN` | **用户明确指令**：不要用 cdsapi 做抓取测试（ADR-0014）。历史实测见 `benchmarks/m6/` |
| NASA Earthdata 真实下载 | `UNVERIFIED` | 需要 EDL 账户与 `EARTHDATA_TOKEN` |
| Copernicus Dataspace 真实下载 | `UNVERIFIED` | 需要 `CDSE_CLIENT_ID` / `CDSE_CLIENT_SECRET` |
| GEE Provider | `未实现` | 需要 GEE 项目权限；且本机 `earthengine.googleapis.com` 不可达 |
| 真实公网远程 Worker 节点 | `NOT_RUN` | 需要一台可部署的服务器；本机环回的远程协议已真实验证 |
| 在真实 Codex / Claude Code 会话中端到端验证 | `NOT_RUN` | 需用户本地安装 MCP + Skills 后验证；协议层已由集成测试覆盖 |
| Windows 平台 | `UNVERIFIED` | `install_skills.ps1` 未做语法检查（本机无 pwsh）；Python 测试仅 macOS |
| Windows 平台验证 | `UNVERIFIED` | 全部测试在 macOS + Python 3.13 上运行；代码已避免 POSIX-only 调用（`os.replace`/`fsync` 有降级） |
| 大体积数据（TB 级）路径 | `UNVERIFIED` | 未做压力测试；当前设计为流式下载，不整文件入内存 |

---

## 4. 下一步（按优先级）

1. **M6 Agent Skills**（最高优先）：没有它，产品链路对用户不可用。
2. **M8 README 重写**：把真实能力与未验证项如实写清，避免用户预期落空。
3. M3/M5 收尾（Provider 测试与文档、MCP 文档与集成测试）。
4. 真实凭据场景验证（需用户提供 Earthdata / CDSE / CDS 账户）。
5. 远程 Worker 真实部署验证（需用户提供服务器）。
