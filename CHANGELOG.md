# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 结构，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] — 2026-11-06

### v1.1.0 增补（真实链路效率基准）

- **新增** `scripts/chain_speed_bench.py`：80% 响应率停止规则的全链路测速基准，
  支持 `local` / `ncar` / `earthdata` / `gee` / `cds` / `copernicus` 模式，
  每条路径输出响应率、中位/P95 耗时、吞吐与逐样本原始记录。
- **新增** [`docs/chain-speed-benchmark.md`](docs/chain-speed-benchmark.md)：三条链路实测对比报告
  （Earthdata / GEE / NCAR），含方法、口径与未测项登记。
- **新增** `redact_url()`：日志/事件写入前对签名类查询参数（`Signature`/`X-Amz-Credential`/
  `token` 等）脱敏并截断，修复**凭据泄露到 `transfer_logs`** 的安全问题（7 个安全用例）。
- **新增** `HttpTransportAdapter(extra_headers=...)`：支持需要 Bearer token 的科研数据源
  （修复 RDH 无法下载任何受保护资源）。

### v1.1.0 实测结果（2026-11-06）

| 链路 | baseline 响应率 | RDH 响应率 | baseline 中位 | RDH 中位 |
|---|---:|---:|---:|---:|
| Earthdata（0.17 MB 受保护） | 100% | **100%** | 3.50 s | 6.04 s |
| GEE（云端 Export→Drive） | 100% | **100%** | 46.56 s | 48.28 s |
| NCAR GDEX（58.3 MB） | **88%** | **100%** | 15.75 s | **14.18 s** |

**核心结论**：本项目不使网络变快；它使**下载失败可恢复**（NCAR 88%→100%）、
**成功数据经过验证**、**固定成本随文件增大被摊薄**。小文件上 RDH 比朴素脚本慢 1.7×（校验/交付成本），
GEE 上仅慢 3.7%（瓶颈是云端排队）。



本轮主题：**从"CLI 数据采集框架"升级为"Agent-native 数据获取基础设施"**。
在保留既有执行层（979 个测试、真实实测记录）的前提下，补齐 Agent 接入层与契约层。

### 新增

- **MCP Server（M5）**：18 个 MCP Tool（`search_datasets` / `inspect_dataset` /
  `compare_datasets` / `get_provider_capabilities` / `check_environment` /
  `get_environment_requirements` / `validate_environment` / `validate_request` /
  `estimate_acquisition` / `create_execution_plan` / `get_execution_plan` /
  `submit_acquisition` / `get_task_status` / `cancel_task` / `list_workers` /
  `get_route_status` / `verify_dataset` / `get_manifest`）；统一响应信封；
  19 个结构化错误码。`pip install -e ".[mcp]"` + `rdh-mcp` 入口。
- **副作用授权（ADR-0011）**：`submit_acquisition` 必须携带 `create_execution_plan`
  签发的 HMAC 令牌（一次性 `jti`、有期限、绑定计划指纹）；密钥 `~/.rdh/approval.key`
  权限 0600、`O_EXCL` 原子生成；防重放与重复提交幂等返回。
- **Agent Skills × 4（M6）**：`data-discovery` / `environment-setup` /
  `data-acquisition` / `data-validation`；跨 Codex 与 Claude Code 复用；
  `scripts/install_skills.{sh,ps1}`（幂等、`--dry-run`、`--uninstall`）。
- **环境配置系统（M2）**：5 个 Provider 的真实要求声明、`find_spec` 依赖探针、
  三种凭据引用（`env:` / `file:` / `keyring:`，**只返回 present 布尔**）、
  只读网络探测、磁盘检查、组合校验器；secret 不泄露有 7 个 sentinel 专项用例。
- **多节点与多链路（M4）**：Worker Contract + SQLite Registry（注册/心跳/健康判定）、
  Local Worker、**真实 Remote Worker 协议**（7 端点，含 Range 回传与幂等）、
  Route Contract + RouteManager（**只凭实测性能打分**）、故障转移与重试。
- **Provider 契约 v1（M3）**：`providers/` 层（数据集目录与能力声明），
  NASA Earthdata（CMR + EDL）、Copernicus Data Space（STAC/OData + OAuth2）。
- **需求级状态机（ADR-0007）**：`core/acquisition.py`，17 态 + 5 个异常态，
  与既有 12 态传输任务状态机**并列不合并**；289 组合穷举测试。
- **版本化请求契约 v1.2（ADR-0012）**：`schema_version` 守卫；坐标顺序固定
  `[W,S,E,N]`（CDS 的 `[N,W,S,E]` 转序集中在 `to_cds_area` 并强制测试）；日期闭开区间；
  仅 UTC；频率枚举；语义变量名 → 平台原生 ID 的显式映射槽；接入 MCP `request` 参数。
- **交付到用户路径（ADR-0015）**：`delivery/` 包，把权威副本交付到用户指定的
  `./data/raw`；默认**不覆盖**，冲突返回 `overwrite`/`skip`/`suffix`；原子写入
  （copy → fsync → 同目录 rename）。

### 修复（都是审计/端到端测试中发现的真实缺陷）

- **交付路径缺失**：原实现只把数据放到 `~/.rdh/raw/`，用户要求的 `./data/raw`
  **从未被使用**。新增 `delivery` 包修正。
- **审批闸门可绕过**：需求级状态机的 `PAUSED → QUEUED` 曾能跳过 `AWAITING_APPROVAL`
  直接入队。已移除该边并加断言。
- **科学校验从未生效**：`variable_present` / `time_coverage` 因调用方不传期望值
  而被永远判 `SKIPPED`。已改为从**请求契约**派生期望值；`time_coverage` 本轮真正实现
  （解码 NetCDF 时间轴），不再是"未实现"。
- **语义名与原生名未桥接**：请求声明 `temperature` 而文件里是 `t` 时永远失败。
  已实现 `native_names` 映射并贯通到 worker 与完整性检查。
- **CDS 签名结果 URL 被改写**：规划器把已完成 submit→poll→results 的资产改写成
  `cds://` 模板，导致传输层拿不到可下载地址、任务必然失败。已改为保留真实结果 URL。
- **MCP 层 3 个只在真实子进程往返时暴露的 bug**：SDK 自行重定向 fd 导致协议帧写进
  stderr；`fingerprint()` 对 JSON 键序敏感导致刚签发的令牌失效；幂等判断位置错误
  导致重复提交被误报。

### 实测与验证边界

- **全量测试**：`1728 passed, 30 deselected, rc=0`（上一版 979）；`ruff check` 全绿；
  `mypy --strict` 128 源文件 0 错误。
- **M7 端到端场景 A–F**：`tests/integration/test_m7_scenarios.py` 10 个测试通过
  （ERA5 全链路 / NCAR 全链路 / 缺凭据引导 / 节点失败 / 中断续传 / 目标已存在）。
- **CDS 真实取数本轮未重测**：按用户明确指令不做 cdsapi 抓取测试（ADR-0014）；
  上次实测 2026-11-04，见 `benchmarks/m6/`。
- **NASA Earthdata / Copernicus Dataspace 真实下载 `UNVERIFIED`**（需各自账户）。
- **GEE 未实现**：本机对 Google 认证域不可达，且需 Cloud 项目资格验证。
- **真实公网远程 Worker 节点 `NOT_RUN`**（本机环回的远程协议已真实验证）。
- **在真实 Codex / Claude Code 会话中的端到端验证 `NOT_RUN`**（需用户本地安装）。

### 文档

- 新增 [`docs/roadmap.md`](docs/roadmap.md)（里程碑真实状态 + 阻塞原因）、
  [`docs/repository-audit.md`](docs/repository-audit.md)（M0 审计）、
  [`docs/decisions/`](docs/decisions/)（9 份 ADR）、[`docs/mcp.md`](docs/mcp.md)、
  [`docs/skills.md`](docs/skills.md)、[`docs/workers.md`](docs/workers.md)、
  [`docs/environment-setup.md`](docs/environment-setup.md)、`docs/research/`（3 份调研）。
- README 按真实能力重写（Agent-native 定位 + 逐项标注已验证/未验证/未实现）。

---

## [0.1.0] — 2026-11-04

首个公开版本：M0–M5 完整采集链路 + M6 轻量效率基准（真实 ERA5）。

### 新增

- **工程骨架（M0）**：10 个 Pydantic v2 持久化模型（全 `frozen` + `extra=forbid`）、
  12 态任务状态机（30 条合法边）、8 个 `@runtime_checkable` Protocol、模块依赖矩阵、
  YAML 严格配置（`env:` 展开、错误定位到 `文件:行`）、Typer CLI 骨架。
- **SQLite Manifest（M1）**：版本化迁移、仓库层（任务/目录/文件/校验/交付）、
  租约 + 心跳、单写者语义、`rdh db init/status/migrate`。
- **传输引擎（M2）**：httpx 流式下载（`.part` + `os.replace` 原子提交）、
  断点续传三重自证（大小 + 校验器 + 来源身份）、按状态码分类的有限重试、
  有界并发、取消；boto3 S3（ETag 不当 MD5）。
- **数据源适配（M3）**：Copernicus CDS（真实 retrieve/v1 schema、参数翻译、异步
  submit→poll→results 生命周期）、NSF NCAR GDEX/RDA（公开目录 API、文件发现）、
  Source Registry（工厂注册，无按源 `if/else`）。
- **计划与调度（M4）**：YAML 需求（分段 + 扁平格式）、源匹配与任务拆分、确定性
  `plan_id`/`task_id`、原子领取、租约 + 心跳、崩溃恢复与对账、暂停/恢复/重试/取消。
- **完整性与存储（M5）**：六层校验链（存在性/大小/SHA-256/协议/格式/科学）、
  staging→raw 原子提交、quarantine 隔离、交付清单与采集报告（JSON 导出）。
- **CDS 传输适配器**：`CdsApiTransport` 接管 `cds_api` 路由（签名 URL 下载、
  过期 403 不可重试），复用 HTTP 适配器的流式/续传/重试语义。
- **M6 轻量下载效率基准**：`scripts/m6_efficiency_bench.py`（三种获取条件、
  分阶段计时、产物 xarray 校验）、报告与验收记录。
- **工程设施**：GitHub Actions CI（Ubuntu + macOS，Python 3.12/3.13：lint、
  mypy strict、离线测试、覆盖率）、tag 触发的 Release 构建工作流。

### 实测数据（M6，2026-11-04）

- 纯 NCAR GDEX：端到端 **113.6 s**，下载 1 134 576 716 B，下载段 10.5 MB/s（无排队）。
- 纯 CDS API：3 天逐时请求在 CDS 拥塞期 **45 min 未就绪**（数据集级队列）；
  降量到 2°×2°×1 时次（25 116 B）后 **30.2 s** 完成（生成 28.71 s + 下载 1.49 s），
  相对拥塞期 **≥89×** 改善。
- 混合制（双源同时投递）：先就绪 **112.55 s**（NCAR 胜出），CDS 侧 45 min 未就绪；
  建议改为"串行 CDS + 超时转 NCAR"（CDS 现行政策要求串行提交）。

### 质量门

- 离线测试 **979 passed / 14 network deselected / 0 failed**（network 用例默认不跑）。
- 覆盖率 **85%**；`ruff check`、`ruff format --check` 全绿；`mypy --strict` 无问题。

### 已知限制与未验证（NOT RUN ≠ PASS）

- 调度器内真实 CDS 端到端（需 CDS 许可 + 排队；传输层已实现并有单测）。
- S3 端到端（无 MinIO/S3 环境）；S3 multipart 并行下载未实现。
- 长时间稳定性（数小时连续采集）、大文件 RSS 曲线未测。
- 多进程同时写同一 Manifest 不支持（单写者设计）。
- Globus / Zarr 访问路径未实现（不虚构桶名/前缀规则）。

[0.1.0]: https://github.com/Neiyako/ResearchDataHub/releases/tag/v0.1.0
