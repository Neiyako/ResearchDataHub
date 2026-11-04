"""传输层异常与稳定错误码。

与 :mod:`researchdatahub.core.errors` 的关系
-------------------------------------------
* 本模块的异常**全部继承** core 中对应类（``RetryableTransportError`` /
  ``PermanentTransportError`` / ``StorageError``），所以调度器仍然可以按
  architecture.md §7.1 的映射做状态推进（可重试 → ``RETRY_WAIT``，不可重试 → ``FAILED``）。
* 额外携带两个东西：
  1. ``error_code``：稳定的机器可读错误码（写入 ``TransferResult.error_code`` 与
     ``transfer_logs``，比异常类名更细）；
  2. ``result``：本次失败的 :class:`TransferResult` 快照，满足「失败也留下结构化记录」，
     调用方无需再从异常字符串里猜数字。
* ``TransferResult`` 由传输层构造，异常只负责传递。任何外部代码都不应“用完即弃”地
  吞掉这些异常：调度器按类型决策，日志按 ``error_code`` 聚合。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from researchdatahub.core.errors import (
    PermanentTransportError,
    RdhError,
    RetryableTransportError,
    StorageError,
)
from researchdatahub.core.models import TransferResult

__all__ = [
    "PermanentTransferError",
    "RetryableTransferError",
    "StorageTransferError",
    "TransferCancelledError",
    "TransferErrorCode",
    "error_code_of",
]


class TransferErrorCode(StrEnum):
    """传输层稳定错误码。新增值只能追加，不得修改既有值（会被 Manifest 永久引用）。"""

    # 调用方 / 规划错误
    UNSUPPORTED_ASSET = "unsupported_asset"
    CANCELLED = "cancelled"
    DESTINATION_EXISTS = "destination_exists"

    # 内容一致性
    SIZE_MISMATCH = "size_mismatch"
    REMOTE_SIZE_UNKNOWN = "remote_size_unknown"
    RESUME_IDENTITY_UNKNOWN = "resume_identity_unknown"

    # 网络 / 协议
    TIMEOUT_CONNECT = "timeout_connect"
    TIMEOUT_READ = "timeout_read"
    TIMEOUT_WRITE = "timeout_write"
    TIMEOUT_POOL = "timeout_pool"
    TIMEOUT_TOTAL = "timeout_total"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_RESET = "connection_reset"
    PROTOCOL_ERROR = "protocol_error"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    INVALID_URL = "invalid_url"

    # HTTP 语义
    HTTP_ACCESS_DENIED = "http_access_denied"
    HTTP_NOT_FOUND = "http_not_found"
    HTTP_RATE_LIMITED = "http_rate_limited"
    HTTP_SERVER_ERROR = "http_server_error"
    HTTP_CLIENT_ERROR = "http_client_error"
    HTTP_REDIRECT = "http_redirect"
    HTTP_RANGE_INVALID = "http_range_invalid"

    # S3 语义
    S3_ACCESS_DENIED = "s3_access_denied"
    S3_NOT_FOUND = "s3_not_found"
    S3_THROTTLED = "s3_throttled"
    S3_SERVER_ERROR = "s3_server_error"
    S3_CLIENT_ERROR = "s3_client_error"
    S3_MALFORMED_RESPONSE = "s3_malformed_response"
    S3_CREDENTIALS = "s3_credentials"
    ETAG_MD5_MISMATCH = "etag_md5_mismatch"

    # 本地存储
    STORAGE_WRITE_FAILED = "storage_write_failed"
    STORAGE_REPLACE_FAILED = "storage_replace_failed"
    STORAGE_DIR_FAILED = "storage_dir_failed"
    STORAGE_PART_INCONSISTENT = "storage_part_inconsistent"


def _detail_with_code(error_code: str, detail: dict[str, Any] | None) -> dict[str, Any]:
    """把错误码合并进 ``detail``，保证两处（属性与 detail）永不漂移。"""
    merged: dict[str, Any] = dict(detail or {})
    merged["error_code"] = error_code
    return merged


class RetryableTransferError(RetryableTransportError):
    """可重试的传输失败；``result`` 是这次失败的完整结构化快照。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: TransferErrorCode | str,
        result: TransferResult,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        code = str(error_code)
        super().__init__(
            message,
            task_id=task_id,
            source_id=source_id,
            detail=_detail_with_code(code, detail),
            retry_after_seconds=retry_after_seconds,
        )
        self.error_code = code
        self.result = result


class PermanentTransferError(PermanentTransportError):
    """不可重试的传输失败；``result`` 是这次失败的完整结构化快照。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: TransferErrorCode | str,
        result: TransferResult,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        code = str(error_code)
        super().__init__(
            message,
            task_id=task_id,
            source_id=source_id,
            detail=_detail_with_code(code, detail),
            retry_after_seconds=retry_after_seconds,
        )
        self.error_code = code
        self.result = result


class StorageTransferError(StorageError):
    """本地落盘失败（磁盘满、权限、原子替换失败）；``result`` 同样携带失败快照。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: TransferErrorCode | str,
        result: TransferResult,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        code = str(error_code)
        super().__init__(
            message,
            task_id=task_id,
            source_id=source_id,
            detail=_detail_with_code(code, detail),
        )
        self.error_code = code
        self.result = result


class TransferCancelledError(PermanentTransferError):
    """调用方请求取消（``threading.Event`` 已置位）。

    语义上这**不是**可重试错误，也不该被判为 ``FAILED`` 的“真失败”：调度器应按
    ``error_code == "cancelled"`` 映射到 ``CANCELLED``。这里归入 Permanent 是因为
    “同一进程内继续重试”没有意义；是否重新入队由调度器结合取消来源决定。
    """

    def __init__(
        self,
        message: str,
        *,
        result: TransferResult,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code=TransferErrorCode.CANCELLED,
            result=result,
            task_id=task_id,
            source_id=source_id,
            detail=detail,
        )


def error_code_of(exc: BaseException) -> str:
    """从异常中取出稳定错误码（优先 ``detail["error_code"]``，否则用类级 code）。

    永远返回非空字符串，便于直接写入 ``TransferResult.error_code``。
    """
    if isinstance(exc, RdhError):
        raw = exc.detail.get("error_code")
        if isinstance(raw, str) and raw:
            return raw[:64]
        return exc.code[:64]
    return "unexpected_error"
