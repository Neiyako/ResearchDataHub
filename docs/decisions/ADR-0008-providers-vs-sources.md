# ADR-0008：`providers/` 与 `sources/` 并存 —— 目录能力层 vs 执行资产层

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

现有 `sources/` 包（CDS、NCAR）的职责是：**给定一个已经结构化的 `DatasetRequest`，查出该数据源上有哪些文件资产（`SourceAsset`）**。
它回答的是"具体哪几个文件、多大、什么校验和、走哪条访问路径"。它是**执行面**。

产品需求（指令 §7 Step 2–3、§5）要求 Agent 能够：

- `search_datasets`：从**多个平台**按关键词/变量/时空范围检索**数据集**（而不是文件）；
- `inspect_dataset`：查看某数据集的真实能力（变量清单与单位、时空覆盖、格式、认证要求、获取方式）；
- `compare_datasets`：并列比较候选数据集；
- `get_provider_capabilities`：某个平台**真实支持**哪些能力。

这些是**发现与评估**，发生在"构造 `DatasetRequest` 之前"。现有 `sources/` 无法承担，
因为它需要一个已经构造好的 `DatasetRequest` 作为输入。

## 决策

**新增 `src/researchdatahub/providers/`，与 `sources/` 并存，职责严格分工。**

| 层 | 包 | 输入 | 输出 | 面向 |
|---|---|---|---|---|
| 目录与能力 | `providers/` | 自然语言派生的检索条件 | `DatasetCandidate` / `DatasetDetail` / `ProviderCapabilities` | **Agent**（MCP `search_datasets` 等） |
| 资产发现与执行 | `sources/` | 已校验的 `DatasetRequest` | `SourceAsset`（文件级） | **执行层**（planner → scheduler → transfer） |

桥接路径（V1 实现方式）：

```
providers.search()  →  DatasetCandidate(provider, dataset_id, variables, coverage, formats)
                    ↓  Agent 与用户确认
providers.inspect() →  DatasetDetail（真实变量/单位/覆盖/认证要求）
                    ↓  构造 DatasetRequest（providers 提供 to_dataset_request() 辅助）
planner/sources/    →  SourceAsset → scheduler → transfer → integrity
```

**关键约束**：`providers/` **不得**自己下载字节、不得自己写文件、不得绕过 `integrity`。
它只做目录查询与能力声明。

## 后果

**正面**

- Agent 面与执行面解耦：新增一个平台可以先只做"可检索+可检视"（`providers/`），
  执行支持稍后再接 `sources/`，不必一次做全；
- 能力声明（`verified` / `unverified` / `unsupported`）有唯一出口，避免 README 与代码漂移。

**负面**

- 同一平台可能出现两份元数据解析代码（`providers/earthdata` 与将来的 `sources/earthdata`）。
  缓解：`providers/` 的解析结果结构（`DatasetDetail`）设计成可直接转换为 `DatasetRequest`，
  且 `sources/` 侧只做文件清单查询，不重复解析科学元数据。
- 需要在 `docs/architecture.md` 明确画出两层边界，否则新人容易往 `sources/` 里加 search 逻辑。

## 被否决的方案

1. **把 `sources/` 扩展成既有 search 又有 assets**：会让 `DataSourceAdapter` Protocol 的方法从 4 个涨到 14 个，
   而多数适配器只需其中一部分，迫使它们实现空方法（正是指令 §17.14 禁止的"仅接口定义冒充完成"）。**否决**。
2. **用 `catalog/`（现有只有 Protocol 的包）代替 `providers/`**：`catalog/` 的既有语义是"声明式产品目录"（静态清单），
   与"运行时真实查询远端平台"不同。把两者合并会让一个包无法判断何时该联网。**否决**，但 `providers/registry.py`
   会提供 `list_providers()`，将来可让 `catalog/` 消费它。
