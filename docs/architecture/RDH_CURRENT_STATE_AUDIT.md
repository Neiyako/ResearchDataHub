# RDH 当前状态审计

审计基于仓库源码、测试与现有架构文档，结论按证据区分。

## 架构与调用链

CLI/MCP 入口调用 `config` 与 `core` 契约，`routing` 发现并选择数据源路线，`planner` 生成 `AcquisitionPlan`，`manifest` 以 SQLite 保存事实，`scheduler` 通过有界线程池领取任务，`transfer` 负责 HTTP/S3 流式传输，`integrity` 分层校验，`storage` 原子提交，`reporting` 生成交付清单。`sources` 只发现资产，未承担字节传输。

## 已有能力

- Pydantic 数据模型、状态机、异常和 Protocol 已在 `core` 集中定义。
- Manifest 已提供迁移、WAL、任务租约、心跳和原子领取。
- HTTP 已支持流式下载、重试、`.part` 暂存、ETag/Last-Modified 自证续传和原子替换。
- S3、CDS、NCAR、Earthdata、Copernicus 适配器已存在，route intelligence 已提供能力检查与可解释选择。
- Integrity checker 已覆盖存在性、大小、校验和、格式、NetCDF 与科学元数据层。
- 本地调度器是 Agent 可选的基础执行路径。

## 未闭合项

- 单次实际响应体上限此前没有显式配置和统一运行时门禁，已在本轮补为 `TransferConfig.max_response_bytes`，硬上限为 400 MiB。
- 真实 CDS 凭据、S3 服务端、VPN 出口和大规模真实数据验收需要外部条件，仓库测试不能替代这些证据。
- 全量测试应使用项目 `.venv`，系统 Python 缺少 `boto3` 会在收集阶段失败。

## 证据与风险

默认测试标记排除 network。`docs/M6-verification.md` 和 `docs/M6-efficiency-benchmark.md` 记录了已运行的离线与轻量基准。数据库 schema、CLI 和模块边界已有专门文档，迁移时应保持向前兼容并继续使用 SQLite manifest 作为事实源。
