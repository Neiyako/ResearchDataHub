"""端到端：真实子进程 Worker Agent + 真实 HTTP + 真实 SHA-256（全离线，默认套件可跑）。

这条测试验证的是**部署形态**而不是函数：
1. 用 ``python -m researchdatahub.network.workers.agent`` 起一个**独立进程**（随机端口，
   读它自己打印的 ``listening on http://...`` 行拿到地址）；
2. 主进程另起一个**本地 HTTP 文件服务器**提供 1–5 MiB 的真实文件；
3. 主进程的 ``FailoverCoordinator`` 通过真实 HTTP 注册该节点、下发下载任务、轮询状态，
   通过 ``GET /v1/files/{id}`` 把产物取回本机；
4. 对**落盘文件**重新计算 SHA-256 并与源文件比对。

不带 ``network`` marker（全离线），``pytest -q`` 默认就会跑。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.test_network.conftest import make_asset, make_task

from researchdatahub.network.routes.failover import FailoverCoordinator, FailoverExhausted
from researchdatahub.network.routes.manager import RouteManager
from researchdatahub.network.routes.stats import RouteStatsStore
from researchdatahub.network.workers import AGENT_MODULE
from researchdatahub.network.workers.client import RemoteWorkerClient
from researchdatahub.network.workers.registry import WorkerRegistry
from researchdatahub.network.workers.store import WorkerStore

TOKEN = "integration-token-abcdef123456"
_PYTHON = sys.executable
_SRC = Path(__file__).resolve().parents[2] / "src"


class LocalFileServer:
    """最小 HTTP 文件服务器（真实 socket，真实字节流，支持 Range）。

    用 ``ThreadingHTTPServer`` 而不是 ``http.server.SimpleHTTPRequestHandler``：后者会把
    目录/路径映射到真实文件系统，测试不该依赖工作目录里的文件。
    """

    def __init__(self, body: bytes) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.body = body
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                outer.requests.append((self.path, self.headers.get("Range")))
                if self.path != "/payload.bin":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                total = len(outer.body)
                range_header = self.headers.get("Range")
                start = 0
                status = 200
                if range_header and range_header.startswith("bytes="):
                    start = int(range_header[len("bytes=") :].split("-", 1)[0] or "0")
                    status = 206
                payload = outer.body[start:]
                # 明确声明 close：客户端中途取消（故障转移）时不会留下半个 keep-alive
                # 连接被复用，也就不会在本端触发 "Connection reset by peer" 噪声。
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Connection", "close")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端在传输中途被取消时连接会被关闭：这是**被测行为**，不是失败。
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.handle_error = lambda *_args: None  # type: ignore[method-assign]
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def url(self, path: str = "/payload.bin") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class AgentProcess:
    """以**独立子进程**方式运行的 worker agent（可观测启动行与退出码）。"""

    def __init__(self, *, worker_id: str, storage_root: Path, token: str) -> None:
        self.worker_id = worker_id
        self.storage_root = storage_root
        self.token = token
        self.stdout: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self.base_url: str | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> str:
        """启动子进程并等待它打印真实监听地址。"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["RDH_WORKER_TOKEN"] = self.token
        self.process = subprocess.Popen(
            [
                _PYTHON,
                "-m",
                AGENT_MODULE,
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--worker-id",
                self.worker_id,
                "--storage-root",
                str(self.storage_root),
                "--max-concurrent",
                "2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert self.process.stdout is not None
        line = self.process.stdout.readline().strip()
        self.stdout.append(line)
        match = re.search(r"listening on (http://127\.0\.0\.1:\d+)", line)
        assert match is not None, f"agent did not announce its address: {line!r}"
        self.base_url = match.group(1)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        return self.base_url

    def _drain(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout.append(line.strip())

    def stop(self) -> int | None:
        """终止子进程并返回退出码（``None`` 表示已提前终止）。"""
        if self.process is None:
            return None
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 仅在 agent 卡死时发生
            self.process.kill()
            self.process.wait(timeout=5)
        return self.process.returncode


def build_payload(size: int = 3 * 1024 * 1024) -> bytes:
    """确定性的 3 MiB 负载（覆盖多 chunk/多 TCP 段）。"""
    block = hashlib.sha256(b"researchdatahub-integration").digest()
    return (block * (size // len(block) + 1))[:size]


@pytest.fixture
def file_server() -> Iterator[LocalFileServer]:
    """提供真实文件的本地 HTTP 服务器。"""
    server = LocalFileServer(build_payload())
    server.start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def agent_process(tmp_path: Path) -> Iterator[AgentProcess]:
    """真实子进程 agent（随机端口）。"""
    process = AgentProcess(
        worker_id="remote-int", storage_root=tmp_path / "agent-root", token=TOKEN
    )
    base_url = process.start()
    print(f"[integration] agent subprocess listening at {base_url}")
    try:
        yield process
    finally:
        code = process.stop()
        print(f"[integration] agent subprocess exit code: {code}")


def test_remote_agent_subprocess_end_to_end(
    tmp_path: Path, file_server: LocalFileServer, agent_process: AgentProcess
) -> None:
    """真实子进程 → 注册 → 下发 → 下载 → 回传 → SHA-256 一致。"""
    assert agent_process.base_url is not None
    data = file_server.body
    expected_sha = hashlib.sha256(data).hexdigest()

    registry = WorkerRegistry(WorkerStore(tmp_path / "workers.db"))
    stats = RouteStatsStore(registry.store)
    routes = RouteManager(registry=registry, stats=stats)
    client = RemoteWorkerClient(
        worker_id=agent_process.worker_id,
        base_url=agent_process.base_url,
        token=TOKEN,
        artifact_root=tmp_path / "delivered",
        timeout_seconds=15.0,
        fetch_timeout_seconds=60.0,
    )
    coordinator = FailoverCoordinator(
        registry=registry,
        routes=routes,
        workers={},
        poll_interval_s=0.05,
        remote_task_timeout_s=120.0,
    )
    try:
        record = coordinator.ensure_remote_worker(
            worker_id=agent_process.worker_id,
            endpoint=agent_process.base_url,
            token=TOKEN,
        )
        assert record.health.status.value == "healthy"
        assert record.kind.value == "remote"
        assert record.capabilities.protocols  # 来自真实 /v1/health

        asset = make_asset(uri=file_server.url("/payload.bin"), data=data, file_name="payload.bin")
        task = make_task(asset, task_id="task-int-remote")
        delivery = coordinator.run(task, provider_id="ncar")

        assert delivery.route.worker_id == agent_process.worker_id
        assert delivery.route.protocol == "https"
        assert delivery.file.size_bytes == len(data)
        disk = delivery.file.path
        assert disk.is_file()
        measured = hashlib.sha256(disk.read_bytes()).hexdigest()
        assert measured == expected_sha
        assert delivery.file.sha256 == expected_sha
        print(
            f"[integration] delivered {delivery.file.size_bytes} bytes via "
            f"{delivery.route.route_id} in {delivery.attempts[0].duration_s:.3f}s; "
            f"sha256={measured}"
        )

        # 链路性能记录来自这次真实传输
        measured_stats = routes.stats_for(delivery.route.route_id, provider_id="ncar")
        assert measured_stats.samples == 1
        assert measured_stats.successes == 1
        assert measured_stats.throughput_bps is not None
        assert measured_stats.throughput_bps > 0

        # 文件真的来自子进程的存储根（而不是同一条本地路径的假象）
        agent_root = agent_process.storage_root
        produced = list(agent_root.rglob("payload.bin"))
        assert produced
        assert produced[0].is_file()
        assert produced[0].resolve() != disk.resolve()
        assert file_server.requests, "the agent never fetched the payload over HTTP"
    finally:
        client.close()


def test_remote_agent_subprocess_failover(
    tmp_path: Path, file_server: LocalFileServer, agent_process: AgentProcess
) -> None:
    """子进程首要失败（坏 URL）→ 记录失败样本 → 仍在该节点上重试可用链路。"""
    assert agent_process.base_url is not None
    data = file_server.body
    registry = WorkerRegistry(WorkerStore(tmp_path / "workers.db"))
    routes = RouteManager(registry=registry, stats=RouteStatsStore(registry.store))
    coordinator = FailoverCoordinator(
        registry=registry,
        routes=routes,
        workers={},
        poll_interval_s=0.05,
        remote_task_timeout_s=60.0,
    )
    try:
        coordinator.ensure_remote_worker(
            worker_id=agent_process.worker_id,
            endpoint=agent_process.base_url,
            token=TOKEN,
        )
        broken = make_asset(uri=file_server.url("/absent.bin"), data=data, file_name="absent.bin")
        with pytest.raises(FailoverExhausted) as excinfo:
            coordinator.run(make_task(broken, task_id="task-int-broken"), provider_id="ncar")
        payload = excinfo.value.as_dict()
        assert payload["attempts"]
        failed_stats = routes.stats_for("remote-int:ncar:https", provider_id="ncar")
        assert failed_stats.failures >= 1

        ok_asset = make_asset(
            uri=file_server.url("/payload.bin"), data=data, file_name="payload.bin"
        )
        delivery = coordinator.run(
            make_task(ok_asset, task_id="task-int-recover"), provider_id="ncar"
        )
        assert delivery.file.sha256 == hashlib.sha256(data).hexdigest()
        stats = routes.stats_for("remote-int:ncar:https", provider_id="ncar")
        assert stats.samples >= 2
        assert stats.successes >= 1
    finally:
        coordinator.close()
