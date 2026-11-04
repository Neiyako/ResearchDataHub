"""有限并发执行器：真实 HTTP 服务器验证「并发被真的限制住、结果顺序稳定」。

并发断言不靠 mock 计数，而是让服务器记录**同时在处理的请求数**：4 个慢任务、
``max_workers=2`` 时，服务端观测到的最大并发必须是 2（而不是 4）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from tests.unit.test_transfer.helpers import make_http_adapter, make_http_asset, make_task_for
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.core.models import TransferStatus
from researchdatahub.transfer import (
    MAX_MAX_WORKERS,
    BoundedTransferRunner,
    TransferErrorCode,
    TransferJob,
)


def _jobs(server: TransferTestServer, tmp_path: Path, count: int) -> list[TransferJob]:
    jobs: list[TransferJob] = []
    for index in range(count):
        body = bytes([index]) * 65536
        server.add(
            f"/job-{index}.bin",
            Resource(
                body=body,
                etag=f'"v{index}"',
                slow_chunk_seconds=0.02,
                send_chunk_bytes=16384,
            ),
        )
        asset = make_http_asset(server.url(f"/job-{index}.bin"), size_bytes=len(body))
        jobs.append(
            TransferJob(
                asset=asset,
                destination=tmp_path / f"job-{index}.bin",
                task=make_task_for(asset),
            )
        )
    return jobs


def test_bounded_concurrency_never_exceeds_max_workers(
    server: TransferTestServer, tmp_path: Path
) -> None:
    jobs = _jobs(server, tmp_path, 4)
    adapter = make_http_adapter()
    with BoundedTransferRunner(adapter, max_workers=2) as runner:
        results = runner.run(jobs)
    assert [r.status for r in results] == [TransferStatus.SUCCEEDED] * 4
    # 服务端真实观测：最多 2 个请求同时在处理
    assert server.max_concurrent == 2
    assert server.max_concurrent <= 2
    for index in range(4):
        assert (tmp_path / f"job-{index}.bin").read_bytes() == bytes([index]) * 65536
    adapter.close()


def test_results_follow_input_order(server: TransferTestServer, tmp_path: Path) -> None:
    jobs = _jobs(server, tmp_path, 3)
    adapter = make_http_adapter()
    with BoundedTransferRunner(adapter, max_workers=2) as runner:
        results = runner.run(jobs)
    assert [result.task_id for result in results] == [job.task.task_id for job in jobs]
    assert [result.destination_path for result in results] == [str(job.destination) for job in jobs]
    adapter.close()


def test_one_failure_does_not_break_the_batch(server: TransferTestServer, tmp_path: Path) -> None:
    jobs = _jobs(server, tmp_path, 2)
    missing_body = b"x" * 10
    missing_asset = make_http_asset(server.url("/missing.bin"), size_bytes=len(missing_body))
    jobs.append(
        TransferJob(
            asset=missing_asset,
            destination=tmp_path / "missing.bin",
            task=make_task_for(missing_asset),
        )
    )
    adapter = make_http_adapter()
    with BoundedTransferRunner(adapter, max_workers=2) as runner:
        results = runner.run(jobs)
    assert results[0].status is TransferStatus.SUCCEEDED
    assert results[1].status is TransferStatus.SUCCEEDED
    assert results[2].status is TransferStatus.FAILED
    assert results[2].error_code == str(TransferErrorCode.HTTP_NOT_FOUND)
    adapter.close()


def test_invalid_worker_count_is_rejected() -> None:
    adapter = make_http_adapter()
    with pytest.raises(ValueError, match="max_workers"):
        BoundedTransferRunner(adapter, max_workers=0)
    with pytest.raises(ValueError, match="max_workers"):
        BoundedTransferRunner(adapter, max_workers=MAX_MAX_WORKERS + 1)
    adapter.close()


def test_closed_runner_rejects_new_jobs(server: TransferTestServer, tmp_path: Path) -> None:
    jobs = _jobs(server, tmp_path, 1)
    adapter = make_http_adapter()
    runner = BoundedTransferRunner(adapter, max_workers=1)
    runner.close()
    runner.close()  # 幂等
    with pytest.raises(RuntimeError):
        runner.run(jobs)
    adapter.close()


def test_cancel_event_cancels_only_its_job(server: TransferTestServer, tmp_path: Path) -> None:
    jobs = _jobs(server, tmp_path, 2)
    cancel = threading.Event()
    cancel.set()
    cancelled = TransferJob(
        asset=jobs[0].asset,
        destination=tmp_path / "cancelled.bin",
        task=jobs[0].task,
        cancel_event=cancel,
    )
    adapter = make_http_adapter()
    with BoundedTransferRunner(adapter, max_workers=2) as runner:
        results = runner.run([cancelled, jobs[1]])
    assert results[0].status is TransferStatus.FAILED
    assert results[0].error_code == str(TransferErrorCode.CANCELLED)
    assert results[1].status is TransferStatus.SUCCEEDED
    adapter.close()
