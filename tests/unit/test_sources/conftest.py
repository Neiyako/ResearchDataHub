"""M3 数据源测试的本地基础设施。

原则：**不 mock socket 语义**。连接失败、超时、5xx 都用真实本地 HTTP 服务器
（``ThreadingHTTPServer`` 监听 127.0.0.1 随机端口）复现；服务器停止后客户端看到的是
真实的 connection refused / 超时，而不是被替换掉的对象。

fixtures/ 下的 JSON 是 2026-11-04 对**真实远端 API** 的响应快照（CDS 与 GDEX），
用于让解析逻辑在离线单测里面对真实形状，而不是手写"想当然"的字段。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from researchdatahub.config.models import SourceConfig, SourcesConfig, TransportBinding

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """读取真实远端响应快照。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 真实本地 HTTP 服务器
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CapturedRequest:
    """服务器收到的一次请求（用于断言客户端行为与泄漏）。"""

    method: str
    path: str
    query: str
    headers: Mapping[str, str]
    body: bytes

    def json_body(self) -> Any:
        """请求体 JSON（无请求体返回 ``None``）。"""
        return json.loads(self.body) if self.body else None


@dataclass
class Response:
    """一次响应定义；``delay_seconds``/``drop_connection`` 用于真实的超时/断连测试。"""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    drop_connection: bool = False

    @classmethod
    def json(
        cls,
        payload: Any,
        *,
        status: int = 200,
        delay_seconds: float = 0.0,
    ) -> Response:
        """构造 JSON 响应。"""
        return cls(
            status=status,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            delay_seconds=delay_seconds,
        )


RouteHandler = Callable[[CapturedRequest], Response]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ResearchDataHubTest/1.0"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        split = urlsplit(self.path)
        captured = CapturedRequest(
            method=method,
            path=split.path,
            query=split.query,
            headers=dict(self.headers.items()),
            body=body,
        )
        server: LocalHttpServer = self.server.rdh_test_server  # type: ignore[attr-defined]
        server.record(captured)
        route = server.resolve(method, captured.path)
        if route is None:
            response = Response.json({"detail": "not found"}, status=404)
        else:
            response = route(captured)
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
        if response.drop_connection:
            self.close_connection = True
            return
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response.body)
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        """禁掉 stdout 访问日志（测试输出保持干净）。"""


class LocalHttpServer:
    """可注册路由的真实本地 HTTP 服务器（随机端口，线程内运行）。"""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], RouteHandler] = {}
        self._requests: list[CapturedRequest] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.rdh_test_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """服务器根 URL（``http://127.0.0.1:<port>``）。"""
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        """监听端口。"""
        return int(self._httpd.server_address[1])

    def route(self, method: str, path: str, handler: RouteHandler) -> None:
        """注册动态路由。"""
        with self._lock:
            self._routes[(method.upper(), path)] = handler

    def add(self, method: str, path: str, response: Response) -> None:
        """注册固定响应。"""
        self.route(method, path, lambda _request, _response=response: _response)

    def add_json(self, method: str, path: str, payload: Any, *, status: int = 200) -> None:
        """注册固定 JSON 响应。"""
        self.add(method, path, Response.json(payload, status=status))

    def resolve(self, method: str, path: str) -> RouteHandler | None:
        """查找路由（测试用；无匹配返回 ``None``）。"""
        with self._lock:
            return self._routes.get((method.upper(), path))

    def record(self, request: CapturedRequest) -> None:
        """记录请求。"""
        with self._lock:
            self._requests.append(request)

    @property
    def requests(self) -> tuple[CapturedRequest, ...]:
        """已收到的请求快照。"""
        with self._lock:
            return tuple(self._requests)

    def requests_to(self, path: str) -> tuple[CapturedRequest, ...]:
        """某路径上的请求。"""
        return tuple(item for item in self.requests if item.path == path)

    def stop(self) -> None:
        """停止服务器（之后的连接是真实 connection refused）。"""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> LocalHttpServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


@pytest.fixture
def http_server() -> Iterator[LocalHttpServer]:
    """真实本地 HTTP 服务器（退出时停止）。"""
    server = LocalHttpServer()
    try:
        yield server
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# 固定时钟（生命周期轮询测试用，不依赖真实时间）
# --------------------------------------------------------------------------- #
class StepClock:
    """每次调用前进固定秒数的时钟。"""

    def __init__(self, *, start: datetime | None = None, step_seconds: float = 1.0) -> None:
        self._now = start or datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


# --------------------------------------------------------------------------- #
# 配置构造器（与 configs/sources.example.yaml 的形态一致）
# --------------------------------------------------------------------------- #
def make_source_config(
    *,
    adapter: str = "rda",
    provider: str = "NSF NCAR RDA",
    display_name: str = "NCAR Research Data Archive",
    datasets: tuple[str, ...] = ("era5-pressure-levels",),
    endpoint: str | None = None,
    credentials: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
    implemented: bool = True,
    enabled: bool = True,
) -> SourceConfig:
    """构造一个数据源配置；传输绑定按适配器类型取合理的 route。"""
    if adapter == "cds":
        transports = [
            TransportBinding(
                route="cds_api",
                adapter="cds_api",
                implemented=True,
                options={"cds_api.request_timeout_seconds": 5},
            )
        ]
    else:
        transports = [TransportBinding(route="https", adapter="https", implemented=True)]
    return SourceConfig.model_validate(
        {
            "provider": provider,
            "display_name": display_name,
            "adapter": adapter,
            "implemented": implemented,
            "enabled": enabled,
            "datasets": list(datasets),
            "transports": [item.model_dump(mode="json") for item in transports],
            "endpoint": endpoint,
            "credentials": dict(credentials or {}),
            "extensions": dict(extensions or {}),
        }
    )


def make_sources_config(items: Mapping[str, SourceConfig]) -> SourcesConfig:
    """构造 ``SourcesConfig``。"""
    return SourcesConfig.model_validate(
        {"sources": {source_id: item.model_dump(mode="json") for source_id, item in items.items()}}
    )
