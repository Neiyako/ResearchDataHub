"""传输过程事件：把每一次传输的关键动作变成不可变的 :class:`TransferLogEvent`。

为什么要事件而不是散落的 logging 字符串
--------------------------------------
上层（调度器 / Manifest）需要在任务结束后回答「这次传输到底发生了什么」：发了几次
请求、Range 探测结果如何、第几次重试、最终错误码是什么。这些信息必须能被写入
``manifest.transfer_logs`` 并参与事后复盘，因此这里产生的是**结构化事件对象**，
而不是只给人看的日志行。

设计
----
* :class:`TransferEventSink` 是线程安全的（同一个适配器实例可被多个 worker 并发使用），
  内部用锁保护定长 ``deque``，避免长跑进程无限增长（超出容量的旧事件被丢弃）。
* 事件时间来自可注入的 ``clock``，测试可以给出固定时间，不依赖真实时钟。
* ``callback`` 是可选出口：调度器可以把它接到 Manifest 的 ``append_event`` 上，
  直接流式落库；不接回调时事件留在内存里供本进程读取。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock

from researchdatahub.core.models import JsonValue, TransferLogEvent

__all__ = ["DEFAULT_EVENT_CAPACITY", "TransferEventSink", "TransferEventType"]

DEFAULT_EVENT_CAPACITY = 512
"""单个适配器保留的事件上限。一次传输通常 10~30 条，512 足够容纳多次任务。"""


class TransferEventType(StrEnum):
    """稳定的机器可读事件类型（与 :mod:`researchdatahub.core.models` 的约定一致）。"""

    STARTED = "started"
    HTTP_STATUS = "http_status"
    RANGE_PROBE = "range_probe"
    RESUME_DECISION = "resume_decision"
    RESUMED = "resumed"
    RESUME_REJECTED = "resume_rejected"
    RETRY_SCHEDULED = "retry_scheduled"
    BYTES_FLUSHED = "bytes_flushed"
    STAGING_CLEANUP = "staging_cleanup"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPLETED = "completed"


class TransferEventSink:
    """线程安全的事件收集器，可选用回调把事件推给上层。

    Parameters
    ----------
    callback:
        可选的事件消费者（例如写入 Manifest）。回调**在调用线程内同步执行**，
        实现必须自行保证线程安全；回调抛出的异常不会被吞掉，会中断当前传输。
    capacity:
        内存中最多保留的事件数；超出后丢弃最旧的事件。回调不受该上限影响。
    clock:
        时间源，默认 :func:`datetime.now` (UTC)；测试可注入固定时钟。
    """

    def __init__(
        self,
        *,
        callback: Callable[[TransferLogEvent], None] | None = None,
        capacity: int = DEFAULT_EVENT_CAPACITY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("event capacity must be >= 1")
        self._callback = callback
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events: deque[TransferLogEvent] = deque(maxlen=capacity)
        self._lock = Lock()
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """因容量上限被丢弃的事件数量（用于诊断“日志丢了”而不是假装没发生）。"""
        with self._lock:
            return self._dropped

    def emit(
        self,
        task_id: str,
        event_type: TransferEventType,
        message: str,
        *,
        bytes_transferred: int = 0,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> TransferLogEvent:
        """构造并记录一条事件，返回该事件对象。

        Parameters
        ----------
        task_id:
            事件归属的任务。
        event_type:
            稳定的类型枚举。
        message:
            人类可读说明（写入 manifest，不需要翻译）。
        bytes_transferred:
            事件发生时累计已写字节数（无意义时保持 0）。
        metadata:
            结构化上下文（状态码、Range、退避秒数等），必须可 JSON 序列化。
        """
        event = TransferLogEvent(
            task_id=task_id,
            event_type=str(event_type),
            message=message,
            bytes_transferred=bytes_transferred,
            metadata=dict(metadata or {}),
            timestamp=self._clock(),
        )
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(event)
        if self._callback is not None:
            self._callback(event)
        return event

    def snapshot(self) -> tuple[TransferLogEvent, ...]:
        """返回当前保留的事件副本（按发生顺序）。"""
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        """清空内存事件（回调不受影响）。用于长跑进程中复用同一个 sink。"""
        with self._lock:
            self._events.clear()
            self._dropped = 0
