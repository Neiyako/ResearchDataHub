"""崩溃恢复测试：租约过期回收、进程重启后续跑、崩溃窗口对账。

全部使用真实 SQLite、真实状态机、真实 ``LocalStorageBackend``（提交日志/隔离区是真的），
不 mock "文件已提交但 DB 未更新" 这类中间状态——它由真实的 ``commit_staging`` 产生。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.unit.test_scheduler.support import (
    DATASET_ID,
    StaticSource,
    directory_files,
    make_https_asset,
    make_scheduler,
    new_request,
    serve_bytes,
    utc_now,
)

from researchdatahub.core.models import Checksum, IntegrityStatus
from researchdatahub.core.state import TaskState, transition
from researchdatahub.planner.service import create_plan
from researchdatahub.scheduler import RecoveryService
from researchdatahub.scheduler.models import ReconcileAction

STALE_SECONDS = 60


def _recovery(config, repo_factory, storage, integrity) -> RecoveryService:
    return RecoveryService(
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        config=config,
        clock=utc_now,
    )


def _queue_plan(config, repo, asset) -> str:
    """真实链路：登记需求 → 计划落库 → PLANNED→QUEUED；返回 task_id。"""
    plan = create_plan(
        repo=repo,
        request=new_request(),
        adapters=[StaticSource("ncar", [asset])],
        config=config,
    )
    task_id = plan.task_ids()[0]
    planned = repo.load_task(task_id)
    queued = transition(planned, TaskState.QUEUED, reason="test: enqueue", now=utc_now())
    repo.save_task(queued, expected_state=TaskState.PLANNED)
    return task_id


def _claim_crashed(repo, task_id: str, *, seconds_ago: int) -> None:
    """模拟"worker 领取后崩溃"：真实 claim，但时间戳在 120 秒前且租约只有 1 秒。"""
    claimed = repo.claim_tasks(
        owner="crashed-worker",
        limit=1,
        now=utc_now() - timedelta(seconds=seconds_ago),
        lease_seconds=1,
    )
    assert [task.task_id for task in claimed] == [task_id]


def _advance_to_verifying(repo, task_id: str) -> None:
    task = repo.load_task(task_id)
    verifying = transition(
        task, TaskState.VERIFYING, reason="test: transfer finished before crash", now=utc_now()
    )
    repo.save_task(verifying, expected_state=TaskState.RUNNING)


def test_release_expired_requeues_stale_running_task(
    config, repo, repo_factory, storage, integrity, server
) -> None:
    data = b"stale-task" * 100
    serve_bytes(server, "/stale/file.nc", data)
    asset = make_https_asset(server, path="/stale/file.nc", data=data, file_name="stale.nc")
    task_id = _queue_plan(config, repo, asset)
    _claim_crashed(repo, task_id, seconds_ago=120)

    results = _recovery(config, repo_factory, storage, integrity).release_expired()

    assert len(results) == 1
    result = results[0]
    assert result.task_id == task_id
    assert result.previous_state == "RUNNING"
    assert result.new_state == "RETRY_WAIT"
    assert result.lease_owner == "crashed-worker"
    recovered = repo.load_task(task_id)
    assert recovered.state is TaskState.RETRY_WAIT
    assert recovered.scheduled_at is not None
    event_types = [event.event_type for event in repo.list_events(task_id)]
    assert "lease_recovered" in event_types


def test_release_expired_renews_when_heartbeat_is_fresh(
    config, repo, repo_factory, storage, integrity, server
) -> None:
    data = b"slow-but-alive" * 50
    serve_bytes(server, "/fresh/file.nc", data)
    asset = make_https_asset(server, path="/fresh/file.nc", data=data, file_name="fresh.nc")
    task_id = _queue_plan(config, repo, asset)
    # 租约 1 秒已过期，但心跳只有 10 秒前（< stale 60 秒）→ 应续租而不是接管。
    _claim_crashed(repo, task_id, seconds_ago=10)

    results = _recovery(config, repo_factory, storage, integrity).release_expired()

    assert len(results) == 1
    assert results[0].previous_state == "RUNNING"
    assert results[0].new_state == "RUNNING"
    renewed = repo.load_task(task_id)
    assert renewed.state is TaskState.RUNNING
    assert renewed.lease_expires_at is not None
    assert renewed.heartbeat_at is not None
    assert renewed.worker_id == "crashed-worker"
    event_types = [event.event_type for event in repo.list_events(task_id)]
    assert "lease_renewed" in event_types


def test_process_restart_recovery_then_run_completes(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"restart-me" * 500
    serve_bytes(server, "/restart/file.nc", data)
    asset = make_https_asset(server, path="/restart/file.nc", data=data, file_name="restart.nc")
    task_id = _queue_plan(config, repo, asset)
    _claim_crashed(repo, task_id, seconds_ago=120)
    assert repo.load_task(task_id).state is TaskState.RUNNING

    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    summary = scheduler.run()

    assert any(result.new_state == "RETRY_WAIT" for result in summary.recovered)
    assert summary.counts == {"completed": 1}, summary.as_dict()
    task = repo.load_task(task_id)
    assert task.state is TaskState.COMPLETED
    assert task.attempt == 2  # 第一次领取后崩溃，恢复后是第二次
    records = list(repo.list_file_records(task_id=task_id))
    assert len(records) == 1
    assert Path(records[0].local_path).read_bytes() == data


def test_committed_file_without_db_record_is_reconciled(
    config, repo, repo_factory, storage, integrity, server
) -> None:
    """崩溃窗口 2：文件已提交（提交日志已 finalize），但 DB 没有文件记录/完成状态。"""
    data = b"committed-but-unrecorded" * 100
    serve_bytes(server, "/recon/file.nc", data)
    asset = make_https_asset(server, path="/recon/file.nc", data=data, file_name="recon.nc")
    task_id = _queue_plan(config, repo, asset)
    _claim_crashed(repo, task_id, seconds_ago=120)
    _advance_to_verifying(repo, task_id)
    # 真实提交：写 staging → commit_staging（会产生正式文件 + 提交日志）。
    staging = storage.allocate_staging(task_id, "recon.nc")
    staging.write_bytes(data)
    publish = storage.commit_staging(
        staging,
        dataset_id=DATASET_ID,
        file_name="recon.nc",
        expected_size=len(data),
        expected_checksum=asset.checksum,
    )
    assert publish.committed
    assert list(repo.list_file_records(task_id=task_id)) == []

    report = _recovery(config, repo_factory, storage, integrity).reconcile()

    assert task_id in report.completed
    assert any(entry.action is ReconcileAction.COMPLETED for entry in report.entries)
    task = repo.load_task(task_id)
    assert task.state is TaskState.COMPLETED
    records = list(repo.list_file_records(task_id=task_id))
    assert len(records) == 1
    assert Path(records[0].local_path) == publish.path
    integrity_reports = repo.list_integrity_reports(task_id=task_id)
    assert [item.status for item in integrity_reports] == [IntegrityStatus.PASSED]


def test_reconcile_reports_missing_file_without_silently_recreating(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"will-be-deleted" * 100
    serve_bytes(server, "/missing/file.nc", data)
    asset = make_https_asset(server, path="/missing/file.nc", data=data, file_name="missing.nc")
    _queue_plan(config, repo, asset)
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    assert scheduler.run().counts == {"completed": 1}
    raw_path = storage.raw_dir / DATASET_ID / "missing.nc"
    assert raw_path.exists()
    raw_path.unlink()

    report = _recovery(config, repo_factory, storage, integrity).reconcile()

    assert report.needs_attention >= 1
    assert any(entry.action is ReconcileAction.FILE_MISSING for entry in report.entries)
    assert not raw_path.exists()  # 不静默重建文件


def test_reconcile_reports_orphan_raw_file(config, repo, repo_factory, storage, integrity) -> None:
    orphan = storage.raw_dir / DATASET_ID / "orphan.nc"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"not-referenced")

    report = _recovery(config, repo_factory, storage, integrity).reconcile()

    assert any(entry.action is ReconcileAction.ORPHAN_RAW for entry in report.entries)
    assert orphan.exists()  # 孤儿文件默认保留，等待人工确认


def test_reconcile_quarantines_committed_file_with_bad_checksum(
    config, repo, repo_factory, storage, integrity, server
) -> None:
    data = b"bad-checksum-content" * 100
    serve_bytes(server, "/bad-recon/file.nc", data)
    asset = make_https_asset(server, path="/bad-recon/file.nc", data=data, file_name="bad-recon.nc")
    wrong = asset.model_copy(update={"checksum": Checksum(value="sha256:" + "1" * 64)})
    task_id = _queue_plan(config, repo, wrong)
    _claim_crashed(repo, task_id, seconds_ago=120)
    _advance_to_verifying(repo, task_id)
    staging = storage.allocate_staging(task_id, "bad-recon.nc")
    staging.write_bytes(data)
    storage.commit_staging(
        staging,
        dataset_id=DATASET_ID,
        file_name="bad-recon.nc",
        expected_size=len(data),
        expected_checksum=None,
    )

    report = _recovery(config, repo_factory, storage, integrity).reconcile()

    assert any(entry.action is ReconcileAction.QUARANTINED for entry in report.entries)
    assert repo.load_task(task_id).state is TaskState.QUARANTINED
    assert list(repo.list_file_records(task_id=task_id)) == []
    quarantined = directory_files(storage.quarantine_dir)
    assert any(path.name.endswith("bad-recon.nc") for path in quarantined)
    reports = repo.list_integrity_reports(task_id=task_id)
    assert reports
    assert reports[-1].status is IntegrityStatus.FAILED
