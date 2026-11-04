"""Manifest 测试共享装配：真实 SQLite 临时库、真实状态机推进。

原则（与 ``tests/conftest.py`` 一致）：**不使用 MagicMock**。数据库是真实的临时文件，
并发测试是真实线程，故障注入用真实 SQLite TRIGGER，不用 mock 假装行为。
"""

from __future__ import annotations

from datetime import datetime

from tests.conftest import FIXED_NOW, make_task

from researchdatahub.core.models import AcquisitionTask
from researchdatahub.core.state import TaskState, transition
from researchdatahub.manifest.repository import SqliteManifestRepository


def make_task_in_state(
    state: TaskState,
    *,
    task_id: str = "task-0001",
    now: datetime = FIXED_NOW,
    **overrides: object,
) -> AcquisitionTask:
    """用真实状态机把任务推进到指定状态（拒绝绕过 ``transition`` 手工构造状态）。"""
    overrides.setdefault("task_id", task_id)
    task = make_task(**overrides)
    if state is TaskState.PENDING:
        return task
    task = transition(task, TaskState.PLANNED, reason="test seed", now=now)
    if state is TaskState.PLANNED:
        return task
    task = transition(task, TaskState.QUEUED, reason="test seed", now=now)
    if state is TaskState.QUEUED:
        return task
    task = transition(
        task,
        TaskState.RUNNING,
        reason="test seed",
        now=now,
        worker_id="seed-worker",
    )
    if state is TaskState.RUNNING:
        return task
    task = transition(task, TaskState.VERIFYING, reason="test seed", now=now)
    if state is TaskState.VERIFYING:
        return task
    raise ValueError(f"make_task_in_state does not support {state.value}")


def seed_queued_task(
    repo: SqliteManifestRepository,
    *,
    task_id: str = "task-0001",
    now: datetime = FIXED_NOW,
    **overrides: object,
) -> AcquisitionTask:
    """真实入库一个 ``QUEUED`` 任务：先写 ``PLANNED``，再经状态机推进到 ``QUEUED``。"""
    planned = make_task_in_state(TaskState.PLANNED, task_id=task_id, now=now, **overrides)
    repo.save_task(planned)
    queued = transition(planned, TaskState.QUEUED, reason="test enqueue", now=now)
    repo.save_task(queued, expected_state=TaskState.PLANNED)
    return queued


def seed_running_task(
    repo: SqliteManifestRepository,
    *,
    task_id: str = "task-0001",
    owner: str = "worker-1",
    now: datetime = FIXED_NOW,
    **overrides: object,
) -> AcquisitionTask:
    """真实入库一个 ``RUNNING`` 任务（经 ``QUEUED -> RUNNING``，租约由状态机签发）。"""
    queued = seed_queued_task(repo, task_id=task_id, now=now, **overrides)
    running = transition(
        queued,
        TaskState.RUNNING,
        reason="test pick up",
        now=now,
        worker_id=owner,
    )
    repo.save_task(running, expected_state=TaskState.QUEUED)
    return running
