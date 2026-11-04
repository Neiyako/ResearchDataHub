"""CRUD、外键、事务原子性、乐观并发与惰性迭代的测试（真实 SQLite）。"""

from __future__ import annotations

import sqlite3
import threading
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import (
    FIXED_NOW,
    make_asset,
    make_file_record,
    make_integrity_report,
    make_metadata,
    make_request,
    make_source_metadata,
    make_task,
)
from tests.unit.test_manifest.helpers import (
    make_task_in_state,
    seed_queued_task,
    seed_running_task,
)

from researchdatahub.core.errors import ManifestError, StateTransitionError
from researchdatahub.core.models import AcquisitionPlan, Priority
from researchdatahub.core.ports import ManifestRepository, assert_implements
from researchdatahub.core.state import TaskState, transition
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository


# --------------------------------------------------------------------------- #
# 数据源 / 数据集 / 请求
# --------------------------------------------------------------------------- #
def test_upsert_source_round_trip_and_idempotent_update(repo: SqliteManifestRepository) -> None:
    source = make_source_metadata()
    repo.upsert_source(source, adapter_id="ncar-http")
    assert repo.get_source(source.source_id) == source
    assert repo.list_sources() == [source]

    updated = source.model_copy(update={"display_name": "NCAR RDA (updated)"})
    repo.upsert_source(updated, adapter_id="ncar-s3")
    assert repo.get_source(source.source_id) == updated
    assert len(repo.list_sources()) == 1


def test_get_source_missing_raises(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.get_source("unknown-source")


def test_save_dataset_round_trip(repo: SqliteManifestRepository) -> None:
    metadata = make_metadata()
    repo.save_dataset("era5-pressure-levels", metadata)
    name, loaded = repo.get_dataset("era5-pressure-levels")
    assert name == metadata.product
    assert loaded == metadata


def test_get_dataset_missing_raises(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.get_dataset("no-such-dataset")


def test_save_request_round_trip_and_upsert(repo: SqliteManifestRepository) -> None:
    request = make_request()
    repo.save_request(request)
    assert repo.get_request(request.request_id) == request

    updated = request.model_copy(update={"priority": Priority.HIGH, "max_sources": 1})
    repo.save_request(updated)
    assert repo.get_request(request.request_id) == updated


def test_get_request_missing_raises(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.get_request("no-such-request")


# --------------------------------------------------------------------------- #
# 资产 / 计划
# --------------------------------------------------------------------------- #
def test_save_asset_round_trip_and_filters(repo: SqliteManifestRepository) -> None:
    asset = make_asset()
    repo.save_asset(asset)
    assert list(repo.list_assets()) == [asset]
    assert list(repo.list_assets(dataset_id=asset.dataset_id)) == [asset]
    assert list(repo.list_assets(source_id=asset.source_id)) == [asset]
    assert list(repo.list_assets(dataset_id="other")) == []


def test_save_asset_without_checksum_round_trips(repo: SqliteManifestRepository) -> None:
    asset = make_asset(asset_id="asset-no-checksum", checksum=None)
    repo.save_asset(asset)
    loaded = next(iter(repo.list_assets()))
    assert loaded.checksum is None
    assert loaded == asset


def _build_plan(tasks: list[object], **overrides: object) -> AcquisitionPlan:
    payload: dict[str, object] = {
        "plan_id": "plan-0001",
        "request_id": "req-era5-clwc-2011-2020",
        "policy": "default",
        "tasks": tasks,
        "unmatched_variables": ["ciwc"],
        "generated_at": FIXED_NOW,
    }
    payload.update(overrides)
    return AcquisitionPlan.model_validate(payload)


def test_save_plan_round_trip_preserves_declaration_order(
    repo: SqliteManifestRepository,
) -> None:
    tasks = [
        make_task_in_state(TaskState.PLANNED, task_id="task-a"),
        make_task_in_state(TaskState.PLANNED, task_id="task-b"),
        make_task_in_state(TaskState.PLANNED, task_id="task-c"),
    ]
    plan = _build_plan(list(tasks))
    repo.save_plan(plan)
    loaded = repo.load_plan(plan.plan_id)
    assert loaded.plan_id == plan.plan_id
    assert loaded.policy == "default"
    assert loaded.unmatched_variables == ["ciwc"]
    assert loaded.task_ids() == ["task-a", "task-b", "task-c"]
    assert loaded.generated_at == FIXED_NOW


def test_save_plan_replay_does_not_downgrade_existing_task(
    repo: SqliteManifestRepository,
) -> None:
    planned = make_task_in_state(TaskState.PLANNED, task_id="task-replay")
    plan = _build_plan([planned])
    repo.save_plan(plan)

    queued = transition(planned, TaskState.QUEUED, reason="replay test", now=FIXED_NOW)
    repo.save_task(queued, expected_state=TaskState.PLANNED)

    replay = _build_plan([planned], policy="replay")
    repo.save_plan(replay)
    loaded = repo.load_plan(plan.plan_id)
    assert loaded.policy == "replay"
    assert loaded.tasks[0].state is TaskState.QUEUED


def test_load_plan_missing_raises(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.load_plan("plan-ghost")


# --------------------------------------------------------------------------- #
# 文件记录 / 完整性报告 / 事件
# --------------------------------------------------------------------------- #
def test_file_record_round_trip_and_task_association(repo: SqliteManifestRepository) -> None:
    seed_running_task(repo, task_id="task-0001")
    record = make_file_record()
    repo.save_file_record(record)

    assert repo.load_file_record(record.file_id) == record
    assert list(repo.list_file_records(task_id=record.task_id)) == [record]
    assert list(repo.list_file_records(request_id="req-era5-clwc-2011-2020")) == [record]
    assert list(repo.list_file_records(task_id="task-other")) == []


def test_file_record_foreign_key_rejects_unknown_task(repo: SqliteManifestRepository) -> None:
    ghost = make_file_record(task_id="task-ghost")
    with pytest.raises(ManifestError):
        repo.save_file_record(ghost)
    with pytest.raises(ManifestError, match="not registered"):
        repo.load_file_record(ghost.file_id)


def test_load_file_record_missing_raises(repo: SqliteManifestRepository) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.load_file_record("file-ghost")


def test_integrity_reports_are_append_only(repo: SqliteManifestRepository) -> None:
    seed_running_task(repo, task_id="task-0001")
    repo.save_file_record(make_file_record())
    report = make_integrity_report()
    repo.save_integrity_report(report)
    assert repo.list_integrity_reports(task_id="task-0001") == [report]
    assert repo.list_integrity_reports(file_id="file-0001") == [report]

    with pytest.raises(ManifestError):
        repo.save_integrity_report(report)


def test_integrity_report_foreign_keys(repo: SqliteManifestRepository) -> None:
    seed_running_task(repo, task_id="task-0001")
    no_task = make_integrity_report(report_id="report-no-task", task_id="task-ghost")
    with pytest.raises(ManifestError):
        repo.save_integrity_report(no_task)

    no_file = make_integrity_report(report_id="report-no-file", file_id="file-ghost")
    with pytest.raises(ManifestError):
        repo.save_integrity_report(no_file)


def test_append_event_and_list_events_in_order(repo: SqliteManifestRepository) -> None:
    seed_running_task(repo, task_id="task-0001")
    repo.append_event("task-0001", "started", {"message": "transfer started", "bytes": 0})
    repo.append_event("task-0001", "bytes_flushed", {"bytes_transferred": 2048})
    repo.append_event("task-0001", "completed", {})

    events = repo.list_events("task-0001")
    assert [event.event_type for event in events] == ["started", "bytes_flushed", "completed"]
    assert events[0].message == "transfer started"
    assert events[1].bytes_transferred == 2048
    assert events[2].message == "event completed"
    assert len(repo.list_events("task-0001", limit=1)) == 1
    assert repo.list_events("task-other") == []


def test_append_event_validation_errors(repo: SqliteManifestRepository) -> None:
    seed_running_task(repo, task_id="task-0001")
    with pytest.raises(ManifestError, match="bytes_transferred"):
        repo.append_event("task-0001", "bad", {"bytes_transferred": "many"})
    with pytest.raises(ManifestError, match="JSON"):
        repo.append_event("task-0001", "bad", {"payload": object()})
    with pytest.raises(ManifestError):
        repo.append_event("task-ghost", "bad", {})
    with pytest.raises(ManifestError, match="limit"):
        repo.list_events("task-0001", limit=-1)


# --------------------------------------------------------------------------- #
# 任务状态推进 / 乐观并发 / 事务回滚
# --------------------------------------------------------------------------- #
def test_save_task_insert_then_round_trip(repo: SqliteManifestRepository) -> None:
    task = make_task()
    repo.save_task(task)
    assert repo.load_task(task.task_id) == task
    assert list(repo.list_tasks()) == [task]


def test_save_task_expected_state_success_and_conflict(repo: SqliteManifestRepository) -> None:
    task = make_task()
    repo.save_task(task)
    planned = transition(task, TaskState.PLANNED, reason="planned", now=FIXED_NOW)
    repo.save_task(planned, expected_state=TaskState.PENDING)
    assert repo.load_task(task.task_id).state is TaskState.PLANNED

    # 另一个仍以为任务停在 PENDING 的 writer（旧快照）再写一次：必须冲突。
    with pytest.raises(ManifestError, match="conflict"):
        repo.save_task(planned, expected_state=TaskState.PENDING)
    assert repo.load_task(task.task_id).state is TaskState.PLANNED


def test_save_task_expected_state_requires_existing_row(repo: SqliteManifestRepository) -> None:
    task = make_task()
    with pytest.raises(ManifestError, match="does not exist"):
        repo.save_task(task, expected_state=TaskState.PENDING)


def test_save_task_rejects_illegal_state_bypassing_state_machine(
    repo: SqliteManifestRepository,
) -> None:
    queued = seed_queued_task(repo, task_id="task-0001")
    illegal = queued.model_copy(update={"state": TaskState.COMPLETED, "finished_at": FIXED_NOW})
    with pytest.raises(StateTransitionError, match="illegal transition"):
        repo.save_task(illegal)
    assert repo.load_task(queued.task_id).state is TaskState.QUEUED


def test_save_task_and_event_is_atomic_under_injected_failure(
    repo: SqliteManifestRepository,
    factory: ManifestConnectionFactory,
) -> None:
    queued = seed_queued_task(repo, task_id="task-tx")
    running = transition(
        queued, TaskState.RUNNING, reason="tx test", now=FIXED_NOW, worker_id="worker-1"
    )

    injector = factory.connect()
    try:
        injector.execute(
            "CREATE TRIGGER fail_transfer_logs BEFORE INSERT ON transfer_logs "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )
    finally:
        injector.close()

    with pytest.raises(ManifestError):
        repo.save_task_and_event(
            running,
            expected_state=TaskState.QUEUED,
            event="task_claimed",
            payload={"worker_id": "worker-1"},
        )
    # 任务状态与事件必须一起回滚：状态没变、事件没写。
    assert repo.load_task("task-tx").state is TaskState.QUEUED
    assert repo.list_events("task-tx") == []

    cleaner = factory.connect()
    try:
        cleaner.execute("DROP TRIGGER fail_transfer_logs")
    finally:
        cleaner.close()

    repo.save_task_and_event(
        running,
        expected_state=TaskState.QUEUED,
        event="task_claimed",
        payload={"worker_id": "worker-1"},
    )
    assert repo.load_task("task-tx").state is TaskState.RUNNING
    events = repo.list_events("task-tx")
    assert [event.event_type for event in events] == ["task_claimed"]


def test_list_tasks_filters_limit_and_offset(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-a")
    seed_queued_task(repo, task_id="task-b", request_id="req-other-0001")
    seed_running_task(repo, task_id="task-c")

    assert [task.task_id for task in repo.list_tasks(state=TaskState.QUEUED)] == [
        "task-a",
        "task-b",
    ]
    assert [task.task_id for task in repo.list_tasks(state=TaskState.RUNNING)] == ["task-c"]
    assert [task.task_id for task in repo.list_tasks(request_id="req-other-0001")] == ["task-b"]
    assert [task.task_id for task in repo.list_tasks(limit=2)] == ["task-a", "task-b"]
    assert [task.task_id for task in repo.list_tasks(limit=2, offset=2)] == ["task-c"]
    assert [task.task_id for task in repo.list_tasks(offset=1)] == ["task-b", "task-c"]
    with pytest.raises(ManifestError, match="limit"):
        list(repo.list_tasks(limit=-1))
    with pytest.raises(ManifestError, match="offset"):
        list(repo.list_tasks(offset=-1))


def test_list_tasks_is_lazy_and_memory_bounded(repo: SqliteManifestRepository) -> None:
    total = 10000
    tasks = [
        make_task_in_state(TaskState.PLANNED, task_id=f"task-{index:04d}") for index in range(total)
    ]
    repo.save_plan(_build_plan(tasks))

    iterator = repo.list_tasks()
    assert isinstance(iterator, Iterator)

    tracemalloc.start()
    try:
        counted = sum(1 for _ in iterator)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert counted == total
    # 惰性迭代：全量 1 万条任务不应一次性驻留（阈值 10 MB，对齐 M1-1 验收标准）。
    assert peak < 10 * 1024 * 1024, f"list_tasks peak memory too high: {peak} bytes"


def test_status_reports_real_counts(repo: SqliteManifestRepository) -> None:
    seed_queued_task(repo, task_id="task-queued")
    seed_running_task(repo, task_id="task-running")
    repo.save_file_record(make_file_record(task_id="task-running"))
    repo.save_integrity_report(
        make_integrity_report(
            report_id="report-failed",
            status="failed",
            failures=["checksum mismatch"],
            file_id=None,
            task_id="task-running",
        )
    )

    status = repo.status()
    assert status.db_path == repo.db_path
    assert status.db_schema_version == repo.schema_version
    assert status.journal_mode == "wal"
    assert status.foreign_keys is True
    assert status.task_counts["QUEUED"] == 1
    assert status.task_counts["RUNNING"] == 1
    assert status.task_counts["COMPLETED"] == 0
    assert status.file_count == 1
    assert status.integrity_failures == 1
    assert status.delivery_ready_requests == []


def test_foreign_keys_are_enforced_at_connection_level(
    factory: ManifestConnectionFactory,
) -> None:
    factory.initialize()
    connection = factory.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO files (file_id, task_id, dataset_id, source_id, access_route, "
                "origin_uri, local_path, size_bytes, checksum, retrieved_at, is_read_only, "
                "extensions, created_at) VALUES ('file-fk', 'task-ghost', 'era5', 'ncar', "
                "'s3', 's3://x/y.nc', '/tmp/y.nc', 1, 'sha256:" + "a" * 64 + "', "
                "?, 1, '{}', ?)",
                (FIXED_NOW.isoformat(), FIXED_NOW.isoformat()),
            )
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# 生命周期与线程模型
# --------------------------------------------------------------------------- #
def test_repository_implements_manifest_protocol(repo: SqliteManifestRepository) -> None:
    assert_implements(repo, ManifestRepository)
    assert isinstance(repo, ManifestRepository)


def test_close_is_idempotent_and_closed_repository_rejects_calls(
    factory: ManifestConnectionFactory,
) -> None:
    factory.initialize()
    repository = SqliteManifestRepository(factory)
    repository.close()
    repository.close()
    with pytest.raises(RuntimeError, match="closed"):
        repository.load_task("task-0001")
    with pytest.raises(RuntimeError, match="closed"):
        _ = repository.schema_version
    with pytest.raises(RuntimeError, match="closed"):
        repository.status()


def test_context_manager_closes_repository(factory: ManifestConnectionFactory) -> None:
    factory.initialize()
    with SqliteManifestRepository(factory) as repository:
        repository.save_task(make_task())
    with pytest.raises(RuntimeError, match="closed"):
        repository.load_task("task-0001")


def test_connection_cannot_be_used_from_another_thread(
    repo: SqliteManifestRepository,
) -> None:
    repo.save_task(make_task())
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            repo.load_task("task-0001")
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], sqlite3.ProgrammingError)


def test_aiter_tasks_yields_all_tasks(repo: SqliteManifestRepository) -> None:
    import asyncio

    seed_queued_task(repo, task_id="task-a")
    seed_queued_task(repo, task_id="task-b")

    async def collect() -> list[str]:
        return [task.task_id async for task in repo.aiter_tasks()]

    assert asyncio.run(collect()) == ["task-a", "task-b"]


def test_task_timestamps_round_trip_with_lease_fields(repo: SqliteManifestRepository) -> None:
    running = seed_running_task(repo, task_id="task-lease", owner="worker-7", now=FIXED_NOW)
    assert running.heartbeat_at == FIXED_NOW
    assert running.lease_expires_at == FIXED_NOW + timedelta(seconds=900)
    loaded = repo.load_task("task-lease")
    assert loaded.heartbeat_at == FIXED_NOW
    assert loaded.lease_expires_at == running.lease_expires_at
    assert loaded.worker_id == "worker-7"
    assert loaded.started_at == FIXED_NOW
    assert loaded.attempt == 1


def test_utc_normalization_preserves_instants(repo: SqliteManifestRepository) -> None:
    """同一时刻的不同时区表示必须落库为同一个 UTC 值（不丢时区语义）。"""
    other_zone = datetime(2026, 11, 4, 20, 0, 0, tzinfo=UTC).astimezone(
        datetime.now().astimezone().tzinfo
    )
    task = make_task_in_state(TaskState.PLANNED, task_id="task-tz", now=other_zone)
    repo.save_task(task)
    loaded = repo.load_task("task-tz")
    assert loaded.last_transition_at == other_zone
    assert loaded.last_transition_at is not None
    assert loaded.last_transition_at.tzinfo is not None
