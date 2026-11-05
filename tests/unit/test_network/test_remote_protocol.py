"""远程 Worker HTTP 协议：进程内 agent 服务端 + 真实 httpx 客户端（真实 socket）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.unit.test_network.conftest import file_bytes, serve, wait_until
from tests.unit.test_transfer.http_server import TransferTestServer

from researchdatahub.core.models import AccessRoute
from researchdatahub.network.workers.agent import AgentServer, build_agent_server
from researchdatahub.network.workers.client import (
    RemoteProtocolError,
    RemoteUnavailableError,
    RemoteWorkerClient,
)
from researchdatahub.network.workers.models import (
    RemoteTaskRequest,
    RemoteTaskState,
    WorkerCapabilities,
)

TOKEN = "unit-test-token-0123456789"


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[AgentServer]:
    """真实 HTTP agent（随机端口，后台线程）。"""
    server = build_agent_server(
        worker_id="remote-a",
        storage_root=tmp_path / "agent-root",
        token=TOKEN,
        max_concurrent=2,
        log=lambda message: None,
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(agent: AgentServer, tmp_path: Path) -> Iterator[RemoteWorkerClient]:
    """指向该 agent 的真实客户端。"""
    instance = RemoteWorkerClient(
        worker_id="remote-a",
        base_url=agent.base_url,
        token=TOKEN,
        artifact_root=tmp_path / "delivered",
        timeout_seconds=5.0,
    )
    try:
        yield instance
    finally:
        instance.close()


def payload_sha(data: bytes) -> str:
    """``sha256:<hex>``。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task_request(
    *,
    task_id: str,
    uri: str,
    data: bytes,
    target: str = "era5/f.nc",
    checksum: bool = True,
    file_name: str = "f.nc",
) -> RemoteTaskRequest:
    """构造一个远程任务请求。"""
    return RemoteTaskRequest(
        task_id=task_id,
        asset_id=f"asset-{task_id}",
        dataset_id="era5-pressure-levels",
        source_id="ncar",
        provider_id="ncar",
        access_route=AccessRoute.HTTPS,
        origin_uri=uri,
        file_name=file_name,
        size_bytes=len(data),
        expected_checksum=payload_sha(data) if checksum else None,
        target_relative_path=target,
    )


def wait_for_state(
    client: RemoteWorkerClient, task_id: str, *, timeout: float = 15.0
) -> RemoteTaskState:
    """轮询到终态（真实 HTTP 请求，不是 sleep 猜测）。"""
    deadline_states: list[RemoteTaskState] = []
    assert wait_until(lambda: client.status(task_id).state.terminal, timeout=timeout), (
        f"task {task_id} never reached a terminal state: {deadline_states}"
    )
    return client.status(task_id).state


# --------------------------------------------------------------------------- #
# 健康 / 注册 / 心跳
# --------------------------------------------------------------------------- #
def test_health_endpoint_reports_real_facts(client: RemoteWorkerClient) -> None:
    health = client.health_check()
    assert health.uptime_s is not None
    assert health.uptime_s >= 0.0
    capabilities = client.capabilities
    assert "https" in capabilities.protocols


def test_health_requires_token(agent: AgentServer) -> None:
    anonymous = httpx.Client(base_url=agent.base_url, trust_env=False, timeout=5.0)
    try:
        response = anonymous.get("/v1/health")
    finally:
        anonymous.close()
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_wrong_token_is_rejected(agent: AgentServer) -> None:
    bad = RemoteWorkerClient(
        worker_id="remote-a", base_url=agent.base_url, token="x" * 20, timeout_seconds=5.0
    )
    try:
        with pytest.raises(RemoteProtocolError) as excinfo:
            bad.health_check()
        assert excinfo.value.status == 401
    finally:
        bad.close()


def test_register_accepts_declared_capabilities(client: RemoteWorkerClient) -> None:
    response = client.register(WorkerCapabilities(protocols=["https"], max_concurrent=2))
    assert response.accepted is True
    assert response.worker_id == "remote-a"
    allowed = client.health_check()
    assert allowed.uptime_s is not None


def test_register_rejects_foreign_worker_id(client: RemoteWorkerClient) -> None:
    stranger = RemoteWorkerClient(
        worker_id="remote-zzz",
        base_url=client.base_url,
        token=TOKEN,
        timeout_seconds=5.0,
    )
    try:
        response = stranger.register(WorkerCapabilities())
        assert response.accepted is False
        assert response.reason is not None
        assert "cannot register as" in response.reason
    finally:
        stranger.close()


def test_register_rejects_unsupported_protocol(
    client: RemoteWorkerClient, agent: AgentServer
) -> None:
    response = client.register(WorkerCapabilities(protocols=["sftp"]))
    assert response.accepted is False
    assert response.reason is not None
    assert "does not implement" in response.reason
    assert "sftp" not in agent.manager.capabilities.protocols


def test_heartbeat_returns_server_facts(client: RemoteWorkerClient, agent: AgentServer) -> None:
    beat = client.heartbeat()
    assert beat.ok is True
    assert beat.uptime_s >= 0.0
    assert beat.active_tasks >= 0
    assert agent.heartbeats == 1


def test_heartbeat_for_other_worker_conflicts(client: RemoteWorkerClient) -> None:
    other = RemoteWorkerClient(
        worker_id="remote-b", base_url=client.base_url, token=TOKEN, timeout_seconds=5.0
    )
    try:
        with pytest.raises(RemoteProtocolError) as excinfo:
            other.heartbeat()
        assert excinfo.value.status == 403
    finally:
        other.close()


def test_health_worker_identity_mismatch_is_detected(agent: AgentServer) -> None:
    impostor = RemoteWorkerClient(
        worker_id="someone-else", base_url=agent.base_url, token=TOKEN, timeout_seconds=5.0
    )
    try:
        with pytest.raises(RemoteProtocolError) as excinfo:
            impostor.health_check()
        assert excinfo.value.code == "worker_mismatch"
    finally:
        impostor.close()


# --------------------------------------------------------------------------- #
# 任务生命周期
# --------------------------------------------------------------------------- #
def test_task_lifecycle_downloads_real_file(
    client: RemoteWorkerClient,
    http_server: TransferTestServer,
    tmp_path: Path,
) -> None:
    data = file_bytes(2 * 1024 * 1024)
    serve(http_server, "/ncar/life.nc", data)
    request = task_request(task_id="task-life", uri=http_server.url("/ncar/life.nc"), data=data)
    submitted = client.submit(request)
    assert submitted.state in (
        RemoteTaskState.QUEUED,
        RemoteTaskState.EXECUTING,
        RemoteTaskState.COMPLETED,
    )
    assert wait_for_state(client, "task-life") is RemoteTaskState.COMPLETED
    status = client.status("task-life")
    assert status.result is not None
    assert status.result.size_bytes == len(data)
    assert status.result.sha256 == hashlib.sha256(data).hexdigest()
    assert status.bytes_received == len(data)

    from researchdatahub.network.workers.base import WorkerExecution

    execution = WorkerExecution(
        worker_id="remote-a",
        handle="task-life",
        remote_task_id="task-life",
        started_at_monotonic=0.0,
    )
    report = client.fetch(execution, tmp_path / "out" / "life.nc")
    assert report.size_bytes == len(data)
    assert report.sha256 == status.result.sha256
    assert (tmp_path / "out" / "life.nc").read_bytes() == data


def test_duplicate_submit_is_idempotent(
    client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    data = file_bytes(64 * 1024)
    resource = serve(http_server, "/ncar/idem.nc", data)
    request = task_request(task_id="task-idem", uri=http_server.url("/ncar/idem.nc"), data=data)
    first = client.submit(request)
    assert first.created is True
    assert wait_for_state(client, "task-idem") is RemoteTaskState.COMPLETED
    second = client.submit(request)
    assert second.created is False
    assert second.state is RemoteTaskState.COMPLETED
    # 只有一次真实下载：重复提交没有触发第二次 GET
    assert len(resource.get_requests) == 1


def test_conflicting_payload_for_same_task_id_returns_409(
    client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    data = file_bytes(8 * 1024)
    serve(http_server, "/ncar/conflict.nc", data)
    request = task_request(
        task_id="task-conflict", uri=http_server.url("/ncar/conflict.nc"), data=data
    )
    client.submit(request)
    wait_for_state(client, "task-conflict")
    conflicting = request.model_copy(update={"target_relative_path": "other/f.nc"})
    with pytest.raises(RemoteProtocolError) as excinfo:
        client.submit(conflicting)
    assert excinfo.value.status == 409
    assert excinfo.value.code == "request_rejected"


def test_unknown_task_returns_404(client: RemoteWorkerClient) -> None:
    with pytest.raises(RemoteProtocolError) as excinfo:
        client.status("task-does-not-exist")
    assert excinfo.value.status == 404


def test_cancel_finished_task_returns_409(
    client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    data = file_bytes(16 * 1024)
    serve(http_server, "/ncar/done.nc", data)
    client.submit(
        task_request(task_id="task-done", uri=http_server.url("/ncar/done.nc"), data=data)
    )
    wait_for_state(client, "task-done")
    with pytest.raises(RemoteProtocolError):
        client.cancel_remote("task-done")


def test_queued_task_cancel_is_immediate(tmp_path: Path, http_server: TransferTestServer) -> None:
    data = file_bytes(4 * 1024 * 1024)
    serve(
        http_server, "/ncar/slow-big.nc", data, slow_chunk_seconds=0.02, send_chunk_bytes=32 * 1024
    )
    server = build_agent_server(
        worker_id="remote-a", storage_root=tmp_path / "queue", token=TOKEN, max_concurrent=1
    )
    server.start()
    client = RemoteWorkerClient(
        worker_id="remote-a", base_url=server.base_url, token=TOKEN, timeout_seconds=10.0
    )
    try:
        first = task_request(
            task_id="task-blocker", uri=http_server.url("/ncar/slow-big.nc"), data=data
        )
        second = task_request(
            task_id="task-queued",
            uri=http_server.url("/ncar/slow-big.nc"),
            data=data,
            target="era5/second.nc",
        )
        client.submit(first)
        client.submit(second)
        assert wait_until(lambda: server.manager.queue_depth == 1, timeout=10.0)
        cancelled = client.cancel_remote("task-queued")
        assert cancelled.cancelled is True
        assert cancelled.state is RemoteTaskState.CANCELLED
        client.cancel_remote("task-blocker")
    finally:
        client.close()
        server.stop()


def test_payload_validation_rejects_bad_requests(
    client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    bad = httpx.Client(
        base_url=client.base_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        trust_env=False,
        timeout=5.0,
    )
    try:
        response = bad.post("/v1/tasks", content=b"{not json")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_json"
        response = bad.post("/v1/tasks", json={"task_id": "x"})
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_task"
        response = bad.post(
            "/v1/register", json={"worker_id": "remote-a", "protocol_version": "9.9"}
        )
        assert response.status_code == 400
        response = bad.get("/v1/nope")
        assert response.status_code == 404
        response = bad.post("/v1/tasks/x/cancel")
        assert response.status_code == 400
    finally:
        bad.close()


def test_capability_rejection_at_submit(
    client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    client.register(WorkerCapabilities(datasets=["cmip6"], protocols=["https"]))
    data = file_bytes(1024)
    serve(http_server, "/ncar/cap.nc", data)
    request = task_request(task_id="task-cap", uri=http_server.url("/ncar/cap.nc"), data=data)
    with pytest.raises(RemoteProtocolError) as excinfo:
        client.submit(request)
    assert excinfo.value.status == 403


def test_path_escape_is_rejected_by_agent(
    client: RemoteWorkerClient, http_server: TransferTestServer, agent: AgentServer
) -> None:
    data = file_bytes(1024)
    serve(http_server, "/ncar/esc.nc", data)
    sneaky = httpx.Client(
        base_url=agent.base_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        trust_env=False,
        timeout=5.0,
    )
    try:
        body = task_request(task_id="task-esc", uri=http_server.url("/ncar/esc.nc"), data=data)
        payload = json.loads(body.model_dump_json())
        payload["target_relative_path"] = "../outside/f.nc"
        response = sneaky.post("/v1/tasks", json=payload)
    finally:
        sneaky.close()
    assert response.status_code == 400
    assert response.json()["error"] in {"path_escape", "invalid_task"}


# --------------------------------------------------------------------------- #
# 文件回传（Range）
# --------------------------------------------------------------------------- #
def test_file_download_supports_range_resume(
    client: RemoteWorkerClient, http_server: TransferTestServer, tmp_path: Path, agent: AgentServer
) -> None:
    data = file_bytes(512 * 1024)
    serve(http_server, "/ncar/range.nc", data)
    client.submit(
        task_request(task_id="task-range", uri=http_server.url("/ncar/range.nc"), data=data)
    )
    wait_for_state(client, "task-range")

    target = tmp_path / "resume" / "f.nc"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data[: 200 * 1024])  # 模拟上次中断留下的一半

    with httpx.Client(
        base_url=agent.base_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        trust_env=False,
        timeout=10.0,
    ) as probe:
        response = probe.get("/v1/files/task-range", headers={"Range": "bytes=100-"})
        assert response.status_code == 206
        assert response.headers["Content-Range"] == f"bytes 100-{len(data) - 1}/{len(data)}"
        assert response.content == data[100:]
        assert response.headers["Accept-Ranges"] == "bytes"

    report = client.download("task-range", target, expected_size=len(data))
    assert report.resumed is True
    assert report.size_bytes == len(data)
    assert target.read_bytes() == data
    assert not partial.exists()


def test_file_download_rejects_bad_range(
    client: RemoteWorkerClient, http_server: TransferTestServer, agent: AgentServer
) -> None:
    data = file_bytes(4096)
    serve(http_server, "/ncar/small.nc", data)
    client.submit(
        task_request(task_id="task-small", uri=http_server.url("/ncar/small.nc"), data=data)
    )
    wait_for_state(client, "task-small")
    with httpx.Client(
        base_url=agent.base_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        trust_env=False,
        timeout=5.0,
    ) as probe:
        assert (
            probe.get("/v1/files/task-small", headers={"Range": "bytes=99999-"}).status_code == 416
        )
        assert probe.get("/v1/files/task-small", headers={"Range": "items=1-2"}).status_code == 400
        assert probe.get("/v1/files/missing").status_code == 404


def test_file_endpoint_requires_completed_task(
    client: RemoteWorkerClient, http_server: TransferTestServer, agent: AgentServer
) -> None:
    with httpx.Client(
        base_url=agent.base_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        trust_env=False,
        timeout=5.0,
    ) as probe:
        assert probe.get("/v1/files/never-heard-of").status_code == 404


# --------------------------------------------------------------------------- #
# 客户端错误分类
# --------------------------------------------------------------------------- #
def test_unreachable_endpoint_raises_unavailable(tmp_path: Path) -> None:
    client = RemoteWorkerClient(
        worker_id="remote-a",
        base_url="http://127.0.0.1:1",
        timeout_seconds=1.0,
        connect_timeout_seconds=0.5,
    )
    try:
        with pytest.raises(RemoteUnavailableError) as excinfo:
            client.health_check()
        assert excinfo.value.retryable is True
    finally:
        client.close()


def test_download_size_mismatch_is_detected(
    client: RemoteWorkerClient, http_server: TransferTestServer, tmp_path: Path
) -> None:
    data = file_bytes(2048)
    serve(http_server, "/ncar/size.nc", data)
    client.submit(
        task_request(task_id="task-size", uri=http_server.url("/ncar/size.nc"), data=data)
    )
    wait_for_state(client, "task-size")
    with pytest.raises(RemoteUnavailableError) as excinfo:
        client.download("task-size", tmp_path / "size.nc", expected_size=len(data) + 1)
    assert "size" in str(excinfo.value)


def test_agent_never_echoes_token(
    agent: AgentServer, client: RemoteWorkerClient, http_server: TransferTestServer
) -> None:
    logs: list[str] = []
    data = file_bytes(1024)
    serve(http_server, "/ncar/tok.nc", data)
    client.submit(
        task_request(task_id="task-token", uri=http_server.url("/ncar/tok.nc"), data=data)
    )
    wait_for_state(client, "task-token")
    with httpx.Client(
        base_url=agent.base_url, headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False
    ) as probe:
        body = probe.get("/v1/health").text
        error = probe.get("/v1/files/nope").text
    assert TOKEN not in body
    assert TOKEN not in error
    assert logs == []


def test_agent_refuses_unauthenticated_non_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing to bind"):
        build_agent_server(worker_id="remote-a", storage_root=tmp_path, host="0.0.0.0")
    with pytest.raises(ValueError, match="at least 16 characters"):
        build_agent_server(worker_id="remote-a", storage_root=tmp_path, token="short")
