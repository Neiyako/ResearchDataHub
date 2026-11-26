# RDH 升级交付报告

## 状态定义

- **IMPLEMENTED** 表示源码或契约已存在。
- **TESTED** 表示有可重复的自动化测试证据。
- **VERIFIED** 表示在目标运行条件中获得实际运行证据。
- **CONDITIONAL** 表示依赖凭据、外部服务或环境条件。
- **NOT_IMPLEMENTED** 表示当前没有实现，不能用文档代替。

## 交付矩阵

| 能力 | 状态 | 证据 |
|---|---|---|
| 现状审计、目标架构、迁移计划 | IMPLEMENTED | `docs/architecture/RDH_*.md` |
| Manifest、任务、路线、完整性契约 | TESTED | `src/researchdatahub/core`、`manifest`、`routing`、`integrity` 与 unit tests |
| 环境与路线探测 | TESTED/CONDITIONAL | `environment`、`routing.probe`，真实出口取决于本机网络 |
| 多源路由与 CDS 回退 | TESTED/CONDITIONAL | `routing.engine`、source tests，CDS 凭据链路未在离线套件伪造 |
| 400 MiB 单次响应体门禁 | IMPLEMENTED | `TransferConfig.max_response_bytes`，运行时在 `TransportAdapterBase._stream_to_part` 拒绝超限 |
| Range、续传、原子提交、重试 | TESTED | `tests/unit/test_transfer` |
| 并发调度、租约、心跳恢复 | TESTED | `tests/unit/test_scheduler`、manifest lease tests |
| 分层验证与报告 | TESTED | `tests/unit/test_integrity`、`reporting` tests |
| 多 Agent dispatcher | IMPLEMENTED/CONDITIONAL | `network/workers` 与本地 scheduler，外部 Agent 不作为运行前提 |
| 真实大规模数据、VPN、S3/CDS 服务端验收 | CONDITIONAL | 需要用户授权凭据和目标环境，当前未伪造 |
| 性能对照与端到端报告 | TESTED/CONDITIONAL | `docs/M6-efficiency-benchmark.md`、`docs/M6-verification.md` |

## 已知验证限制

系统 Python 直接运行全量 pytest 会因缺少 `boto3` 在收集阶段失败。项目 `.venv` 已包含该依赖，应使用 `PYTHONPATH=src .venv/bin/python -m pytest`。真实 network 标记默认关闭，相关结果必须单独标记为 CONDITIONAL 或 NOT_RUN。
