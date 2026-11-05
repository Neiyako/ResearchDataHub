"""本地 worker：复用真实传输通道、真实 SHA-256、真实并发与取消。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.test_network.conftest import (
    file_bytes,
    make_asset,
    make_task,
    serve,
    wait_until,
)
from tests.unit.test_transfer.http_server import TransferTestServer

from researchdatahub.network.workers.base import WorkerTaskError
from researchdatahub.network.workers.local import LocalWorker
from researchdatahub.network.workers.models import RemoteTaskState, WorkerKind


def test_local_worker_downloads_real_bytes(
    local_worker: LocalWorker, http_server: TransferTestServer
) -> None:
    data = file_bytes(256 * 1024)
    serve(http_server, "/ncar/e5.nc", data)
    task = make_task(make_asset(uri=http_server.url("/ncar/e5.nc"), data=data))
    execution = local_worker.prepare(task, relative_path="era5/f.nc")
    assert wait_until(lambda: local_worker.poll(execution).terminal)
    assert local_worker.poll(execution) is RemoteTaskState.COMPLETED
    result = local_worker.result(execution)
    assert result.size_bytes == len(data)
    assert result.sha256 == task.asset.checksum.hexdigest  # type: ignore[union-attr]
    assert Path(result.path).is_file()
    assert Path(result.path).read_bytes() == data


def test_local_worker_fetch_writes_verified_copy(
    local_worker: LocalWorker, http_server: TransferTestServer, tmp_path: Path
) -> None:
    data = file_bytes(64 * 1024)
    serve(http_server, "/ncar/copy.nc", data)
    task = make_task(make_asset(uri=http_server.url("/ncar/copy.nc"), data=data))
    execution = local_worker.prepare(task, relative_path="copy/f.nc")
    assert wait_until(lambda: local_worker.poll(execution).terminal)
    destination = tmp_path / "delivered" / "f.nc"
    report = local_worker.fetch(execution, destination)
    assert destination.read_bytes() == data
    assert report.size_bytes == len(data)
    assert report.resumed is False
    # 二次 fetch：目标已存在且校验一致 → resumed=True（不重复复制）
    again = local_worker.fetch(execution, destination)
    assert again.resumed is True


def test_local_worker_is_idempotent_per_task(
    local_worker: LocalWorker, http_server: TransferTestServer
) -> None:
    data = file_bytes(32 * 1024)
    resource = serve(http_server, "/ncar/idem.nc", data)
    task = make_task(make_asset(uri=http_server.url("/ncar/idem.nc"), data=data))
    first = local_worker.prepare(task, relative_path="idem/f.nc")
    second = local_worker.prepare(task, relative_path="idem/f.nc")
    assert first == second
    assert wait_until(lambda: local_worker.poll(first).terminal)
    assert local_worker.executions == 1
    assert len(resource.get_requests) == 1


def test_local_worker_detects_size_mismatch(
    local_worker: LocalWorker, http_server: TransferTestServer
) -> None:
    data = file_bytes(16 * 1024)
    serve(http_server, "/ncar/trunc.nc", data)
    asset = make_asset(uri=http_server.url("/ncar/trunc.nc"), data=data)
    lying = asset.model_copy(update={"size_bytes": len(data) + 10})
    task = make_task(lying)
    execution = local_worker.prepare(task, relative_path="trunc/f.nc")
    assert wait_until(lambda: local_worker.poll(execution).terminal)
    assert local_worker.poll(execution) is RemoteTaskState.FAILED
    with pytest.raises(WorkerTaskError) as excinfo:
        local_worker.result(execution)
    assert excinfo.value.retryable is True
    assert "size mismatch" in str(excinfo.value)


def test_local_worker_detects_checksum_mismatch(
    local_worker: LocalWorker, http_server: TransferTestServer
) -> None:
    data = file_bytes(16 * 1024)
    serve(http_server, "/ncar/bad.nc", data)
    asset = make_asset(uri=http_server.url("/ncar/bad.nc"), data=data)
    tampered = asset.model_copy(
        update={"checksum": asset.checksum.model_copy(update={"value": "sha256:" + "b" * 64})}
    )
    task = make_task(tampered)
    execution = local_worker.prepare(task, relative_path="bad/f.nc")
    assert wait_until(lambda: local_worker.poll(execution).terminal)
    assert local_worker.poll(execution) is RemoteTaskState.FAILED


def test_local_worker_reports_transport_failure(
    local_worker: LocalWorker, http_server: TransferTestServer
) -> None:
    task = make_task(make_asset(uri=http_server.url("/missing.nc"), data=b"x" * 16))
    execution = local_worker.prepare(task, relative_path="missing/f.nc")
    assert wait_until(lambda: local_worker.poll(execution).terminal, timeout=15.0)
    assert local_worker.poll(execution) is RemoteTaskState.FAILED


def test_local_worker_rejects_path_escape(local_worker: LocalWorker) -> None:
    task = make_task(make_asset(uri="http://127.0.0.1:1/f.nc", data=b"x"))
    with pytest.raises(WorkerTaskError):
        local_worker.prepare(task, relative_path="../escape.nc")


def test_local_worker_enforces_concurrency_limit(
    tmp_path: Path, http_adapter: object, http_server: TransferTestServer
) -> None:
    data = file_bytes(1024)
    serve(http_server, "/ncar/slow.nc", data, slow_chunk_seconds=0.05)
    worker = LocalWorker(
        worker_id="local-one",
        storage_root=tmp_path / "one",
        transports=[http_adapter],  # type: ignore[list-item]
        max_concurrent=1,
    )
    try:
        slow_asset = make_asset(uri=http_server.url("/ncar/slow.nc"), data=data)
        first = make_task(slow_asset, task_id="t-a")
        second = make_task(slow_asset, task_id="t-b")
        worker.prepare(first, relative_path="a/f.nc")
        with pytest.raises(WorkerTaskError) as excinfo:
            worker.prepare(second, relative_path="b/f.nc")
        assert excinfo.value.retryable is True
    finally:
        worker.close()


def test_local_worker_cancel_stops_execution(
    tmp_path: Path, http_adapter: object, http_server: TransferTestServer
) -> None:
    data = file_bytes(8 * 1024 * 1024)
    serve(http_server, "/ncar/big.nc", data, slow_chunk_seconds=0.01, send_chunk_bytes=64 * 1024)
    worker = LocalWorker(
        worker_id="local-cancel",
        storage_root=tmp_path / "cancel",
        transports=[http_adapter],  # type: ignore[list-item]
        max_concurrent=2,
    )
    try:
        task = make_task(make_asset(uri=http_server.url("/ncar/big.nc"), data=data))
        execution = worker.prepare(task, relative_path="big/f.nc")
        assert worker.cancel(execution) is True
        assert wait_until(lambda: worker.poll(execution).terminal, timeout=20.0)
        assert worker.poll(execution) is RemoteTaskState.CANCELLED
        assert worker.cancel(execution) is False
    finally:
        worker.close()


def test_local_worker_identity_and_health(local_worker: LocalWorker) -> None:
    assert local_worker.worker_id == "local-a"
    assert local_worker.kind is WorkerKind.LOCAL
    assert local_worker.artifact_root == local_worker.storage_root
    health = local_worker.health()
    assert health.uptime_s is not None
    assert local_worker.active_executions == 0


def test_local_worker_requires_transport(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one transfer executor"):
        LocalWorker(worker_id="x", storage_root=tmp_path, transports=[])
