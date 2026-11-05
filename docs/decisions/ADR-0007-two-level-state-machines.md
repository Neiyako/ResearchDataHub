# ADR-0007：两层状态机 —— 传输任务态 与 数据获取需求态，并列不合并

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

仓库已有一套成熟的状态机 `core/state.py::TaskState`（12 态，144 组穷举测试）：

```
PENDING → PLANNED → QUEUED → RUNNING → VERIFYING → COMPLETED
                     ↘ RETRY_WAIT ↗        ↘ QUARANTINED
  失效态：FAILED / CANCELLED / SKIPPED / PAUSED
```

它描述的是**一个传输任务**（一个远端文件 → 一次字节搬运 + 校验）的生命周期。
`manifest` 的 `tasks` 表、`scheduler` 的原子领取、`recovery` 的过期接管、以及 144 组穷举测试
全部依赖它的语义。

而产品需求（指令 §11）要求的是**一次数据获取需求**的端到端生命周期：

```
CREATED → DISCOVERING → ASSESSING → VALIDATING → PLANNING → AWAITING_APPROVAL
        → QUEUED → EXECUTING → TRANSFERRING → VERIFYING → COMPLETED
异常态：NEEDS_CLARIFICATION / NEEDS_CONFIGURATION / PARTIALLY_SUPPORTED / FAILED / CANCELLED / PAUSED
```

它包含"发现、评估、环境检查引导、审批"等**根本不产生字节搬运**的阶段，
并且一次需求会在 `PLANNING` 之后**展开成 N 个传输任务**。

## 决策

**并列实现两套状态机，不合并、不扩展 `TaskState`。**

1. 保留 `core/state.py::TaskState` 与 `transition()` 原样不动（**一行不改转换表**）；
   仅在 docstring 中加一句指向本 ADR 的交叉引用，避免读者误以为它是全局状态机。
2. 新增 `core/acquisition.py`：

   - `AcquisitionState`（17 态），`ACQUISITION_TRANSITIONS` 转换表；
   - `transition_acquisition(record, target, *, reason, now, error)`，与 `transition()` 同样返回新对象、
     同样带业务前提检查（例如 `COMPLETED` 只能从 `VERIFYING` 进入）；
   - `aggregate_task_result(results) -> AcquisitionState`：把子任务结果聚合成需求级结论；
   - 穷举测试：`(17 × 17 = 289 组合)` 全部分区，与 `test_state_machine_illegal.py` 同等强度。
3. 持久化到 manifest 库的**新表** `acquisitions`（`acquisition_id` 主键 + `request_id` + `plan_id`
   + `state` + `state_history` JSON + `created_at` / `updated_at`），**不修改既有 `tasks` 表结构**。
4. 映射关系：

| 需求级状态 | 与任务级的关系 |
|---|---|
| `CREATED` … `AWAITING_APPROVAL` | 尚无任务；只有需求与计划 |
| `QUEUED` | 计划已批准，任务以 `PLANNED`/`QUEUED` 落库 |
| `EXECUTING` / `TRANSFERRING` | 至少一个任务处于 `RUNNING` |
| `VERIFYING` | 全部任务已结束，正在做需求级汇总校验 |
| `COMPLETED` | 全部任务 `COMPLETED` 且需求级校验 `PASSED` |
| `PARTIALLY_SUPPORTED` | 部分任务 `COMPLETED`、部分 `FAILED`/`SKIPPED`，且策略允许部分成功 |
| `FAILED` | 无可用产物，或策略不允许部分成功 |
| `NEEDS_CONFIGURATION` | 环境检查未通过（由 M2 `environment` 包判定） |
| `NEEDS_CLARIFICATION` | 需求缺关键科学参数（由 Agent 追问，Core 不猜） |

## 后果

**正面**

- 现有 979 个测试与 manifest/scheduler 语义零风险；
- 两套状态机各自有穷举测试，语义边界清晰；
- 需求级状态可以稳定回答"用户那次请求到底进行到哪一步了"，而传输态回答的是"第几个文件怎么样了"。

**负面 / 维护成本**

- 有两份"状态转换表"需要同步维护文档（`docs/architecture.md` 必须同时列出，且标注各自适用范围）；
- 需求级状态机在 `EXECUTING` 段是**聚合态**，聚合函数必须有测试（`aggregate_task_result`）。

## 被否决的方案

1. **扩展 `TaskState` 加入 8 个新态**：会让"一个传输任务"出现 `DISCOVERING` 这种无意义状态，
   并使 `recover_stale` 的语义污染（发现阶段没有租约）。**否决**。
2. **用需求级状态机替换 `TaskState`**：需要重写 manifest `tasks` 表、scheduler 领取逻辑、
   recovery、以及 144 组穷举测试，与指令 §17.12（不要把大规模重写已有可用模块）冲突。**否决**。
3. **不引入需求级状态机，靠 Agent 对话历史表示进度**：指令 §11 明确禁止
   "不得依赖 Agent 的对话历史模拟任务状态"。**否决**。
