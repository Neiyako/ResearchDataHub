"""S3 传输适配器（``boto3``，流式 + 分块）。

为什么不用 boto3 的高层 ``download_file``
----------------------------------------
* 它内部有重试/分片并发，会与 :class:`~researchdatahub.transfer.retry.RetryPolicy`
  重复放大（一次失败变成 N×M 次请求），也会让 ``retries_used`` 失真；
* 它不能把“部分字节 + sidecar 身份校验”暴露给我们，断点续传的安全性无法自证。

因此这里用 ``head_object`` + ``get_object`` 手工流式实现，与 HTTP 适配器共享同一套
基类语义（``.part`` 暂存、原子提交、大小校验、重试、取消、事件）。

**未实现 multipart 并行下载（如实标注）**
------------------------------------------
M2 只做“单条 ``get_object`` 的流式顺序读取”：``s3.part_size_mb`` 仅作为流式块上限
（与 ``transfer.chunk_size_bytes`` 取较小者），不当成并发分片。multipart 并行下载
（多 Range 并发 + 分片重组）不在 M2 范围内，后续若实现必须复用同一套
``.part``/sidecar 自证与原子提交语义，而不是绕开它们。

**ETag 不是 MD5（硬性纪律）**
------------------------------
* 普通单段上传对象的 ETag 恰好等于 MD5，但**这是实现细节，不是 S3 协议保证**：
  SSE-KMS、SSE-C、部分兼容实现都会改变 ETag 语义；
* 分段上传（multipart）对象的 ETag 形如 ``"<md5>-<partcount>"``，**永远不是文件 MD5**；
* 因此默认 ``verify_etag_as_md5=False``。只有配置显式打开、且 ETag 不含 ``-``（非
  multipart）时，才会在流式读取过程中顺带算 MD5 并与 ETag 比较；其余情况一律不比较，
  绝不出现“把 ETag 当 MD5 校验通过/失败”的假证据。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

import boto3
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from researchdatahub.config.models import S3TransferConfig, TransferConfig
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
    ResumeProbe,
    TransportAdapterBase,
)
from researchdatahub.transfer.errors import TransferErrorCode
from researchdatahub.transfer.events import TransferEventSink
from researchdatahub.transfer.retry import RetryPolicy

__all__ = [
    "S3TransportAdapter",
    "classify_client_error",
    "etag_md5_digest",
    "is_multipart_etag",
    "metadata_from_head",
    "parse_s3_uri",
]


class _StreamingBody(Protocol):
    """``botocore.response.StreamingBody`` 的最小结构（只依赖我们用到的两个方法）。"""

    def iter_chunks(self, chunk_size: int = 1024) -> Iterator[bytes]: ...

    def close(self) -> None: ...


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """把 ``s3://bucket/key`` 解成 ``(bucket, key)``。

    Raises
    ------
    PermanentTransportError
        scheme 不是 ``s3``、没有 bucket 或 key 为空。
    """
    text = uri.strip()
    if not text.startswith("s3://"):
        raise PermanentTransportError(
            f"not an S3 URI: {uri!r}",
            detail={"error_code": str(TransferErrorCode.INVALID_URL)},
        )
    remainder = text[len("s3://") :]
    if "/" not in remainder:
        raise PermanentTransportError(
            f"S3 URI has no object key: {uri!r}",
            detail={"error_code": str(TransferErrorCode.INVALID_URL)},
        )
    bucket, key = remainder.split("/", 1)
    if not bucket or not key:
        raise PermanentTransportError(
            f"S3 URI has empty bucket or key: {uri!r}",
            detail={"error_code": str(TransferErrorCode.INVALID_URL)},
        )
    return bucket, key


def is_multipart_etag(etag: str | None) -> bool:
    """ETag 是否形如 ``"<digest>-<partcount>"``（分段上传产物，绝不是文件 MD5）。"""
    if etag is None:
        return False
    text = etag.strip().strip('"')
    return "-" in text


def etag_md5_digest(etag: str | None) -> str | None:
    """把**单段** ETag 规整成 32 位小写 MD5 十六进制；不满足条件返回 ``None``。

    这不是“信任 ETag”，而是为 ``verify_etag_as_md5=True`` 的场景准备比较对象：
    multipart、空 ETag、非 32 位 hex 一律返回 ``None``（即不比较）。
    """
    if etag is None:
        return None
    text = etag.strip().strip('"')
    if not text or is_multipart_etag(text):
        return None
    lowered = text.lower()
    if len(lowered) != 32 or any(ch not in "0123456789abcdef" for ch in lowered):
        return None
    return lowered


def classify_client_error(exc: ClientError) -> tuple[bool, TransferErrorCode, str | None]:
    """把 ``ClientError`` 分类为 ``(retryable, error_code, condition)``。

    * 403 / ``AccessDenied`` → 不可重试（凭证或桶策略问题）；
    * 404 / ``NoSuchKey`` / ``NoSuchBucket`` → 不可重试；
    * 429 / ``SlowDown`` / ``Throttling*`` / 503 → 可重试（``http_429`` / ``http_5xx``）；
    * 5xx / ``InternalError`` / ``RequestTimeout`` → 可重试（``http_5xx``）；
    * 其它 → 不可重试的客户端错误。

    注意：在无 ``s3:ListBucket`` 权限时，S3 对**不存在**的对象也返回 403，因此 403
    统一按“不可重试的权限问题”处理，不会猜“也许只是对象不存在”。
    """
    error = exc.response.get("Error")
    error_map: Mapping[str, object] = error if isinstance(error, Mapping) else {}
    code_raw = error_map.get("Code")
    code = code_raw if isinstance(code_raw, str) else ""
    metadata = exc.response.get("ResponseMetadata")
    meta_map: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
    status_raw = meta_map.get("HTTPStatusCode")
    status = status_raw if isinstance(status_raw, int) else 0

    if code in {"AccessDenied", "403"} or status == 403:
        return False, TransferErrorCode.S3_ACCESS_DENIED, None
    if code in {"NoSuchKey", "NoSuchBucket", "404"} or status == 404:
        return False, TransferErrorCode.S3_NOT_FOUND, None
    if code in {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded"} or (
        status == 429
    ):
        return True, TransferErrorCode.S3_THROTTLED, "http_429"
    if status in {500, 502, 503, 504} or code in {
        "InternalError",
        "ServiceUnavailable",
        "RequestTimeout",
        "500",
        "503",
    }:
        return True, TransferErrorCode.S3_SERVER_ERROR, "http_5xx"
    return False, TransferErrorCode.S3_CLIENT_ERROR, None


def metadata_from_head(
    *,
    origin_uri: str,
    transport_id: str,
    head: Mapping[str, object],
) -> RemoteMetadata:
    """``head_object`` 响应 → :class:`RemoteMetadata`；字段缺失/类型错误立即失败。"""
    size_raw = head.get("ContentLength")
    if not isinstance(size_raw, int) or isinstance(size_raw, bool) or size_raw < 0:
        raise PermanentTransportError(
            f"head_object response for {origin_uri} has no valid ContentLength",
            detail={
                "error_code": str(TransferErrorCode.S3_MALFORMED_RESPONSE),
                "origin_uri": origin_uri,
            },
        )
    etag_raw = head.get("ETag")
    etag = etag_raw if isinstance(etag_raw, str) else None
    modified_raw = head.get("LastModified")
    last_modified: datetime | None = None
    if isinstance(modified_raw, datetime):
        last_modified = (
            modified_raw.astimezone(UTC)
            if modified_raw.tzinfo is not None and modified_raw.utcoffset() is not None
            else modified_raw.replace(tzinfo=UTC)
        )
    content_type_raw = head.get("ContentType")
    content_type = content_type_raw if isinstance(content_type_raw, str) else None
    return RemoteMetadata(
        origin_uri=origin_uri,
        url=origin_uri,
        transport_id=transport_id,
        http_status=200,
        size_bytes=size_raw,
        etag=etag,
        last_modified=last_modified,
        content_type=content_type,
        accepts_ranges=True,  # S3 的 GetObject 规范保证支持 Range
    )


def _parse_s3_content_range(value: str | None) -> tuple[int, int | None] | None:
    """解析 S3 ``ContentRange``（形如 ``bytes 100-999/1000``）。"""
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
        start = int(start_text)
        end = int(end_text)
    except ValueError:
        return None
    total: int | None
    if total_text.strip() == "*":
        total = None
    else:
        try:
            total = int(total_text)
        except ValueError:
            return None
    if start < 0 or end < start or (total is not None and total < 0):
        return None
    return start, total


def _iter_body(
    body: _StreamingBody,
    *,
    chunk_size: int,
    expected_md5: str | None,
    origin_uri: str,
) -> Iterator[bytes]:
    """流式读取 body；仅在 ``expected_md5`` 非空时顺带计算 MD5 并在结尾比较。

    ``expected_md5`` 只会由 :meth:`S3TransportAdapter._open_stream` 在
    “``verify_etag_as_md5=True`` 且 ETag 非 multipart 且本次从 0 开始传”时传入。
    """
    hasher = hashlib.md5(usedforsecurity=False) if expected_md5 is not None else None
    for chunk in body.iter_chunks(chunk_size):
        if not chunk:
            continue
        if hasher is not None:
            hasher.update(chunk)
        yield chunk
    if hasher is not None and expected_md5 is not None:
        actual = hasher.hexdigest()
        if actual != expected_md5:
            raise PermanentTransportError(
                f"ETag/MD5 mismatch for {origin_uri}: expected {expected_md5}, got {actual}",
                detail={
                    "error_code": str(TransferErrorCode.ETAG_MD5_MISMATCH),
                    "origin_uri": origin_uri,
                },
            )


class S3TransportAdapter(TransportAdapterBase):
    """``s3://`` 传输实现（``boto3``，流式 ``get_object``）。"""

    TRANSPORT_ID = "s3"

    def __init__(
        self,
        *,
        config: TransferConfig | None = None,
        s3_config: S3TransferConfig | None = None,
        retry: RetryPolicy | None = None,
        event_sink: TransferEventSink | None = None,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        random_source: Callable[[], float] | None = None,
        unverifiable_resume_policy: Literal["restart", "manual"] = "restart",
    ) -> None:
        effective = config or TransferConfig()
        self._s3_config = s3_config or effective.s3
        boto_kwargs: dict[str, object] = {
            "config": BotoConfig(
                s3={"addressing_style": self._s3_config.addressing_style},
                max_pool_connections=self._s3_config.max_pool_connections,
                connect_timeout=effective.connect_timeout_seconds,
                read_timeout=effective.read_timeout_seconds,
                # 重试统一由 RetryPolicy 控制；禁止 botocore 再叠一层（避免 N×M 放大）。
                retries={"mode": "standard", "max_attempts": 1},
                signature_version=UNSIGNED if self._s3_config.anonymous else "s3v4",
            ),
            "use_ssl": self._s3_config.use_ssl,
        }
        if self._s3_config.endpoint_url is not None:
            boto_kwargs["endpoint_url"] = self._s3_config.endpoint_url
        if self._s3_config.region_name is not None:
            boto_kwargs["region_name"] = self._s3_config.region_name
        self._client = boto3.client("s3", **boto_kwargs)
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

    @property
    def s3_config(self) -> S3TransferConfig:
        """S3 适配器选项（只读）。"""
        return self._s3_config

    @property
    def _stream_chunk_size(self) -> int:
        """单次 GET 的流式读取块大小（字节）。

        取 ``transfer.chunk_size_bytes`` 与 ``transfer.s3.part_size_mb`` 的较小者：
        前者是全局内存预算（默认 1 MiB），后者是 S3 层的分块上限。8GB 设备上必须
        保持“一次只有一块在内存里”，因此不允许任何一方把它放大到 MB 级失控。
        """
        part_bytes = self._s3_config.part_size_mb * 1024 * 1024
        return min(self._config.chunk_size_bytes, part_bytes)

    # ------------------------------------------------------------------ #
    # 协议钩子
    # ------------------------------------------------------------------ #
    def _supports_asset(self, asset: SourceAsset) -> bool:
        return asset.access_route.value == "s3" and asset.origin_uri.startswith("s3://")

    def _probe_from_metadata(self, asset: SourceAsset, metadata: RemoteMetadata) -> ResumeProbe:
        """S3 GetObject 规范保证支持 Range，能力不由 ``Accept-Ranges`` 头决定。"""
        return ResumeProbe(
            origin_uri=asset.origin_uri,
            transport_id=self.transport_id,
            accepts_ranges=True,
            content_length=metadata.size_bytes,
            etag=metadata.etag,
            last_modified=metadata.last_modified,
        )

    def get_remote_metadata(self, asset: SourceAsset) -> RemoteMetadata:
        """``head_object`` 取大小 / ETag / Last-Modified / Content-Type。"""
        self._require_open()
        bucket, key = parse_s3_uri(asset.origin_uri)
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise self._client_error(exc, bucket=bucket, key=key) from exc
        except BotoCoreError as exc:
            raise self._botocore_error(exc, bucket=bucket, key=key) from exc
        head: Mapping[str, object] = response
        return metadata_from_head(
            origin_uri=asset.origin_uri,
            transport_id=self.transport_id,
            head=head,
        )

    @contextmanager
    def _open_stream(
        self,
        asset: SourceAsset,
        *,
        offset: int,
        expected_total: int | None,
        cancel_event: object,
    ) -> Iterator[RemoteStream]:
        """``get_object`` 流式读取；Range 偏移必须被 ``ContentRange`` 证实。"""
        del cancel_event  # boto3 请求不可中断；基类在 chunk 边界检查取消
        bucket, key = parse_s3_uri(asset.origin_uri)
        kwargs: dict[str, object] = {"Bucket": bucket, "Key": key}
        if offset > 0:
            kwargs["Range"] = f"bytes={offset}-"
        try:
            response = self._client.get_object(**kwargs)
        except ClientError as exc:
            raise self._client_error(exc, bucket=bucket, key=key) from exc
        except BotoCoreError as exc:
            raise self._botocore_error(exc, bucket=bucket, key=key) from exc
        raw: Mapping[str, object] = response
        body_raw = raw.get("Body")
        if not hasattr(body_raw, "iter_chunks") or not hasattr(body_raw, "close"):
            raise PermanentTransportError(
                f"get_object response body for {asset.origin_uri} is not a streaming body",
                detail={
                    "error_code": str(TransferErrorCode.S3_MALFORMED_RESPONSE),
                    "origin_uri": asset.origin_uri,
                },
            )
        body = cast(_StreamingBody, body_raw)
        expected_total_size = expected_total
        start_offset = 0
        resumed = False
        status = self._http_status(raw)
        try:
            if offset > 0:
                content_range_raw = raw.get("ContentRange")
                parsed = _parse_s3_content_range(
                    content_range_raw if isinstance(content_range_raw, str) else None
                )
                if parsed is None or parsed[0] != offset:
                    raise RetryableTransportError(
                        f"S3 ContentRange for {asset.origin_uri} does not match requested "
                        f"offset {offset}",
                        detail={
                            "error_code": str(TransferErrorCode.PROTOCOL_ERROR),
                            "condition": "connection_reset",
                            "origin_uri": asset.origin_uri,
                        },
                    )
                start_offset = offset
                resumed = True
                if parsed[1] is not None:
                    expected_total_size = parsed[1]
            else:
                content_length = raw.get("ContentLength")
                if isinstance(content_length, int) and not isinstance(content_length, bool):
                    expected_total_size = content_length
            expected_md5 = self._expected_md5(raw.get("ETag"), offset=offset)
            yield RemoteStream(
                chunks=_iter_body(
                    body,
                    chunk_size=self._stream_chunk_size,
                    expected_md5=expected_md5,
                    origin_uri=asset.origin_uri,
                ),
                start_offset=start_offset,
                expected_total_size=expected_total_size,
                resumed=resumed,
                http_status=status,
            )
        finally:
            body.close()

    def close(self) -> None:
        """关闭 boto3 客户端连接池；幂等。"""
        if getattr(self, "_closed", True):
            return
        self._client.close()
        super().close()

    # ------------------------------------------------------------------ #
    # 异常翻译
    # ------------------------------------------------------------------ #
    def _translate_exception(self, exc: BaseException) -> TransportError | None:
        """把 botocore 异常映射成 core 传输异常（condition 供重试白名单判断）。"""
        if isinstance(exc, ClientError):
            return self._client_error(exc)
        if isinstance(exc, (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError)):
            return PermanentTransportError(
                f"S3 credentials unavailable: {exc}",
                detail={"error_code": str(TransferErrorCode.S3_CREDENTIALS)},
            )
        if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
            return RetryableTransportError(
                f"S3 timeout: {exc}",
                detail={
                    "error_code": str(TransferErrorCode.TIMEOUT_READ),
                    "condition": "timeout",
                },
            )
        if isinstance(exc, EndpointConnectionError):
            return RetryableTransportError(
                f"S3 endpoint unreachable: {exc}",
                detail={
                    "error_code": str(TransferErrorCode.CONNECTION_FAILED),
                    "condition": "connection_reset",
                },
            )
        if isinstance(exc, ConnectionClosedError):
            return RetryableTransportError(
                f"S3 connection closed mid-stream: {exc}",
                detail={
                    "error_code": str(TransferErrorCode.CONNECTION_RESET),
                    "condition": "connection_reset",
                },
            )
        if isinstance(exc, ParamValidationError):
            return PermanentTransportError(
                f"S3 request parameter invalid: {exc}",
                detail={"error_code": str(TransferErrorCode.S3_CLIENT_ERROR)},
            )
        if isinstance(exc, BotoCoreError):
            # 未枚举的 botocore 客户端错误通常是连接层瞬时故障，标记可重试交给策略判断。
            return RetryableTransportError(
                f"S3 client error: {exc}",
                detail={
                    "error_code": str(TransferErrorCode.CONNECTION_RESET),
                    "condition": "connection_reset",
                },
            )
        return None

    def _client_error(self, exc: ClientError, *, bucket: str = "", key: str = "") -> TransportError:
        retryable, code, condition = classify_client_error(
            exc,
        )
        detail: dict[str, object] = {
            "error_code": str(code),
            "bucket": bucket,
            "key": key,
        }
        if condition is not None:
            detail["condition"] = condition
        message = f"S3 ClientError {code.value}: {exc}"
        if retryable:
            return RetryableTransportError(message, detail=detail)
        return PermanentTransportError(message, detail=detail)

    def _botocore_error(self, exc: BotoCoreError, *, bucket: str, key: str) -> TransportError:
        translated = self._translate_exception(exc)
        if translated is not None:
            translated.detail.setdefault("bucket", bucket)
            translated.detail.setdefault("key", key)
            return translated
        return PermanentTransportError(
            f"S3 error: {exc}",
            detail={
                "error_code": str(TransferErrorCode.S3_CLIENT_ERROR),
                "bucket": bucket,
                "key": key,
            },
        )

    def _expected_md5(self, etag_raw: object, *, offset: int) -> str | None:
        """决定是否用 ETag 当 MD5 比较——只有三个条件同时成立才返回比较目标。

        1. 配置 ``verify_etag_as_md5=True``（默认 False）；
        2. ETag 不是 multipart（不含 ``-``）；
        3. 本次从偏移 0 开始完整读取（续传只读到后半段，算不出全文 MD5）。
        """
        if not self._s3_config.verify_etag_as_md5 or offset > 0:
            return None
        etag = etag_raw if isinstance(etag_raw, str) else None
        return etag_md5_digest(etag)

    def _http_status(self, response: Mapping[str, object]) -> int | None:
        metadata = response.get("ResponseMetadata")
        if not isinstance(metadata, Mapping):
            return None
        status = metadata.get("HTTPStatusCode")
        return status if isinstance(status, int) else None
