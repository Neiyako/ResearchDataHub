# ADR 索引（Decision Log）

本目录记录 ResearchDataHub 的架构决策。与 `docs/adr/`（M0–M6 时期的历史决策）并存：
`docs/adr/` 是既有记录，不搬迁；本目录承载 **M0 审计之后** 的新决策。

格式：每条 ADR 含 **背景 / 决策 / 后果 / 被否决的方案 / 状态**。
状态取值：`Accepted` / `Superseded by ADR-xxxx` / `Deprecated`。

---

## 历史决策（`docs/adr/`，前置，仍然有效）

| ADR | 主题 | 状态 |
|---|---|---|
| [ADR-0001](../adr/ADR-0001-modular-monolith.md) | 模块化单体，不拆服务、不引入消息队列 | Accepted |
| [ADR-0002](../adr/ADR-0002-source-transport-separation.md) | 数据源适配与传输实现分离 | Accepted |
| [ADR-0003](../adr/ADR-0003-sqlite-manifest-source-of-truth.md) | SQLite Manifest 作为唯一事实来源 | Accepted |
| [ADR-0004](../adr/ADR-0004-task-recovery-lease-heartbeat.md) | 任务恢复用租约 + 心跳 | Accepted |
| [ADR-0005](../adr/ADR-0005-staging-raw-quarantine-storage.md) | staging → raw 原子提交 + 隔离区 | Accepted |
| [ADR-0006](../adr/ADR-0006-integrity-check-layering.md) | 完整性检查分层 | Accepted |

## 本轮决策（M0 审计之后）

| ADR | 主题 | 状态 |
|---|---|---|
| [ADR-0007](ADR-0007-two-level-state-machines.md) | 两层状态机：传输任务态 vs 数据获取需求态，**并列不合并** | Accepted |
| [ADR-0008](ADR-0008-providers-vs-sources.md) | `providers/`（Agent 面目录与能力）与 `sources/`（执行面资产发现）**并存** | Accepted |
| [ADR-0009](ADR-0009-cli-and-mcp-share-core-services.md) | CLI 与 MCP 是两个入口、同一 Core service 层 | Accepted |
| [ADR-0010](ADR-0010-python-version-and-deps.md) | 维持 Python 3.12+ 与轻依赖；MCP SDK 作可选依赖 | Accepted |
| [ADR-0011](ADR-0011-approval-token-model.md) | 有副作用操作的 HMAC 审批令牌模型 | Accepted |
| [ADR-0012](ADR-0012-request-contract-v1-2.md) | 版本化数据请求契约 v1.2 的坐标/时间/频率/原生变量映射语义 | Accepted |
| [ADR-0013](ADR-0013-route-selection-from-measurement.md) | 链路选择只依据**实测**性能记录，禁止按地域推断 | Accepted |
| [ADR-0014](ADR-0014-no-cdsapi-live-tests.md) | 禁止用 cdsapi 做联网抓取测试（用户指令），CDS 走离线 schema/翻译 + 目录只读探测 | Accepted |
| [ADR-0015](ADR-0015-delivery-to-user-path.md) | 交付路径：数据必须落到用户要求的位置，默认不静默覆盖 | Accepted |

---

## 决策记录模板

```markdown
# ADR-XXXX：<一句话标题>

- 状态：Accepted
- 日期：YYYY-MM-DD
- 决策人：<角色>

## 背景
（为什么需要这个决策；约束来自哪里）

## 决策
（明确、可执行的选择）

## 后果
（正面 / 负面 / 需要额外维护的东西）

## 被否决的方案
（以及否决理由——这一节不能留空，否则看不出真的权衡过）
```
