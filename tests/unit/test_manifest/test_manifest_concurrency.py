"""真实多线程并发测试：写事务零 ``database is locked``、领取绝不重复。

这些测试不使用 Mock：每个线程持有自己的真实 SQLite 连接（``check_same_thread=True``），
共同操作同一个临时数据库文件，用 ``threading.Barrier`` 制造真实的并发窗口。
"""

from __future__ import annotations

import threading
from datetime import datetime

from tests.conftest import FIXED_NOW, make_task
from tests.unit.test_manifest.helpers import seed_queued_task

from researchdatahub.core.state import TaskState, transition
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository

WRITERS = 4
TASKS_PER_WRITER = 25
CLAIM_ROUNDS = 100


def _open(factory: ManifestConnectionFactory) -> SqliteManifestRepository:
    return SqliteManifestRepository(factory)


def test_four_threads_write_one_hundred_tasks_without_lock_errors(
    factory: ManifestConnectionFactory,
) -> None:
    """M1-1 验收：4 线程写 100 个任务，零 ``database is locked``。"""
    factory.initialize()
    failures: list[BaseException] = []
    start = threading.Barrier(WRITERS)

    def writer(index: int) -> None:
        repository = _open(factory)
        try:
            start.wait(timeout=10)
            for offset in range(TASKS_PER_WRITER):
                sequence = index * TASKS_PER_WRITER + offset
                task = make_task(task_id=f"task-conc-{sequence:03d}")
                planned = transition(task, TaskState.PLANNED, reason="concurrency", now=FIXED_NOW)
                repository.save_task_and_event(
                    planned,
                    event="planned",
                    payload={"writer": index, "sequence": sequence},
                )
        except BaseException as exc:
            failures.append(exc)
        finally:
            repository.close()

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "writer thread did not finish"

    assert failures == [], f"concurrent writers failed: {failures!r}"
    repository = _open(factory)
    try:
        loaded = list(repository.list_tasks())
        assert len(loaded) == WRITERS * TASKS_PER_WRITER
        assert {task.task_id for task in loaded} == {
            f"task-conc-{sequence:03d}" for sequence in range(WRITERS * TASKS_PER_WRITER)
        }
        for task in loaded:
            assert task.state is TaskState.PLANNED
            assert len(repository.list_events(task.task_id)) == 1
    finally:
        repository.close()


def test_concurrent_claim_never_assigns_same_task_twice(
    factory: ManifestConnectionFactory,
) -> None:
    """100 轮、两个 worker 同时领取同一任务：每轮恰好一个 worker 领到。"""
    factory.initialize()
    seed_repository = _open(factory)
    expected_task_ids: list[str] = []
    try:
        for index in range(CLAIM_ROUNDS):
            task_id = f"task-claim-{index:03d}"
            expected_task_ids.append(task_id)
            seed_queued_task(
                seed_repository,
                task_id=task_id,
                request_id=f"req-claim-{index:03d}",
            )
    finally:
        seed_repository.close()

    barrier = threading.Barrier(2)
    claims: dict[tuple[int, str], list[str]] = {}
    failures: list[BaseException] = []
    lock = threading.Lock()

    def worker(owner: str) -> None:
        repository = _open(factory)
        try:
            for index in range(CLAIM_ROUNDS):
                barrier.wait(timeout=30)
                claimed = repository.claim_tasks(
                    owner=owner,
                    limit=1,
                    now=FIXED_NOW,
                    request_id=f"req-claim-{index:03d}",
                )
                with lock:
                    claims[(index, owner)] = [task.task_id for task in claimed]
        except BaseException as exc:
            failures.append(exc)
        finally:
            repository.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("worker-1", "worker-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), "claimer thread did not finish"

    assert failures == [], f"concurrent claim failed: {failures!r}"
    for index, expected_task_id in enumerate(expected_task_ids):
        owner_1 = claims[(index, "worker-1")]
        owner_2 = claims[(index, "worker-2")]
        assert len(owner_1) + len(owner_2) == 1, (
            f"round {index}: task claimed {len(owner_1) + len(owner_2)} times "
            f"(owner-1={owner_1}, owner-2={owner_2})"
        )
        claimed_by = "worker-1" if owner_1 else "worker-2"
        assert (owner_1 or owner_2) == [expected_task_id]

        verifier = _open(factory)
        try:
            task = verifier.load_task(expected_task_id)
            assert task.state is TaskState.RUNNING
            assert task.worker_id == claimed_by
            assert task.attempt == 1
            events = verifier.list_events(expected_task_id)
            assert [event.metadata["owner"] for event in events] == [claimed_by]
        finally:
            verifier.close()


def test_concurrent_claim_over_shared_pool_has_no_duplicates(
    factory: ManifestConnectionFactory,
) -> None:
    """两个 worker 抢同一个任务池（不带 request 过滤）：并集去重、互不重叠。"""
    factory.initialize()
    seed_repository = _open(factory)
    try:
        for index in range(10):
            seed_queued_task(seed_repository, task_id=f"task-pool-{index:02d}")
    finally:
        seed_repository.close()

    barrier = threading.Barrier(2)
    claimed: dict[str, list[str]] = {}
    failures: list[BaseException] = []

    def worker(owner: str) -> None:
        repository = _open(factory)
        try:
            barrier.wait(timeout=30)
            tasks = repository.claim_tasks(owner=owner, limit=3, now=FIXED_NOW)
            claimed[owner] = [task.task_id for task in tasks]
        except BaseException as exc:
            failures.append(exc)
        finally:
            repository.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("worker-1", "worker-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == [], f"pool claim failed: {failures!r}"
    first = claimed["worker-1"]
    second = claimed["worker-2"]
    assert len(first) == 3
    assert len(second) == 3
    assert set(first).isdisjoint(second)
    assert len(set(first + second)) == 6


def test_heartbeat_from_other_thread_uses_its_own_connection(
    factory: ManifestConnectionFactory,
) -> None:
    """每个线程独立连接：主线程领取，worker 线程用新连接续约成功。"""
    factory.initialize()
    seed_repository = _open(factory)
    try:
        seed_queued_task(seed_repository, task_id="task-thread")
        seed_repository.claim_tasks(owner="worker-1", limit=1, now=FIXED_NOW)
    finally:
        seed_repository.close()

    result: list[bool] = []

    def heartbeat_worker() -> None:
        repository = _open(factory)
        try:
            now: datetime = FIXED_NOW
            result.append(repository.heartbeat("task-thread", owner="worker-1", now=now))
        finally:
            repository.close()

    thread = threading.Thread(target=heartbeat_worker)
    thread.start()
    thread.join(timeout=30)
    assert result == [True]
