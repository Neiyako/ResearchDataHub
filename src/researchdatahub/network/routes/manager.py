"""RouteManager：注册链路、按**实测**数据选路、记录真实样本、给出可审计的被拒原因。

选路流程（``select``）
----------------------
1. **过滤**：``enabled``、数据集白名单、提供方匹配、节点健康（``offline`` 直接排除，
   ``degraded`` 保留但降权）、节点能力声明的协议白名单、单文件大小上限、并发占用。
   每一条被排除的链路都产生一条 :class:`RouteRejection`，``code`` 是机器可读原因。
2. **排序**：有实测样本的链路按 :func:`~researchdatahub.network.routes.stats.score_route`
   的分数降序；无样本的链路整体排在后面（``basis=no_measurement``），同组内按
   ``worker_status``（healthy 先）与 ``route_id`` 字典序稳定排序。
3. **回传**：:class:`RouteSelection` 同时给出排序后的候选与被拒清单；**绝不隐藏拒绝原因**
   ——"为什么不用这条链路"是运维要看的核心信息。

记录真实样本（``record_attempt``）
--------------------------------
每次真实传输结束都调用一次（成功/失败都要），``success_rate`` / ``throughput_bps`` /
``latency_ms`` / ``failure_rate`` / ``retry_count`` / ``transfer_duration_s`` /
``transferred_bytes`` / ``samples`` 全部由这里累计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from researchdatahub.network.routes.models import (
    NO_MEASUREMENT,
    MeasurementBasis,
    Route,
    RouteOption,
    RouteRejection,
    RouteRejectionCode,
    RouteSelection,
    RouteStats,
)
from researchdatahub.network.routes.stats import RouteStatsStore, score_route
from researchdatahub.network.workers.models import WorkerRecord, WorkerStatus
from researchdatahub.network.workers.registry import WorkerRegistry

__all__ = [
    "DEFAULT_DEGRADED_SCORE_FACTOR",
    "NoRouteAvailable",
    "RouteManager",
    "RouteRecord",
]

DEFAULT_DEGRADED_SCORE_FACTOR: Final[float] = 0.7
"""``degraded`` 节点的实测得分折减系数（可用但排在健康节点之后）。"""


class NoRouteAvailable(Exception):
    """没有任何链路可以承接该请求（结构化错误，携带完整被拒原因）。"""

    def __init__(
        self,
        *,
        provider_id: str,
        dataset_id: str,
        size_bytes: int,
        rejected: tuple[RouteRejection, ...],
        message: str | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.dataset_id = dataset_id
        self.size_bytes = size_bytes
        self.rejected = rejected
        summary = message or (
            f"no route available for provider={provider_id!r} dataset={dataset_id!r} "
            f"size={size_bytes}: {len(rejected)} candidate(s) rejected"
        )
        super().__init__(summary)
        self.code = "RDH_NO_ROUTE_AVAILABLE"

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示（MCP / CLI 直接展示）。"""
        return {
            "error": "NoRouteAvailable",
            "code": self.code,
            "message": str(self),
            "provider_id": self.provider_id,
            "dataset_id": self.dataset_id,
            "size_bytes": self.size_bytes,
            "rejected": [item.as_dict() for item in self.rejected],
        }


@dataclass(frozen=True, slots=True)
class RouteRecord:
    """一次样本写入的返回值（便于调用方断言"确实落库了"）。"""

    route_id: str
    stats: RouteStats


class RouteManager:
    """链路的注册表 + 选择器 + 实测统计写入器。

    Parameters
    ----------
    registry:
        Worker 注册表（用于健康度与能力过滤）。
    stats:
        链路统计存储（写入真实样本）。
    degraded_score_factor:
        ``degraded`` 节点的得分折减系数。
    """

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        stats: RouteStatsStore,
        degraded_score_factor: float = DEFAULT_DEGRADED_SCORE_FACTOR,
    ) -> None:
        if not 0.0 < degraded_score_factor <= 1.0:
            raise ValueError("degraded_score_factor must be in (0, 1]")
        self._registry = registry
        self._stats = stats
        self._degraded_factor = degraded_score_factor
        self._routes: dict[str, Route] = {}
        self._in_flight: dict[str, int] = {}

    # -- 注册表 ------------------------------------------------------------ #
    def add(self, route: Route) -> Route:
        """登记/覆盖一条链路（``route_id`` 为幂等键）。"""
        self._routes[route.route_id] = route
        return route

    def add_many(self, routes: list[Route]) -> tuple[Route, ...]:
        """批量登记，返回登记结果。"""
        return tuple(self.add(route) for route in routes)

    def remove(self, route_id: str) -> bool:
        """移除一条链路（返回是否真的删除）。"""
        return self._routes.pop(route_id, None) is not None

    def get(self, route_id: str) -> Route:
        """读取链路。

        Raises
        ------
        KeyError
            链路未登记。
        """
        return self._routes[route_id]

    def list_routes(self, *, worker_id: str | None = None) -> tuple[Route, ...]:
        """列出链路（按 ``route_id`` 升序，可按节点过滤）。"""
        routes = sorted(self._routes.values(), key=lambda item: item.route_id)
        if worker_id is not None:
            routes = [route for route in routes if route.worker_id == worker_id]
        return tuple(routes)

    @property
    def stats_store(self) -> RouteStatsStore:
        """底层统计存储（只读用途）。"""
        return self._stats

    # -- 选择 -------------------------------------------------------------- #
    def select(
        self,
        *,
        provider_id: str,
        dataset_id: str,
        size_bytes: int,
        target: str | None = None,
        exclude: frozenset[str] = frozenset(),
        include_degraded: bool = True,
        worker_records: dict[str, WorkerRecord] | None = None,
    ) -> RouteSelection:
        """按实测数据对候选链路排序；没有任何候选时抛 :class:`NoRouteAvailable`。

        Parameters
        ----------
        provider_id:
            数据提供方（对应资产 ``source_id``）。
        dataset_id:
            逻辑数据集 ID（用于链路的数据集白名单）。
        size_bytes:
            单文件字节数（用于大小上限与规模惩罚）。
        target:
        调用方关注的目标描述（仅透传进结果，便于 MCP 展示）。
        exclude:
            本次已失败的链路（故障转移时传入）。
        include_degraded:
            是否允许 ``degraded`` 节点参与（``False`` 时只留 ``healthy``）。
        worker_records:
            节点快照（``None`` 时实时从注册表读取；故障转移可传入一次快照复用）。

        Raises
        ------
        NoRouteAvailable
            全部候选都被拒绝。
        """
        records = (
            worker_records
            if worker_records is not None
            else {record.worker_id: record for record in self._registry.list_workers()}
        )
        rejected: list[RouteRejection] = []
        scored: list[tuple[RouteOption, str]] = []
        for route in self.list_routes():
            reason = self._reject(
                route,
                provider_id=provider_id,
                dataset_id=dataset_id,
                size_bytes=size_bytes,
                exclude=exclude,
                include_degraded=include_degraded,
                records=records,
            )
            if reason is not None:
                rejected.append(reason)
                continue
            record = records[route.worker_id]
            stats = self._stats.get(route.route_id, provider_id=provider_id) or RouteStats(
                route_id=route.route_id, provider_id=provider_id
            )
            score, rationale = score_route(
                stats, supports_resume=route.supports_resume, size_bytes=size_bytes
            )
            if record.health.status is WorkerStatus.DEGRADED and score is not None:
                score = score * self._degraded_factor
                rationale += " (degraded worker: score scaled)"
            basis = MeasurementBasis.NO_MEASUREMENT if score is None else MeasurementBasis.MEASURED
            option = RouteOption(
                route=route,
                stats=stats,
                score=score,
                basis=basis,
                worker_status=record.health.status.value,
                rationale=rationale,
            )
            scored.append((option, ""))
        if not scored:
            raise NoRouteAvailable(
                provider_id=provider_id,
                dataset_id=dataset_id,
                size_bytes=size_bytes,
                rejected=tuple(rejected),
            )
        ordered = sorted(
            (item for item, _ in scored),
            key=lambda option: (
                0 if option.basis is MeasurementBasis.MEASURED else 1,
                -(option.score if option.score is not None else 0.0),
                0 if option.worker_status == WorkerStatus.HEALTHY.value else 1,
                option.route.route_id,
            ),
        )
        return RouteSelection(
            provider_id=provider_id,
            dataset_id=dataset_id,
            size_bytes=size_bytes,
            target=target,
            basis=ordered[0].basis,
            options=ordered,
            rejected=rejected,
        )

    def _reject(
        self,
        route: Route,
        *,
        provider_id: str,
        dataset_id: str,
        size_bytes: int,
        exclude: frozenset[str],
        include_degraded: bool,
        records: dict[str, WorkerRecord],
    ) -> RouteRejection | None:
        """判断一条链路是否被排除；返回结构化原因或 ``None``（通过）。"""

        def reject(code: RouteRejectionCode, detail: str) -> RouteRejection:
            return RouteRejection(route_id=route.route_id, code=code, detail=detail)

        if not route.enabled:
            return reject(RouteRejectionCode.DISABLED, "route is disabled")
        if route.route_id in exclude:
            return reject(
                RouteRejectionCode.DISABLED, "route excluded after a failure in this attempt"
            )
        if not route.supports_protocol:
            return reject(
                RouteRejectionCode.UNSUPPORTED_PROTOCOL,
                f"protocol {route.protocol!r} is not implemented",
            )
        if route.dataset_ids and dataset_id not in route.dataset_ids:
            return reject(
                RouteRejectionCode.DATASET_MISMATCH,
                f"dataset {dataset_id!r} outside route allowlist {route.dataset_ids}",
            )
        if route.provider_id != provider_id:
            return reject(
                RouteRejectionCode.PROVIDER_MISMATCH,
                f"route serves provider {route.provider_id!r}, requested {provider_id!r}",
            )
        record = records.get(route.worker_id)
        if record is None:
            return reject(
                RouteRejectionCode.WORKER_OFFLINE, f"worker {route.worker_id!r} is not registered"
            )
        if record.capabilities.protocols and route.protocol not in record.capabilities.protocols:
            return reject(
                RouteRejectionCode.PROTOCOL_NOT_SUPPORTED_BY_WORKER,
                f"worker {route.worker_id!r} does not declare protocol {route.protocol!r}",
            )
        if record.health.status is WorkerStatus.OFFLINE:
            return reject(
                RouteRejectionCode.WORKER_OFFLINE,
                f"worker {route.worker_id!r} is offline (heartbeat timeout)",
            )
        if record.health.status is WorkerStatus.DEGRADED and not include_degraded:
            return reject(
                RouteRejectionCode.WORKER_OFFLINE,
                f"worker {route.worker_id!r} is degraded and degraded use is disabled",
            )
        if record.capabilities.max_bytes and size_bytes > record.capabilities.max_bytes:
            return reject(
                RouteRejectionCode.SIZE_EXCEEDS_LIMIT,
                f"size {size_bytes} exceeds worker max_bytes {record.capabilities.max_bytes}",
            )
        if self._in_flight.get(route.route_id, 0) >= route.max_concurrent:
            return reject(
                RouteRejectionCode.CONCURRENCY_EXHAUSTED,
                f"route concurrency {route.max_concurrent} already in use",
            )
        return None

    # -- 并发记账 ---------------------------------------------------------- #
    def acquire(self, route_id: str) -> None:
        """占用一个链路并发名额（调用方必须在 ``finally`` 里 :meth:`release`）。"""
        self._in_flight[route_id] = self._in_flight.get(route_id, 0) + 1

    def release(self, route_id: str) -> None:
        """释放一个链路并发名额（幂等下界为 0）。"""
        current = self._in_flight.get(route_id, 0)
        if current <= 1:
            self._in_flight.pop(route_id, None)
        else:
            self._in_flight[route_id] = current - 1

    @property
    def in_flight(self) -> dict[str, int]:
        """当前各链路的在途计数（只读快照）。"""
        return dict(self._in_flight)

    # -- 实测记录 ---------------------------------------------------------- #
    def record_attempt(
        self,
        route: Route,
        *,
        success: bool,
        latency_ms: float,
        duration_s: float,
        bytes_transferred: int,
        retried: bool,
        outcome: str,
        provider_id: str | None = None,
        at: datetime | None = None,
    ) -> RouteRecord:
        """写入一次**真实**传输样本（与 :meth:`RouteStatsStore.record` 等价，带异常包装）。"""
        stats = self._stats.record(
            route,
            success=success,
            latency_ms=latency_ms,
            duration_s=duration_s,
            bytes_transferred=bytes_transferred,
            retried=retried,
            outcome=outcome,
            provider_id=provider_id,
            at=at,
        )
        self._stats.audit_event(
            route.worker_id,
            "route_sample",
            {
                "route_id": route.route_id,
                "provider_id": stats.provider_id,
                "success": success,
                "latency_ms": round(latency_ms, 3),
                "duration_s": round(duration_s, 6),
                "bytes": bytes_transferred,
                "retried": retried,
                "outcome": outcome,
            },
        )
        return RouteRecord(route_id=route.route_id, stats=stats)

    def stats_for(self, route_id: str, *, provider_id: str | None = None) -> RouteStats:
        """读取链路统计（无样本时返回全零统计，``samples == 0``）。"""
        found = self._stats.get(route_id, provider_id=provider_id)
        if found is not None:
            return found
        return RouteStats(route_id=route_id, provider_id=provider_id or "")

    def status(self) -> dict[str, object]:
        """``get_route_status`` 的数据来源：链路 + 统计 + 节点健康。"""
        records = {record.worker_id: record for record in self._registry.list_workers()}
        routes = []
        for route in self.list_routes():
            record = records.get(route.worker_id)
            stats = self.stats_for(route.route_id)
            routes.append(
                {
                    "route_id": route.route_id,
                    "protocol": route.protocol,
                    "worker_id": route.worker_id,
                    "provider_id": route.provider_id,
                    "worker_status": None if record is None else record.health.status.value,
                    "supports_resume": route.supports_resume,
                    "max_concurrent": route.max_concurrent,
                    "in_flight": self._in_flight.get(route.route_id, 0),
                    "measurement": NO_MEASUREMENT if stats.samples == 0 else "measured",
                    "stats": stats.as_dict(),
                }
            )
        return {"routes": routes}
