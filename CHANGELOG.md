# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 结构，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
