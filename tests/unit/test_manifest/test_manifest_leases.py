"""租约领取、心跳续约、过期回收的测试（真实 SQLite + 真实状态机）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from tests.conftest import FIXED_NOW
from tests.unit.test_manifest.helpers import seed_queued_task, seed_running_task

from researchdatahub.core.errors import ManifestError
from researchdatahub.core.state import TaskState, transition
from researchdatahub.manifest.repository import SqliteManifestRepository

LEASE = 60
STALE = 3600


def _claim_one(
    repo: SqliteManifestRepository,
    *,
    owner: str = "worker-1",
    now: datetime = FIXED_NOW,
    request_id: str | None = None,
) -> list[str]:
    claimed = repo.claim_tasks(
        owner=owner, limit=1, now=now, lease_seconds=LEASE, request_id=request_id
    )
    return [task.task_id for task in claimed]


# --------------------------------------------------------------------------- #
# claim_tasks
# --------------------------------------------------------------------------- #
def test_claim_sets_running_state_lease_and_audit_event(
    repo: SqliteManifestRepository,
) -> None:
    seed_queued_task(repo, task_id="task-claim")
    claimed = repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)
    assert len(claimed) == 1
    task = claimed[0]
    assert task.task_id == "task-claim"
    assert task.state is TaskState.RUNNING
    assert task.worker_id == "worker-1"
    assert task.attempt == 1
    assert task.started_at == FIXED_NOW
    assert task.heartbeat_at == FIXED_NOW
    assert task.lease_expires_at == FIXED_NOW + timedelta(seconds=LEASE)
    assert repo.load_task("task-claim") == task

    events = repo.list_events("task-claim")
    assert [event.event_type for event in events] == ["task_claimed"]
    assert events[0].metadata["owner"] == "worker-1"
    assert events[0].metadata["attempt"] == 1


def test_claim_respects_priority_then_limit(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-low", priority="low")
    seed_queued_task(repo, task_id="task-critical", priority="critical")
    seed_queued_task(repo, task_id="task-normal", priority="normal")

    claimed = repo.claim_tasks(owner="worker-1", limit=2, now=FIXED_NOW, lease_seconds=LEASE)
    assert [task.task_id for task in claimed] == ["task-critical", "task-normal"]

    remaining = _claim_one(repo, owner="worker-2")
    assert remaining == ["task-low"]


def test_claim_only_takes_due_retry_wait_tasks(repo: SqliteManifestRepository) -> None:
    running = seed_running_task(repo, task_id="task-retry")
    waiting = transition(
        running, TaskState.RETRY_WAIT, reason="temporary failure", now=FIXED_NOW
    ).model_copy(update={"scheduled_at": FIXED_NOW + timedelta(seconds=120)})
    repo.save_task(waiting, expected_state=TaskState.RUNNING)

    assert _claim_one(repo, now=FIXED_NOW) == []
    later = FIXED_NOW + timedelta(seconds=121)
    claimed = repo.claim_tasks(owner="worker-1", limit=1, now=later, lease_seconds=LEASE)
    assert [task.task_id for task in claimed] == ["task-retry"]
    assert claimed[0].attempt == 2


def test_claim_rejects_invalid_arguments(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo)
    with pytest.raises(ManifestError, match="owner"):
        repo.claim_tasks(owner="   ", limit=1, now=FIXED_NOW)
    with pytest.raises(ManifestError, match="owner"):
        repo.claim_tasks(owner="w" * 65, limit=1, now=FIXED_NOW)
    with pytest.raises(ManifestError, match="limit"):
        repo.claim_tasks(owner="worker-1", limit=0, now=FIXED_NOW)
    with pytest.raises(ManifestError, match="lease_seconds"):
        repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.claim_tasks(owner="worker-1", limit=1, now=datetime(2026, 11, 4, 12, 0, 0))


def test_claim_with_request_filter(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-a", request_id="req-one-0001")
    seed_queued_task(repo, task_id="task-b", request_id="req-two-0001")
    claimed = repo.claim_tasks(
        owner="worker-1", limit=5, now=FIXED_NOW, lease_seconds=LEASE, request_id="req-two-0001"
    )
    assert [task.task_id for task in claimed] == ["task-b"]


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_renews_lease(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-hb")
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)
    later = FIXED_NOW + timedelta(seconds=30)
    assert repo.heartbeat("task-hb", owner="worker-1", now=later, lease_seconds=LEASE) is True
    loaded = repo.load_task("task-hb")
    assert loaded.heartbeat_at == later
    assert loaded.lease_expires_at == later + timedelta(seconds=LEASE)


def test_heartbeat_rejects_wrong_owner_expired_lease_and_non_running(
    repo: SqliteManifestRepository,
) -> None:
    seed_queued_task(repo, task_id="task-hb")
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)

    assert repo.heartbeat("task-hb", owner="worker-2", now=FIXED_NOW) is False
    expired = FIXED_NOW + timedelta(seconds=LEASE + 1)
    assert repo.heartbeat("task-hb", owner="worker-1", now=expired) is False
    assert repo.heartbeat("task-ghost", owner="worker-1", now=FIXED_NOW) is False

    queued = seed_queued_task(repo, task_id="task-queued")
    assert repo.heartbeat(queued.task_id, owner="worker-1", now=FIXED_NOW) is False


# --------------------------------------------------------------------------- #
# release_expired：续租 vs 接管
# --------------------------------------------------------------------------- #
def test_release_expired_renews_but_does_not_take_over_when_heartbeat_fresh(
    repo: SqliteManifestRepository,
) -> None:
    seed_queued_task(repo, task_id="task-slow")
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)

    later = FIXED_NOW + timedelta(seconds=LEASE + 1)
    results = repo.release_expired(now=later, stale_timeout_seconds=STALE, lease_seconds=300)
    assert len(results) == 1
    result = results[0]
    assert result.task_id == "task-slow"
    assert result.previous_state == TaskState.RUNNING.value
    assert result.new_state == TaskState.RUNNING.value  # 同状态 = 只续租，不接管
    assert "heartbeat fresh" in result.reason
    assert result.lease_owner == "worker-1"

    loaded = repo.load_task("task-slow")
    assert loaded.state is TaskState.RUNNING
    assert loaded.worker_id == "worker-1"
    assert loaded.lease_expires_at == later + timedelta(seconds=300)
    # 另一个 worker 依然领不到这个任务。
    assert _claim_one(repo, owner="worker-2", now=later) == []
    assert "lease_renewed" in [event.event_type for event in repo.list_events("task-slow")]


def test_release_expired_recovers_stale_running_task_and_requeues(
    repo: SqliteManifestRepository,
) -> None:
    seed_queued_task(repo, task_id="task-dead")
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)

    later = FIXED_NOW + timedelta(seconds=STALE + 100)
    results = repo.release_expired(now=later, stale_timeout_seconds=STALE, lease_seconds=LEASE)
    assert len(results) == 1
    result = results[0]
    assert result.previous_state == TaskState.RUNNING.value
    assert result.new_state == TaskState.RETRY_WAIT.value
    assert "heartbeat older" in result.reason
    assert result.lease_owner == "worker-1"

    loaded = repo.load_task("task-dead")
    assert loaded.state is TaskState.RETRY_WAIT
    assert loaded.lease_expires_at is None
    assert loaded.scheduled_at == later
    assert loaded.last_error is not None
    assert [event.event_type for event in repo.list_events("task-dead")] == [
        "task_claimed",
        "lease_recovered",
    ]

    # 模拟进程重启：新连接（新 repo）继续领取被恢复的任务。
    restarted = SqliteManifestRepository(repo.factory)
    try:
        claimed = restarted.claim_tasks(
            owner="worker-2", limit=1, now=later + timedelta(seconds=1), lease_seconds=LEASE
        )
        assert [task.task_id for task in claimed] == ["task-dead"]
        assert claimed[0].attempt == 2
        assert claimed[0].state is TaskState.RUNNING
    finally:
        restarted.close()


def test_release_expired_marks_failed_when_retry_budget_exhausted(
    repo: SqliteManifestRepository,
) -> None:
    seed_queued_task(repo, task_id="task-exhausted", max_attempts=1)
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)
    assert repo.load_task("task-exhausted").attempt == 1

    later = FIXED_NOW + timedelta(seconds=STALE + 100)
    results = repo.release_expired(now=later, stale_timeout_seconds=STALE, lease_seconds=LEASE)
    assert [result.new_state for result in results] == [TaskState.FAILED.value]

    loaded = repo.load_task("task-exhausted")
    assert loaded.state is TaskState.FAILED
    assert loaded.finished_at == later
    assert loaded.lease_expires_at is None
    assert "exhausted" in str(loaded.last_error)


def test_release_expired_recovers_stale_verifying_task(repo: SqliteManifestRepository) -> None:
    running = seed_running_task(repo, task_id="task-verify")
    verifying = transition(running, TaskState.VERIFYING, reason="verify", now=FIXED_NOW)
    repo.save_task(verifying, expected_state=TaskState.RUNNING)

    later = FIXED_NOW + timedelta(seconds=STALE + 100)
    results = repo.release_expired(now=later, stale_timeout_seconds=STALE, lease_seconds=LEASE)
    assert [(result.previous_state, result.new_state) for result in results] == [
        (TaskState.VERIFYING.value, TaskState.RETRY_WAIT.value)
    ]
    assert repo.load_task("task-verify").state is TaskState.RETRY_WAIT


def test_release_expired_ignores_valid_lease(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-fresh")
    repo.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW, lease_seconds=LEASE)
    assert repo.release_expired(now=FIXED_NOW, stale_timeout_seconds=STALE) == []
    assert repo.load_task("task-fresh").state is TaskState.RUNNING


def test_release_expired_validates_parameters(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="stale_timeout_seconds"):
        repo.release_expired(now=FIXED_NOW, stale_timeout_seconds=0)
    with pytest.raises(ManifestError, match="lease_seconds"):
        repo.release_expired(now=FIXED_NOW, lease_seconds=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.release_expired(now=datetime(2026, 11, 4, 12, 0, 0))
