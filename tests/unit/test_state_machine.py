"""状态机：合法转换、终态、重试预算、过期恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_task

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.models import AcquisitionTask
from researchdatahub.core.state import (
    ACTIVE_STATES,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    TERMINAL_STATES,
    TRANSITIONS,
    TaskState,
    can_transition,
    describe_transitions,
    is_stale,
    next_state_after_failure,
    recover_stale,
    transition,
)

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)

# 走到各状态的“最短路径”，用于参数化合法转换。
PATHS: dict[TaskState, list[TaskState]] = {
    TaskState.PENDING: [],
    TaskState.PLANNED: [TaskState.PLANNED],
    TaskState.QUEUED: [TaskState.PLANNED, TaskState.QUEUED],
    TaskState.RUNNING: [TaskState.PLANNED, TaskState.QUEUED, TaskState.RUNNING],
    TaskState.VERIFYING: [
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
    ],
    TaskState.RETRY_WAIT: [
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.RETRY_WAIT,
    ],
    TaskState.PAUSED: [TaskState.PLANNED, TaskState.QUEUED, TaskState.PAUSED],
    TaskState.COMPLETED: [
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.COMPLETED,
    ],
    TaskState.FAILED: [
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.RETRY_WAIT,
        TaskState.FAILED,
    ],
    TaskState.QUARANTINED: [
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.QUARANTINED,
    ],
    TaskState.CANCELLED: [TaskState.PLANNED, TaskState.CANCELLED],
    TaskState.SKIPPED: [TaskState.SKIPPED],
}


def walk(path: list[TaskState], *, max_attempts: int = 3) -> AcquisitionTask:
    """沿给定路径推进任务；每步使用同一固定时间戳，便于断言。"""
    task = make_task(max_attempts=max_attempts)
    for target in path:
        task = transition(task, target, reason=f"step to {target.value}", now=NOW)
    return task


def test_every_state_is_reachable() -> None:
    """12 个状态都能通过合法路径到达（防止状态表里有死状态）。"""
    assert set(PATHS) == set(TaskState)
    assert len(TaskState) == 12
    for state, path in PATHS.items():
        assert walk(path).state is state, state


def test_transitions_table_covers_all_states() -> None:
    assert set(TRANSITIONS) == set(TaskState)
    for state, targets in TRANSITIONS.items():
        assert isinstance(targets, frozenset), state
        assert state not in targets, f"{state} 不应出现在自己的目标集合中"


@pytest.mark.parametrize(
    ("source", "target"),
    [(s, t) for s, targets in TRANSITIONS.items() for t in sorted(targets, key=lambda x: x.value)],
)
def test_legal_transitions_are_table_driven(source: TaskState, target: TaskState) -> None:
    """表中每一条合法转换都能实际执行，且状态与时间戳被正确更新。"""
    task = walk(PATHS[source])
    assert task.state is source
    moved = transition(task, target, reason="legal case", now=NOW)
    assert moved.state is target
    assert moved.last_transition_at == NOW
    assert moved.notes[-1].endswith(f"{source.value}->{target.value}: legal case")
    if target in TERMINAL_STATES:
        assert moved.finished_at == NOW
    else:
        assert moved.finished_at is None


def test_same_state_is_not_a_transition() -> None:
    """重复执行同一状态不算转换，避免用“无害的自我转换”掩盖重复投递。"""
    task = walk([TaskState.PLANNED])
    assert can_transition(TaskState.PLANNED, TaskState.PLANNED) is False
    with pytest.raises(StateTransitionError, match="illegal transition"):
        transition(task, TaskState.PLANNED, reason="no-op", now=NOW)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_are_immutable(terminal: TaskState) -> None:
    """终态不可再转换；FAILED -> QUARANTINED 属于事后隔离，需 after_integrity 显式放行。"""
    task = walk(PATHS[terminal])
    assert task.state is terminal
    for target in TaskState:
        if terminal is TaskState.FAILED and target is TaskState.QUARANTINED:
            assert can_transition(terminal, target) is False
            assert can_transition(terminal, target, after_integrity=True) is True
            continue
        assert can_transition(terminal, target) is False, (terminal, target)
        with pytest.raises(StateTransitionError):
            transition(task, target, reason="escape attempt", now=NOW)


def test_cancelled_reachable_from_every_non_terminal() -> None:
    """用户可在任意在途状态叫停。"""
    for state in TaskState:
        if state in TERMINAL_STATES:
            continue
        task = walk(PATHS[state])
        moved = transition(task, TaskState.CANCELLED, reason="user cancel", now=NOW)
        assert moved.state is TaskState.CANCELLED


def test_running_cannot_jump_to_completed() -> None:
    """Integrity Before Delivery：RUNNING 必须先到 VERIFYING。"""
    task = walk(PATHS[TaskState.RUNNING])
    assert can_transition(TaskState.RUNNING, TaskState.COMPLETED) is False
    with pytest.raises(StateTransitionError):
        transition(task, TaskState.COMPLETED, reason="skip verification", now=NOW)


def test_completed_only_from_verifying() -> None:
    for state in TaskState:
        if state is TaskState.VERIFYING or state in TERMINAL_STATES:
            continue
        assert can_transition(state, TaskState.COMPLETED) is False, state


def test_quarantined_only_from_verifying_or_failed() -> None:
    """常规路径只有 VERIFYING 能进隔离区；FAILED 需 after_integrity 上下文。"""
    allowed = {TaskState.VERIFYING}
    for state in TaskState:
        ok = can_transition(state, TaskState.QUARANTINED)
        assert ok is (state in allowed), state


def test_skipped_only_before_execution() -> None:
    allowed = {TaskState.PENDING, TaskState.PLANNED, TaskState.QUEUED}
    for state in TaskState:
        assert can_transition(state, TaskState.SKIPPED) is (state in allowed), state


# --------------------------------------------------------------------------- #
# 重试预算
# --------------------------------------------------------------------------- #
def test_attempt_increments_on_running() -> None:
    task = walk(PATHS[TaskState.RUNNING])
    assert task.attempt == 1
    assert task.started_at == NOW


def test_next_state_after_failure_respects_budget() -> None:
    fresh = walk([TaskState.PLANNED, TaskState.QUEUED, TaskState.RUNNING], max_attempts=3)
    assert next_state_after_failure(fresh) is TaskState.RETRY_WAIT

    exhausted = walk(
        [
            TaskState.PLANNED,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.RETRY_WAIT,
            TaskState.QUEUED,
            TaskState.RUNNING,
        ],
        max_attempts=2,
    )
    assert exhausted.attempt == 2
    assert next_state_after_failure(exhausted) is TaskState.FAILED


def test_requeue_refused_when_budget_exhausted() -> None:
    """重试次数耗尽后，RETRY_WAIT -> QUEUED 必须被状态机拒绝（而不是无限重试）。"""
    task = walk(
        [
            TaskState.PLANNED,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.RETRY_WAIT,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.RETRY_WAIT,
        ],
        max_attempts=2,
    )
    assert task.state is TaskState.RETRY_WAIT
    with pytest.raises(StateTransitionError, match="retry budget exhausted"):
        transition(task, TaskState.QUEUED, reason="requeue", now=NOW)

    failed = transition(task, TaskState.FAILED, reason="exhausted", now=NOW)
    assert failed.state is TaskState.FAILED


def test_paused_resumes_to_queued() -> None:
    task = walk([TaskState.PLANNED, TaskState.QUEUED, TaskState.PAUSED])
    resumed = transition(task, TaskState.QUEUED, reason="resume", now=NOW)
    assert resumed.state is TaskState.QUEUED


# --------------------------------------------------------------------------- #
# 过期 RUNNING 恢复
# --------------------------------------------------------------------------- #
def test_is_stale_only_for_in_flight_states() -> None:
    later = NOW + timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS + 1)
    for state in TaskState:
        task = walk(PATHS[state])
        expected = state in (TaskState.RUNNING, TaskState.VERIFYING)
        assert is_stale(task, now=later) is expected, state


def test_is_stale_threshold_boundary() -> None:
    task = walk(PATHS[TaskState.RUNNING])
    at_limit = NOW + timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS)
    just_before = NOW + timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS - 1)
    assert is_stale(task, now=just_before) is False
    assert is_stale(task, now=at_limit) is True


def test_running_without_timestamps_is_stale() -> None:
    """无法自证的运行中任务不可信：没有时间戳即视为过期。"""
    task = make_task(state=TaskState.RUNNING)
    assert is_stale(task, now=NOW) is True


def test_recover_stale_running_requeues() -> None:
    task = walk(PATHS[TaskState.RUNNING])
    later = NOW + timedelta(hours=2)
    recovered = recover_stale(task, now=later)
    assert recovered.state is TaskState.RETRY_WAIT
    assert "recovered stale RUNNING" in recovered.notes[-1]


def test_recover_stale_with_exhausted_budget_fails() -> None:
    task = walk(
        [TaskState.PLANNED, TaskState.QUEUED, TaskState.RUNNING],
        max_attempts=1,
    )
    later = NOW + timedelta(hours=2)
    recovered = recover_stale(task, now=later)
    assert recovered.state is TaskState.FAILED
    assert recovered.last_error is not None


def test_recover_stale_refuses_live_task() -> None:
    task = walk(PATHS[TaskState.RUNNING])
    with pytest.raises(StateTransitionError, match="not stale"):
        recover_stale(task, now=NOW + timedelta(minutes=1))


def test_transition_rejects_naive_timestamp() -> None:
    task = walk([TaskState.PLANNED])
    with pytest.raises(ValueError, match="timezone-aware"):
        transition(task, TaskState.QUEUED, reason="naive", now=datetime(2026, 11, 4))


def test_active_states_subset_of_non_terminal() -> None:
    assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)


def test_describe_transitions_mentions_all_states() -> None:
    text = describe_transitions()
    for state in TaskState:
        assert state.value in text


def test_error_message_carries_from_and_to() -> None:
    task = walk(PATHS[TaskState.COMPLETED])
    with pytest.raises(StateTransitionError) as excinfo:
        transition(task, TaskState.QUEUED, reason="rerun completed task", now=NOW)
    err = excinfo.value
    assert err.from_state == "COMPLETED"
    assert err.to_state == "QUEUED"
    assert err.task_id == task.task_id
    assert err.to_dict()["code"] == "RDH_STATE_TRANSITION"
