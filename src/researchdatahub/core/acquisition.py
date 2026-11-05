"""数据获取**需求级**状态机（与传输任务级状态机并列，见 ADR-0007）。

为什么要有第二套状态机
----------------------
``core.state.TaskState`` 描述的是**一个传输任务**（一个远端文件 → 一次字节搬运 + 校验）的生命周期。
而用户提出的是**一次数据获取需求**，它包含"发现数据源、检查数据集能力、检查环境、生成计划、
等待用户批准"等阶段——这些阶段不产生任何字节搬运，也不存在租约与心跳。

两者语义不同，强行合并会让 ``TaskState`` 出现 ``DISCOVERING`` 这类无意义状态，
并污染已由 144 组穷举测试覆盖的既有不变式。因此本模块**独立**定义需求级状态机。

状态图
------
::

    CREATED ──► DISCOVERING ──► ASSESSING ──► VALIDATING ──► PLANNING ──► AWAITING_APPROVAL
                   │               │              │              │                │
                   │               │              │              │                ▼
                   │               │              │              │              QUEUED
                   │               │              │              │                │
                   │               │              │              │                ▼
                   │               │              │              │             EXECUTING
                   │               │              │              │                │
                   │               │              │              │                ▼
                   │               │              │              │          TRANSFERRING
                   │               │              │              │                │
                   │               │              │              │                ▼
                   │               │              │              │             VERIFYING
                   │               │              │              │                │
                   ▼               ▼              ▼              ▼                ▼
              NEEDS_CLARIFICATION / NEEDS_CONFIGURATION / PARTIALLY_SUPPORTED / FAILED

   任意非终态 ──► CANCELLED（用户随时可以叫停）
   任意非终态 ──► PAUSED ──► 回到暂停前的状态（由 :func:`resume_state` 计算）
   异常态 NEEDS_CLARIFICATION / NEEDS_CONFIGURATION ──► 回到 VALIDATING（补齐后继续）
   PARTIALLY_SUPPORTED ──► VERIFYING / COMPLETED（用户接受部分结果）

不变式
------
1. 终态（``COMPLETED`` / ``FAILED`` / ``CANCELLED``）不再转换。
2. ``COMPLETED`` 只能从 ``VERIFYING`` 进入（对应传输态的 "Integrity Before Delivery" 原则：
   没有经过验证的交付不算完成）。
3. ``CANCELLED`` 可从任意非终态到达。
4. ``PAUSED`` 可从任意非终态进入；恢复时回到 ``resume_state``，**不从 CREATED 重新开始**。
5. ``AWAITING_APPROVAL`` 只能从 ``PLANNING`` 进入；``QUEUED`` 只能从 ``AWAITING_APPROVAL`` 进入
   （审批令牌是进入执行前的唯一闸门，见 ADR-0011）。
6. ``PARTIALLY_SUPPORTED`` 只能从 ``VERIFYING`` 进入
   （部分成功也必须先经过验证，否则无法知道哪部分成功）。

与传输任务状态的关系
--------------------
需求级在 ``EXECUTING`` / ``TRANSFERRING`` / ``VERIFYING`` 段是**聚合态**，
由 :func:`aggregate_task_result` 根据其下任务的 ``TaskState`` 推导。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.state import TaskState

__all__ = [
    "ACQUISITION_TERMINAL_STATES",
    "ACQUISITION_TRANSITIONS",
    "AcquisitionRecord",
    "AcquisitionState",
    "AggregateOutcome",
    "aggregate_task_state",
    "can_transition_acquisition",
    "coerce_acquisition_state",
    "describe_acquisition_transitions",
    "is_terminal_acquisition",
    "resume_state",
    "transition_acquisition",
]


class AcquisitionState(StrEnum):
    """一次数据获取需求的端到端生命周期。

    持久化为字符串，因此新增状态不会破坏已落库的记录。
    """

    CREATED = "CREATED"  # 需求已登记，尚未开始发现
    DISCOVERING = "DISCOVERING"  # 正在搜索数据平台/候选数据集
    ASSESSING = "ASSESSING"  # 正在检查候选数据集的真实能力（变量/时空/格式/访问方式）
    VALIDATING = "VALIDATING"  # 正在校验请求参数与环境（依赖/凭据/连通性/磁盘）
    PLANNING = "PLANNING"  # 正在生成获取计划（平台/节点/链路/规模/风险）
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 计划已生成，等待用户批准（副作用闸门）
    QUEUED = "QUEUED"  # 计划已批准，任务已入队
    EXECUTING = "EXECUTING"  # 至少一个任务正在执行
    TRANSFERRING = "TRANSFERRING"  # 数据正在传输
    VERIFYING = "VERIFYING"  # 传输结束，正在做需求级汇总校验
    COMPLETED = "COMPLETED"  # 全部成功且校验通过（终态）
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"  # 缺少关键科学参数，需用户补充
    NEEDS_CONFIGURATION = "NEEDS_CONFIGURATION"  # 环境/凭据未就绪，需用户配置
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"  # 部分变量/任务无法满足，已交付其余部分
    FAILED = "FAILED"  # 无可用产物（终态）
    CANCELLED = "CANCELLED"  # 用户取消（终态）
    PAUSED = "PAUSED"  # 用户暂停


ACQUISITION_TERMINAL_STATES: Final[frozenset[AcquisitionState]] = frozenset(
    {
        AcquisitionState.COMPLETED,
        AcquisitionState.FAILED,
        AcquisitionState.CANCELLED,
    }
)
"""需求级终态：不再发生任何转换。"""

_NON_TERMINAL: Final[tuple[AcquisitionState, ...]] = tuple(
    state for state in AcquisitionState if state not in ACQUISITION_TERMINAL_STATES
)

ACQUISITION_TRANSITIONS: Final[dict[AcquisitionState, frozenset[AcquisitionState]]] = {
    AcquisitionState.CREATED: frozenset(
        {
            AcquisitionState.DISCOVERING,
            AcquisitionState.NEEDS_CLARIFICATION,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.DISCOVERING: frozenset(
        {
            AcquisitionState.ASSESSING,
            AcquisitionState.NEEDS_CLARIFICATION,
            AcquisitionState.NEEDS_CONFIGURATION,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.ASSESSING: frozenset(
        {
            AcquisitionState.VALIDATING,
            AcquisitionState.PARTIALLY_SUPPORTED,
            AcquisitionState.NEEDS_CLARIFICATION,
            AcquisitionState.NEEDS_CONFIGURATION,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.VALIDATING: frozenset(
        {
            AcquisitionState.PLANNING,
            AcquisitionState.NEEDS_CLARIFICATION,
            AcquisitionState.NEEDS_CONFIGURATION,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.PLANNING: frozenset(
        {
            AcquisitionState.AWAITING_APPROVAL,
            AcquisitionState.NEEDS_CONFIGURATION,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    # 审批令牌是进入执行前的唯一闸门：QUEUED 只能由 AWAITING_APPROVAL 进入。
    AcquisitionState.AWAITING_APPROVAL: frozenset(
        {
            AcquisitionState.QUEUED,
            AcquisitionState.PLANNING,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.QUEUED: frozenset(
        {
            AcquisitionState.EXECUTING,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.EXECUTING: frozenset(
        {
            AcquisitionState.TRANSFERRING,
            AcquisitionState.VERIFYING,
            AcquisitionState.FAILED,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.TRANSFERRING: frozenset(
        {
            AcquisitionState.VERIFYING,
            AcquisitionState.EXECUTING,
            AcquisitionState.FAILED,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.VERIFYING: frozenset(
        {
            AcquisitionState.COMPLETED,
            AcquisitionState.PARTIALLY_SUPPORTED,
            AcquisitionState.FAILED,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.NEEDS_CLARIFICATION: frozenset(
        {
            AcquisitionState.VALIDATING,
            AcquisitionState.DISCOVERING,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.NEEDS_CONFIGURATION: frozenset(
        {
            AcquisitionState.VALIDATING,
            AcquisitionState.PLANNING,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    AcquisitionState.PARTIALLY_SUPPORTED: frozenset(
        {
            AcquisitionState.VERIFYING,
            AcquisitionState.COMPLETED,
            AcquisitionState.PAUSED,
            AcquisitionState.CANCELLED,
            AcquisitionState.FAILED,
        }
    ),
    # 暂停是"回到之前的位置"，因此可回到任一非终态**除了 QUEUED**：
    # 进入 QUEUED 是审批闸门的唯一出口，若允许 PAUSED -> QUEUED，
    # 用户就能"暂停一个未批准的 AWAITING_APPROVAL，再恢复成 QUEUED"，绕过审批令牌（ADR-0011）。
    # 未批准的计划恢复后回到 AWAITING_APPROVAL 重新审批；已批准的在执行中暂停，恢复后回到执行态。
    AcquisitionState.PAUSED: frozenset(
        {
            *(
                state
                for state in _NON_TERMINAL
                if state not in (AcquisitionState.PAUSED, AcquisitionState.QUEUED)
            ),
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.COMPLETED: frozenset(),
    AcquisitionState.FAILED: frozenset(),
    AcquisitionState.CANCELLED: frozenset(),
}
"""合法需求级状态转换表。它是唯一事实来源，测试与文档都以此为准。"""

# 常量自检（导入即验证，与 core.state 同样的做法）。
if len(ACQUISITION_TRANSITIONS) != len(AcquisitionState):  # pragma: no cover - 常量自检
    raise AssertionError("ACQUISITION_TRANSITIONS must cover every AcquisitionState")
for _state in _NON_TERMINAL:  # pragma: no cover - 常量自检
    if AcquisitionState.CANCELLED not in ACQUISITION_TRANSITIONS[_state]:
        raise AssertionError(f"invariant violated: {_state.value} must allow CANCELLED")
for _terminal in ACQUISITION_TERMINAL_STATES:  # pragma: no cover - 常量自检
    if ACQUISITION_TRANSITIONS[_terminal]:
        raise AssertionError(f"invariant violated: terminal {_terminal.value} must have no exits")

_PAUSABLE_STATES: Final[frozenset[AcquisitionState]] = frozenset(
    state for state in AcquisitionState if state not in ACQUISITION_TERMINAL_STATES
)
"""可进入 PAUSED 的状态：全部非终态（``PAUSED -> PAUSED`` 由幂等规则挡掉）。"""


def is_terminal_acquisition(state: AcquisitionState) -> bool:
    """判断需求级状态是否为终态。

    Parameters
    ----------
    state:
        待判断的状态。

    Returns
    -------
    bool
        ``True`` 表示终态（``COMPLETED`` / ``FAILED`` / ``CANCELLED``）。
    """
    return state in ACQUISITION_TERMINAL_STATES


def can_transition_acquisition(source: AcquisitionState, target: AcquisitionState) -> bool:
    """判断 ``source -> target`` 是否被转换表允许（不代表业务前提已满足）。

    Parameters
    ----------
    source, target:
        起始与目标状态。

    Returns
    -------
    bool
        同一状态返回 ``False``（幂等，不算转换）。
    """
    if source is target:
        return False
    return target in ACQUISITION_TRANSITIONS[source]


def _normalize_now(now: datetime | None) -> datetime:
    """把时间戳规整为 UTC；naive 时间直接拒绝（时区不明的状态时间戳不可审计）。"""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


class AcquisitionRecord(BaseModel):
    """一次数据获取需求的持久化状态记录（manifest 库 ``acquisitions`` 表）。

    Attributes
    ----------
    acquisition_id:
        需求级标识；与 ``request_id`` 一同用于跨会话查询。
    state:
        当前需求级状态。
    previous_state:
        进入 ``PAUSED`` 前的状态，用于 :func:`resume_state`；非暂停态时为 ``None``。
    history:
        追加式状态历史（``"<ts> A->B: reason"``），用于审计，不参与业务判断。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(default="1.0")
    acquisition_id: str = Field(min_length=2, max_length=64)
    request_id: str = Field(min_length=2, max_length=64)
    plan_id: str | None = Field(default=None, max_length=64)
    state: AcquisitionState = AcquisitionState.CREATED
    previous_state: AcquisitionState | None = None
    created_at: datetime
    updated_at: datetime
    error: str | None = Field(default=None, max_length=2048)
    history: list[str] = Field(default_factory=list)
    extensions: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acquisition timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def is_terminal(self) -> bool:
        """该需求是否已到终态。"""
        return is_terminal_acquisition(self.state)


def transition_acquisition(
    record: AcquisitionRecord,
    target: AcquisitionState,
    *,
    reason: str,
    now: datetime | None = None,
    error: str | None = None,
    plan_id: str | None = None,
) -> AcquisitionRecord:
    """把需求推进到 ``target``，返回**新的**记录对象（不就地修改）。

    Parameters
    ----------
    record:
        当前记录（不可变）。
    target:
        目标状态。
    reason:
        人类可读的转换原因，追加进 ``history``，用于审计。
    now:
        tz-aware 时间戳；``None`` 时取当前 UTC。
    error:
        失败/异常说明，写入 ``error``。
    plan_id:
        绑定计划 ID（进入 ``AWAITING_APPROVAL`` 时需要）。

    Returns
    -------
    AcquisitionRecord
        状态更新后的新记录。调用方负责持久化。

    Raises
    ------
    StateTransitionError
        转换不在 :data:`ACQUISITION_TRANSITIONS` 中，或违反业务前提。
    """
    current = record.state
    if not can_transition_acquisition(current, target):
        raise StateTransitionError(
            f"illegal acquisition transition {current.value} -> {target.value}",
            from_state=current.value,
            to_state=target.value,
            detail={"reason": reason},
        )

    stamp = _normalize_now(now)

    # ---- 业务前提：表驱动允许 ≠ 业务成立 ------------------------------- #
    if target is AcquisitionState.COMPLETED and current is not AcquisitionState.VERIFYING:
        raise StateTransitionError(
            "COMPLETED is only reachable from VERIFYING (integrity before delivery)",
            from_state=current.value,
            to_state=target.value,
        )
    if target is AcquisitionState.PARTIALLY_SUPPORTED and current not in (
        AcquisitionState.VERIFYING,
        AcquisitionState.ASSESSING,
    ):
        raise StateTransitionError(
            "PARTIALLY_SUPPORTED is only reachable from VERIFYING (or ASSESSING for "
            "unsatisfiable variables)",
            from_state=current.value,
            to_state=target.value,
        )
    if target is AcquisitionState.QUEUED and current is not AcquisitionState.AWAITING_APPROVAL:
        raise StateTransitionError(
            "QUEUED requires a preceding AWAITING_APPROVAL (approval gate, see ADR-0011)",
            from_state=current.value,
            to_state=target.value,
        )
    if target is AcquisitionState.PAUSED and current is AcquisitionState.PAUSED:
        raise StateTransitionError(
            "already PAUSED",
            from_state=current.value,
            to_state=target.value,
        )

    updates: dict[str, object] = {
        "state": target,
        "updated_at": stamp,
        "history": [
            *record.history,
            f"{stamp.isoformat()} {current.value}->{target.value}: {reason}",
        ],
    }
    if target is AcquisitionState.PAUSED:
        # 暂停时记录来源状态；若已处于暂停（理论上被上面挡掉），保留原 previous_state。
        updates["previous_state"] = current
    elif current is AcquisitionState.PAUSED:
        updates["previous_state"] = None
    if plan_id is not None:
        updates["plan_id"] = plan_id
    if target in (
        AcquisitionState.FAILED,
        AcquisitionState.NEEDS_CLARIFICATION,
        AcquisitionState.NEEDS_CONFIGURATION,
    ):
        updates["error"] = error or reason
    return record.model_copy(update=updates)


def resume_state(record: AcquisitionRecord) -> AcquisitionState:
    """计算从 ``PAUSED`` 恢复时应回到的状态。

    回退到**暂停前**的状态，而不是从头开始；这才是"恢复"而非"重跑"。

    Parameters
    ----------
    record:
        当前记录，必须处于 ``PAUSED``。

    Returns
    -------
    AcquisitionState
        应恢复到的状态。

    Raises
    ------
    StateTransitionError
        记录不是 ``PAUSED``，或缺少 ``previous_state``。
    """
    if record.state is not AcquisitionState.PAUSED:
        raise StateTransitionError(
            "record is not PAUSED; nothing to resume",
            from_state=record.state.value,
            to_state=record.state.value,
        )
    previous = record.previous_state
    if previous is None:
        raise StateTransitionError(
            "PAUSED record has no previous_state; cannot decide where to resume",
            from_state=record.state.value,
            to_state=record.state.value,
        )
    if is_terminal_acquisition(previous):  # pragma: no cover - 转换表已阻止
        raise StateTransitionError(
            "pause origin is terminal; refusing to resume",
            from_state=record.state.value,
            to_state=previous.value,
        )
    return previous


class AggregateOutcome(StrEnum):
    """子任务状态的聚合结论。"""

    PENDING = "pending"  # 还有任务未结束
    ALL_SUCCEEDED = "all_succeeded"  # 全部 COMPLETED
    PARTIAL = "partial"  # 有成功也有非成功（失败/隔离/跳过）
    ALL_FAILED = "all_failed"  # 无一 COMPLETED 且已全部结束
    EMPTY = "empty"  # 没有任务（计划为空）


_SUCCEEDED_STATES: Final[frozenset[TaskState]] = frozenset({TaskState.COMPLETED})
_UNFINISHED_STATES: Final[frozenset[TaskState]] = frozenset(
    {
        TaskState.PENDING,
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.RETRY_WAIT,
        TaskState.PAUSED,
    }
)


def aggregate_task_state(states: Iterable[TaskState]) -> AggregateOutcome:
    """把一组**传输任务**状态聚合成需求级结论。

    Parameters
    ----------
    states:
        下属任务的状态集合（可重复，重复不影响结论）。

    Returns
    -------
    AggregateOutcome
        聚合结论。只要有未结束任务就返回 ``PENDING``（不提前下结论）。
    """
    materialised = list(states)
    if not materialised:
        return AggregateOutcome.EMPTY
    if any(state in _UNFINISHED_STATES for state in materialised):
        return AggregateOutcome.PENDING
    succeeded = sum(1 for state in materialised if state in _SUCCEEDED_STATES)
    if succeeded == len(materialised):
        return AggregateOutcome.ALL_SUCCEEDED
    if succeeded == 0:
        return AggregateOutcome.ALL_FAILED
    return AggregateOutcome.PARTIAL


def aggregate_task_result(
    states: Sequence[TaskState],
    *,
    allow_partial: bool = False,
) -> AcquisitionState | None:
    """把子任务状态映射为需求级目标状态。

    Parameters
    ----------
    states:
        下属任务状态。
    allow_partial:
        是否允许"部分成功"仍算可接受结果（``planner.allow_partial``）。

    Returns
    -------
    AcquisitionState | None
        ``None`` 表示"还不能下结论"（仍有任务在跑），调用方应保持在
        ``EXECUTING`` / ``TRANSFERRING``。
    """
    outcome = aggregate_task_state(states)
    if outcome in (AggregateOutcome.PENDING,):
        return None
    if outcome is AggregateOutcome.EMPTY:
        return AcquisitionState.FAILED
    if outcome is AggregateOutcome.ALL_SUCCEEDED:
        return AcquisitionState.VERIFYING
    if outcome is AggregateOutcome.ALL_FAILED:
        return AcquisitionState.FAILED
    # PARTIAL
    return AcquisitionState.VERIFYING if allow_partial else AcquisitionState.FAILED


def coerce_acquisition_state(value: str) -> AcquisitionState:
    """把落库的字符串转回枚举。

    Parameters
    ----------
    value:
        状态字符串。

    Returns
    -------
    AcquisitionState
        对应枚举。

    Raises
    ------
    StateTransitionError
        未知状态（说明数据库里有本版本不认识的写入方，必须显式处理而不是静默当 CREATED）。
    """
    try:
        return AcquisitionState(value)
    except ValueError as exc:
        raise StateTransitionError(
            f"unknown acquisition state {value!r}",
            from_state=value,
            to_state=value,
        ) from exc


def describe_acquisition_transitions() -> str:
    """生成人类可读的需求级转换表（文档与 CLI 复用，避免文档与代码漂移）。"""
    lines = []
    for state in AcquisitionState:
        targets = sorted(item.value for item in ACQUISITION_TRANSITIONS[state])
        lines.append(f"{state.value:<20} -> {', '.join(targets) if targets else '(terminal)'}")
    return "\n".join(lines)


def history_summary(history: Sequence[str], *, limit: int = 5) -> Mapping[str, object]:
    """返回历史摘要（最后 ``limit`` 条 + 总数），供 MCP ``get_task_status`` 使用。"""
    recent = list(history[-limit:]) if limit > 0 else []
    return {"total": len(history), "recent": recent}
