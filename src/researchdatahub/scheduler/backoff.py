"""重试退避：与 ``config.retry`` 单一来源，抖动可注入以保证测试确定性。

分工
----
* M2 的 ``RetryPolicy`` 负责**单次传输内部**的重试（网络抖动、5xx、429）；
* 本模块负责**调度层**的重试等待：任务进入 ``RETRY_WAIT`` 时计算 ``scheduled_at``，
  到点后由 ``claim_tasks`` 重新领取。两层重试互不替代：传输层重试用尽才把任务交回调度层。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from researchdatahub.config.models import RetryConfig

__all__ = ["BackoffPolicy"]


class BackoffPolicy:
    """指数退避 + 均匀抖动（``retry.backoff_seconds * 2**(attempt-1)``，封顶后加抖动）。"""

    def __init__(
        self, config: RetryConfig | None = None, *, random_source: Callable[[], float] | None = None
    ) -> None:
        self._config = config or RetryConfig()
        self._random = random_source or _default_random

    @property
    def config(self) -> RetryConfig:
        """底层重试配置（只读）。"""
        return self._config

    def delay_seconds(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待秒数（``attempt`` 从 1 开始，与 ``attempt`` 字段一致）。

        抖动范围 ``[(1-jitter), (1+jitter)]``，结果再做一次 ``backoff_max_seconds`` 封顶，
        保证不会出现负等待或无限膨胀。
        """
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        base = self._config.delay_for(attempt)
        jitter = self._config.jitter
        factor = 1.0 if jitter <= 0.0 else 1.0 + (self._random() * 2.0 - 1.0) * jitter
        return max(0.0, min(base * factor, self._config.backoff_max_seconds))

    def scheduled_at(self, now: datetime, attempt: int) -> datetime:
        """``now + delay``（tz-aware；调用方用同一时钟写库与展示）。"""
        return now + timedelta(seconds=self.delay_seconds(attempt))


def _default_random() -> float:
    """默认抖动源（``random.random`` 在 [0,1)）。"""
    import random

    return random.random()
