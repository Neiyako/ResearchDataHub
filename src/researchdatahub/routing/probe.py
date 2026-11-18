"""低成本 route probe。

默认实现只调用数据源适配器的 ``health_check()``，不提交科研数据请求、不调用
``discover_assets()``、不下载文件。它回答的是“endpoint/metadata 服务当前是否可达”，
因此**不会**仅凭一次健康检查就把变量/时间/空间能力判成 PASS。需要证明这些维度时，
调用方应注入 source-specific prober 或 benchmarker，并把证据写进 ``ProbeResult``。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Protocol

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import DatasetRequest
from researchdatahub.core.ports import DataSourceAdapter
from researchdatahub.routing.models import (
    AdapterImplementationStatus,
    BenchmarkResult,
    CheckStatus,
    ProbeResult,
    Route,
    RouteCapabilities,
)

__all__ = ["DEFAULT_MAX_PROBE_BYTES", "ConnectivityProbe", "DefaultConnectivityProbe"]


class ConnectivityProbe(Protocol):
    """可注入的 route probe 接口（测试使用 fake，生产可接源专属探针）。"""

    def probe(
        self,
        route: Route,
        request: DatasetRequest,
        adapter: DataSourceAdapter | None,
    ) -> ProbeResult:
        """执行一次低字节量探活。"""
        ...


DEFAULT_MAX_PROBE_BYTES: Final[int] = 8 * 1024**2
"""默认单次 benchmark 的最大探针字节数；禁止把 GB 级科研数据当探活。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DefaultConnectivityProbe:
    """只做 endpoint health check 的保守探针。

    Parameters
    ----------
    benchmark:
        可选的小文件传输基准函数。只有在调用方明确提供时才执行，避免默认探针下载数据。
    clock/timer:
        可注入时钟，便于测试与确定性日志。
    """

    def __init__(
        self,
        *,
        benchmark: Callable[[Route, DatasetRequest, DataSourceAdapter], BenchmarkResult]
        | None = None,
        max_probe_bytes: int = DEFAULT_MAX_PROBE_BYTES,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        self._benchmark = benchmark
        self._max_probe_bytes = max_probe_bytes
        self._clock = clock or _utc_now
        self._timer = timer or time.monotonic

    def probe(
        self,
        route: Route,
        request: DatasetRequest,
        adapter: DataSourceAdapter | None,
    ) -> ProbeResult:
        """执行一次只读健康检查；不把未知能力伪造成通过。"""
        if route.adapter_status is AdapterImplementationStatus.NOT_IMPLEMENTED:
            return ProbeResult(
                route_id=route.id,
                status=CheckStatus.NOT_IMPLEMENTED,
                detail=(
                    "RDH has no implemented adapter for this route; this says nothing "
                    "about whether the source provides the dataset"
                ),
                checked_at=self._clock(),
            )
        if adapter is None:
            return ProbeResult(
                route_id=route.id,
                status=CheckStatus.UNKNOWN,
                detail=(
                    "route was discovered from configuration but no adapter instance is available"
                ),
                checked_at=self._clock(),
            )

        adapter_caps = None
        capabilities_fn = getattr(adapter, "capabilities", None)
        if callable(capabilities_fn):
            try:
                adapter_caps = capabilities_fn()
            except RdhError as exc:
                return ProbeResult(
                    route_id=route.id,
                    status=CheckStatus.UNKNOWN,
                    detail=f"adapter capability probe failed: {exc.message}",
                    checked_at=self._clock(),
                )
        missing_credentials = tuple(getattr(adapter_caps, "missing_credentials", ()))
        if missing_credentials:
            return ProbeResult(
                route_id=route.id,
                status=CheckStatus.FAIL,
                dns_ok=None,
                endpoint_ok=None,
                auth_ok=False,
                response_valid=False,
                detail=(
                    "adapter reports missing credentials: "
                    + ", ".join(str(item) for item in missing_credentials)
                ),
                checked_at=self._clock(),
            )

        started = self._timer()
        try:
            healthy = bool(adapter.health_check())
        except RdhError as exc:
            elapsed_ms = max(0.0, (self._timer() - started) * 1000.0)
            return ProbeResult(
                route_id=route.id,
                status=CheckStatus.FAIL,
                endpoint_ok=False,
                response_valid=False,
                latency_ms=elapsed_ms,
                detail=f"health check failed: {exc.message}",
                checked_at=self._clock(),
            )
        elapsed_ms = max(0.0, (self._timer() - started) * 1000.0)
        if not healthy:
            return ProbeResult(
                route_id=route.id,
                status=CheckStatus.FAIL,
                endpoint_ok=False,
                response_valid=False,
                latency_ms=elapsed_ms,
                detail="source health check returned False",
                checked_at=self._clock(),
            )

        capabilities: RouteCapabilities = route.capabilities.model_copy(
            update={"authentication": CheckStatus.NOT_TESTED}
        )
        benchmark: BenchmarkResult | None = None
        if self._benchmark is not None:
            benchmark = self._benchmark(route, request, adapter)
            if benchmark.probe_size > self._max_probe_bytes:
                return ProbeResult(
                    route_id=route.id,
                    status=CheckStatus.FAIL,
                    endpoint_ok=True,
                    metadata_ok=True,
                    response_valid=False,
                    latency_ms=elapsed_ms,
                    benchmark=benchmark,
                    detail=(
                        f"benchmark probe_size={benchmark.probe_size} exceeds "
                        f"max_probe_bytes={self._max_probe_bytes}"
                    ),
                    checked_at=self._clock(),
                )
            capabilities = capabilities.model_copy(
                update={
                    "format": CheckStatus.PASS if benchmark.success else CheckStatus.FAIL,
                    "transport": CheckStatus.PASS if benchmark.success else CheckStatus.FAIL,
                    "authentication": CheckStatus.PASS if benchmark.success else CheckStatus.FAIL,
                }
            )
        return ProbeResult(
            route_id=route.id,
            status=CheckStatus.PASS,
            dns_ok=True,
            endpoint_ok=True,
            metadata_ok=True,
            request_ok=True,
            response_valid=True,
            latency_ms=elapsed_ms,
            benchmark=benchmark,
            capabilities=capabilities,
            detail=(
                "endpoint health check passed; scientific capability dimensions remain "
                "UNKNOWN/NOT_TESTED until a source-specific probe provides evidence"
            ),
            checked_at=self._clock(),
        )
