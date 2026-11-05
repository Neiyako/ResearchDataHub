"""网络检查子包：只读 HEAD/GET 轻量探测，绝不触发检索、绝不使用 cdsapi。

公开入口
--------
* :class:`~researchdatahub.environment.network.checker.NetworkChecker`
* :data:`~researchdatahub.environment.network.checker.DEFAULT_TIMEOUT_SECONDS`
"""

from researchdatahub.environment.network.checker import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    NetworkChecker,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "NetworkChecker",
]
