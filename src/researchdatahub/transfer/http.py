"""HTTP/HTTPS 传输适配器（``httpx`` 流式下载）。

实现要点
--------
* 流式：``client.stream(...)`` / ``response.iter_bytes(chunk_size)``，任何时刻内存里
  只有一个 chunk，8GB 设备上也能下大文件。
* 超时三层：connect / read 由 ``httpx.Timeout`` 负责；**总超时**由基类在每个 chunk
  边界检查单调时钟（httpx 没有“整个响应总时长”的概念）。
* 断点续传：只在基类的三重自证全部通过后才发 ``Range``；即使发了，也必须收到
  ``206`` 且 ``Content-Range`` 的起点等于请求偏移。服务端回 ``200``（忽略 Range）或
  ``416`` 时**不拼接**，丢弃 ``.part`` 整文件重下——宁可多下一次，也不能交付
  “前半段来自旧版本、后半段来自新版本”的文件。
* ETag / Last-Modified 原样保留在 sidecar 中；比较规则见基类 ``decide_resume``。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, NoReturn
from urllib.parse import urlsplit

import httpx

from researchdatahub.config.models import TransferConfig
from researchdatahub.core.errors import (
    PermanentTransportError,
    RetryableTransportError,
    TransportError,
)
from researchdatahub.core.models import SourceAsset
from researchdatahub.transfer.base import (
    ProgressUpdate,
    RemoteMetadata,
    RemoteStream,
    TransportAdapterBase,
)
from researchdatahub.transfer.errors import TransferErrorCode
from researchdatahub.transfer.events import TransferEventSink
from researchdatahub.transfer.retry import (
    HttpStatusClass,
    RetryPolicy,
    classify_http_status,
    condition_for_http_status,
)

__all__ = ["HttpTransportAdapter"]

_HEAD_NOT_ALLOWED = frozenset({405, 501})
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 501, 502, 503, 504, 507, 509})
_DEFAULT_MAX_CONNECTIONS = 8


def _parse_http_date(value: str | None) -> datetime | None:
    """解析 ``Last-Modified`` 等 HTTP 日期；无法解析时返回 ``None``（当作没有校验器）。"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After``（秒数或 HTTP 日期）；无法解析返回 ``None``。"""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _parse_content_range(value: str | None) -> tuple[int, int | None] | None:
    """解析 ``Content-Range: bytes start-end/total``；格式不符返回 ``None``。

    返回 ``(start, total)``；``total`` 为 ``None`` 表示服务端给了 ``*``（无法校验总大小）。
    """
    if not value:
        return None
    text = value.strip()
    if not text.lower().startswith("bytes "):
        return None
    remainder = text[len("bytes ") :].strip()
    if "/" not in remainder:
        return None
    span, total_text = remainder.split("/", 1)
    if "-" not in span:
        return None
    start_text, end_text = span.split("-", 1)
    try:
        start = int(start_text.strip())
        end = int(end_text.strip())
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    total: int | None
    if total_text.strip() == "*":
        total = None
    else:
        try:
            total = int(total_text.strip())
        except ValueError:
            return None
        if total < 0:
            return None
    return start, total


class HttpTransportAdapter(TransportAdapterBase):
    """``http`` / ``https`` 传输实现（``httpx.Client``，流式）。

    ``trust_env``
    -------------
    默认 ``True``：尊重系统/环境代理（企业网络的常见部署方式，代理往往也是唯一出口）。
    代价是行为不完全可预测——代理可能改写响应头、把连接错误包成 502、或缓存内容；
    因此：
    * 传输层只把代理当作透明通道，所有大小/ETag/Range 校验仍然针对**响应字节**做，
      不信任任何中间层声明；
    * 测试与需要严格可复现的生产环境应显式传 ``trust_env=False`` 直连；
    * 若将来需要“显式代理配置”，应新增配置项而不是依赖环境变量（隐藏全局状态）。
    """

    TRANSPORT_ID = "https"

    def __init__(
        self,
        *,
        config: TransferConfig | None = None,
        retry: RetryPolicy | None = None,
        event_sink: TransferEventSink | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        random_source: Callable[[], float] | None = None,
        unverifiable_resume_policy: Literal["restart", "manual"] = "restart",
        trust_env: bool = True,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """构造 HTTP 传输适配器。

        Parameters
        ----------
        extra_headers:
            附加到**每个**请求上的头，用于需要认证的公开科研数据源
            （例如 NASA Earthdata 的 ``Authorization: Bearer <EDL token>``）。

            **安全约束**：调用方负责保证这个映射里的值不会被写进返回值、日志或
            Manifest —— 本类只把它交给 httpx，不读取、不回显。凭据解析由
            ``environment``/``providers`` 层从环境变量或密钥环完成，不经由本参数传递
            明文。值为 ``None`` 的项会被跳过（便于从"可能缺失"的配置直接展开）。
        """
        effective = config or TransferConfig()
        # 注意：**不要**把 transfer.timeout_seconds 传给 httpx 的 ``total`` 参数——
        # 那会让“单次尝试总时长”变成对整条响应的硬截止，数 GB 的正常下载会被掐断。
        # 总时长由基类按可选的 deadline 处理（None = 不加总限制）；httpx 只负责
        # connect/read/write/pool 四类超时，其中 read 是“读空闲 = 卡死”的最终兜底，
        # 因此无论 timeout_seconds 是否为 None，它们都始终显式生效。
        timeout = httpx.Timeout(
            connect=effective.connect_timeout_seconds,
            read=effective.read_timeout_seconds,
            write=effective.read_timeout_seconds,
            pool=effective.connect_timeout_seconds,
        )
        self._client = httpx.Client(
            timeout=timeout,
            verify=effective.verify_tls,
            follow_redirects=True,
            max_redirects=effective.max_redirects,
            # 默认尊重系统/环境代理（企业网络常见）；测试置 False 走直连，保证真实
            # loopback socket 行为可断言（代理会把「连接被拒绝」包装成 502）。
            trust_env=trust_env,
            limits=httpx.Limits(
                max_connections=_DEFAULT_MAX_CONNECTIONS,
                max_keepalive_connections=_DEFAULT_MAX_CONNECTIONS,
            ),
            headers={
                "User-Agent": "researchdatahub/0.1 (transfer)",
                # 禁用压缩协商：Content-Length 必须等于落盘字节数，否则大小校验无意义。
                "Accept-Encoding": "identity",
                **{
                    str(name): str(value)
                    for name, value in (extra_headers or {}).items()
                    if value is not None
                },
            },
        )
        super().__init__(
            config=effective,
            retry=retry,
            event_sink=event_sink,
            on_progress=on_progress,
            clock=clock,
            sleeper=sleeper,
            random_source=random_source,
            unverifiable_resume_policy=unverifiable_resume_policy,
        )

    # ------------------------------------------------------------------ #
    # 协议钩子
    # ------------------------------------------------------------------ #
    def _supports_asset(self, asset: SourceAsset) -> bool:
        if asset.access_route.value != "https":
            return False
        parts = urlsplit(asset.origin_uri)
        return parts.scheme in {"http", "https"} and bool(parts.hostname)

    def get_remote_metadata(self, asset: SourceAsset) -> RemoteMetadata:
        """``HEAD`` 探测远端声明；服务端不支持 HEAD 时用 ``Range: bytes=0-0`` 兜底。"""
        url = self._url(asset)
        self._require_open()
        response = self._send_head(url)
        if response.status_code in _HEAD_NOT_ALLOWED:
            response.close()
            return self._metadata_via_ranged_get(asset, url)
        try:
            self._raise_for_status(response, url=url)
            return self._metadata_from_response(asset, response, total_from_range=False)
        finally:
            response.close()

    @contextmanager
    def _open_stream(
        self,
        asset: SourceAsset,
        *,
        offset: int,
        expected_total: int | None,
        cancel_event: object,
    ) -> Iterator[RemoteStream]:
        """打开 GET 流；Range 协商失败时降级为整文件重下（不报错，交由基类截断）。"""
        del cancel_event  # HTTP 请求本身不可中断；基类在 chunk 边界检查取消
        url = self._url(asset)
        response = self._send_get(url, offset=offset)
        rejection: str | None = None
        start_offset = 0
        resumed = False
        stream_expected = expected_total
        status = response.status_code
        try:
            if offset > 0:
                if status == 416:
                    rejection = "range_not_satisfiable"
                elif status == 206:
                    parsed = _parse_content_range(response.headers.get("content-range"))
                    if parsed is None:
                        rejection = "content_range_missing"
                    elif parsed[0] != offset:
                        rejection = "content_range_start_mismatch"
                    else:
                        start_offset = offset
                        resumed = True
                        stream_expected = parsed[1] if parsed[1] is not None else expected_total
                elif status == 200:
                    rejection = "server_ignored_range"
                else:
                    self._raise_for_status(response, url=url)
            else:
                if status == 206:
                    parsed = _parse_content_range(response.headers.get("content-range"))
                    if parsed is None:
                        raise self._protocol_error(
                            "206 response without a valid Content-Range", url=url
                        )
                    start_offset = parsed[0]
                    if start_offset != 0:
                        raise self._protocol_error(
                            f"206 response starts at {start_offset}, expected 0", url=url
                        )
                    stream_expected = parsed[1] if parsed[1] is not None else expected_total
                elif status == 200:
                    stream_expected = self._content_length(response, url=url) or expected_total
                else:
                    self._raise_for_status(response, url=url)

            if rejection is not None:
                response.close()
                response = self._send_get(url, offset=0)
                status = response.status_code
                self._raise_for_status(response, url=url)
                stream_expected = self._content_length(response, url=url) or expected_total
                start_offset = 0
                resumed = False
            elif (
                expected_total is not None
                and stream_expected is not None
                and stream_expected != expected_total
            ):
                # HEAD 与 GET 之间对象变了：这条响应的偏移语义已经不可信，交给重试重新探测。
                raise RetryableTransportError(
                    f"remote size changed between probe and GET "
                    f"({expected_total} -> {stream_expected}) for {url}",
                    detail={
                        "error_code": str(TransferErrorCode.PROTOCOL_ERROR),
                        "condition": "connection_reset",
                        "status_code": status,
                        "url": url,
                    },
                )
            yield RemoteStream(
                chunks=response.iter_bytes(self._config.chunk_size_bytes),
                start_offset=start_offset,
                expected_total_size=stream_expected,
                resumed=resumed,
                http_status=status,
                resume_rejection_reason=rejection,
            )
        finally:
            response.close()

    def close(self) -> None:
        """关闭 httpx 连接池；幂等。"""
        if getattr(self, "_closed", True):
            return
        self._client.close()
        super().close()

    # ------------------------------------------------------------------ #
    # 异常翻译
    # ------------------------------------------------------------------ #
    def _translate_exception(self, exc: BaseException) -> TransportError | None:
        """把 httpx 异常映射成 core 的传输异常（condition 供重试白名单判断）。"""
        if isinstance(exc, httpx.ConnectTimeout):
            return self._retryable(
                "connect timeout",
                TransferErrorCode.TIMEOUT_CONNECT,
                condition="timeout",
                exc=exc,
            )
        if isinstance(exc, httpx.ReadTimeout):
            return self._retryable(
                "read timeout", TransferErrorCode.TIMEOUT_READ, condition="timeout", exc=exc
            )
        if isinstance(exc, httpx.WriteTimeout):
            return self._retryable(
                "write timeout", TransferErrorCode.TIMEOUT_WRITE, condition="timeout", exc=exc
            )
        if isinstance(exc, httpx.PoolTimeout):
            return self._retryable(
                "connection pool timeout",
                TransferErrorCode.TIMEOUT_POOL,
                condition="timeout",
                exc=exc,
            )
        if isinstance(exc, httpx.ConnectError):
            return self._retryable(
                "connection failed",
                TransferErrorCode.CONNECTION_FAILED,
                condition="connection_reset",
                exc=exc,
            )
        if isinstance(exc, (httpx.ReadError, httpx.WriteError)):
            return self._retryable(
                "connection reset",
                TransferErrorCode.CONNECTION_RESET,
                condition="connection_reset",
                exc=exc,
            )
        if isinstance(exc, httpx.RemoteProtocolError):
            return self._retryable(
                f"protocol error: {exc}",
                TransferErrorCode.PROTOCOL_ERROR,
                condition="connection_reset",
                exc=exc,
            )
        if isinstance(exc, httpx.TooManyRedirects):
            return PermanentTransportError(
                f"too many redirects: {exc}",
                detail={"error_code": str(TransferErrorCode.TOO_MANY_REDIRECTS)},
            )
        if isinstance(exc, httpx.UnsupportedProtocol):
            return PermanentTransportError(
                f"unsupported protocol: {exc}",
                detail={"error_code": str(TransferErrorCode.UNSUPPORTED_PROTOCOL)},
            )
        if isinstance(exc, httpx.InvalidURL):
            return PermanentTransportError(
                f"invalid URL: {exc}",
                detail={"error_code": str(TransferErrorCode.INVALID_URL)},
            )
        if isinstance(exc, httpx.HTTPError):
            return self._retryable(
                f"http client error: {exc}",
                TransferErrorCode.PROTOCOL_ERROR,
                condition="connection_reset",
                exc=exc,
            )
        return None

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _url(self, asset: SourceAsset) -> str:
        parts = urlsplit(asset.origin_uri)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise PermanentTransportError(
                f"unsupported HTTP URI {asset.origin_uri!r}",
                task_id=None,
                source_id=asset.source_id,
                detail={"error_code": str(TransferErrorCode.INVALID_URL)},
            )
        return asset.origin_uri

    def _send_head(self, url: str) -> httpx.Response:
        try:
            return self._client.head(url)
        except httpx.HTTPError as exc:
            self._raise_http_error(exc)

    def _send_get(self, url: str, *, offset: int) -> httpx.Response:
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
        try:
            request = self._client.build_request("GET", url, headers=headers)
            return self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            self._raise_http_error(exc)

    def _metadata_via_ranged_get(self, asset: SourceAsset, url: str) -> RemoteMetadata:
        """HEAD 被拒（405/501）时的兜底：``Range: bytes=0-0`` 取头部。"""
        with self._client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            self._raise_for_status(response, url=url)
            for _ in response.iter_bytes(1):
                break
            return self._metadata_from_response(
                asset, response, total_from_range=response.status_code == 206
            )

    def _metadata_from_response(
        self, asset: SourceAsset, response: httpx.Response, *, total_from_range: bool
    ) -> RemoteMetadata:
        size = self._content_length(response, url=str(response.url))
        if total_from_range:
            parsed = _parse_content_range(response.headers.get("content-range"))
            if parsed is not None and parsed[1] is not None:
                size = parsed[1]
        return RemoteMetadata(
            origin_uri=asset.origin_uri,
            url=str(response.url),
            transport_id=self.transport_id,
            http_status=response.status_code,
            size_bytes=size,
            etag=response.headers.get("etag"),
            last_modified=_parse_http_date(response.headers.get("last-modified")),
            content_type=response.headers.get("content-type"),
            accepts_ranges=self._accepts_ranges(response),
        )

    def _accepts_ranges(self, response: httpx.Response) -> bool:
        value = response.headers.get("accept-ranges")
        return value is not None and value.strip().lower() == "bytes"

    def _content_length(self, response: httpx.Response, *, url: str) -> int | None:
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise self._protocol_error(
                f"malformed Content-Length {raw!r}", url=url, status=response.status_code
            ) from exc
        if value < 0:
            raise self._protocol_error(
                f"negative Content-Length {raw!r}", url=url, status=response.status_code
            )
        return value

    def _raise_for_status(self, response: httpx.Response, *, url: str) -> None:
        """非 2xx/3xx 一律映射为可重试/不可重试的 core 异常。"""
        status = response.status_code
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise PermanentTransportError(
                f"unfollowed HTTP redirect {status} for {url}",
                detail={
                    "error_code": str(TransferErrorCode.HTTP_REDIRECT),
                    "status_code": status,
                    "url": url,
                },
            )
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        condition = condition_for_http_status(status)
        classification = classify_http_status(status, self._retry.retry_on)
        if status in (401, 403):
            code = TransferErrorCode.HTTP_ACCESS_DENIED
        elif status in (404, 410):
            code = TransferErrorCode.HTTP_NOT_FOUND
        elif status == 429:
            code = TransferErrorCode.HTTP_RATE_LIMITED
        elif 500 <= status < 600:
            code = TransferErrorCode.HTTP_SERVER_ERROR
        elif status in _RETRYABLE_STATUS:
            code = TransferErrorCode.HTTP_CLIENT_ERROR
        else:
            code = TransferErrorCode.HTTP_CLIENT_ERROR
        detail: dict[str, object] = {
            "error_code": str(code),
            "condition": condition if condition is not None else "http_4xx",
            "status_code": status,
            "url": url,
        }
        message = f"HTTP {status} for {url}"
        if classification is HttpStatusClass.RETRYABLE:
            raise RetryableTransportError(message, detail=detail, retry_after_seconds=retry_after)
        raise PermanentTransportError(message, detail=detail, retry_after_seconds=retry_after)

    def _protocol_error(
        self, message: str, *, url: str, status: int | None = None
    ) -> PermanentTransportError:
        detail: dict[str, object] = {
            "error_code": str(TransferErrorCode.PROTOCOL_ERROR),
            "url": url,
        }
        if status is not None:
            detail["status_code"] = status
        return PermanentTransportError(message, detail=detail)

    def _retryable(
        self,
        message: str,
        code: TransferErrorCode,
        *,
        condition: str,
        exc: BaseException,
    ) -> RetryableTransportError:
        return RetryableTransportError(
            f"{message}: {exc}",
            detail={
                "error_code": str(code),
                "condition": condition,
            },
        )

    def _raise_http_error(self, exc: httpx.HTTPError) -> NoReturn:
        """翻译并抛出 httpx 异常；无法识别的异常原样抛出（不伪装成传输失败）。"""
        translated = self._translate_exception(exc)
        if translated is not None:
            raise translated from exc
        raise exc
