"""需求级状态机（``core.acquisition``）的测试。

测试策略与 ``tests/unit/test_state_machine_illegal.py``（传输任务态）保持一致：
**穷举 17×17 = 289 个组合并完整分区**，任何新增状态或新增边都会让分区测试失败。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from researchdatahub.core.acquisition import (
    ACQUISITION_TERMINAL_STATES,
    ACQUISITION_TRANSITIONS,
    AcquisitionRecord,
    AcquisitionState,
    AggregateOutcome,
    aggregate_task_result,
    aggregate_task_state,
    can_transition_acquisition,
    coerce_acquisition_state,
    describe_acquisition_transitions,
    history_summary,
    is_terminal_acquisition,
    resume_state,
    transition_acquisition,
)
from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.state import TaskState

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_record(
    state: AcquisitionState = AcquisitionState.CREATED,
    *,
    previous_state: AcquisitionState | None = None,
) -> AcquisitionRecord:
    return AcquisitionRecord(
        acquisition_id="acq-1",
        request_id="req-1",
        state=state,
        previous_state=previous_state,
        created_at=NOW,
        updated_at=NOW,
    )


# --------------------------------------------------------------------------- #
# 结构不变量
# --------------------------------------------------------------------------- #
def test_state_count_is_seventeen() -> None:
    """状态数量固定为 17；新增状态必须同步检查转换表与文档。"""
    assert len(list(AcquisitionState)) == 17


def test_transition_table_covers_every_state() -> None:
    assert set(ACQUISITION_TRANSITIONS) == set(AcquisitionState)


def test_terminal_states_have_no_exits() -> None:
    for state in ACQUISITION_TERMINAL_STATES:
        assert ACQUISITION_TRANSITIONS[state] == frozenset()


def test_terminal_set_matches_table() -> None:
    derived = {state for state, targets in ACQUISITION_TRANSITIONS.items() if not targets}
    assert derived == set(ACQUISITION_TERMINAL_STATES)


def test_every_non_terminal_state_can_be_cancelled() -> None:
    """不变式 3：用户随时可以叫停。"""
    for state in AcquisitionState:
        if state in ACQUISITION_TERMINAL_STATES:
            continue
        assert state in ACQUISITION_TRANSITIONS
        if state is AcquisitionState.CANCELLED:  # pragma: no cover - 终态已被上面跳过
            continue
        assert AcquisitionState.CANCELLED in ACQUISITION_TRANSITIONS[state]


def test_every_non_terminal_state_can_be_paused_except_paused_itself() -> None:
    """不变式 4：任意非终态可暂停（暂停态自身不再暂停）。"""
    for state in AcquisitionState:
        if state in ACQUISITION_TERMINAL_STATES or state is AcquisitionState.PAUSED:
            continue
        assert AcquisitionState.PAUSED in ACQUISITION_TRANSITIONS[state]


def test_main_path_is_walkable_end_to_end() -> None:
    """主链路 CREATED→…→COMPLETED 的每条边都必须存在。"""
    path = [
        AcquisitionState.CREATED,
        AcquisitionState.DISCOVERING,
        AcquisitionState.ASSESSING,
        AcquisitionState.VALIDATING,
        AcquisitionState.PLANNING,
        AcquisitionState.AWAITING_APPROVAL,
        AcquisitionState.QUEUED,
        AcquisitionState.EXECUTING,
        AcquisitionState.TRANSFERRING,
        AcquisitionState.VERIFYING,
        AcquisitionState.COMPLETED,
    ]
    for source, target in itertools.pairwise(path):
        assert can_transition_acquisition(source, target), f"{source} -> {target} missing"


# --------------------------------------------------------------------------- #
# 穷举分区：289 组合
# --------------------------------------------------------------------------- #
def test_transition_matrix_is_fully_partitioned() -> None:
    """每条 (source, target) 组合都必须被明确判定为合法或非法，不留灰区。"""
    all_states = list(AcquisitionState)
    legal: list[tuple[AcquisitionState, AcquisitionState]] = []
    illegal: list[tuple[AcquisitionState, AcquisitionState]] = []
    for source in all_states:
        for target in all_states:
            if source is target:
                continue
            (legal if can_transition_acquisition(source, target) else illegal).append(
                (source, target)
            )
    total = len(all_states) * (len(all_states) - 1)
    assert len(legal) + len(illegal) == total == 17 * 16
    # 自反转换一律不合法（幂等不算转换）。
    for state in all_states:
        assert can_transition_acquisition(state, state) is False


def test_legal_edge_count_is_stable() -> None:
    """合法边数量固化：改动转换表必须显式更新此断言（防止悄悄加边）。"""
    edges = sum(len(targets) for targets in ACQUISITION_TRANSITIONS.values())
    assert edges == 81


# --------------------------------------------------------------------------- #
# transition_acquisition：业务前提
# --------------------------------------------------------------------------- #
def test_transition_returns_new_object_and_records_history() -> None:
    record = make_record()
    moved = transition_acquisition(record, AcquisitionState.DISCOVERING, reason="start", now=NOW)
    assert moved is not record
    assert moved.state is AcquisitionState.DISCOVERING
    assert record.state is AcquisitionState.CREATED  # 原对象不变
    assert len(moved.history) == 1
    assert "CREATED->DISCOVERING" in moved.history[0]


def test_illegal_transition_raises() -> None:
    record = make_record()
    with pytest.raises(StateTransitionError, match="illegal acquisition transition"):
        transition_acquisition(record, AcquisitionState.COMPLETED, reason="skip", now=NOW)


def test_completed_only_from_verifying_or_partially_supported() -> None:
    """不变式 2：没有经过验证的交付不算完成。

    转换表里 ``COMPLETED`` 的入边只有 ``VERIFYING`` 与 ``PARTIALLY_SUPPORTED``
    （后者表示"用户接受了部分结果"，它是从 ``VERIFYING`` 来的）。
    函数层再收紧一次：从 ``PARTIALLY_SUPPORTED`` 到 ``COMPLETED`` 是**表驱动允许但业务拒绝**的，
    因为部分成功永远不应被记成"全部完成"。
    """
    sources = {
        state
        for state in AcquisitionState
        if can_transition_acquisition(state, AcquisitionState.COMPLETED)
    }
    assert sources == {
        AcquisitionState.VERIFYING,
        AcquisitionState.PARTIALLY_SUPPORTED,
    }
    moved = transition_acquisition(
        make_record(AcquisitionState.VERIFYING), AcquisitionState.COMPLETED, reason="ok", now=NOW
    )
    assert moved.state is AcquisitionState.COMPLETED
    # 表驱动放行、业务必须拒绝：
    with pytest.raises(StateTransitionError, match="only reachable from VERIFYING"):
        transition_acquisition(
            make_record(AcquisitionState.PARTIALLY_SUPPORTED),
            AcquisitionState.COMPLETED,
            reason="pretend all ok",
            now=NOW,
        )


def test_queued_requires_awaiting_approval() -> None:
    """审批闸门：QUEUED 只能从 AWAITING_APPROVAL 进入（ADR-0011）。"""
    sources = {
        state
        for state in AcquisitionState
        if can_transition_acquisition(state, AcquisitionState.QUEUED)
    }
    assert sources == {AcquisitionState.AWAITING_APPROVAL}


def test_partially_supported_requires_verifying_or_assessing() -> None:
    sources = {
        state
        for state in AcquisitionState
        if can_transition_acquisition(state, AcquisitionState.PARTIALLY_SUPPORTED)
    }
    assert sources == {
        AcquisitionState.ASSESSING,
        AcquisitionState.VERIFYING,
        AcquisitionState.PAUSED,
    }
    # DISCOVERING 连转换表都不允许到 PARTIALLY_SUPPORTED（表驱动拒绝）。
    with pytest.raises(StateTransitionError, match="illegal acquisition transition"):
        transition_acquisition(
            make_record(AcquisitionState.DISCOVERING),
            AcquisitionState.PARTIALLY_SUPPORTED,
            reason="too early",
            now=NOW,
        )


def test_pause_sets_previous_state_and_resume_returns_there() -> None:
    record = make_record(AcquisitionState.EXECUTING)
    paused = transition_acquisition(record, AcquisitionState.PAUSED, reason="user", now=NOW)
    assert paused.state is AcquisitionState.PAUSED
    assert paused.previous_state is AcquisitionState.EXECUTING
    assert resume_state(paused) is AcquisitionState.EXECUTING
    resumed = transition_acquisition(paused, AcquisitionState.EXECUTING, reason="resume", now=NOW)
    assert resumed.previous_state is None


def test_pause_from_paused_is_rejected() -> None:
    record = make_record(AcquisitionState.PAUSED, previous_state=AcquisitionState.EXECUTING)
    with pytest.raises(StateTransitionError, match="illegal acquisition transition"):
        transition_acquisition(record, AcquisitionState.PAUSED, reason="again", now=NOW)


def test_resume_state_requires_paused_record() -> None:
    with pytest.raises(StateTransitionError, match="not PAUSED"):
        resume_state(make_record(AcquisitionState.EXECUTING))


def test_resume_state_requires_previous_state() -> None:
    record = make_record(AcquisitionState.PAUSED)
    with pytest.raises(StateTransitionError, match="no previous_state"):
        resume_state(record)


def test_error_field_only_set_for_exceptional_targets() -> None:
    record = make_record(AcquisitionState.DISCOVERING)
    needs_cfg = transition_acquisition(
        record, AcquisitionState.NEEDS_CONFIGURATION, reason="missing token", now=NOW
    )
    assert needs_cfg.error == "missing token"
    ok = transition_acquisition(record, AcquisitionState.ASSESSING, reason="ok", now=NOW)
    assert ok.error is None


def test_plan_id_bound_on_transition() -> None:
    record = make_record(AcquisitionState.PLANNING)
    moved = transition_acquisition(
        record, AcquisitionState.AWAITING_APPROVAL, reason="plan ready", now=NOW, plan_id="plan-42"
    )
    assert moved.plan_id == "plan-42"


def test_naive_now_is_rejected() -> None:
    """时区不明的时间戳不可审计，必须拒绝。"""
    with pytest.raises(ValueError, match="timezone-aware"):
        transition_acquisition(
            make_record(), AcquisitionState.DISCOVERING, reason="x", now=datetime(2026, 1, 1)
        )


def test_record_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AcquisitionRecord(
            acquisition_id="acq-1",
            request_id="req-1",
            created_at=datetime(2026, 1, 1),
            updated_at=NOW,
        )


def test_is_terminal_helpers() -> None:
    assert make_record(AcquisitionState.COMPLETED).is_terminal
    assert make_record(AcquisitionState.FAILED).is_terminal
    assert make_record(AcquisitionState.CANCELLED).is_terminal
    assert not make_record(AcquisitionState.PAUSED).is_terminal
    assert is_terminal_acquisition(AcquisitionState.VERIFYING) is False


def test_coerce_rejects_unknown_state() -> None:
    """未知状态必须显式失败，不能静默当作 CREATED。"""
    assert coerce_acquisition_state("COMPLETED") is AcquisitionState.COMPLETED
    with pytest.raises(StateTransitionError, match="unknown acquisition state"):
        coerce_acquisition_state("SKIPPED")


def test_describe_covers_all_states() -> None:
    text = describe_acquisition_transitions()
    for state in AcquisitionState:
        assert state.value in text


# --------------------------------------------------------------------------- #
# 聚合：子任务状态 → 需求级结论
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((), AggregateOutcome.EMPTY),
        ((TaskState.COMPLETED, TaskState.COMPLETED), AggregateOutcome.ALL_SUCCEEDED),
        ((TaskState.FAILED, TaskState.FAILED), AggregateOutcome.ALL_FAILED),
        ((TaskState.COMPLETED, TaskState.FAILED), AggregateOutcome.PARTIAL),
        ((TaskState.COMPLETED, TaskState.QUARANTINED), AggregateOutcome.PARTIAL),
        ((TaskState.COMPLETED, TaskState.SKIPPED), AggregateOutcome.PARTIAL),
        ((TaskState.COMPLETED, TaskState.RUNNING), AggregateOutcome.PENDING),
        ((TaskState.COMPLETED, TaskState.RETRY_WAIT), AggregateOutcome.PENDING),
        ((TaskState.PENDING,), AggregateOutcome.PENDING),
        ((TaskState.PAUSED,), AggregateOutcome.PENDING),
    ],
)
def test_aggregate_task_state(states: tuple[TaskState, ...], expected: AggregateOutcome) -> None:
    assert aggregate_task_state(states) is expected


@pytest.mark.parametrize(
    ("states", "allow_partial", "expected"),
    [
        ((TaskState.RUNNING,), False, None),
        ((TaskState.RUNNING,), True, None),
        ((), False, AcquisitionState.FAILED),
        ((TaskState.COMPLETED,), False, AcquisitionState.VERIFYING),
        ((TaskState.COMPLETED, TaskState.COMPLETED), False, AcquisitionState.VERIFYING),
        ((TaskState.FAILED,), False, AcquisitionState.FAILED),
        ((TaskState.COMPLETED, TaskState.FAILED), True, AcquisitionState.VERIFYING),
        ((TaskState.COMPLETED, TaskState.FAILED), False, AcquisitionState.FAILED),
    ],
)
def test_aggregate_task_result(
    states: tuple[TaskState, ...],
    allow_partial: bool,
    expected: AcquisitionState | None,
) -> None:
    assert aggregate_task_result(states, allow_partial=allow_partial) is expected


def test_aggregate_path_leads_to_completed() -> None:
    """聚合结论必须能被状态机接受：VERIFYING -> COMPLETED 合法。"""
    target = aggregate_task_result([TaskState.COMPLETED, TaskState.COMPLETED])
    assert target is AcquisitionState.VERIFYING
    record = make_record(AcquisitionState.TRANSFERRING)
    moved = transition_acquisition(record, target, reason="all done", now=NOW)
    done = transition_acquisition(moved, AcquisitionState.COMPLETED, reason="verified", now=NOW)
    assert done.state is AcquisitionState.COMPLETED


def test_history_summary() -> None:
    summary = history_summary([f"e{i}" for i in range(10)], limit=3)
    assert summary["total"] == 10
    assert summary["recent"] == ["e7", "e8", "e9"]
