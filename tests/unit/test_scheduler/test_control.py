"""控制面测试：pause/resume/cancel/retry/submit/status 的状态机合法性。

全部使用真实 SQLite 临时库与真实状态机；不 mock 状态转换。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.conftest import FIXED_NOW, make_task
from tests.unit.test_scheduler.support import make_scheduler, new_request

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.models import AcquisitionTask
from researchdatahub.core.state import TaskState, transition
from researchdatahub.scheduler import TaskControlService


def _seed(repo, state: TaskState, *, task_id: str = "task-control") -> AcquisitionTask:
    """经真实状态机把任务推进到目标状态并入库。"""
    task = make_task(task_id=task_id, plan_id="plan-control", request_id="req-control")
    planned = transition(task, TaskState.PLANNED, reason="seed", now=FIXED_NOW)
    repo.save_task(planned)
    if state is TaskState.PLANNED:
        return planned
    queued = transition(planned, TaskState.QUEUED, reason="seed", now=FIXED_NOW)
    repo.save_task(queued, expected_state=TaskState.PLANNED)
    if state is TaskState.QUEUED:
        return queued
    running = transition(
        queued, TaskState.RUNNING, reason="seed", now=FIXED_NOW, worker_id="seed-worker"
    )
    repo.save_task(running, expected_state=TaskState.QUEUED)
    if state is TaskState.RUNNING:
        return running
    if state is TaskState.RETRY_WAIT:
        retry_wait = transition(
            running, TaskState.RETRY_WAIT, reason="seed", now=FIXED_NOW, error="seed"
        ).model_copy(update={"scheduled_at": FIXED_NOW + timedelta(seconds=30)})
        repo.save_task(retry_wait, expected_state=TaskState.RUNNING)
        return retry_wait
    if state is TaskState.FAILED:
        failed = transition(running, TaskState.FAILED, reason="seed", now=FIXED_NOW, error="seed")
        repo.save_task(failed, expected_state=TaskState.RUNNING)
        return failed
    if state is TaskState.VERIFYING:
        verifying = transition(running, TaskState.VERIFYING, reason="seed", now=FIXED_NOW)
        repo.save_task(verifying, expected_state=TaskState.RUNNING)
        return verifying
    if state is TaskState.PAUSED:
        paused = transition(queued, TaskState.PAUSED, reason="seed", now=FIXED_NOW)
        repo.save_task(paused, expected_state=TaskState.QUEUED)
        return paused
    raise AssertionError(f"seed does not support {state.value}")


def test_pause_and_resume_queued_task(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.QUEUED)
    paused = service.pause("task-control", "测试暂停")
    assert paused.state is TaskState.PAUSED
    assert repo.load_task("task-control").state is TaskState.PAUSED
    events = [event.event_type for event in repo.list_events("task-control")]
    assert "task_paused" in events

    resumed = service.resume("task-control", "测试恢复")
    assert resumed.state is TaskState.QUEUED
    assert repo.load_task("task-control").scheduled_at is None
    assert "task_resumed" in [event.event_type for event in repo.list_events("task-control")]


def test_pause_rejects_terminal_and_unpausable_states(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.FAILED, task_id="task-failed")
    with pytest.raises(StateTransitionError) as excinfo:
        service.pause("task-failed")
    assert "terminal" in str(excinfo.value)

    _seed(repo, TaskState.PLANNED, task_id="task-planned")
    with pytest.raises(StateTransitionError):
        service.pause("task-planned")


def test_cancel_queued_and_reject_terminal(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.QUEUED, task_id="task-cancel")
    cancelled = service.cancel("task-cancel", "用户取消")
    assert cancelled.state is TaskState.CANCELLED
    assert "task_cancelled" in [event.event_type for event in repo.list_events("task-cancel")]

    with pytest.raises(StateTransitionError):
        service.cancel("task-cancel")


def test_retry_retry_wait_returns_to_queued_immediately(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.RETRY_WAIT, task_id="task-retry")
    retried = service.retry("task-retry", "手动重试")
    assert retried.state is TaskState.QUEUED
    assert retried.scheduled_at is None
    stored = repo.load_task("task-retry")
    assert stored.state is TaskState.QUEUED
    assert stored.scheduled_at is None


def test_retry_failed_task_requires_replan(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.FAILED, task_id="task-dead")
    with pytest.raises(StateTransitionError) as excinfo:
        service.retry("task-dead")
    assert "Re-plan" in str(excinfo.value)
    assert "terminal" in str(excinfo.value)


def test_status_reports_lease_and_events(repo) -> None:
    service = TaskControlService(repo, clock=lambda: FIXED_NOW)
    _seed(repo, TaskState.RUNNING, task_id="task-status")
    view = service.status("task-status")
    assert view.task.state is TaskState.RUNNING
    assert view.task.lease_expires_at is not None
    assert view.task.heartbeat_at is not None
    payload = view.as_dict()
    assert payload["state"] == "RUNNING"
    assert payload["worker_id"] == "seed-worker"
    assert "task_claimed" in " ".join(view.events) or view.events == ()


def test_submit_is_idempotent_and_queues_planned_tasks(
    config, repo_factory, storage, integrity, transport, repo
) -> None:
    from tests.unit.test_scheduler.support import create_and_submit, make_https_asset, serve_bytes
    from tests.unit.test_transfer.http_server import TransferTestServer

    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    server = TransferTestServer()
    try:
        data = b"submit-body"
        serve_bytes(server, "/submit.nc", data)
        asset = make_https_asset(server, path="/submit.nc", data=data)
        plan = create_and_submit(
            scheduler=scheduler,
            repo=repo,
            request=new_request(),
            assets=[asset],
            config=config,
        )
        assert scheduler.submit(plan) == 0  # 幂等：已 QUEUED，不重复入队
        claimed = repo.claim_tasks(owner="probe", limit=10, now=FIXED_NOW, lease_seconds=60)
        assert [task.task_id for task in claimed] == plan.task_ids()
    finally:
        server.close()
