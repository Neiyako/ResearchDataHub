# ADR-0009：CLI 与 MCP 是两个入口、同一 Core service 层

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

仓库现有的唯一入口是 CLI（`rdh`），业务逻辑分布在 service 层：

- `planner/service.py`（`validate_request_file` / `create_plan` / `show_plan` / `plan_overview`）
- `scheduler/service.py`（`TaskControlService`：`status` / `pause` / `resume` / `retry` / `cancel`）
- `manifest/cli_service.py`、`integrity/`、`reporting/`

产品需求要求 MCP Server 作为 Agent 的正式入口（指令 §3、§8），并把 CLI 降为运维面。
风险：**如果 MCP handler 自己实现一遍业务逻辑，两份实现必然分叉**（CLI 能用、MCP 不能用，或反之），
而这种分叉在科研数据基础设施里是数据可靠性事故（例如校验规则不一致）。

## 决策

1. **入口层薄，service 层厚**。CLI 命令函数与 MCP tool handler **都只做**：
   解析/校验入参 → 调用 Core service → 包装返回结构。禁止在入口层写任何业务规则。
2. **禁止入口互相调用**。`cli/*` 不得 import `interfaces/mcp/*`，反之亦然；两者只共同 import Core service。
3. **新增能力时先加 service**：若 MCP 需要的功能在 service 层不存在（例如 `estimate_acquisition`），
   先在该领域的 service 层实现并写单测，再由 MCP handler 调用；**不允许**在 handler 里内联实现。
4. Core service 层不依赖任何入口库（typer / mcp），也不依赖 LLM（指令 §17.7）。

依赖方向：

```
cli/            interfaces/mcp/         ← 入口层（薄）
     ↘            ↙
      Core services (planner/scheduler/manifest/integrity/reporting/environment/providers/network)
                    ↓
                core/ (契约、状态机、错误、Protocol)
```

## 后果

**正面**

- 一份业务实现，两个入口，行为一致；
- MCP tool 的单测可以只测"入参校验 + service 被正确调用"（用 mock service），业务正确性由 service 单测保证，测试成本低；
- 将来加第三个入口（HTTP API、gRPC）不需要动业务代码。

**负面**

- 需要额外的 service 层封装（多一层转发）；对简单 read-only 工具（如 `get_provider_capabilities`）显得啰嗦。
  缓解：允许"极薄读取"直接在 handler 里做**一次**既有 service/registry 调用，但只要出现分支/聚合/写操作就必须下沉。
- 需要在 code review 中持续把关，否则 handler 会慢慢长胖。

## 被否决的方案

1. **MCP handler 直接调用 `planner/planner.py`、`scheduler/scheduler.py` 的底层类**：会让 handler 承担装配与错误映射，
   与 CLI 的装配逻辑分叉。**否决**。
2. **让 CLI 调用 MCP tool**（把 MCP 当唯一 API）：会引入不必要的进程/序列化开销，且 CLI 需要更多人类友好渲染
   （中文表格、退出码）。**否决**。
