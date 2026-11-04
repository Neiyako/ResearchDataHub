"""真实 HTTP 测试服务器（不是 mock：真 socket、真 HTTP 语义、真中断）。

为什么不用 ``httpx.MockTransport``
----------------------------------
``MockTransport`` 只替换了客户端，不经过 TCP，也不产生「连接被对端关闭」「Content-Length
与实际字节不符」「Range 被服务端忽略」这类真实协议行为。传输层的 bug 恰恰都长在这些
地方，所以这里起一个 ``ThreadingHTTPServer``（``127.0.0.1:0`` 随机端口），用可编程的
:class:`Resource` 构造各种真实场景：

* ``status_plan``：按顺序返回 429/500 等错误状态码；
* ``truncate_after``：只发送一部分 body 就关闭连接（一次性的，用于测中断）；
* ``slow_chunk_seconds``：每块之间 sleep（用于读超时/总超时）；
* ``ignore_range``：无视 Range 一律回 200（测「服务端不配合续传」）；
* ``accepts_ranges=False``：HEAD 不带 ``Accept-Ranges``（测续传前置条件）；
* ``bad_content_range_start``：206 的 Content-Range 起点与请求不符（测严格校验）。

服务器同时记录每个请求的方法/路径/Range 头，以及观测到的最大并发请求数，供测试断言
「客户端真的发了 ``Range: bytes=50000-``」「并发确实被限制在 2」。
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

__all__ = ["RequestRecord", "Resource", "TransferTestServer"]


@dataclass(slots=True)
class RequestRecord:
    """一次被服务器完整接收的请求。"""

    method: str
    path: str
    range_header: str | None


@dataclass
class Resource:
    """一个可编程的远端对象。

    Parameters
    ----------
    body:
        对象的真实字节内容。
    etag:
        ``ETag`` 头（``None`` 表示不提供）。
    last_modified:
        ``Last-Modified`` 头的原始 HTTP 日期字符串。
    content_type:
        ``Content-Type`` 头。
    accepts_ranges:
        是否在响应里声明 ``Accept-Ranges: bytes`` 并真正处理 ``Range``。
    head_supported:
        ``False`` 时 HEAD 返回 405（用于测 HEAD 兜底）。
    status_plan:
        依次消费的错误状态码（例如 ``[500, 429]``）；用完后恢复正常响应。
    truncate_after:
        只发送这么多字节就关闭连接（**一次性**，测中断/续传）。
    slow_chunk_seconds:
        发送每个 chunk 前的 sleep 秒数（测读超时/总超时）。
    send_chunk_bytes:
        服务端每次 ``write`` 的块大小。
    ignore_range:
        收到 Range 仍回 200 全量（测「忽略 Range」）。
    bad_content_range_start:
        返回 206 时故意把 Content-Range 起点写成这个值（测严格校验）。
    """

    body: bytes
    etag: str | None = None
    last_modified: str | None = None
    content_type: str = "application/octet-stream"
    accepts_ranges: bool = True
    head_supported: bool = True
    status_plan: list[int] = field(default_factory=list)
    head_status_plan: list[int] = field(default_factory=list)
    truncate_after: int | None = None
    slow_chunk_seconds: float = 0.0
    send_chunk_bytes: int = 65536
    ignore_range: bool = False
    bad_content_range_start: int | None = None
    retry_after: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    get_requests: list[str | None] = field(default_factory=list)
    head_requests: int = 0

    def pop_status(self) -> int | None:
        """取出下一个预设的 GET 错误状态码（线程安全）。"""
        with self._lock:
            if not self.status_plan:
                return None
            return self.status_plan.pop(0)

    def pop_head_status(self) -> int | None:
        """取出下一个预设的 HEAD 错误状态码（线程安全）。"""
        with self._lock:
            if not self.head_status_plan:
                return None
            return self.head_status_plan.pop(0)

    def pop_truncate(self) -> int | None:
        """取出一次性截断阈值（线程安全）。"""
        with self._lock:
            value = self.truncate_after
            self.truncate_after = None
            return value

    def record_get(self, range_header: str | None) -> None:
        with self._lock:
            self.get_requests.append(range_header)

    def record_head(self) -> None:
        with self._lock:
            self.head_requests += 1

    def build_response(self, range_header: str | None) -> tuple[int, bytes, dict[str, str]]:
        """根据 Range 头返回 ``(status, payload, extra_headers)``。"""
        total = len(self.body)
        if range_header is None or not self.accepts_ranges or self.ignore_range:
            return 200, self.body, {}
        requested = _parse_range_header(range_header, total)
        if requested is None:
            return 416, b"", {"Content-Range": f"bytes */{total}"}
        start, end = requested
        if self.ignore_range:
            return 200, self.body, {}
        reported_start = (
            self.bad_content_range_start if self.bad_content_range_start is not None else start
        )
        return (
            206,
            self.body[start : end + 1],
            {
                "Content-Range": f"bytes {reported_start}-{end}/{total}",
            },
        )

    def base_headers(self) -> dict[str, str]:
        headers = {"Content-Type": self.content_type}
        if self.etag is not None:
            headers["ETag"] = self.etag
        if self.last_modified is not None:
            headers["Last-Modified"] = self.last_modified
        return headers

    def range_headers(self) -> dict[str, str]:
        if not self.accepts_ranges:
            return {}
        return {"Accept-Ranges": "bytes"}


def _parse_range_header(value: str, total: int) -> tuple[int, int] | None:
    text = value.strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[len("bytes=") :].strip()
    if "," in spec or "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            suffix = int(end_text)
            if suffix <= 0 or suffix > total:
                return None
            return total - suffix, total - 1
        start = int(start_text)
        end = total - 1 if end_text == "" else int(end_text)
    except ValueError:
        return None
    if start < 0 or start >= total or end < start:
        return None
    return start, min(end, total - 1)


class _Handler(BaseHTTPRequestHandler):
    """把请求转交给 :class:`TransferTestServer`（逻辑集中在 server 上便于测试）。"""

    protocol_version = "HTTP/1.1"
    server_version = "RdhTransferTest/1.0"

    def log_message(self, format: str, *args: object) -> None:
        """吞掉访问日志（测试输出保持干净，不是业务异常）。"""

    def do_HEAD(self) -> None:
        """处理 HEAD。"""
        self._owner().handle(self, head=True)

    def do_GET(self) -> None:
        """处理 GET。"""
        self._owner().handle(self, head=False)

    def _owner(self) -> TransferTestServer:
        server = self.server
        assert isinstance(server, _TestHTTPServer)
        return server.owner


class _TestHTTPServer(ThreadingHTTPServer):
    """带 owner 引用的 ``ThreadingHTTPServer``（daemon 线程避免测试卡住）。

    被测试的传输会**故意**在响应中途断开连接（取消/中断用例），服务端线程因此会看到
    ``BrokenPipeError``/``ConnectionResetError``。这是预期行为，不应打印 traceback 污染
    测试输出；其它异常仍交给 ``socketserver`` 默认处理（不掩盖真实缺陷）。
    """

    daemon_threads = True
    owner: TransferTestServer

    def handle_error(self, request: object, client_address: object) -> None:
        """只吞掉“客户端主动断开”，其余异常照常输出。"""
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
        ):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


class TransferTestServer:
    """可编程的真实 HTTP 服务器；上下文管理器可用，``close()`` 释放端口与线程。"""

    def __init__(self) -> None:
        self._resources: dict[str, Resource] = {}
        self._requests: list[RequestRecord] = []
        self._lock = threading.Lock()
        self._active = 0
        self._max_active = 0
        self._httpd = _TestHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="rdh-test-server", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """监听端口（``127.0.0.1``）。"""
        return int(self._httpd.server_address[1])

    def url(self, path: str) -> str:
        """构造 ``http://127.0.0.1:<port><path>``。"""
        if not path.startswith("/"):
            path = "/" + path
        return f"http://127.0.0.1:{self.port}{path}"

    def add(self, path: str, resource: Resource) -> Resource:
        """注册一个路径 → 资源映射。"""
        self._resources[path] = resource
        return resource

    def remove(self, path: str) -> Resource:
        """移除路径映射（模拟资源消失）。"""
        return self._resources.pop(path)

    def requests_for(self, path: str) -> list[RequestRecord]:
        """该路径收到的请求记录（按到达顺序）。"""
        with self._lock:
            return [record for record in self._requests if record.path == path]

    @property
    def max_concurrent(self) -> int:
        """观测到的最大的同时在处理请求数。"""
        with self._lock:
            return self._max_active

    def reset_stats(self) -> None:
        """清空请求与并发统计（资源内容保留）。"""
        with self._lock:
            self._requests.clear()
            self._active = 0
            self._max_active = 0

    # -- 请求处理 -------------------------------------------------------- #
    def handle(self, handler: BaseHTTPRequestHandler, *, head: bool) -> None:
        """处理一个请求：配额、状态码计划、Range、限速、截断。"""
        parsed = urlsplit(handler.path)
        path = parsed.path
        range_header = handler.headers.get("Range")
        with self._lock:
            self._requests.append(
                RequestRecord(
                    method="HEAD" if head else "GET", path=path, range_header=range_header
                )
            )
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            resource = self._resources.get(path)
            if resource is None:
                self._respond(handler, 404, b"not found", {}, content_length=9)
                return
            if head:
                resource.record_head()
                if not resource.head_supported:
                    self._respond(handler, 405, b"", {"Allow": "GET"}, content_length=0)
                    return
                head_status = resource.pop_head_status()
                if head_status is not None:
                    self._respond(
                        handler,
                        head_status,
                        b"",
                        self._error_headers(resource, head_status),
                        content_length=0,
                    )
                    return
                headers = resource.base_headers()
                headers.update(resource.range_headers())
                self._respond(handler, 200, b"", headers, content_length=len(resource.body))
                return
            resource.record_get(range_header)
            status = resource.pop_status()
            if status is not None:
                body = f"server error {status}".encode()
                self._respond(
                    handler,
                    status,
                    body,
                    self._error_headers(resource, status),
                    content_length=len(body),
                )
                return
            status, payload, extra = resource.build_response(range_header)
            headers = resource.base_headers()
            headers.update(resource.range_headers())
            for name, value in extra.items():
                headers[name] = value
            self._respond(
                handler,
                status,
                payload,
                headers,
                content_length=len(payload),
                resource=resource,
            )
        finally:
            with self._lock:
                self._active -= 1

    def _error_headers(self, resource: Resource, status: int) -> dict[str, str]:
        headers = {"Content-Type": "text/plain"}
        if status == 429 and resource.retry_after is not None:
            headers["Retry-After"] = resource.retry_after
        return headers

    def _respond(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        headers: dict[str, str],
        *,
        content_length: int,
        resource: Resource | None = None,
    ) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Length", str(content_length))
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
            return
        if not body:
            return
        truncate_after = resource.pop_truncate() if resource is not None else None
        chunk_size = resource.send_chunk_bytes if resource is not None else 65536
        slow = resource.slow_chunk_seconds if resource is not None else 0.0
        written = 0
        try:
            for offset in range(0, len(body), chunk_size):
                piece = body[offset : offset + chunk_size]
                if truncate_after is not None and written + len(piece) > truncate_after:
                    piece = piece[: truncate_after - written]
                if slow > 0:
                    time.sleep(slow)
                if piece:
                    handler.wfile.write(piece)
                    handler.wfile.flush()
                    written += len(piece)
                if truncate_after is not None and written >= truncate_after:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            handler.close_connection = True
            return
        if truncate_after is not None and written < len(body):
            # 声明了完整 Content-Length 却提前关闭：客户端必须看到“传输不完整”。
            handler.close_connection = True
            with contextlib.suppress(OSError):
                handler.connection.close()

    # -- 生命周期 -------------------------------------------------------- #
    def close(self) -> None:
        """停止服务并释放端口（幂等）。"""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> TransferTestServer:
        """上下文管理器入口。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """上下文管理器出口：关闭服务器。"""
        self.close()
