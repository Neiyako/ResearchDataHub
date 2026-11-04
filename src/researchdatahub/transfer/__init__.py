"""传输层：按访问路径搬运字节（S3 / HTTPS），不感知数据源的业务语义（ADR-0002）。

公开 API
--------
* 基类：:class:`TransportAdapterBase`（实现 ``core.ports.TransportAdapter``，并额外提供
  ``get_remote_metadata`` / ``probe_resume`` / ``transfer_outcome``）。
* 实现：:class:`HttpTransportAdapter`（``https``/``http``）、
  :class:`S3TransportAdapter`（``s3``）。
* 重试：:class:`RetryPolicy`（从 ``config.models.RetryConfig`` 构造）。
* 并发：:class:`BoundedTransferRunner`（固定并发、提交端限流）。
* 事件：:class:`TransferEventSink`（产出 ``core.models.TransferLogEvent``）。
* 异常：:class:`RetryableTransferError` / :class:`PermanentTransferError` /
  :class:`StorageTransferError` / :class:`TransferCancelledError`，均继承 core 对应异常
  并携带失败 ``TransferResult``。

代理（``trust_env``）
--------------------
:class:`HttpTransportAdapter` 默认 ``trust_env=True``：尊重系统/环境代理（企业网络常见，
代理往往也是唯一出口）。代价是可预测性下降（代理可能改写响应头、把连接错误包成 502）；
传输层因此不信任任何中间层声明，大小/ETag/Range 校验只针对响应字节做。需要严格可复现
或直连的环境（含测试）请显式传 ``trust_env=False``。

失败契约（调度器集成）
----------------------
成功返回 ``TransferResult(status=SUCCEEDED/SKIPPED)``；失败抛上面四个异常，其中
``exc.result`` 是完整的失败 ``TransferResult``。不想处理异常时用 ``transfer_outcome()``，
它把传输失败折成 ``TransferResult(status=FAILED)`` 返回；程序缺陷仍然抛出。
"""

from __future__ import annotations

from researchdatahub.transfer.base import (
    ProgressUpdate,
    RemoteMetadata,
    RemoteStream,
    ResumeDecision,
    ResumeMode,
    ResumeProbe,
    TransportAdapterBase,
    decide_resume,
)
from researchdatahub.transfer.errors import (
    PermanentTransferError,
    RetryableTransferError,
    StorageTransferError,
    TransferCancelledError,
    TransferErrorCode,
    error_code_of,
)
from researchdatahub.transfer.events import TransferEventSink, TransferEventType
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import (
    HttpStatusClass,
    RetryPolicy,
    classify_http_status,
    condition_for_http_status,
)
from researchdatahub.transfer.runner import (
    DEFAULT_MAX_WORKERS,
    MAX_MAX_WORKERS,
    BoundedTransferRunner,
    TransferJob,
)
from researchdatahub.transfer.s3 import (
    S3TransportAdapter,
    classify_client_error,
    etag_md5_digest,
    is_multipart_etag,
    metadata_from_head,
    parse_s3_uri,
)
from researchdatahub.transfer.staging import (
    PART_SUFFIX,
    SIDECAR_SUFFIX,
    PartSidecar,
    part_path_for,
    read_sidecar,
    remove_staged,
    sidecar_path_for,
    write_sidecar,
)

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "MAX_MAX_WORKERS",
    "PART_SUFFIX",
    "SIDECAR_SUFFIX",
    "BoundedTransferRunner",
    "HttpStatusClass",
    "HttpTransportAdapter",
    "PartSidecar",
    "PermanentTransferError",
    "ProgressUpdate",
    "RemoteMetadata",
    "RemoteStream",
    "ResumeDecision",
    "ResumeMode",
    "ResumeProbe",
    "RetryPolicy",
    "RetryableTransferError",
    "S3TransportAdapter",
    "StorageTransferError",
    "TransferAdapterBase",
    "TransferCancelledError",
    "TransferErrorCode",
    "TransferEventSink",
    "TransferEventType",
    "TransferJob",
    "classify_client_error",
    "classify_http_status",
    "condition_for_http_status",
    "decide_resume",
    "error_code_of",
    "etag_md5_digest",
    "is_multipart_etag",
    "metadata_from_head",
    "parse_s3_uri",
    "part_path_for",
    "read_sidecar",
    "remove_staged",
    "sidecar_path_for",
    "write_sidecar",
]

# 别名：便于 ``from researchdatahub.transfer import TransferAdapterBase as TransportAdapter``。
TransferAdapterBase = TransportAdapterBase
