"""Worker 管道测试：claim → transfer → integrity → commit raw → 文件记录 → COMPLETED。

全部走真实链路：真实临时 SQLite、真实本地 HTTP 服务器（真 socket/真 Range/真中断）、
真实 ``LocalStorageBackend`` 文件移动。没有 mock 掉传输或校验语义。
"""

from __future__ import annotations

import threading
from pathlib import Path

from tests.unit.test_scheduler.support import (
    create_and_submit,
    directory_files,
    make_https_asset,
    make_scheduler,
    new_request,
    serve_bytes,
    wait_until,
)

from researchdatahub.core.models import Checksum, IntegrityStatus
from researchdatahub.core.state import TaskState
from researchdatahub.scheduler import TaskControlService


def _files_for(repo, task_id: str):
    return list(repo.list_file_records(task_id=task_id))


def test_single_task_completes_full_pipeline(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"era5-netcdf-bytes" * 1000
    serve_bytes(server, "/era5/e5.1-2011.nc", data)
    asset = make_https_asset(server, path="/era5/e5.1-2011.nc", data=data, file_name="e5.1-2011.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    request = new_request()
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=request, assets=[asset], config=config
    )

    summary = scheduler.run()

    task_id = plan.task_ids()[0]
    task = repo.load_task(task_id)
    assert task.state is TaskState.COMPLETED
    assert task.attempt == 1
    assert summary.counts == {"completed": 1}
    assert summary.completed == 1

    records = _files_for(repo, task_id)
    assert len(records) == 1
    record = records[0]
    raw_path = Path(record.local_path)
    assert raw_path == storage.raw_dir / "era5-pressure-levels" / "e5.1-2011.nc"
    assert raw_path.read_bytes() == data
    assert record.size_bytes == len(data)
    assert record.checksum.value == "sha256:" + __import__("hashlib").sha256(data).hexdigest()

    reports = repo.list_integrity_reports(task_id=task_id)
    assert [report.status for report in reports] == [IntegrityStatus.PASSED]
    assert reports[0].permits_completion

    delivery = repo.build_delivery_manifest(request.request_id)
    assert len(delivery.files) == 1
    assert delivery.total_bytes == len(data)
    assert delivery.complete is True

    event_types = [event.event_type for event in repo.list_events(task_id)]
    assert "task_claimed" in event_types
    assert "transfer_completed" in event_types
    assert "task_completed" in event_types


def test_worker_concurrency_is_bounded(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    assets = []
    for index in range(4):
        data = bytes([index]) * 120_000
        path = f"/slow/{index}.nc"
        serve_bytes(server, path, data, slow_chunk_seconds=0.02, send_chunk_bytes=8192)
        assets.append(make_https_asset(server, path=path, data=data, file_name=f"slow-{index}.nc"))
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
        heartbeat_interval_seconds=5,
        lease_seconds=60,
    )
    create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=assets, config=config
    )

    summary = scheduler.run(max_workers=2)

    assert summary.workers == 2
    assert summary.completed == len(assets)
    assert server.max_concurrent == 2
    assert server.max_concurrent <= 2


def test_retryable_http_error_sets_retry_wait_then_second_run_completes(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"retry-me" * 500
    resource = serve_bytes(server, "/retry/file.nc", data)
    resource.status_plan = [500]
    asset = make_https_asset(server, path="/retry/file.nc", data=data, file_name="retry.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    request = new_request()
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=request, assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    first = scheduler.run()
    assert first.counts == {"retry_wait": 1}
    waiting = repo.load_task(task_id)
    assert waiting.state is TaskState.RETRY_WAIT
    assert waiting.scheduled_at is not None
    assert waiting.last_error is not None
    assert "http_server_error" in waiting.last_error
    assert _files_for(repo, task_id) == []

    second = scheduler.run()
    assert second.counts == {"completed": 1}
    assert repo.load_task(task_id).state is TaskState.COMPLETED


def test_permanent_http_error_fails_task_with_reason(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    # 未注册的路径 → 真实 404 → 永久失败。
    data = b"missing"
    asset = make_https_asset(server, path="/does/not/exist.nc", data=data, file_name="missing.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    summary = scheduler.run()

    assert summary.counts == {"failed": 1}
    task = repo.load_task(task_id)
    assert task.state is TaskState.FAILED
    assert task.last_error is not None
    assert "http_not_found" in task.last_error
    assert _files_for(repo, task_id) == []
    event_types = [event.event_type for event in repo.list_events(task_id)]
    assert "task_failed" in event_types


def test_integrity_failure_moves_file_to_quarantine(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"corrupted-content" * 100
    serve_bytes(server, "/bad/file.nc", data)
    asset = make_https_asset(server, path="/bad/file.nc", data=data, file_name="bad.nc")
    wrong = asset.model_copy(update={"checksum": Checksum(value="sha256:" + "0" * 64)})
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[wrong], config=config
    )
    task_id = plan.task_ids()[0]

    summary = scheduler.run()

    assert summary.counts == {"quarantined": 1}
    task = repo.load_task(task_id)
    assert task.state is TaskState.QUARANTINED
    assert task.last_error is not None
    assert "checksum" in task.last_error
    assert _files_for(repo, task_id) == []
    quarantined = directory_files(storage.quarantine_dir)
    assert any(path.name.endswith("bad.nc") for path in quarantined)
    reports = repo.list_integrity_reports(task_id=task_id)
    assert reports
    assert reports[-1].status is IntegrityStatus.FAILED
    assert reports[-1].file_id is None  # 没有 files 行，报告不得伪造外键


def test_duplicate_execution_is_refused_after_completion(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"once-only" * 300
    serve_bytes(server, "/dup/file.nc", data)
    asset = make_https_asset(server, path="/dup/file.nc", data=data, file_name="dup.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]
    assert scheduler.run().counts == {"completed": 1}
    raw_files_after_first = directory_files(storage.raw_dir)
    assert len(raw_files_after_first) == 1

    second = scheduler.run()

    assert second.counts == {}
    assert second.records == ()
    assert repo.load_task(task_id).state is TaskState.COMPLETED
    assert len(directory_files(storage.raw_dir)) == 1
    assert len(_files_for(repo, task_id)) == 1


def test_identical_existing_target_is_skipped_not_overwritten(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"already-present" * 200
    serve_bytes(server, "/exists/file.nc", data)
    asset = make_https_asset(server, path="/exists/file.nc", data=data, file_name="exists.nc")
    # 预先在正式区放一份内容相同的文件（模拟上一次提交成功但 DB 未记录/重跑）。
    target_dir = storage.raw_dir / "era5-pressure-levels"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "exists.nc"
    target.write_bytes(data)
    original_mtime = target.stat().st_mtime_ns

    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    summary = scheduler.run()

    assert summary.counts == {"skipped_identical": 1}
    assert repo.load_task(task_id).state is TaskState.COMPLETED
    assert target.read_bytes() == data
    assert target.stat().st_mtime_ns == original_mtime  # 没被覆盖
    records = _files_for(repo, task_id)
    assert len(records) == 1
    assert Path(records[0].local_path) == target


def test_interrupted_transfer_resumes_with_range_on_next_run(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    # 400_000 字节、chunk=64KiB：第一次响应在 200_000 字节处被断开，
    # 已经 flush 的 .part 至少有一个完整 chunk，第二次应走 Range 续传。
    data = bytes(range(256)) * 1563  # 400128 字节
    resource = serve_bytes(server, "/resume/file.nc", data, etag='"v1"', truncate_after=200_000)
    asset = make_https_asset(server, path="/resume/file.nc", data=data, file_name="resume.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    first = scheduler.run()
    assert first.counts == {"retry_wait": 1}, first.as_dict()
    assert repo.load_task(task_id).state is TaskState.RETRY_WAIT

    second = scheduler.run()
    assert second.counts == {"completed": 1}, second.as_dict()
    assert repo.load_task(task_id).state is TaskState.COMPLETED
    assert second.records[0].resumed is True
    requests = server.requests_for("/resume/file.nc")
    assert len(requests) >= 2
    assert any(request.range_header for request in requests[1:]), requests
    record = _files_for(repo, task_id)[0]
    assert Path(record.local_path).read_bytes() == data
    assert resource.truncate_after is None


def test_heartbeat_renews_lease_during_long_transfer(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"h" * 160_000
    serve_bytes(server, "/slow-hb/file.nc", data, slow_chunk_seconds=0.02, send_chunk_bytes=8192)
    asset = make_https_asset(server, path="/slow-hb/file.nc", data=data, file_name="hb.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
        heartbeat_interval_seconds=0.2,
        lease_seconds=1,
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    holder: dict[str, object] = {}

    def _run() -> None:
        holder["summary"] = scheduler.run()

    thread = threading.Thread(target=_run, name="scheduler-run", daemon=True)
    thread.start()
    assert wait_until(lambda: repo.load_task(task_id).state is TaskState.RUNNING, timeout=10)
    initial = repo.load_task(task_id)
    assert initial.heartbeat_at is not None
    assert initial.lease_expires_at is not None
    assert wait_until(
        lambda: (
            (repo.load_task(task_id).heartbeat_at or initial.heartbeat_at) > initial.heartbeat_at
        ),
        timeout=10,
    )
    thread.join(timeout=30)
    assert not thread.is_alive()
    summary = holder["summary"]
    assert summary.counts == {"completed": 1}
    final = repo.load_task(task_id)
    assert final.lease_expires_at is None  # 终态释放租约
    assert final.state is TaskState.COMPLETED


def test_lease_loss_stops_worker_without_writing_state(
    config, repo, repo_factory, storage, integrity, transport, server
) -> None:
    data = b"z" * 160_000
    serve_bytes(server, "/lease/file.nc", data, slow_chunk_seconds=0.03, send_chunk_bytes=8192)
    asset = make_https_asset(server, path="/lease/file.nc", data=data, file_name="lease.nc")
    scheduler = make_scheduler(
        config=config,
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=[transport],
        heartbeat_interval_seconds=0.2,
        lease_seconds=1,
    )
    plan = create_and_submit(
        scheduler=scheduler, repo=repo, request=new_request(), assets=[asset], config=config
    )
    task_id = plan.task_ids()[0]

    thread = threading.Thread(target=lambda: scheduler.run(), name="scheduler-run", daemon=True)
    thread.start()
    assert wait_until(lambda: bool(server.requests_for("/lease/file.nc")), timeout=10)
    # 这里必须用真实时钟：调度器线程用真实时间写 started_at，若 cancel 用 FIXED_NOW（固定
    # 2026-11-04T12:00Z），一旦真实时钟越过该时刻，finished_at < started_at 会让持久化任务
    # 违反数据契约（M6 验收时在 12:20Z 触发）。取消时间跟随真实时钟即可。
    TaskControlService(repo).cancel(task_id, "用户取消")
    thread.join(timeout=30)
    assert not thread.is_alive()

    task = repo.load_task(task_id)
    assert task.state is TaskState.CANCELLED
    event_types = [event.event_type for event in repo.list_events(task_id)]
    assert "task_cancelled" in event_types
    assert "task_completed" not in event_types
    assert "task_failed" not in event_types
    assert "lease_lost" in event_types or "lease_lost_detected" in event_types
    assert _files_for(repo, task_id) == []
