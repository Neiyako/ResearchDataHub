# ADR-0010：维持 Python 3.12+ 与轻依赖；MCP SDK 作可选依赖

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

指令 §15 写 "Python 3.11+，具体版本根据依赖兼容性确认"，并明确"不要为了架构完整而堆砌
Redis、Kafka、Kubernetes、PostgreSQL 等基础设施；本地运行应保持轻量，适配低内存设备"。

现状：`pyproject.toml` 声明 `requires-python = ">=3.12"`，源码大量使用：

- PEP 695 `type X = ...` 语句（`models.py`、`transfer/base.py` 等）；
- `enum.StrEnum`（3.11 起有，没问题）；
- 3.12 的 f-string 语法与 `itertools.batched` 等。

## 决策

1. **维持 `requires-python = ">=3.12"`**，不在本轮改。
   理由：降到 3.11 需要把全部 PEP 695 `type` 语句改回 `TypeAlias`，纯属语法降级、零功能收益，
   且会大面积触碰 979 个测试覆盖的既有文件（违反 §17.12"不要大规模重写已有可用模块"）。
   3.12 于 2023-10 发布，已达稳定期；低内存设备上 3.12 的 venv 内存占用与 3.11 无实质差别。
2. **运行时依赖只增不减，且只增必需项**：
   - 现有：`pydantic` / `PyYAML` / `typer` / `rich` / `httpx` / `boto3`（均已被使用）；
   - 新增：**无**。MCP Server 用 `mcp` 作为 **optional extra**（`pip install "researchdatahub[mcp]"`），
     因为 MCP 只是入口之一；不用 MCP 的部署（纯 CLI）不应被迫安装它。
3. **不引入**：Redis、Kafka、Kubernetes、PostgreSQL、Celery、Dask、Spark。
   多节点能力由 `network/` 包用 HTTP + SQLite 实现（见 ADR-0013）。
4. **不引入 pandas/sklearn/torch**：本项目不做分析（指令 §2 边界），这些依赖出现即为越界信号。

## 后果

**正面**

- 安装体积小、冷启动快，在 2 GB 内存的设备上可跑；
- 不依赖外部服务，`pip install` 后即可用；
- 可选依赖模型让 "只用 CLI" 与 "给 Agent 用" 两种部署互不拖累。

**负面**

- 放弃对 Python 3.11 用户的支持（需在 README 明示，避免用户装完才失败）；
- `mcp` 作为可选依赖意味着 MCP 代码路径在默认测试环境里可能 import 失败 ——
  因此 `interfaces/mcp/` 必须做到"SDK 缺失时模块仍可导入，并返回结构化 `not_available` 错误"，
  而不是在 import 期抛 `ImportError`。此项写入 M5 验收标准。

## 被否决的方案

1. **降到 Python 3.11**：收益为零，成本是大面积语法改写。**否决**。
2. **把 `mcp` 放进必装依赖**：会让所有 CLI 用户多装一个只在 Agent 场景用的 SDK。**否决**。
3. **自研 MCP 协议层**（不依赖官方 SDK）：需要在 stdio 上手工实现 JSON-RPC 帧、initialize 握手、
   notifications 与 tools/list 变更通知，维护成本高且容易偏离规范。**否决**。
