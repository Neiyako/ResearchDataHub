"""租约与心跳语义（M1 前置契约）。

为什么单独一组测试
------------------
"任务不会被两个 worker 同时执行"与"进程崩溃后任务能被接管"是两条**相反**的要求：
前者要求租约从严（别人不能抢），后者要求租约从宽（死了必须能被接管）。M0 只有
``is_stale`` 一条时间线，无法同时满足。Phase-0 因此引入两条显式时间线：

* :func:`lease_expired`：短周期抢占保护（默认 900 秒租约），越界即"我失去执行权"；
* :func:`is_stale`：长周期崩溃判定（默认 3600 秒无心跳），越界即"进程可能已死"。

本文件的断言就是这两条时间线的行为契约，任何一边被改宽/改窄都会在这里失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.models import AcquisitionTask
from researchdatahub.core.state import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    TaskState,
    is_stale,
    lease_expired,
    renew_lease,
    transition,
)

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def _task(**overrides: Any) -> AcquisitionTask:
    """构造一个处于 QUEUED 的真实任务（字段来自 M0 的 fixture 构造器）。"""
    from tests.conftest import make_task  # 延迟导入，避免 pytest 收集期循环

    return make_task(**overrides)


def _running(**overrides: Any) -> AcquisitionTask:
    """QUEUED → RUNNING，拿到租约。"""
    queued = _task(state=TaskState.QUEUED)
    return transition(queued, TaskState.RUNNING, reason="claim", now=NOW, worker_id="w1", **overrides)


# --------------------------------------------------------------------------- #
# 进入 RUNNING 即持有租约
# --------------------------------------------------------------------------- #
def test_entering_running_grants_lease_and_heartbeat() -> None:
    running = _running()
    assert running.state is TaskState.RUNNING
    assert running.attempt == 1
    assert running.worker_id == "w1"
    assert running.heartbeat_at == NOW
    assert running.lease_expires_at == NOW + timedelta(seconds=DEFAULT_LEASE_SECONDS)


def test_lease_seconds_is_configurable_per_transition() -> None:
    running = _running(lease_seconds=120)
    assert running.lease_expires_at == NOW + timedelta(seconds=120)


def test_terminal_state_releases_lease() -> None:
    """终态必须清空租约，否则恢复流程会把已结束的任务当成"仍被持有"。"""
    running = _running()
    completed = transition(running, TaskState.CANCELLED, reason="user cancel", now=NOW)
    assert completed.lease_expires_at is None
    assert completed.finished_at == NOW


# --------------------------------------------------------------------------- #
# lease_expired：短周期抢占保护
# --------------------------------------------------------------------------- #
def test_lease_absent_counts_as_expired() -> None:
    assert lease_expired(_task(state=TaskState.QUEUED), now=NOW) is True


def test_lease_valid_before_expiry() -> None:
    running = _running()
    assert lease_expired(running, now=NOW + timedelta(seconds=10)) is False


def test_lease_expired_at_boundary_and_after() -> None:
    running = _running()
    deadline = NOW + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    assert lease_expired(running, now=deadline) is True
    assert lease_expired(running, now=deadline + timedelta(seconds=1)) is True


def test_lease_grace_extends_deadline() -> None:
    running = _running()
    deadline = NOW + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    assert lease_expired(running, now=deadline + timedelta(seconds=30), grace_seconds=60) is False


# --------------------------------------------------------------------------- #
# renew_lease：续约
# --------------------------------------------------------------------------- #
def test_renew_lease_moves_both_timestamps() -> None:
    running = _running()
    later = NOW + timedelta(seconds=300)
    renewed = renew_lease(running, now=later, lease_seconds=600)
    assert renewed.heartbeat_at == later
    assert renewed.lease_expires_at == later + timedelta(seconds=600)
    # 原始对象不变（frozen）
    assert running.heartbeat_at == NOW


def test_renew_lease_rejects_non_active_state() -> None:
    with pytest.raises(StateTransitionError) as excinfo:
        renew_lease(_task(state=TaskState.QUEUED), now=NOW)
    assert "only RUNNING/VERIFYING" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# is_stale：长周期崩溃判定，必须优先看心跳
# --------------------------------------------------------------------------- #
def test_is_stale_prefers_heartbeat_over_last_transition() -> None:
    """任务 2 小时前进入 RUNNING，但 10 秒前刚心跳 —— 进程活着，绝不能判过期。

    这是 M1 最容易写错的地方：只看 last_transition_at 会把长任务误判为崩溃并重复下载。
    """
    running = transition(
        _task(state=TaskState.QUEUED), TaskState.RUNNING, reason="claim", now=NOW - timedelta(hours=2)
    )
    fresh = renew_lease(running, now=NOW - timedelta(seconds=10))
    assert is_stale(fresh, now=NOW) is False
    # 对照：同一个任务若不刷新心跳，则已过期
    assert is_stale(running, now=NOW) is True


def test_is_stale_uses_default_timeout() -> None:
    running = _running()
    almost = NOW + timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS - 1)
    assert is_stale(running, now=almost) is False
    assert is_stale(running, now=NOW + timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS)) is True


def test_is_stale_only_for_in_flight_states() -> None:
    for state in (TaskState.PENDING, TaskState.QUEUED, TaskState.COMPLETED, TaskState.FAILED):
        assert is_stale(_task(state=state), now=NOW) is False


def test_lease_and_stale_timeouts_are_ordered() -> None:
    """租约必须显著短于过期阈值：否则慢任务会被误判崩溃并被第二个 worker 抢走。"""
    assert DEFAULT_LEASE_SECONDS * 2 <= DEFAULT_STALE_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# 序列化往返：租约字段必须能落库再读回
# --------------------------------------------------------------------------- #
def test_lease_fields_round_trip_json() -> None:
    running = _running()
    payload = running.to_json_dict()
    assert payload["heartbeat_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert payload["lease_expires_at"] is not None
    restored = AcquisitionTask.model_validate(payload)
    assert restored.heartbeat_at == running.heartbeat_at
    assert restored.lease_expires_at == running.lease_expires_at
    assert restored.fingerprint() == running.fingerprint()


def test_config_scheduler_lease_is_consistent_with_state_defaults() -> None:
    """配置文件默认值必须与状态机常量一致，否则"租约"在配置里写 60 秒也能过校验。"""
    from researchdatahub.config.models import AppConfig

    defaults = AppConfig().defaults
    assert defaults.scheduler.lease_seconds == DEFAULT_LEASE_SECONDS
    assert defaults.integrity.stale_timeout_seconds == DEFAULT_STALE_TIMEOUT_SECONDS
    assert defaults.scheduler.heartbeat_interval_seconds * 3 <= defaults.scheduler.lease_seconds
    assert defaults.scheduler.lease_seconds < defaults.integrity.stale_timeout_seconds
