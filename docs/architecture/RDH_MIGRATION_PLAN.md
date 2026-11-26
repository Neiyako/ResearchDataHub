# RDH 迁移计划

| 阶段 | 交付 | 复用 | 新增或修改 | 验收 |
|---|---|---|---|---|
| 0 审计 | 当前状态、目标架构、差距 | `docs/architecture.md`、模块边界 | 三份架构文档 | 源码与测试证据可追溯 |
| 1 契约 | Manifest、Task、Route、Verification 序列化 | `core.models`、`manifest`、`routing.models` | 仅补兼容字段 | round-trip 与迁移测试 |
| 2 初始化 | 环境探测、网络 probe、benchmark、route registry | `environment`、`routing.probe`、`routing.registry` | CLI init/network 命令 | 幂等、失败态、无凭据落盘 |
| 3 路由 | 多源资格与 CDS 回退策略 | `routing.engine`、source registry | 选择原因和历史衰减 | 离线 fixture + network canary |
| 4 传输 | 400 MiB 门禁、Range、续传、重试 | `transfer.base/http/s3` | `max_response_bytes` 配置 | 超限、200/206、ETag 变化测试 |
| 5 调度 | 全局资源、动态并发、本地恢复 | `scheduler`、manifest lease | dispatcher 接口 | 并发、限流、重启恢复测试 |
| 6 验证 | 查漏补缺与跨路线比较 | `integrity`、reporting | 缺失项任务生成 | 分层验证和不完整终态 |
| 7 记忆与验收 | 环境/路线记忆、benchmark、报告 | 现有持久化和文档 | 记忆写入策略 | 真实条件明确标记，不伪造结果 |

迁移遵循增量原则。每阶段先运行针对性测试，再运行默认离线套件。数据库只通过版本化迁移更新，失败时回滚事务。旧 CLI、数据路径、任务状态和报告字段保持兼容。
