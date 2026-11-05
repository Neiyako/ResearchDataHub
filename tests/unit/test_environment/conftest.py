"""``tests/unit/test_environment`` 的公共夹具。

设计原则
--------
* **绝不联网**：网络检查一律注入 :class:`httpx.MockTransport` 或本地 ``http.server``；
  默认套件里的 provider 端点要求都是 ``https://``，MockTransport 会拦下所有真实请求。
* **绝不碰真实钥匙串**：``keyring_getter`` 永远是假函数。
* **绝不调用 cdsapi**：任何测试都不 import / 调用 cdsapi 的发请求方法。
* secret 一律用 sentinel 字符串，便于断言「不出现在报告 JSON 里」。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import httpx
import pytest
from sentinels import SECRET_SENTINEL, SECRET_SENTINEL_2  # noqa: F401 - 供其它测试间接使用


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """把所有 ``httpx.Client`` 的网络能力阉掉：任何真实请求都会失败。

    这样即便被测代码漏传 mock client，测试也只会在本地失败，绝不会连到公网。
    """
    real_client = httpx.Client

    def _guarded_client(*args: object, **kwargs: object) -> httpx.Client:
        transport = kwargs.get("transport")
        if transport is None:
            kwargs["transport"] = httpx.MockTransport(_reject)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", _guarded_client)


def _reject(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"test attempted a real network call to {request.url}")


@pytest.fixture
def mock_transport() -> httpx.MockTransport:
    """返回一个只对白名单 URL 应答 200 的 MockTransport；其余 URL 一律连不上。"""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in {"reachable.test", "cds.climate.copernicus.eu"}:
            return httpx.Response(200, headers={"content-type": "text/plain"})
        if host == "auth.test":
            return httpx.Response(401)
        raise httpx.ConnectError("unreachable in test", request=request)

    return httpx.MockTransport(handler)


@pytest.fixture
def local_http_server() -> Iterator[str]:
    """起一个本地 ``http.server``，返回其 base URL（测真实 socket 路径用）。

    这是**唯一**允许真实联网的测试通道，且只连 127.0.0.1。
    """

    class _Handler(BaseHTTPRequestHandler):
        status_by_path: ClassVar[dict[str, int]] = {"/ok": 200, "/boom": 503}

        def do_HEAD(self) -> None:
            self._respond(body=False)

        def do_GET(self) -> None:
            self._respond(body=True)

        def _respond(self, *, body: bool) -> None:
            status = self.status_by_path.get(self.path, 404)
            payload = b"ok\n"
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            """静默：测试输出里不需要访问日志。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
