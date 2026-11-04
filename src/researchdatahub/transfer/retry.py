"""重试策略：错误分类、指数退避、上限与抖动。

分工
----
* 传输适配器只负责**把异常翻译成语义正确的异常类型**（可重试 / 不可重试）；
* :class:`RetryPolicy` 负责“要不要再试一次、等多久”。
* 抖动随机源与 sleep 都可注入：测试给出固定随机数/记录器，不依赖真实等待；
  生产默认使用 ``random.random`` 与 ``time.sleep``。

``RetryConfig.retry_on`` 的语义
------------------------------
它是**传输层内部自动重试**的条件白名单，取值 ``timeout`` / ``connection_reset`` /
``http_5xx`` / ``http_429``。某类错误即使本身是“可重试”的，只要对应条件不在白名单里，
适配器就立刻放弃并把 ``TransferResult.retryable=True`` 交给调度器决定后续动作
（配置控制的是“本层重试”，不是“这个错误是否可恢复”）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Collection
from enum import StrEnum
from typing import Final

from researchdatahub.config.models import RetryConfig
from researchdatahub.core.errors import RetryableTransportError, TransportError

__all__ = [
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_CONDITIONS",
    "HttpStatusClass",
    "RetryPolicy",
    "classify_http_status",
    "condition_for_http_status",
]

DEFAULT_MAX_ATTEMPTS: Final[int] = 3
DEFAULT_BACKOFF_SECONDS: Final[float] = 5.0
DEFAULT_BACKOFF_MAX_SECONDS: Final[float] = 300.0

DEFAULT_RETRY_CONDITIONS: Final[frozenset[str]] = frozenset(
    {"timeout", "connection_reset", "http_5xx", "http_429"}
)
"""与 ``RetryConfig.retry_on`` 的默认值一致；只有这些条件受白名单约束。"""

_HTTP_CONDITIONS: Final[dict[int, str]] = {
    408: "timeout",
    425: "timeout",
    429: "http_429",
    500: "http_5xx",
    501: "http_5xx",
    502: "http_5xx",
    503: "http_5xx",
    504: "http_5xx",
    507: "http_5xx",
    509: "http_5xx",
}


class HttpStatusClass(StrEnum):
    """HTTP 状态码的传输层语义分类。"""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"


def condition_for_http_status(status: int) -> str | None:
    """返回状态码对应的重试条件名（如 ``http_5xx``）；非错误码返回 ``None``。"""
    if status in _HTTP_CONDITIONS:
        return _HTTP_CONDITIONS[status]
    return None


def classify_http_status(status: int, retry_on: Collection[str]) -> HttpStatusClass:
    """把 HTTP 错误状态码分类为可重试 / 不可重试。

    Parameters
    ----------
    status:
        HTTP 状态码（调用方只应在 4xx/5xx 时调用；2xx/3xx 会抛 ``ValueError``，
        因为那不是“失败”）。
    retry_on:
        启用的重试条件白名单（来自 ``RetryConfig.retry_on``）。

    Raises
    ------
    ValueError
        传入的是成功/重定向状态码，说明调用方逻辑有缺陷。
    """
    if 200 <= status < 400:
        raise ValueError(f"HTTP {status} is not an error status")
    condition = condition_for_http_status(status)
    if condition is not None:
        return HttpStatusClass.RETRYABLE if condition in retry_on else HttpStatusClass.PERMANENT
    if 500 <= status < 600:
        # 未知 5xx 仍属于服务端瞬时故障，按 http_5xx 条件处理。
        return HttpStatusClass.RETRYABLE if "http_5xx" in retry_on else HttpStatusClass.PERMANENT
    return HttpStatusClass.PERMANENT


class RetryPolicy:
    """指数退避 + 抖动的重试策略。

    Parameters
    ----------
    max_attempts:
        总尝试次数（含首次）。``1`` 表示不重试。
    backoff_seconds:
        指数退避基数（第 n 次失败后的确定性等待 = ``base * 2**(n-1)``）。
    backoff_max_seconds:
        单次等待上限（抖动与 ``Retry-After`` 之后仍然封顶）。
    jitter:
        ``[0, 1]`` 的均匀抖动比例；0 表示不抖动。
    retry_on:
        传输层内部自动重试的条件白名单；``None`` 表示用默认集合。
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        jitter: float = 0.2,
        retry_on: Collection[str] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if backoff_seconds <= 0:
            raise ValueError("backoff_seconds must be > 0")
        if backoff_max_seconds < backoff_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_seconds")
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be within [0, 1]")
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._jitter = jitter
        self._retry_on = frozenset(DEFAULT_RETRY_CONDITIONS if retry_on is None else retry_on)

    @classmethod
    def from_config(cls, config: RetryConfig) -> RetryPolicy:
        """从配置模型构造（唯一推荐的构造方式：配置是策略的单一来源）。"""
        return cls(
            max_attempts=config.max_attempts,
            backoff_seconds=config.backoff_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
            jitter=config.jitter,
            retry_on=config.retry_on,
        )

    @property
    def max_attempts(self) -> int:
        """总尝试次数（含首次）。"""
        return self._max_attempts

    @property
    def backoff_seconds(self) -> float:
        """指数退避基数（秒）。"""
        return self._backoff_seconds

    @property
    def backoff_max_seconds(self) -> float:
        """单次退避上限（秒）。"""
        return self._backoff_max_seconds

    @property
    def jitter(self) -> float:
        """抖动比例。"""
        return self._jitter

    @property
    def retry_on(self) -> frozenset[str]:
        """启用的自动重试条件白名单。"""
        return self._retry_on

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的确定性等待（不含抖动，已按上限封顶）。

        ``attempt`` 从 1 开始：第 1 次失败后等待 ``backoff_seconds``。
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        raw = self._backoff_seconds * (2 ** (attempt - 1))
        return float(min(raw, self._backoff_max_seconds))

    def delay_for_attempt(
        self,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> float:
        """带抖动与 ``Retry-After`` 的实际等待秒数。

        * 抖动在 ``[1 - jitter, 1 + jitter]`` 区间内均匀缩放确定性退避，再按上限封顶；
        * 服务端 ``Retry-After`` 优先于本地退避（取较大者），但同样不得突破上限。

        Parameters
        ----------
        attempt:
            第几次失败（从 1 开始）。
        retry_after_seconds:
            服务端建议等待时间；``None`` 表示无建议。
        random_source:
            返回 ``[0, 1)`` 的随机源；``None`` 用 ``random.random``。测试注入固定值。
        """
        base = self.delay_for(attempt)
        source = random_source or random.random
        if self._jitter > 0.0:
            factor = 1.0 + (source() * 2.0 - 1.0) * self._jitter
            delay = base * factor
        else:
            delay = base
        if retry_after_seconds is not None:
            delay = max(delay, retry_after_seconds)
        return float(min(max(delay, 0.0), self._backoff_max_seconds))

    def allows_condition(self, condition: str | None, *, retryable: bool) -> bool:
        """给定“是否可重试 + 条件名”，判断白名单是否允许本层自动重试。

        未知条件（例如 ``s3_throttled``）不受白名单约束，只由 ``retryable`` 决定。
        """
        if not retryable:
            return False
        if condition is None or condition not in DEFAULT_RETRY_CONDITIONS:
            return True
        return condition in self._retry_on

    def should_retry(self, exc: BaseException) -> bool:
        """基于异常对象判断是否值得重试。

        传输层错误看 ``retryable`` 标志与 ``detail["condition"]``；未翻译的
        ``ConnectionError`` / ``TimeoutError`` 视为瞬时网络错误。
        """
        if isinstance(exc, TransportError):
            if not exc.retryable:
                return False
            raw_condition = exc.detail.get("condition")
            condition = raw_condition if isinstance(raw_condition, str) else None
            return self.allows_condition(condition, retryable=True)
        if isinstance(exc, TimeoutError):
            return self.allows_condition("timeout", retryable=True)
        if isinstance(exc, ConnectionError):
            return self.allows_condition("connection_reset", retryable=True)
        return False

    def sleep_for(
        self,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
        random_source: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> float:
        """等待并返回实际等待秒数；``sleeper`` 默认 ``time.sleep``。"""
        delay = self.delay_for_attempt(
            attempt, retry_after_seconds=retry_after_seconds, random_source=random_source
        )
        (sleeper or time.sleep)(delay)
        return delay

    def is_retryable_error(self, exc: BaseException) -> bool:
        """``should_retry`` 的别名式语义：异常本身是否被标记为可重试（不看白名单）。"""
        return isinstance(exc, RetryableTransportError)
