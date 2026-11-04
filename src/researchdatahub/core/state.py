"""任务状态机。

所有任务状态变更**必须**通过本模块的 :func:`transition`，禁止在其它模块里
``task.state = ...``（模型是 frozen 的，因此凑巧也改不了，但更重要的是语义集中）。

状态图
------
::

    PENDING ──► PLANNED ──► QUEUED ──► RUNNING ──► VERIFYING ──► COMPLETED
       │           │          │           │            │
       │           │          │           ├──► RETRY_WAIT ──► QUEUED
       │           │          │           └──► FAILED (terminal)
       │           │          │                        │
       │           │          └──► PAUSED (queued)    └──(after_integrity)──► QUARANTINED
       │           │                  │
       ├───────────┴──────────────────┴──► CANCELLED (terminal)
       └──► SKIPPED (terminal，仅未执行阶段)

    VERIFYING ──► COMPLETED / QUARANTINED / RETRY_WAIT / FAILED / CANCELLED
    PAUSED    ──► QUEUED（恢复）/ CANCELLED / FAILED
    RETRY_WAIT──► QUEUED / PAUSED / FAILED / CANCELLED

不变式
------
1. 终态（``COMPLETED`` / ``FAILED`` / ``CANCELLED`` / ``SKIPPED`` / ``QUARANTINED``）
   不可再转换；``QUARANTINED`` 只能通过重新规划（新任务）离开。
2. ``RETRY_WAIT`` 只能回到 ``QUEUED`` 或 ``FAILED``；是否重试由 ``attempt < max_attempts``
   决定，由调度器在转换前判断。
3. ``CANCELLED`` 可从任意非终态到达（用户随时可以叫停）。
4. ``PAUSED`` 只能从 ``QUEUED`` / ``RUNNING`` / ``RETRY_WAIT`` 进入，回到 ``QUEUED``。
5. ``QUARANTINED`` 只能从 ``VERIFYING`` 进入（校验失败的产物）。事后审计发现
   ``FAILED`` 任务的产物已损坏时，允许 ``FAILED -> QUARANTINED``，但必须显式传
   ``after_integrity=True``，以免被当成普通转移路径。
6. ``SKIPPED`` 只能从 ``PENDING`` / ``PLANNED`` / ``QUEUED`` 进入（未开始执行）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from researchdatahub.core.errors import StateTransitionError

if TYPE_CHECKING:
    from researchdatahub.core.models import AcquisitionTask

__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_STALE_TIMEOUT_SECONDS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "TaskState",
    "can_transition",
    "describe_transitions",
    "is_stale",
    "next_state_after_failure",
    "recover_stale",
    "transition",
]


class TaskState(StrEnum):
    """任务生命周期状态。持久化为字符串，因此新增状态不会破坏已有数据。"""

    PENDING = "PENDING"  # 已创建，尚未规划
    PLANNED = "PLANNED"  # 已绑定资产与传输路由
    QUEUED = "QUEUED"  # 已入队等待执行
    RUNNING = "RUNNING"  # 正在传输
    VERIFYING = "VERIFYING"  # 传输完成，正在做完整性检查
    RETRY_WAIT = "RETRY_WAIT"  # 可重试失败后的退避等待
    COMPLETED = "COMPLETED"  # 传输 + 校验通过（终态）
    FAILED = "FAILED"  # 永久失败或重试耗尽（终态）
    PAUSED = "PAUSED"  # 用户暂停
    CANCELLED = "CANCELLED"  # 用户取消（终态）
    SKIPPED = "SKIPPED"  # 无需执行（终态）
    QUARANTINED = "QUARANTINED"  # 校验失败，产物隔离（终态）


TERMINAL_STATES: Final[frozenset[TaskState]] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SKIPPED,
        TaskState.QUARANTINED,
    }
)
"""终态：不允许再发生任何转换。"""

ACTIVE_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.QUEUED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.RETRY_WAIT}
)
"""“在途”状态：进程重启时需要被恢复或判定为过期。"""

DEFAULT_STALE_TIMEOUT_SECONDS: Final[int] = 3600
"""默认的 RUNNING 过期阈值（秒）。超过该时长仍为 RUNNING 视为进程异常退出遗留。"""


def _cancel_from() -> frozenset[TaskState]:
    """可与 CANCELLED 互通的非终态集合。"""
    return frozenset(
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


TRANSITIONS: Final[dict[TaskState, frozenset[TaskState]]] = {
    TaskState.PENDING: frozenset(
        {TaskState.PLANNED, TaskState.SKIPPED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.PLANNED: frozenset(
        {TaskState.QUEUED, TaskState.SKIPPED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.QUEUED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PAUSED,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    # RUNNING 不能直接到 COMPLETED：必须先经过 VERIFYING（Integrity Before Delivery）。
    TaskState.RUNNING: frozenset(
        {
            TaskState.VERIFYING,
            TaskState.RETRY_WAIT,
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.QUARANTINED,
            TaskState.RETRY_WAIT,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RETRY_WAIT: frozenset(
        {TaskState.QUEUED, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.PAUSED: frozenset({TaskState.QUEUED, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.SKIPPED: frozenset(),
    TaskState.QUARANTINED: frozenset(),
}
"""合法状态转换表。它是唯一事实来源，测试与文档都以此为基准。"""

# 自检：保证 CANCELLED 的入边覆盖所有非终态（不变式 3）。
for _state in _cancel_from():  # pragma: no branch - 常量自检
    if _state in TRANSITIONS and TaskState.CANCELLED not in TRANSITIONS[_state]:
        raise AssertionError(f"invariant violated: {_state} must allow CANCELLED")


def can_transition(source: TaskState, target: TaskState, *, after_integrity: bool = False) -> bool:
    """判断 ``source -> target`` 是否合法。

    Parameters
    ----------
    source, target:
        起始与目标状态。
    after_integrity:
        是否在完整性检查之后发起该转换。``FAILED -> QUARANTINED`` 只在事后检查发现
        **已交付文件损坏**时允许：这属于事故记录，不是成功路径。默认 ``False``。

    Returns
    -------
    bool
        ``True`` 表示转换被状态机允许（不代表业务前提已满足，例如重试次数）。
    """
    if source is target:
        # 幂等：同一状态不算转换，但也不允许“重新应用”以掩盖重复执行。
        return False
    if after_integrity and source is TaskState.FAILED and target is TaskState.QUARANTINED:
        # 唯一例外：事后审计发现历史失败任务的产物已被污染，需要移入隔离区。
        return True
    return target in TRANSITIONS[source]


def _normalize_now(now: datetime | None) -> datetime:
    """把时间戳规整为 UTC；naive 时间直接拒绝（时区不明的状态时间戳不可审计）。"""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def transition(
    task: AcquisitionTask,
    target: TaskState,
    *,
    reason: str,
    now: datetime | None = None,
    error: str | None = None,
    worker_id: str | None = None,
    after_integrity: bool = False,
) -> AcquisitionTask:
    """把任务推进到 ``target`` 状态，返回**新的**任务对象。

    任务模型是 frozen 的，因此不存在“就地修改后忘记持久化”的半更新状态；
    调用方负责把返回的新对象写回 Manifest Repository。

    Parameters
    ----------
    task:
        当前任务（不可变）。
    target:
        目标状态。
    reason:
        人类可读的转换原因，追加到 ``notes``，用于审计。
    now:
        tz-aware 时间戳；``None`` 时使用当前 UTC 时间。
    error:
        失败说明，写入 ``last_error``（``FAILED`` / ``QUARANTINED`` / ``RETRY_WAIT``）。
    worker_id:
        执行该转换的 worker 标识。
    after_integrity:
        置于完整性检查上下文；仅对 ``FAILED -> QUARANTINED`` 这一事后隔离转换放行。

    Returns
    -------
    AcquisitionTask
        状态更新后的新任务。

    Raises
    ------
    StateTransitionError
        转换不在 :data:`TRANSITIONS` 中，或违反业务前提（如重试预算耗尽仍要求重排）。
    """
    current = task.state
    if not can_transition(current, target, after_integrity=after_integrity):
        raise StateTransitionError(
            f"illegal transition {current.value} -> {target.value}",
            from_state=current.value,
            to_state=target.value,
            task_id=task.task_id,
            detail={"reason": reason},
        )

    stamp = _normalize_now(now)

    # ---- 业务前提：状态机允许 ≠ 业务上成立 -------------------------------- #
    if target is TaskState.COMPLETED and current is not TaskState.VERIFYING:
        raise StateTransitionError(
            "COMPLETED is only reachable from VERIFYING (integrity before delivery)",
            from_state=current.value,
            to_state=target.value,
            task_id=task.task_id,
        )
    retry_exhausted = (
        target is TaskState.QUEUED
        and current is TaskState.RETRY_WAIT
        and task.attempt >= task.max_attempts
    )
    if retry_exhausted:
        raise StateTransitionError(
            f"retry budget exhausted (attempt={task.attempt}, max={task.max_attempts}); "
            "transition to FAILED instead",
            from_state=current.value,
            to_state=target.value,
            task_id=task.task_id,
        )

    updates: dict[str, object] = {
        "state": target,
        "last_transition_at": stamp,
        # 必须保持 list：模型声明为 list[str]，传 tuple 会让 pydantic 在序列化时告警，
        # 且序列化结果与 round-trip 后的对象类型不一致。
        "notes": [*task.notes, f"{stamp.isoformat()} {current.value}->{target.value}: {reason}"],
    }
    if worker_id is not None:
        updates["worker_id"] = worker_id
    if target is TaskState.RUNNING:
        updates["started_at"] = stamp
        updates["attempt"] = task.attempt + 1
    if target in TERMINAL_STATES:
        updates["finished_at"] = stamp
    if target in (TaskState.FAILED, TaskState.QUARANTINED, TaskState.RETRY_WAIT):
        updates["last_error"] = error or reason

    return task.model_copy(update=updates)


def next_state_after_failure(task: AcquisitionTask) -> TaskState:
    """根据剩余重试预算决定失败后的目标状态。

    Returns
    -------
    TaskState
        仍有预算返回 ``RETRY_WAIT``，预算耗尽返回 ``FAILED``。
    """
    return TaskState.RETRY_WAIT if task.attempt < task.max_attempts else TaskState.FAILED


def is_stale(
    task: AcquisitionTask,
    *,
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS,
) -> bool:
    """判断在途任务是否因进程异常退出而滞留。

    只有 ``RUNNING`` / ``VERIFYING`` 可能过期，其它状态一律返回 ``False``。
    参考时间取 ``last_transition_at``，其次 ``started_at``；两者皆无视为过期
    （无法自证的运行中任务不应被信任）。
    """
    if task.state not in (TaskState.RUNNING, TaskState.VERIFYING):
        return False
    stamp = _normalize_now(now)
    reference = task.last_transition_at or task.started_at
    if reference is None:
        return True
    return (stamp - reference).total_seconds() >= timeout_seconds


def recover_stale(
    task: AcquisitionTask,
    *,
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS,
) -> AcquisitionTask:
    """把过期在途任务重新排队（有预算）或标记失败（预算耗尽）。

    Raises
    ------
    StateTransitionError
        任务并未过期，或不在 ``RUNNING`` / ``VERIFYING``。
    """
    if not is_stale(task, now=now, timeout_seconds=timeout_seconds):
        raise StateTransitionError(
            "task is not stale; recovery refused",
            from_state=task.state.value,
            to_state=task.state.value,
            task_id=task.task_id,
        )
    stamp = _normalize_now(now)
    if task.state is TaskState.VERIFYING:
        return transition(task, TaskState.RETRY_WAIT, reason="recovered stale VERIFYING", now=stamp)
    if task.attempt < task.max_attempts:
        return transition(task, TaskState.RETRY_WAIT, reason="recovered stale RUNNING", now=stamp)
    return transition(
        task,
        TaskState.FAILED,
        reason="stale RUNNING with exhausted retries",
        now=stamp,
        error="process exited while RUNNING and retry budget is exhausted",
    )


def describe_transitions() -> str:
    """生成人类可读的转换表（文档与 CLI 复用，避免文档与代码漂移）。"""
    lines = []
    for state in TaskState:
        targets = sorted(t.value for t in TRANSITIONS[state])
        lines.append(f"{state.value:<12} -> {', '.join(targets) if targets else '(terminal)'}")
    return "\n".join(lines)
