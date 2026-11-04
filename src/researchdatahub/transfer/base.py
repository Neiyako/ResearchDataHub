"""传输适配器基类：把「搬字节」的公共骨架集中在一处。

职责划分
--------
子类（HTTP / S3）只负责**协议层**的三件事：

1. :meth:`TransportAdapterBase.get_remote_metadata`：远端声明（大小、ETag、
   Last-Modified、Content-Type、是否支持 Range）；
2. :meth:`TransportAdapterBase.probe_resume`：为断点续传做现场探测；
3. :meth:`TransportAdapterBase._open_stream`：打开一条流，给出「从哪个偏移开始、
   期望多少字节」的 :class:`RemoteStream`。

其余全部在本基类完成，且各协议行为一致：

* ``.part`` 暂存 + ``os.replace`` 原子提交（读者不会看到半截文件）；
* Content-Length / Content-Range 与实收字节的严格校验；
* 断点续传的三重自证（Accept-Ranges、206+Content-Range、ETag/Last-Modified 与
  sidecar 一致），任何一条不满足就整文件重下，绝不拼接来源不明的前缀；
* ``threading.Event`` 取消、进度回调、单次传输总超时；
* 有限次重试（:class:`~researchdatahub.transfer.retry.RetryPolicy`）；
* 失败时按策略保留或清理 ``.part`` + sidecar；
* 结构化事件（:class:`~researchdatahub.transfer.events.TransferEventSink`）。

失败语义（与调度器约定）
------------------------
* 成功返回 ``TransferResult(status=SUCCEEDED)``；目标已存在且声明大小一致时返回
  ``SKIPPED``（绝不覆盖既有正式文件）。
* 失败**抛异常**（architecture.md §7.1）：``RetryableTransferError`` 继承
  core 的 ``RetryableTransportError`` → 调度器进入 ``RETRY_WAIT``；``PermanentTransferError``
  → ``FAILED``；``StorageTransferError`` → ``FAILED``；``TransferCancelledError``
  携带 ``error_code=cancelled`` → 调度器按取消处理。
* 每个失败异常都带 ``result`` 字段（完整的 ``TransferResult(status=FAILED)``），
  所以「失败也留下结构化记录」不依赖调用方自己拼字段。需要结果而不要异常的调用方
  可以使用 :meth:`TransportAdapterBase.transfer_outcome`。

线程模型
--------
单个适配器实例可被多个线程并发调用 ``transfer``（ports.py 的硬性要求）：
所有每次传输的状态都在栈上（``_AttemptState``），实例上只有不可变配置、线程安全的
事件 sink 与 httpx/boto3 客户端（两者自身线程安全）。``close()`` 之后拒绝新调用。
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from researchdatahub.config.models import TransferConfig
from researchdatahub.core.errors import (
    PermanentTransportError,
    RetryableTransportError,
    StorageError,
    TransportError,
)
from researchdatahub.core.models import (
    AcquisitionTask,
    JsonValue,
    SourceAsset,
    TransferResult,
    TransferStatus,
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
from researchdatahub.transfer.retry import RetryPolicy
from researchdatahub.transfer.staging import (
    PartSidecar,
    part_path_for,
    read_sidecar,
    remove_staged,
    sidecar_path_for,
    write_sidecar,
)

__all__ = [
    "ProgressUpdate",
    "RemoteMetadata",
    "RemoteStream",
    "ResumeDecision",
    "ResumeMode",
    "ResumeProbe",
    "TransportAdapterBase",
    "decide_resume",
]


class _StructuredModel(BaseModel):
    """传输层结构化模型的公共配置（不可变、拒绝未知字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class RemoteMetadata(_StructuredModel):
    """远端对象的结构化元数据（:meth:`get_remote_metadata` 的返回值）。"""

    origin_uri: str = Field(min_length=1, max_length=4096)
    url: str = Field(min_length=1, max_length=4096, description="实际请求的最终 URL（含重定向后）")
    transport_id: str = Field(min_length=1, max_length=64)
    http_status: int | None = Field(default=None, ge=100, le=599, description="探测响应状态码")
    size_bytes: int | None = Field(default=None, ge=0, description="远端声明的 Content-Length")
    etag: str | None = Field(default=None, max_length=256)
    last_modified: datetime | None = None
    content_type: str | None = Field(default=None, max_length=256)
    accepts_ranges: bool | None = Field(
        default=None, description="远端是否明确声明支持 Range（None = 未声明/不可知）"
    )

    @field_validator("last_modified")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_modified must be timezone-aware")
        return value.astimezone(UTC)


class ResumeProbe(_StructuredModel):
    """断点续传探测结果：Range 能力 + 可用的内容校验器 + 远端长度。"""

    origin_uri: str = Field(min_length=1, max_length=4096)
    transport_id: str = Field(min_length=1, max_length=64)
    accepts_ranges: bool
    content_length: int | None = Field(default=None, ge=0)
    etag: str | None = Field(default=None, max_length=256)
    last_modified: datetime | None = None

    @field_validator("last_modified")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_modified must be timezone-aware")
        return value.astimezone(UTC)

    @property
    def identity_known(self) -> bool:
        """是否存在可用于自证一致性的强校验器（强 ETag 或 Last-Modified）。"""
        return strong_etag(self.etag) is not None or self.last_modified is not None


class ResumeMode(StrEnum):
    """续传决策的四种结果。"""

    FRESH = "fresh"
    RESUME = "resume"
    RESTART = "restart"
    MANUAL = "manual"


class ResumeDecision(_StructuredModel):
    """一次续传决策及其可审计理由。"""

    mode: ResumeMode
    offset: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=256)

    @property
    def resumable(self) -> bool:
        """是否真的从 ``.part`` 的偏移继续写。"""
        return self.mode is ResumeMode.RESUME


@dataclass(slots=True)
class RemoteStream:
    """一条打开的远端字节流（由子类的 ``_open_stream`` 产出的上下文管理器提供）。"""

    chunks: Iterator[bytes]
    start_offset: int
    expected_total_size: int | None
    resumed: bool
    http_status: int | None = None
    resume_rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """进度回调快照（不可变，避免回调方误改）。"""

    task_id: str
    destination: Path
    attempt: int
    bytes_written: int
    expected_size: int | None
    elapsed_seconds: float
    resumed: bool

    @property
    def fraction(self) -> float | None:
        """完成比例；远端未声明大小时返回 ``None``（不假装算得出来）。"""
        if self.expected_size is None or self.expected_size <= 0:
            return None
        return min(self.bytes_written / self.expected_size, 1.0)


@dataclass(slots=True)
class _Failure:
    """终端/中间失败的内部表示；失败异常与结果都从这里派生。"""

    error_code: str
    message: str
    retryable: bool
    kind: Literal["transport", "storage"] = "transport"
    keep_part: bool = False
    cancelled: bool = False
    condition: str | None = None
    detail: dict[str, object] = field(default_factory=dict)
    retry_after_seconds: float | None = None


@dataclass(slots=True)
class _AttemptState:
    """单次尝试的可变状态（每次尝试新建，绝不放在实例上，保证并发安全）。"""

    attempt: int
    started_at: datetime
    started_monotonic: float
    part_size: int = 0
    expected_size: int | None = None
    resumed: bool = False
    resume_possible: bool = False
    sidecar: PartSidecar | None = None


class _Abort(Exception):
    """尝试内部的中断信号，携带结构化失败与当前状态；不直接暴露给调用方。"""

    def __init__(self, failure: _Failure, state: _AttemptState) -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.state = state


def strong_etag(value: str | None) -> str | None:
    """返回强 ETag 的原始字符串；弱 ETag（``W/`` 前缀）与空值返回 ``None``。

    RFC 9110：弱校验器不能用于 ``If-Range``/字节区间一致性判断，因此一律不参与续传自证。
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text.startswith("W/"):
        return None
    return text


def _utc_seconds(value: datetime | None) -> int | None:
    """把时间规整到整秒（HTTP 日期只有秒精度）用于比较；``None`` 原样返回。"""
    if value is None:
        return None
    return int(value.astimezone(UTC).timestamp())


def _sidecar_identity(sidecar: PartSidecar) -> tuple[str | None, int | None]:
    return strong_etag(sidecar.etag), _utc_seconds(sidecar.last_modified)


def decide_resume(
    *,
    part_size: int,
    sidecar: PartSidecar | None,
    probe: ResumeProbe,
    resume_enabled: bool,
    unverifiable_policy: Literal["restart", "manual"],
    expected_size: int | None = None,
) -> ResumeDecision:
    """决定「从头下 / 续传 / 重下 / 人工介入」。

    续传必须同时满足：

    1. 远端明确支持 Range（``Accept-Ranges: bytes``）；
    2. 本地 ``.part`` 非空且小于远端当前大小；
    3. sidecar 记录的 ETag（优先）或 Last-Modified 与现场探测一致。

    任一不满足：能安全重下就 ``RESTART``；若“身份无法自证”且策略要求人工介入，
    返回 ``MANUAL``（调用方保留 ``.part`` 并报 ``resume_identity_unknown``）。

    Parameters
    ----------
    part_size:
        ``.part`` 的真实字节数（以文件系统为准，不信任 sidecar）。
    sidecar:
        上次留下的 sidecar；``None`` 表示没有可靠的身份记录。
    probe:
        现场探测结果。
    resume_enabled:
        配置开关；关闭时一律 ``RESTART``。
    unverifiable_policy:
        当既无强 ETag 也无 Last-Modified、但存在 ``.part`` 时的策略。
    expected_size:
        远端声明的总大小（可为 ``None``）。与 ``probe.content_length`` 互为冗余校验。
    """
    if part_size <= 0:
        return ResumeDecision(mode=ResumeMode.FRESH, reason="no_partial")
    if not resume_enabled:
        return ResumeDecision(mode=ResumeMode.RESTART, reason="resume_disabled")
    remote_size = probe.content_length if probe.content_length is not None else expected_size
    if remote_size is not None and part_size > remote_size:
        return ResumeDecision(mode=ResumeMode.RESTART, reason="partial_exceeds_remote")
    if remote_size is not None and part_size == remote_size:
        # 大小相等也不能证明内容正确（可能截断后恰好补齐/来自另一个版本），一律重下。
        return ResumeDecision(mode=ResumeMode.RESTART, reason="partial_complete_unverified")
    if not probe.accepts_ranges:
        return ResumeDecision(mode=ResumeMode.RESTART, reason="range_unsupported")
    if sidecar is None:
        return ResumeDecision(mode=ResumeMode.RESTART, reason="missing_sidecar")
    if sidecar.origin_uri != probe.origin_uri:
        return ResumeDecision(mode=ResumeMode.RESTART, reason="origin_uri_changed")

    remote_etag = strong_etag(probe.etag)
    sidecar_etag, sidecar_seconds = _sidecar_identity(sidecar)
    remote_seconds = _utc_seconds(probe.last_modified)
    if remote_etag is not None and sidecar_etag is not None:
        if remote_etag != sidecar_etag:
            return ResumeDecision(mode=ResumeMode.RESTART, reason="etag_changed")
    elif remote_seconds is not None and sidecar_seconds is not None:
        if remote_seconds != sidecar_seconds:
            return ResumeDecision(mode=ResumeMode.RESTART, reason="last_modified_changed")
    else:
        if unverifiable_policy == "manual":
            return ResumeDecision(mode=ResumeMode.MANUAL, reason="identity_unknown")
        return ResumeDecision(mode=ResumeMode.RESTART, reason="identity_unknown")

    if (
        remote_size is not None
        and sidecar.expected_size is not None
        and remote_size != sidecar.expected_size
    ):
        return ResumeDecision(mode=ResumeMode.RESTART, reason="size_changed")
    return ResumeDecision(mode=ResumeMode.RESUME, offset=part_size, reason="validators_match")


class TransportAdapterBase(ABC):
    """``TransportAdapter`` 的实现基类；子类只实现协议相关的三个钩子。

    Parameters
    ----------
    config:
        传输通用参数（超时、分块、TLS、续传开关）。
    retry:
        重试策略；``None`` 时用默认值（生产环境应从 ``DefaultsConfig.retry`` 构造）。
    event_sink:
        事件收集器；``None`` 时创建内部实例（通过 :attr:`events` 读取）。
    on_progress:
        进度回调；每写一个 chunk 调用一次。回调抛出的异常会中断传输（视为缺陷）。
    clock:
        当地时间源（写 sidecar / 事件 / TransferResult 时间戳），测试可注入。
    sleeper:
        重试等待函数，默认 ``time.sleep``；测试可注入记录器。
    random_source:
        抖动随机源，默认 ``random.random``。
    unverifiable_resume_policy:
        存在 ``.part`` 但无法自证身份时的策略：``restart``（默认，安全重下）或
        ``manual``（保留文件并报 ``resume_identity_unknown``，需要人工判断）。
    monotonic:
        单调时钟，用于总超时与 ``duration_seconds``。
    """

    TRANSPORT_ID: ClassVar[str] = "base"

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
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or TransferConfig()
        self._retry = retry or RetryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep
        self._random = random_source or random.random
        self._monotonic = monotonic or time.monotonic
        self._on_progress = on_progress
        self._events = event_sink or TransferEventSink(clock=self._clock)
        self._unverifiable_resume_policy = unverifiable_resume_policy
        self._closed = False

    # ------------------------------------------------------------------ #
    # 公开接口（TransportAdapter 协议）
    # ------------------------------------------------------------------ #
    @property
    def transport_id(self) -> str:
        """传输实现标识（``https`` / ``s3``）。"""
        return self.TRANSPORT_ID

    @property
    def events(self) -> TransferEventSink:
        """本适配器使用的事件收集器。"""
        return self._events

    @property
    def config(self) -> TransferConfig:
        """传输配置（只读）。"""
        return self._config

    @property
    def retry_policy(self) -> RetryPolicy:
        """重试策略（只读）。"""
        return self._retry

    def supports(self, asset: SourceAsset) -> bool:
        """该适配器是否能处理此资产（依据 access_route 与 URI scheme）。"""
        return not self._closed and self._supports_asset(asset)

    def transfer(
        self,
        asset: SourceAsset,
        destination: Path,
        *,
        task: AcquisitionTask,
        cancel_event: threading.Event | None = None,
    ) -> TransferResult:
        """把资产下载到 ``destination``（先写 ``.part``，成功后原子 rename）。

        Parameters
        ----------
        asset:
            远端资产；``asset.size_bytes`` 只用于“目标已存在”时的跳过判断，
            真实期望大小以远端声明（``get_remote_metadata``）为准。
        destination:
            最终目标路径；父目录会被创建。
        task:
            所属任务（事件、错误上下文）。
        cancel_event:
            置位后在下一个检查点中止；``.part`` 与 sidecar 按可续传性保留。

        Raises
        ------
        RetryableTransferError
            可重试失败且重试已用尽（含 ``result``）。
        PermanentTransferError
            不可重试失败（含 ``result``）。
        StorageTransferError
            本地落盘失败（含 ``result``）。
        TransferCancelledError
            取消（``error_code == "cancelled"``）。
        RuntimeError
            适配器已 ``close()``。
        """
        self._require_open()
        if not self._supports_asset(asset):
            raise PermanentTransportError(
                f"transport {self.transport_id!r} does not support asset {asset.origin_uri!r}",
                task_id=task.task_id,
                source_id=asset.source_id,
                detail={
                    "error_code": str(TransferErrorCode.UNSUPPORTED_ASSET),
                    "origin_uri": asset.origin_uri,
                    "access_route": asset.access_route.value,
                    "transport_id": self.transport_id,
                },
            )

        destination = Path(destination)
        started_at = self._clock()
        started_monotonic = self._monotonic()
        self._emit(
            task.task_id,
            TransferEventType.STARTED,
            f"start {self.transport_id} transfer of {asset.origin_uri}",
            metadata={
                "origin_uri": asset.origin_uri,
                "destination": str(destination),
                "declared_size": asset.size_bytes,
                "transport_id": self.transport_id,
            },
        )
        self._prepare_destination(asset, destination, task, started_at, started_monotonic)
        skipped = self._skip_existing(asset, destination, task, started_at, started_monotonic)
        if skipped is not None:
            return skipped

        attempt = 1
        failure: _Failure | None = None
        state: _AttemptState | None = None
        while True:
            state = _AttemptState(
                attempt=attempt, started_at=started_at, started_monotonic=started_monotonic
            )
            try:
                return self._execute_attempt(asset, destination, task, cancel_event, state)
            except _Abort as abort:
                failure, state = abort.failure, abort.state
            except TransportError as exc:
                failure = self._failure_from_transport(exc, state)
            except StorageError as exc:
                failure = self._failure_from_storage(exc, state)
            except OSError as exc:
                failure = self._failure_from_oserror(exc, state)

            assert failure is not None
            should_retry = (
                failure.retryable
                and not failure.cancelled
                and self._retry.allows_condition(failure.condition, retryable=True)
                and attempt < self._retry.max_attempts
            )
            if not should_retry:
                break
            delay = self._retry.delay_for_attempt(
                attempt,
                retry_after_seconds=failure.retry_after_seconds,
                random_source=self._random,
            )
            self._emit(
                task.task_id,
                TransferEventType.RETRY_SCHEDULED,
                f"attempt {attempt} failed with {failure.error_code}; retry in {delay:.3f}s",
                bytes_transferred=state.part_size,
                metadata={
                    "error_code": failure.error_code,
                    "attempt": attempt,
                    "max_attempts": self._retry.max_attempts,
                    "delay_seconds": delay,
                    "retryable": True,
                },
            )
            self._sleeper(delay)
            attempt += 1

        assert state is not None
        self._cleanup_staged(task.task_id, destination, state, keep=failure.keep_part)
        self._raise_terminal(
            asset,
            destination,
            task,
            failure,
            started_at=started_at,
            started_monotonic=started_monotonic,
            state=state,
        )

    def transfer_outcome(
        self,
        asset: SourceAsset,
        destination: Path,
        *,
        task: AcquisitionTask,
        cancel_event: threading.Event | None = None,
    ) -> TransferResult:
        """``transfer`` 的“结果优先”包装：传输类失败返回 ``TransferResult(FAILED)``。

        只有本模块定义的传输失败被转换；``RuntimeError``（生命周期误用）、
        ``ValidationError`` 等程序缺陷仍然抛出——不允许用“返回失败结果”掩盖 bug。
        """
        try:
            return self.transfer(asset, destination, task=task, cancel_event=cancel_event)
        except (RetryableTransferError, PermanentTransferError, StorageTransferError) as exc:
            return exc.result

    async def atransfer(
        self,
        asset: SourceAsset,
        destination: Path,
        *,
        task: AcquisitionTask,
        cancel_event: threading.Event | None = None,
    ) -> TransferResult:
        """``transfer`` 的异步版本；用线程执行同步实现，不阻塞事件循环。"""
        return await asyncio.to_thread(
            self.transfer,
            asset,
            destination,
            task=task,
            cancel_event=cancel_event,
        )

    def close(self) -> None:
        """释放连接池/客户端；幂等，之后拒绝新传输（抛 ``RuntimeError``）。"""
        self._closed = True

    def __enter__(self) -> TransportAdapterBase:
        """上下文管理器入口。"""
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """上下文管理器出口：关闭适配器。"""
        self.close()

    # ------------------------------------------------------------------ #
    # 子类必须实现的协议钩子
    # ------------------------------------------------------------------ #
    @abstractmethod
    def _supports_asset(self, asset: SourceAsset) -> bool:
        """协议判定（不含 ``close`` 状态检查）。"""

    @abstractmethod
    def get_remote_metadata(self, asset: SourceAsset) -> RemoteMetadata:
        """探测远端声明（大小、ETag、Last-Modified、Content-Type、Range 能力）。

        Raises
        ------
        RetryableTransportError
            瞬时故障（超时、连接重置、5xx、429）。
        PermanentTransportError
            不可重试故障（403/404、协议不支持）。
        """

    def probe_resume(self, asset: SourceAsset) -> ResumeProbe:
        """为断点续传做现场探测（Range 能力 + 校验器 + 远端长度）。

        默认实现复用 :meth:`get_remote_metadata`（一次 HEAD 同时拿到元数据与校验器）；
        子类若协议层能强制提供 Range 能力（如 S3），可覆写 :meth:`_probe_from_metadata`。
        """
        return self._probe_from_metadata(asset, self.get_remote_metadata(asset))

    def _probe_from_metadata(self, asset: SourceAsset, metadata: RemoteMetadata) -> ResumeProbe:
        """由远端元数据构造探测结果；单次尝试内部复用，避免重复请求。"""
        return ResumeProbe(
            origin_uri=asset.origin_uri,
            transport_id=self.transport_id,
            accepts_ranges=metadata.accepts_ranges is True,
            content_length=metadata.size_bytes,
            etag=metadata.etag,
            last_modified=metadata.last_modified,
        )

    @abstractmethod
    def _open_stream(
        self,
        asset: SourceAsset,
        *,
        offset: int,
        expected_total: int | None,
        cancel_event: threading.Event | None,
    ) -> AbstractContextManager[RemoteStream]:
        """打开字节流；``offset > 0`` 时请求 Range 并校验 206/Content-Range。

        服务端不配合 Range（200 或 416）时**不报错**：返回
        ``start_offset=0, resumed=False, resume_rejection_reason=...`` 的流，由基类
        截断 ``.part`` 后整文件重下。
        """

    # ------------------------------------------------------------------ #
    # 供子类使用的工具
    # ------------------------------------------------------------------ #
    def _require_open(self) -> None:
        """生命周期检查：``close()`` 之后一切调用都失败。"""
        if self._closed:
            raise RuntimeError(f"transport {self.transport_id!r} is closed")

    def _emit(
        self,
        task_id: str,
        event_type: TransferEventType,
        message: str,
        *,
        bytes_transferred: int = 0,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> None:
        """发送一条结构化事件。"""
        self._events.emit(
            task_id,
            event_type,
            message,
            bytes_transferred=bytes_transferred,
            metadata=metadata,
        )

    def _translate_exception(self, exc: BaseException) -> TransportError | None:
        """把子类客户端库的异常翻译成传输异常；基类默认不翻译（原样抛出）。"""
        return None

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _prepare_destination(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        started_at: datetime,
        started_monotonic: float,
    ) -> None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            failure = _Failure(
                error_code=str(TransferErrorCode.STORAGE_DIR_FAILED),
                message=f"cannot create destination directory {destination.parent}: {exc}",
                retryable=False,
                kind="storage",
                detail={"path": str(destination.parent), "errno": exc.errno},
            )
            self._raise_terminal(
                asset,
                destination,
                task,
                failure,
                started_at=started_at,
                started_monotonic=started_monotonic,
                state=None,
            )

    def _skip_existing(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        started_at: datetime,
        started_monotonic: float,
    ) -> TransferResult | None:
        """目标已存在时的保护策略：绝不覆盖；声明大小一致则 ``SKIPPED``。"""
        if not destination.exists():
            return None
        try:
            local_size = destination.stat().st_size
        except OSError as exc:
            failure = _Failure(
                error_code=str(TransferErrorCode.STORAGE_WRITE_FAILED),
                message=f"cannot stat existing destination {destination}: {exc}",
                retryable=False,
                kind="storage",
                detail={"path": str(destination), "errno": exc.errno},
            )
            self._raise_terminal(
                asset,
                destination,
                task,
                failure,
                started_at=started_at,
                started_monotonic=started_monotonic,
                state=None,
            )
        if local_size == asset.size_bytes:
            self._emit(
                task.task_id,
                TransferEventType.SKIPPED,
                f"destination already exists with declared size {local_size}; not overwriting",
                metadata={"destination": str(destination), "size_bytes": local_size},
            )
            return TransferResult(
                task_id=task.task_id,
                status=TransferStatus.SKIPPED,
                transport_id=self.transport_id,
                bytes_transferred=0,
                duration_seconds=self._elapsed(started_monotonic),
                destination_path=str(destination),
                error_code=None,
                completed_at=self._clock(),
                source_uri=asset.origin_uri,
                expected_size=asset.size_bytes,
                actual_size=local_size,
                started_at=started_at,
            )
        failure = _Failure(
            error_code=str(TransferErrorCode.DESTINATION_EXISTS),
            message=(
                f"destination {destination} already exists with size {local_size}, "
                f"declared {asset.size_bytes}; refusing to overwrite"
            ),
            retryable=False,
            detail={
                "destination": str(destination),
                "local_size": local_size,
                "declared_size": asset.size_bytes,
            },
        )
        self._raise_terminal(
            asset,
            destination,
            task,
            failure,
            started_at=started_at,
            started_monotonic=started_monotonic,
            state=None,
        )

    def _execute_attempt(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        cancel_event: threading.Event | None,
        state: _AttemptState,
    ) -> TransferResult:
        """执行一次完整尝试：探测 → 续传决策 → 流式落盘 → 校验 → 原子提交。"""
        self._check_cancel(cancel_event, state)

        metadata = self.get_remote_metadata(asset)
        self._emit(
            task.task_id,
            TransferEventType.HTTP_STATUS,
            f"remote metadata status={metadata.http_status} for {metadata.url}",
            metadata={
                "method": "HEAD",
                "status_code": metadata.http_status if metadata.http_status is not None else 0,
                "url": metadata.url,
            },
        )
        probe = self._probe_from_metadata(asset, metadata)
        state.expected_size = (
            metadata.size_bytes if metadata.size_bytes is not None else probe.content_length
        )
        self._emit(
            task.task_id,
            TransferEventType.RANGE_PROBE,
            f"range probe: accepts_ranges={probe.accepts_ranges}, "
            f"etag={probe.etag}, last_modified={probe.last_modified}",
            metadata={
                "accepts_ranges": probe.accepts_ranges,
                "content_length": probe.content_length,
                "etag": probe.etag,
                "last_modified": (
                    probe.last_modified.isoformat() if probe.last_modified is not None else None
                ),
            },
        )

        part = part_path_for(destination)
        part_size = part.stat().st_size if part.exists() else 0
        state.part_size = part_size
        state.resume_possible = probe.accepts_ranges and probe.identity_known
        sidecar: PartSidecar | None
        try:
            sidecar = read_sidecar(sidecar_path_for(destination))
        except ValueError as exc:
            # 损坏的 sidecar 等价于“身份不可证明”，但必须先记录，不能静默当作不存在。
            self._emit(
                task.task_id,
                TransferEventType.STAGING_CLEANUP,
                f"unreadable sidecar treated as unknown identity: {exc}",
                metadata={"reason": "sidecar_corrupt"},
            )
            sidecar = None
        decision = decide_resume(
            part_size=part_size,
            sidecar=sidecar,
            probe=probe,
            resume_enabled=self._config.resume_enabled,
            unverifiable_policy=self._unverifiable_resume_policy,
            expected_size=state.expected_size,
        )
        self._emit(
            task.task_id,
            TransferEventType.RESUME_DECISION,
            f"resume decision: {decision.mode.value} ({decision.reason})",
            bytes_transferred=part_size,
            metadata={
                "mode": decision.mode.value,
                "offset": decision.offset,
                "reason": decision.reason,
                "part_size": part_size,
            },
        )
        if decision.mode is ResumeMode.MANUAL:
            raise _Abort(
                _Failure(
                    error_code=str(TransferErrorCode.RESUME_IDENTITY_UNKNOWN),
                    message=(
                        f"partial file for {destination} cannot be proven to belong to the current "
                        "remote object (no strong ETag / Last-Modified); "
                        "manual intervention required"
                    ),
                    retryable=False,
                    keep_part=True,
                    detail={
                        "destination": str(destination),
                        "part_size": part_size,
                        "origin_uri": asset.origin_uri,
                    },
                ),
                state,
            )
        if decision.mode is ResumeMode.RESTART and part_size > 0:
            discarded_bytes = part_size
            if part.exists():
                part.unlink()
            sidecar_path = sidecar_path_for(destination)
            if sidecar_path.exists():
                sidecar_path.unlink()
            state.part_size = 0
            part_size = 0
            self._emit(
                task.task_id,
                TransferEventType.STAGING_CLEANUP,
                f"discarded unverifiable partial file ({decision.reason})",
                metadata={"reason": decision.reason, "discarded_bytes": discarded_bytes},
            )

        offset = decision.offset if decision.mode is ResumeMode.RESUME else 0
        state.resumed = offset > 0
        if state.resumed:
            self._emit(
                task.task_id,
                TransferEventType.RESUMED,
                f"resuming at byte {offset} ({decision.reason})",
                bytes_transferred=offset,
                metadata={"offset": offset, "reason": decision.reason},
            )

        self._check_cancel(cancel_event, state)
        with self._open_stream(
            asset, offset=offset, expected_total=state.expected_size, cancel_event=cancel_event
        ) as stream:
            if stream.http_status is not None:
                self._emit(
                    task.task_id,
                    TransferEventType.HTTP_STATUS,
                    f"GET status={stream.http_status} for {asset.origin_uri}",
                    metadata={
                        "method": "GET",
                        "status_code": stream.http_status,
                        "url": asset.origin_uri,
                        "range_offset": offset,
                    },
                )
            if stream.resume_rejection_reason is not None:
                state.resumed = False
                if part.exists():
                    part.unlink()
                state.part_size = 0
                self._emit(
                    task.task_id,
                    TransferEventType.RESUME_REJECTED,
                    f"server rejected range resume: {stream.resume_rejection_reason}",
                    metadata={"reason": stream.resume_rejection_reason},
                )
            elif offset > 0 and stream.start_offset != offset:
                state.resumed = False
                if part.exists():
                    part.unlink()
                state.part_size = 0
                self._emit(
                    task.task_id,
                    TransferEventType.RESUME_REJECTED,
                    "server started at an unexpected offset; restarting from zero",
                    metadata={"expected_offset": offset, "actual_offset": stream.start_offset},
                )

            if stream.expected_total_size is not None:
                state.expected_size = stream.expected_total_size
            start_offset = 0 if stream.start_offset != offset else offset
            state.part_size = start_offset
            self._write_sidecar_before_stream(task, destination, state, probe, asset, start_offset)
            self._stream_to_part(stream, part, task, cancel_event, state, start_offset)

        return self._finalize_success(asset, destination, task, state, metadata)

    def _write_sidecar_before_stream(
        self,
        task: AcquisitionTask,
        destination: Path,
        state: _AttemptState,
        probe: ResumeProbe,
        asset: SourceAsset,
        start_offset: int,
    ) -> None:
        """在开始写字节前落 sidecar：进程被杀时下一次仍能自证身份。"""
        sidecar = PartSidecar(
            origin_uri=asset.origin_uri,
            transport_id=self.transport_id,
            task_id=task.task_id,
            expected_size=state.expected_size,
            etag=probe.etag,
            last_modified=probe.last_modified,
            size_written=start_offset,
            updated_at=self._clock(),
        )
        state.sidecar = sidecar
        try:
            write_sidecar(sidecar_path_for(destination), sidecar)
        except OSError as exc:
            # sidecar 写不进去只意味着“以后不可续传”，不阻止本次下载；但必须留痕。
            state.sidecar = None
            state.resume_possible = False
            self._emit(
                task.task_id,
                TransferEventType.STAGING_CLEANUP,
                f"cannot persist sidecar ({exc}); resumability disabled for this transfer",
                metadata={"reason": "sidecar_write_failed", "errno": exc.errno},
            )

    def _stream_to_part(
        self,
        stream: RemoteStream,
        part: Path,
        task: AcquisitionTask,
        cancel_event: threading.Event | None,
        state: _AttemptState,
        start_offset: int,
    ) -> None:
        """把流写入 ``.part``，校验取消/总超时/本地写失败。

        总超时（``transfer.timeout_seconds``）是**可选**的单次尝试预算：
        ``None`` 表示不限制总时长（默认，科研数据单文件数 GB，固定总时长会把正常下载
        误判为超时），此时真卡死由 ``connect_timeout_seconds`` / ``read_timeout_seconds``
        兜底；有值时行为不变（命中抛 ``TIMEOUT_TOTAL``，可重试）。
        """
        timeout_seconds = self._config.timeout_seconds
        deadline: float | None = (
            None if timeout_seconds is None else state.started_monotonic + timeout_seconds
        )
        mode = "ab" if start_offset > 0 else "wb"
        try:
            handle = open(part, mode, buffering=0)  # noqa: SIM115 - 需要精确控制 flush/fsync
        except OSError as exc:
            raise StorageError(
                f"cannot open staging file {part}: {exc}",
                task_id=task.task_id,
                detail={
                    "error_code": str(TransferErrorCode.STORAGE_WRITE_FAILED),
                    "path": str(part),
                    "errno": exc.errno,
                },
            ) from exc
        with handle:
            written = start_offset
            if start_offset > 0:
                position = handle.tell()
                if position != start_offset:
                    raise StorageError(
                        f"staging file {part} changed size unexpectedly "
                        f"({position} != {start_offset})",
                        task_id=task.task_id,
                        detail={
                            "error_code": str(TransferErrorCode.STORAGE_PART_INCONSISTENT),
                            "path": str(part),
                        },
                    )
            iterator = stream.chunks
            while True:
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    translated = self._translate_exception(exc)
                    if translated is None:
                        raise
                    raise _Abort(self._failure_from_transport(translated, state), state) from exc
                if cancel_event is not None and cancel_event.is_set():
                    raise _Abort(
                        _Failure(
                            error_code=str(TransferErrorCode.CANCELLED),
                            message="transfer cancelled by caller",
                            retryable=False,
                            keep_part=state.resume_possible,
                            cancelled=True,
                        ),
                        state,
                    )
                if deadline is not None and self._monotonic() > deadline:
                    raise _Abort(
                        _Failure(
                            error_code=str(TransferErrorCode.TIMEOUT_TOTAL),
                            message=f"transfer exceeded total timeout {timeout_seconds}s",
                            retryable=True,
                            keep_part=state.resume_possible,
                            condition="timeout",
                            detail={"timeout_seconds": timeout_seconds},
                        ),
                        state,
                    )
                if not chunk:
                    continue
                try:
                    handle.write(chunk)
                except OSError as exc:
                    raise StorageError(
                        f"failed writing staging file {part}: {exc}",
                        task_id=task.task_id,
                        detail={
                            "error_code": str(TransferErrorCode.STORAGE_WRITE_FAILED),
                            "path": str(part),
                            "errno": exc.errno,
                        },
                    ) from exc
                written += len(chunk)
                state.part_size = written
                self._notify_progress(task, part, state, written)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                raise StorageError(
                    f"failed flushing staging file {part}: {exc}",
                    task_id=task.task_id,
                    detail={
                        "error_code": str(TransferErrorCode.STORAGE_WRITE_FAILED),
                        "path": str(part),
                        "errno": exc.errno,
                    },
                ) from exc

    def _finalize_success(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        state: _AttemptState,
        metadata: RemoteMetadata,
    ) -> TransferResult:
        """大小严格校验 → 原子 rename → 清理 sidecar → 成功结果。"""
        part = part_path_for(destination)
        try:
            actual = part.stat().st_size
        except OSError as exc:
            raise StorageError(
                f"staging file {part} disappeared before commit: {exc}",
                task_id=task.task_id,
                detail={"error_code": str(TransferErrorCode.STORAGE_WRITE_FAILED)},
            ) from exc
        state.part_size = actual
        if state.expected_size is not None and actual != state.expected_size:
            raise _Abort(
                _Failure(
                    error_code=str(TransferErrorCode.SIZE_MISMATCH),
                    message=(
                        f"size mismatch for {asset.origin_uri}: expected "
                        f"{state.expected_size}, wrote {actual}"
                    ),
                    retryable=True,
                    keep_part=state.resume_possible,
                    condition="connection_reset",
                    detail={"expected": state.expected_size, "actual": actual},
                ),
                state,
            )
        self._emit(
            task.task_id,
            TransferEventType.BYTES_FLUSHED,
            f"flushed {actual} bytes to {part}",
            bytes_transferred=actual,
            metadata={"staged_path": str(part), "size_bytes": actual},
        )
        try:
            os.replace(part, destination)
        except OSError as exc:
            raise StorageError(
                f"atomic replace failed for {destination}: {exc}",
                task_id=task.task_id,
                detail={
                    "error_code": str(TransferErrorCode.STORAGE_REPLACE_FAILED),
                    "path": str(destination),
                    "errno": exc.errno,
                },
            ) from exc
        try:
            sidecar_path_for(destination).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._emit(
                task.task_id,
                TransferEventType.STAGING_CLEANUP,
                f"cannot remove sidecar after commit: {exc}",
                metadata={"reason": "sidecar_remove_failed", "errno": exc.errno},
            )
        duration = self._elapsed(state.started_monotonic)
        result = TransferResult(
            task_id=task.task_id,
            status=TransferStatus.SUCCEEDED,
            transport_id=self.transport_id,
            bytes_transferred=actual,
            duration_seconds=duration,
            destination_path=str(destination),
            checksum=None,
            error_code=None,
            completed_at=self._clock(),
            source_uri=asset.origin_uri,
            expected_size=state.expected_size,
            actual_size=actual,
            started_at=state.started_at,
            retries_used=state.attempt - 1,
            retryable=False,
            resumed=state.resumed,
        )
        self._emit(
            task.task_id,
            TransferEventType.COMPLETED,
            f"completed {actual} bytes to {destination} in {duration:.3f}s",
            bytes_transferred=actual,
            metadata={
                "destination": str(destination),
                "size_bytes": actual,
                "resumed": state.resumed,
                "attempt": state.attempt,
                "etag": metadata.etag,
            },
        )
        return result

    def _notify_progress(
        self, task: AcquisitionTask, part: Path, state: _AttemptState, written: int
    ) -> None:
        if self._on_progress is None:
            return
        update = ProgressUpdate(
            task_id=task.task_id,
            destination=part,
            attempt=state.attempt,
            bytes_written=written,
            expected_size=state.expected_size,
            elapsed_seconds=self._elapsed(state.started_monotonic),
            resumed=state.resumed,
        )
        self._on_progress(update)

    def _cleanup_staged(
        self,
        task_id: str,
        destination: Path,
        state: _AttemptState,
        *,
        keep: bool,
    ) -> None:
        """失败后的 ``.part`` 策略：可续传则保留并刷新 sidecar，否则彻底删除。"""
        part = part_path_for(destination)
        sidecar_path = sidecar_path_for(destination)
        if not part.exists() and not sidecar_path.exists():
            return
        if keep and part.exists():
            size_now = part.stat().st_size
            if state.sidecar is not None:
                updated = state.sidecar.model_copy(
                    update={"size_written": size_now, "updated_at": self._clock()}
                )
                try:
                    write_sidecar(sidecar_path, updated)
                except OSError as exc:
                    self._emit(
                        task_id,
                        TransferEventType.STAGING_CLEANUP,
                        f"cannot refresh sidecar for resumable partial: {exc}",
                        bytes_transferred=size_now,
                        metadata={"reason": "sidecar_refresh_failed", "errno": exc.errno},
                    )
            self._emit(
                task_id,
                TransferEventType.STAGING_CLEANUP,
                f"kept {size_now} partial bytes for a future resume",
                bytes_transferred=size_now,
                metadata={"kept": True, "size_bytes": size_now, "path": str(part)},
            )
            return
        discard_bytes = part.stat().st_size if part.exists() else 0
        remove_staged(destination)
        self._emit(
            task_id,
            TransferEventType.STAGING_CLEANUP,
            f"removed partial staging files ({discard_bytes} bytes)",
            metadata={"kept": False, "discarded_bytes": discard_bytes},
        )

    def _failure_from_transport(self, exc: TransportError, state: _AttemptState) -> _Failure:
        retryable = isinstance(exc, RetryableTransportError)
        raw_condition = exc.detail.get("condition")
        condition = raw_condition if isinstance(raw_condition, str) else None
        return _Failure(
            error_code=error_code_of(exc),
            message=str(exc),
            retryable=retryable,
            keep_part=retryable and state.resume_possible and state.part_size > 0,
            condition=condition,
            detail=dict(exc.detail),
            retry_after_seconds=exc.retry_after_seconds,
        )

    def _failure_from_storage(self, exc: StorageError, state: _AttemptState) -> _Failure:
        return _Failure(
            error_code=error_code_of(exc),
            message=str(exc),
            retryable=False,
            kind="storage",
            keep_part=False,
            detail=dict(exc.detail),
        )

    def _failure_from_oserror(self, exc: OSError, state: _AttemptState) -> _Failure:
        return _Failure(
            error_code=str(TransferErrorCode.STORAGE_WRITE_FAILED),
            message=f"local storage error: {exc}",
            retryable=False,
            kind="storage",
            keep_part=False,
            detail={"errno": exc.errno},
        )

    def _failure_result(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        *,
        started_at: datetime,
        started_monotonic: float,
        failure: _Failure,
        state: _AttemptState | None,
    ) -> TransferResult:
        part = part_path_for(destination)
        if part.exists():
            actual: int | None = part.stat().st_size
        elif destination.exists():
            actual = destination.stat().st_size
        else:
            actual = None
        retries_used = (state.attempt - 1) if state is not None else 0
        return TransferResult(
            task_id=task.task_id,
            status=TransferStatus.FAILED,
            transport_id=self.transport_id,
            bytes_transferred=state.part_size if state is not None else 0,
            duration_seconds=self._elapsed(started_monotonic),
            destination_path=str(destination),
            checksum=None,
            error_code=failure.error_code,
            error_detail=failure.message[:2048],
            retries_used=retries_used,
            completed_at=self._clock(),
            source_uri=asset.origin_uri,
            expected_size=state.expected_size if state is not None else None,
            actual_size=actual,
            started_at=started_at,
            retryable=failure.retryable,
            resumed=state.resumed if state is not None else False,
        )

    def _raise_terminal(
        self,
        asset: SourceAsset,
        destination: Path,
        task: AcquisitionTask,
        failure: _Failure,
        *,
        started_at: datetime,
        started_monotonic: float,
        state: _AttemptState | None,
    ) -> NoReturn:
        """构造失败结果并抛出对应的具体异常（不再返回）。"""
        result = self._failure_result(
            asset,
            destination,
            task,
            started_at=started_at,
            started_monotonic=started_monotonic,
            failure=failure,
            state=state,
        )
        self._emit(
            task.task_id,
            TransferEventType.FAILED,
            f"{failure.error_code}: {failure.message}",
            bytes_transferred=result.bytes_transferred,
            metadata={
                "error_code": failure.error_code,
                "retryable": failure.retryable,
                "cancelled": failure.cancelled,
                "attempt": state.attempt if state is not None else 0,
                "destination": str(destination),
            },
        )
        detail = dict(failure.detail)
        if failure.cancelled:
            raise TransferCancelledError(
                failure.message,
                result=result,
                task_id=task.task_id,
                source_id=asset.source_id,
                detail=detail,
            )
        if failure.retryable:
            raise RetryableTransferError(
                failure.message,
                error_code=failure.error_code,
                result=result,
                task_id=task.task_id,
                source_id=asset.source_id,
                detail=detail,
                retry_after_seconds=failure.retry_after_seconds,
            )
        if failure.kind == "storage":
            raise StorageTransferError(
                failure.message,
                error_code=failure.error_code,
                result=result,
                task_id=task.task_id,
                source_id=asset.source_id,
                detail=detail,
            )
        raise PermanentTransferError(
            failure.message,
            error_code=failure.error_code,
            result=result,
            task_id=task.task_id,
            source_id=asset.source_id,
            detail=detail,
            retry_after_seconds=failure.retry_after_seconds,
        )

    def _check_cancel(self, cancel_event: threading.Event | None, state: _AttemptState) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _Abort(
                _Failure(
                    error_code=str(TransferErrorCode.CANCELLED),
                    message="transfer cancelled by caller",
                    retryable=False,
                    keep_part=state.resume_possible,
                    cancelled=True,
                ),
                state,
            )

    def _elapsed(self, started_monotonic: float) -> float:
        return max(0.0, self._monotonic() - started_monotonic)
