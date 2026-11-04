"""非法状态转换：穷举 12×12 矩阵中所有非表项组合。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import make_task

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.state import TRANSITIONS, TaskState, can_transition, transition

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)

ALL_PAIRS = [(s, t) for s in TaskState for t in TaskState]
ILLEGAL_PAIRS = [pair for pair in ALL_PAIRS if pair[1] not in TRANSITIONS[pair[0]]]


def test_matrix_is_fully_partitioned() -> None:
    """144 个组合必须恰好划分为合法/非法两部分，不存在"未定义"的空洞。"""
    legal = sum(len(targets) for targets in TRANSITIONS.values())
    assert legal + len(ILLEGAL_PAIRS) == len(ALL_PAIRS) == 144
    assert len(ILLEGAL_PAIRS) > 0


def test_can_transition_matches_table_exactly() -> None:
    for source, target in ALL_PAIRS:
        expected = (source is not target) and (target in TRANSITIONS[source])
        assert can_transition(source, target) is expected, (source, target)


@pytest.mark.parametrize(
    ("source", "target"), ILLEGAL_PAIRS, ids=[f"{s.value}->{t.value}" for s, t in ILLEGAL_PAIRS]
)
def test_illegal_transition_raises(source: TaskState, target: TaskState) -> None:
    """非法转换必须抛 StateTransitionError，且不改变原任务。"""
    task = make_task(state=source)
    with pytest.raises(StateTransitionError) as excinfo:
        transition(task, target, reason="illegal probe", now=NOW)
    assert excinfo.value.from_state == source.value
    assert excinfo.value.to_state == target.value
    assert task.state is source, "失败的转换不得修改原任务"


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED", "SKIPPED", "QUARANTINED"])
def test_terminal_states_allow_nothing(terminal: str) -> None:
    assert TRANSITIONS[TaskState(terminal)] == frozenset()


def test_failed_is_a_pure_terminal_state() -> None:
    """FAILED 是终态：常规路径没有任何出边。"""
    assert TRANSITIONS[TaskState.FAILED] == frozenset()


def test_failed_to_quarantined_requires_integrity_context() -> None:
    """事故事后隔离：只有显式 after_integrity=True 才允许 FAILED -> QUARANTINED。"""
    task = make_task(state=TaskState.FAILED, attempt=1, max_attempts=1)
    assert can_transition(TaskState.FAILED, TaskState.QUARANTINED) is False
    assert can_transition(TaskState.FAILED, TaskState.QUARANTINED, after_integrity=True) is True
    with pytest.raises(StateTransitionError):
        transition(task, TaskState.QUARANTINED, reason="no context", now=NOW)
    quarantined = transition(
        task, TaskState.QUARANTINED, reason="post-hoc audit", now=NOW, after_integrity=True
    )
    assert quarantined.state is TaskState.QUARANTINED
