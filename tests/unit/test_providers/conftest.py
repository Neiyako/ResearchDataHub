"""Provider 测试的本地基础设施：真实本地 HTTP 服务器 + 真实响应样本回放。

原则（与 ``tests/unit/test_sources/conftest.py`` 一致，但**独立实现**，不跨包 import）
----------------------------------------------------------------------------------------
* **不 mock socket 语义**：用 ``ThreadingHTTPServer`` 监听 ``127.0.0.1:0``，Provider 发出的
  是真实 HTTP 请求，服务器返回的是 **2026-11-05 用 ``curl`` 抓到的真实响应**（见
  ``fixtures/`` 与 ``docs/providers/*.md`` 的实证记录）。测试校验的是"真实形状 → 解析结果"，
  而不是"我按想象写的形状 → 我的框架"。
* 服务器可以给任意状态码/延迟/断连，用于覆盖 404/400/401/429/5xx/超时等真实错误路径。
* **不发外网**：所有路由都在本地注册；未注册路径返回 404（Provider 会映射成
  :class:`~researchdatahub.core.errors.SourceNotFoundError`）。

样本清单（全部来自真实响应）
----------------------------
CMR（NASA Earthdata）::

    cmr_collections_keyword_precipitation.json   /search/collections.json（多参数组合）
    cmr_umm_collection_gpm_2adpr.json            /search/collections.umm_json（concept_id）
    cmr_granules_gpm_2adpr.json                  /search/granules.json（concept_id + page_size）
    cmr_variables_gpm_2adpr.json                 /search/variables.umm_json（concept_id）
    cmr_collections_empty.json                   /search/collections.json（不存在的 concept_id）
    cmr_error_400_page_size.json                 /search/collections.json（page_size=notanumber）

CDSE（Copernicus Data Space）::

    cdse_stac_collections.json                   GET  /stac/collections
    cdse_stac_collection_sentinel2_l2a.json      GET  /stac/collections/sentinel-2-l2a
    cdse_stac_search_sentinel2.json              POST /stac/search
    cdse_odata_products_sentinel2.json           GET  /odata/v1/Products（$top/$expand/$filter）
    cdse_odata_attributes_sentinel2.json         GET  /odata/v1/Attributes
    cdse_stac_error_404_collection.json          GET  /stac/collections/not-a-real-collection-xyz
    cdse_token_error_401_invalid_client.json     POST /identity/.../token（无效凭证）
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

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)

# 真实响应里的稳定标识（来自 2026-11-05 的 curl 记录）
CMR_COLLECTION_ID = "C2179081499-GES_DISC"
CMR_GRANULE_ID = "G2187436353-GES_DISC"
CMR_VARIABLE_ID = "V2758121925-GES_DISC"
CMR_PROVIDER = "GES_DISC"
CDSE_S2_COLLECTION = "sentinel-2-l2a"


def load_fixture(name: str) -> Any:
    """读取真实远端响应样本（``fixtures/<name>``）。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 真实本地 HTTP 服务器
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CapturedRequest:
    """服务器收到的一次请求（供断言客户端行为：参数、方法、是否带鉴权头）。"""

    method: str
    path: str
    query: str
    headers: Mapping[str, str]
    body: bytes

    def json_body(self) -> Any:
        """请求体 JSON（无请求体返回 ``None``）。"""
        return json.loads(self.body) if self.body else None

    @property
    def authorization(self) -> str | None:
        """``Authorization`` 头原文（用于断言"匿名请求不带 token"）。"""
        for key, value in self.headers.items():
            if key.lower() == "authorization":
                return value
        return None


@dataclass
class Response:
    """一次响应定义；``delay_seconds`` 用于真实超时测试。"""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0

    @classmethod
    def json(cls, payload: Any, *, status: int = 200, delay_seconds: float = 0.0) -> Response:
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
    server_version = "ResearchDataHubProviderTest/1.0"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

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
        server: LocalHttpServer = self.server.rdh_provider_server  # type: ignore[attr-defined]
        server.record(captured)
        handler = server.resolve(method, captured.path)
        response = handler(captured) if handler is not None else Response.json({}, status=404)
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
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
    """可注册路由的真实本地 HTTP 服务器（随机端口，后台线程）。"""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], RouteHandler] = {}
        self._requests: list[CapturedRequest] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.rdh_provider_server = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """服务器根 URL（``http://127.0.0.1:<port>``）。"""
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

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
        """查找路由；无匹配返回 ``None``（服务器返回 404）。"""
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
        """某路径上的全部请求。"""
        return tuple(item for item in self.requests if item.path == path)

    def stop(self) -> None:
        """停止服务器。"""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> LocalHttpServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


@pytest.fixture
def httpserver() -> Iterator[LocalHttpServer]:
    """真实本地 HTTP 服务器（退出时停止）。"""
    server = LocalHttpServer()
    try:
        yield server
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# 固定时钟
# --------------------------------------------------------------------------- #
class StepClock:
    """每次调用前进固定秒数（避免依赖真实时间）。"""

    def __init__(self, *, start: datetime | None = None, step_seconds: float = 1.0) -> None:
        self._now = start or FIXED_NOW
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


def make_client(base_url: str, *, timeout: float = 5.0) -> httpx.Client:
    """构造指向本地服务器的 httpx 客户端（跟随重定向、短超时）。"""
    return httpx.Client(base_url=base_url, timeout=httpx.Timeout(timeout), follow_redirects=True)


# --------------------------------------------------------------------------- #
# CMR（Earthdata）路由安装
# --------------------------------------------------------------------------- #
CMR_SEARCH = "/search"


def install_cmr_routes(server: LocalHttpServer) -> None:
    """注册真实 CMR 响应形状的路由（样本全部来自 2026-11-05 的 curl）。"""
    server.add_json(
        "GET",
        f"{CMR_SEARCH}/collections.json",
        load_fixture("cmr_collections_keyword_precipitation.json"),
    )
    server.add_json(
        "GET",
        f"{CMR_SEARCH}/collections.umm_json",
        load_fixture("cmr_umm_collection_gpm_2adpr.json"),
    )
    server.add_json(
        "GET",
        f"{CMR_SEARCH}/granules.json",
        load_fixture("cmr_granules_gpm_2adpr.json"),
    )
    server.add_json(
        "GET",
        f"{CMR_SEARCH}/variables.umm_json",
        load_fixture("cmr_variables_gpm_2adpr.json"),
    )


# --------------------------------------------------------------------------- #
# CDSE 路由安装
# --------------------------------------------------------------------------- #
def install_cdse_routes(server: LocalHttpServer) -> None:
    """注册真实 CDSE 响应形状的路由（样本全部来自 2026-11-05 的 curl）。"""
    server.add_json("GET", "/stac/collections", load_fixture("cdse_stac_collections.json"))
    server.add_json(
        "GET",
        f"/stac/collections/{CDSE_S2_COLLECTION}",
        load_fixture("cdse_stac_collection_sentinel2_l2a.json"),
    )
    server.route(
        "GET",
        "/stac/collections/not-a-real-collection-xyz",
        lambda _request: Response.json(
            load_fixture("cdse_stac_error_404_collection.json"), status=404
        ),
    )
    server.route(
        "POST",
        "/stac/search",
        lambda _request: Response.json(load_fixture("cdse_stac_search_sentinel2.json")),
    )
    server.add_json("GET", "/odata/v1/Products", load_fixture("cdse_odata_products_sentinel2.json"))
    server.add_json(
        "GET", "/odata/v1/Attributes", load_fixture("cdse_odata_attributes_sentinel2.json")
    )
    server.route(
        "POST",
        "/identity/token",
        lambda _request: Response.json(
            load_fixture("cdse_token_error_401_invalid_client.json"), status=401
        ),
    )
