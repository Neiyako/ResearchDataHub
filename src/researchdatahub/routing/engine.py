"""Dataset Route Intelligence：发现、能力检查、探活、排序与下载闸门。

这个模块位于现有 source adapter 与 planner 之上，不替代它们：

* Source adapter 仍负责“数据在哪、元数据如何翻译”；
* DefaultTaskPlanner 仍负责把已选 source 的资产拆成任务；
* 本模块负责“在规划前先证明有哪些 routes，以及哪一条允许被使用”。

因此没有 ``if source_id == "cds"`` 的特殊优先级。CDS 只是普通 route，必须和 NCAR/
其他 source 一样经过能力矩阵、qualification 与实测指标。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from researchdatahub.config.models import (
    PlannerConfig,
    SourceConfig,
    SourcesConfig,
    TransportBinding,
)
from researchdatahub.core.errors import SourceNotFoundError
from researchdatahub.core.models import (
    AcquisitionPlan,
    DatasetRequest,
    JsonValue,
    RouteQualification,
)
from researchdatahub.core.ports import DataSourceAdapter
from researchdatahub.planner.planner import DefaultTaskPlanner
from researchdatahub.routing.capabilities import check_capabilities
from researchdatahub.routing.errors import (
    NoQualifiedRouteError,
    RouteExecutionError,
    RoutePolicyError,
)
from researchdatahub.routing.models import (
    AdapterImplementationStatus,
    CheckStatus,
    DownloadDecision,
    DownloadResult,
    ProbeResult,
    Route,
    RouteCapabilities,
    RouteDecision,
    RouteEvaluation,
    RoutePlan,
    RouteSelection,
    RouteStatus,
    WorkloadTier,
)
from researchdatahub.routing.probe import ConnectivityProbe, DefaultConnectivityProbe
from researchdatahub.routing.registry import RouteExperienceRegistry

__all__ = [
    "DEFAULT_LARGE_BYTES",
    "DEFAULT_MEDIUM_BYTES",
    "RouteIntelligence",
    "estimate_request_bytes",
    "workload_tier",
]

DEFAULT_MEDIUM_BYTES: Final[int] = 1024**3
DEFAULT_LARGE_BYTES: Final[int] = 100 * 1024**3
"""第一版分档：<=1 GiB 小任务，<=100 GiB 中任务，其余大任务。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text or "route"


def _json_list(value: JsonValue | None, *, floats: bool = False) -> list[str] | list[float]:
    if not isinstance(value, list):
        return []
    if floats:
        return [float(item) for item in value if isinstance(item, (int, float))]
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _extension_bytes(request: DatasetRequest) -> int | None:
    for source in (request.metadata.extensions, request.extensions):
        for key in ("routing.expected_bytes", "rdh.expected_bytes"):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def estimate_request_bytes(request: DatasetRequest) -> int:
    """估计传输规模；显式 ``routing.expected_bytes`` 优先。

    没有显式值时只做透明的量级估算（时间点数 × 网格点数 × 层数 × 变量数 × 4 bytes）。
    这个数字用于选择 policy 分档，不写进科学结果，也不冒充远端文件大小。
    """
    explicit = _extension_bytes(request)
    if explicit is not None:
        return explicit
    meta = request.metadata
    days = max(1.0, meta.temporal_coverage.duration_days())
    definition = meta.time_definition.lower()
    if "hour" in definition:
        steps_per_day = 24.0
    elif "day" in definition:
        steps_per_day = 1.0
    elif "month" in definition:
        steps_per_day = 1.0 / 30.44
    else:
        steps_per_day = 1.0
    grid = meta.grid.resolution_degrees or meta.spatial_extent.grid_degrees
    if grid and grid > 0:
        lon_cells = max(1.0, (meta.spatial_extent.east - meta.spatial_extent.west) / grid)
        lat_cells = max(1.0, (meta.spatial_extent.north - meta.spatial_extent.south) / grid)
    else:
        lon_cells, lat_cells = 1.0, 1.0
    levels = max(1, len(meta.vertical_levels.levels))
    variables = max(1, len(meta.variables))
    return max(1, int(days * steps_per_day * lon_cells * lat_cells * levels * variables * 4))


def workload_tier(
    request: DatasetRequest,
    *,
    medium_bytes: int = DEFAULT_MEDIUM_BYTES,
    large_bytes: int = DEFAULT_LARGE_BYTES,
) -> WorkloadTier:
    """按明确的字节阈值返回 small/medium/large；阈值可由组合根覆盖。"""
    expected = estimate_request_bytes(request)
    if expected > large_bytes:
        return WorkloadTier.LARGE
    if expected > medium_bytes:
        return WorkloadTier.MEDIUM
    return WorkloadTier.SMALL


class RouteIntelligence:
    """第一版 dataset route 规划与 policy 引擎。"""

    def __init__(
        self,
        *,
        sources: SourcesConfig | Mapping[str, SourceConfig] | None = None,
        adapters: Mapping[str, DataSourceAdapter] | Sequence[DataSourceAdapter] = (),
        registry: RouteExperienceRegistry | None = None,
        prober: ConnectivityProbe | None = None,
        clock: Callable[[], datetime] | None = None,
        qualification_ttl: timedelta = timedelta(days=14),
        medium_bytes: int = DEFAULT_MEDIUM_BYTES,
        large_bytes: int = DEFAULT_LARGE_BYTES,
        max_probe_routes: int = 8,
    ) -> None:
        if isinstance(sources, SourcesConfig):
            self._configs: dict[str, SourceConfig] = dict(sources.sources)
        else:
            self._configs = dict(sources or {})
        if isinstance(adapters, Mapping):
            self._adapters = dict(adapters)
        else:
            self._adapters = {adapter.source_id: adapter for adapter in adapters}
        self._registry = registry or RouteExperienceRegistry()
        self._prober = prober or DefaultConnectivityProbe()
        self._clock = clock or _utc_now
        self._qualification_ttl = qualification_ttl
        self._medium_bytes = medium_bytes
        self._large_bytes = large_bytes
        self._max_probe_routes = max_probe_routes
        self._seeded: dict[str, Route] = {}

    @property
    def registry(self) -> RouteExperienceRegistry:
        """经验注册表（只读访问器，便于 CLI 展示）。"""
        return self._registry

    def register_routes(self, routes: Sequence[Route]) -> tuple[Route, ...]:
        """登记显式 route 定义（测试、fixture 或外部 discovery 插件）。"""
        registered: list[Route] = []
        for route in routes:
            self._seeded[route.id] = route
            registered.append(self._registry.upsert(route).route)
        return tuple(registered)

    def plan(self, requirement: DatasetRequest) -> RoutePlan:
        """Discover candidate routes from source config/adapters and experience registry."""
        generated_at = self._clock()
        candidates: list[Route] = []
        attempts: list[dict[str, JsonValue]] = []
        seen: set[str] = set()

        for source_id, config in self._configs.items():
            routes = [
                self._route_from_config(source_id, config, requirement, binding)
                for binding in config.transports
            ]
            count = 0
            for route in routes:
                merged = self._merge_registry(route)
                if merged.id not in seen:
                    candidates.append(merged)
                    seen.add(merged.id)
                    count += 1
            if not config.enabled:
                attempt_status = "disabled"
            elif not config.implemented:
                attempt_status = "adapter_not_implemented"
            elif not config.usable_routes:
                attempt_status = "transport_not_implemented"
            else:
                attempt_status = "discovered"
            attempts.append(
                {
                    "source": source_id,
                    "status": attempt_status,
                    "routes": count,
                    "adapter_implemented": config.implemented,
                    "usable_transports": cast(
                        "JsonValue", sorted(item.value for item in config.usable_routes)
                    ),
                }
            )

        for source_id, adapter in self._adapters.items():
            if source_id in self._configs:
                continue
            adapter_config: object = getattr(adapter, "config", None)
            adapter_routes: Sequence[Route] = ()
            if isinstance(adapter_config, SourceConfig):
                adapter_routes = tuple(
                    self._route_from_config(source_id, adapter_config, requirement, binding)
                    for binding in adapter_config.transports
                )
            else:
                adapter_routes = tuple(
                    self._route_from_adapter(source_id, adapter, transport, requirement)
                    for transport in sorted(getattr(adapter, "supported_routes", ()))
                )
            added = 0
            for route in adapter_routes:
                merged = self._merge_registry(route)
                if merged.id not in seen:
                    candidates.append(merged)
                    seen.add(merged.id)
                    added += 1
            attempts.append(
                {
                    "source": source_id,
                    "status": "discovered" if added else "no_routes",
                    "routes": added,
                    "adapter_implemented": True,
                    "usable_transports": sorted(getattr(adapter, "supported_routes", ())),
                }
            )

        for route in self._seeded.values():
            if route.id in seen:
                continue
            candidates.append(self._merge_registry(route))
            seen.add(route.id)
        for record in self._registry.list_records():
            route = record.route
            if route.dataset == requirement.dataset_id and route.id not in seen:
                candidates.append(route)
                seen.add(route.id)

        notes: list[str] = []
        if not candidates:
            notes.append(
                "no route candidates were discovered; this does not mean the dataset is unavailable"
            )
        unimplemented = [
            route.id
            for route in candidates
            if route.adapter_status is AdapterImplementationStatus.NOT_IMPLEMENTED
        ]
        if unimplemented:
            notes.append(
                "adapter is not implemented for: "
                + ", ".join(sorted(unimplemented))
                + "; no data-unavailability conclusion is allowed"
            )
        transport_unimplemented = [
            route.id
            for route in candidates
            if route.capabilities.transport is CheckStatus.NOT_IMPLEMENTED
        ]
        if transport_unimplemented:
            notes.append(
                "transport is not implemented for: "
                + ", ".join(sorted(transport_unimplemented))
                + "; the source/dataset may still be valid through another route"
            )
        mismatched = [
            route.id for route in candidates if route.capability_summary is CheckStatus.FAIL
        ]
        if mismatched:
            notes.append("capability mismatch (known evidence): " + ", ".join(sorted(mismatched)))
        return RoutePlan(
            requirement=requirement,
            requirement_id=requirement.request_id,
            dataset=requirement.dataset_id,
            candidate_routes=candidates,
            source_attempts=attempts,
            generated_at=generated_at,
            notes=notes,
            extensions={
                "routing.workload": workload_tier(
                    requirement, medium_bytes=self._medium_bytes, large_bytes=self._large_bytes
                ).value,
                "routing.estimated_bytes": estimate_request_bytes(requirement),
            },
        )

    def evaluate(
        self,
        route_plan: RoutePlan,
        *,
        objective: str = "min_time",
        auto_probe: bool = True,
        allow_degraded: bool = False,
    ) -> RouteEvaluation:
        """检查全部候选 route，必要时执行低字节量 probe，并返回可解释排序。"""
        request = route_plan.requirement
        tier = workload_tier(
            request, medium_bytes=self._medium_bytes, large_bytes=self._large_bytes
        )
        evaluated: list[Route] = []
        probed = 0

        for candidate in route_plan.candidate_routes:
            route = candidate.model_copy(
                update={"capabilities": check_capabilities(request, candidate)}
            )
            if self._qualification_expired(route):
                route = route.model_copy(update={"qualification": RouteQualification.EXPIRED})
            summary = route.capability_summary
            if (
                auto_probe
                and probed < self._max_probe_routes
                and summary not in (CheckStatus.FAIL, CheckStatus.NOT_IMPLEMENTED)
                and (
                    route.qualification
                    in (RouteQualification.DISCOVERED, RouteQualification.EXPIRED)
                    or summary in (CheckStatus.UNKNOWN, CheckStatus.NOT_TESTED)
                )
            ):
                route = self._qualify_route(route, request)
                probed += 1
            evaluated.append(route)

        decisions: list[RouteDecision] = []
        eligible: list[tuple[RouteDecision, Route]] = []
        needs_probe: list[str] = []
        for route in evaluated:
            summary = route.capability_summary
            capability_factor = 1.0 if summary is CheckStatus.PASS else 0.0
            row = self._decision(route, request, tier, capability_factor, allow_degraded)
            decisions.append(row)
            if row.eligible:
                eligible.append((row, route))
            if route.qualification in (
                RouteQualification.DISCOVERED,
                RouteQualification.EXPIRED,
                RouteQualification.DEGRADED,
            ) and summary not in (CheckStatus.FAIL, CheckStatus.NOT_IMPLEMENTED):
                needs_probe.append(route.id)

        if objective not in {"min_time", "balanced", "reliability"}:
            raise RoutePolicyError(
                f"unsupported route objective {objective!r}",
                detail={"allowed": ["min_time", "balanced", "reliability"]},
            )
        selected: Route | None = None
        reason = "no eligible route satisfied capability, qualification, and workload policy"
        if eligible:
            if objective == "min_time":
                eligible.sort(
                    key=lambda item: (
                        item[0].expected_seconds is None,
                        item[0].expected_seconds or float("inf"),
                        -item[0].score,
                        item[0].route_id,
                    )
                )
            elif objective == "reliability":
                eligible.sort(
                    key=lambda item: (
                        -item[0].score,
                        item[0].expected_seconds or 1e99,
                        item[0].route_id,
                    )
                )
            else:
                eligible.sort(key=lambda item: (-item[0].score, item[0].route_id))
            selected = eligible[0][1]
            winner = eligible[0][0]
            reason = (
                f"selected {selected.id}: capability=PASS, "
                f"qualification={selected.qualification.value}, score={winner.score:.1f}; "
                f"{winner.rationale}"
            )
        ineligible_decisions = [item for item in decisions if not item.eligible]
        ranked_decisions = [item for item, _ in eligible] + ineligible_decisions
        decisions = [
            item.model_copy(update={"rank": index})
            for index, item in enumerate(ranked_decisions, start=1)
        ]
        return RouteEvaluation(
            requirement_id=request.request_id,
            workload=tier,
            objective=objective,
            routes=evaluated,
            decisions=decisions,
            selected=selected,
            selection_reason=reason,
            needs_probe=sorted(set(needs_probe)),
            evaluated_at=self._clock(),
        )

    def select(
        self,
        route_plan: RoutePlan | RouteEvaluation,
        *,
        objective: str = "min_time",
        allow_degraded: bool = False,
        preferred_route_id: str | None = None,
    ) -> RouteSelection:
        """选择唯一 route；默认调用方不得用未 qualification 的 route 下载。"""
        evaluation = (
            route_plan
            if isinstance(route_plan, RouteEvaluation)
            else self.evaluate(route_plan, objective=objective, allow_degraded=allow_degraded)
        )
        eligible = {item.route_id: item for item in evaluation.decisions if item.eligible}
        selected: Route | None
        if preferred_route_id is not None:
            if preferred_route_id not in eligible:
                route = next(
                    (item for item in evaluation.routes if item.id == preferred_route_id), None
                )
                detail: dict[str, object] = {
                    "route_id": preferred_route_id,
                    "needs_probe": evaluation.needs_probe,
                    "selection": evaluation.selection_reason,
                }
                if route is not None:
                    detail["qualification"] = route.qualification.value
                    detail["capability"] = route.capability_summary.value
                raise NoQualifiedRouteError(
                    f"preferred route {preferred_route_id!r} is not eligible",
                    detail=detail,
                )
            selected = next(item for item in evaluation.routes if item.id == preferred_route_id)
        else:
            selected = evaluation.selected
        if selected is None:
            raise NoQualifiedRouteError(
                "no qualified route is available; probe/qualify a candidate before download",
                detail={
                    "request_id": evaluation.requirement_id,
                    "workload": evaluation.workload.value,
                    "needs_probe": evaluation.needs_probe,
                    "decisions": [item.model_dump(mode="json") for item in evaluation.decisions],
                },
            )
        return RouteSelection(
            route=selected,
            reason=evaluation.selection_reason,
            evaluated_at=evaluation.evaluated_at,
        )

    def qualify(
        self,
        route_plan: RoutePlan,
        route_id: str,
        *,
        prober: ConnectivityProbe | None = None,
    ) -> tuple[ProbeResult, Route]:
        """执行一次显式 probe；成功时按能力矩阵决定是否授予 QUALIFIED。"""
        route = next((item for item in route_plan.candidate_routes if item.id == route_id), None)
        if route is None:
            raise SourceNotFoundError(
                f"route {route_id!r} is not in this discovery plan",
                detail={"request_id": route_plan.requirement_id},
            )
        route = route.model_copy(
            update={"capabilities": check_capabilities(route_plan.requirement, route)}
        )
        effective = prober or self._prober
        result = effective.probe(route, route_plan.requirement, self._adapters.get(route.source))
        capabilities = result.capabilities or route.capabilities
        probe_route = route.model_copy(update={"capabilities": capabilities})
        can_qualify = (
            probe_route.capability_summary is CheckStatus.PASS
            and result.is_success
            and result.benchmark is not None
        )
        record = self._registry.record_probe(probe_route, result, qualify=can_qualify)
        return result, record.route

    def materialize(
        self,
        route_plan: RoutePlan,
        *,
        route_id: str | None = None,
        planner: DefaultTaskPlanner | None = None,
    ) -> AcquisitionPlan:
        """为已选 route 的 source 生成 AcquisitionPlan（不下载）。"""
        evaluation = self.evaluate(route_plan)
        selection = self.select(evaluation, preferred_route_id=route_id)
        route = selection.route
        adapter = self._adapters.get(route.source)
        if adapter is None:
            raise RouteExecutionError(
                f"route {route.id!r} has no constructed source adapter; cannot materialize",
                source_id=route.source,
            )
        effective_planner = planner or DefaultTaskPlanner(PlannerConfig())
        plan = effective_planner.plan(route_plan.requirement, [adapter])
        if not plan.tasks:
            raise RouteExecutionError(
                f"selected route {route.id!r} produced no acquisition tasks",
                source_id=route.source,
            )
        wrong = [
            task.asset.access_route.value
            for task in plan.tasks
            if task.asset.access_route.value != route.transport
        ]
        if wrong:
            raise RouteExecutionError(
                f"selected route transport {route.transport!r} does not match produced assets: "
                f"{sorted(set(wrong))}",
                source_id=route.source,
                detail={"route_id": route.id},
            )
        extensions = dict(plan.extensions)
        extensions.update(
            {
                "routing.selected_route_id": route.id,
                "routing.selected_source": route.source,
                "routing.selected_transport": route.transport,
                "routing.qualification": route.qualification.value,
                "routing.requires_qualification": True,
            }
        )
        return plan.model_copy(
            update={
                "policy": f"route-selected/{route.id}",
                "extensions": extensions,
            }
        )

    def authorize_download(
        self,
        route_plan: RoutePlan,
        *,
        route: Route | RouteSelection | str | None = None,
        route_id: str | None = None,
        acquisition_plan: AcquisitionPlan | None = None,
        allow_degraded: bool = False,
    ) -> DownloadDecision:
        """下载前唯一应被调用的 policy 闸门。"""
        requested_id = _route_id_from(route, route_id)
        evaluation = self.evaluate(route_plan, allow_degraded=allow_degraded)
        try:
            selection = self.select(
                evaluation, preferred_route_id=requested_id, allow_degraded=allow_degraded
            )
        except NoQualifiedRouteError as exc:
            return DownloadDecision(
                allowed=False,
                reason=exc.message,
                requires_probe=bool(evaluation.needs_probe),
            )
        route = selection.route
        if acquisition_plan is not None:
            sources = {task.asset.source_id for task in acquisition_plan.tasks}
            if route.source not in sources:
                return DownloadDecision(
                    allowed=False,
                    route=route,
                    reason=(
                        "acquisition plan has no tasks from the selected route source; "
                        "regenerate/materialize the plan instead of calling an adapter directly"
                    ),
                )
            planned_route_id = acquisition_plan.extensions.get("routing.selected_route_id")
            if isinstance(planned_route_id, str) and planned_route_id != route.id:
                return DownloadDecision(
                    allowed=False,
                    route=route,
                    reason=(
                        "acquisition plan is bound to a different route; "
                        "materialize it again for the selected route"
                    ),
                )
        return DownloadDecision(allowed=True, route=route, reason=selection.reason)

    def download(
        self,
        route_plan: RoutePlan,
        *,
        route: Route | RouteSelection | str | None = None,
        route_id: str | None = None,
        acquisition_plan: AcquisitionPlan | None = None,
        executor: Callable[[DatasetRequest, AcquisitionPlan, Route], DownloadResult] | None = None,
    ) -> DownloadResult:
        """先过 route policy，再把执行交给注入的 executor。

        真实字节搬运仍由现有 transport/scheduler 完成；本方法不复制下载逻辑，只在调用
        executor 前强制证明 route 已 qualification。未注入 executor 时明确失败，绝不
        退化成直接调用某个 platform adapter。
        """
        requested_id = _route_id_from(route, route_id)
        effective_plan = acquisition_plan or self.materialize(route_plan, route_id=requested_id)
        decision = self.authorize_download(
            route_plan,
            route_id=requested_id,
            acquisition_plan=effective_plan,
        )
        if not decision.allowed or decision.route is None:
            raise RoutePolicyError(decision.reason, detail={"needs_probe": decision.requires_probe})
        if executor is None:
            raise RouteExecutionError(
                "route policy passed, but no download executor was injected; use the existing "
                "scheduler/transport composition root to execute the materialized plan",
                source_id=decision.route.source,
                detail={"route_id": decision.route.id},
            )
        result = executor(route_plan.requirement, effective_plan, decision.route)
        self._registry.record_transfer(
            decision.route,
            success=result.status == "succeeded",
            failure_reason=None if result.status == "succeeded" else result.detail,
        )
        return result

    # -- internals -------------------------------------------------------- #
    def _route_from_config(
        self,
        source_id: str,
        config: SourceConfig,
        requirement: DatasetRequest,
        binding: TransportBinding,
    ) -> Route:
        runtime_route = binding.route
        if requirement.dataset_id in config.datasets:
            dataset_status = CheckStatus.PASS
        elif config.datasets:
            dataset_status = CheckStatus.FAIL
        else:
            dataset_status = CheckStatus.UNKNOWN
        transport_status = CheckStatus.PASS if binding.implemented else CheckStatus.NOT_IMPLEMENTED
        adapter_status = (
            AdapterImplementationStatus.IMPLEMENTED
            if config.implemented
            else AdapterImplementationStatus.NOT_IMPLEMENTED
        )
        formats = _json_list(config.extensions.get("routing.formats"))
        temporal_resolutions = _json_list(config.extensions.get("routing.temporal_resolutions"))
        spatial_resolutions = _json_list(
            config.extensions.get("routing.spatial_resolutions"), floats=True
        )
        subset_value = config.extensions.get("routing.subset_supported")
        subset_supported = subset_value if isinstance(subset_value, bool) else None
        capabilities = RouteCapabilities(
            dataset=dataset_status,
            variables={},
            temporal_coverage=CheckStatus.UNKNOWN,
            temporal_resolution=CheckStatus.UNKNOWN,
            spatial_coverage=CheckStatus.UNKNOWN,
            spatial_resolution=CheckStatus.UNKNOWN,
            vertical_levels=CheckStatus.UNKNOWN,
            format=CheckStatus.UNKNOWN,
            subset=(
                CheckStatus.PASS
                if subset_supported is True
                else CheckStatus.FAIL
                if subset_supported is False
                else CheckStatus.UNKNOWN
            ),
            authentication=CheckStatus.NOT_TESTED,
            transport=transport_status,
            formats=[str(item) for item in formats],
            temporal_resolutions=[str(item) for item in temporal_resolutions],
            spatial_resolutions=[float(item) for item in spatial_resolutions],
            subset_supported=subset_supported,
        )
        status = RouteStatus.DISABLED if not config.enabled else RouteStatus.UNKNOWN
        route = Route(
            id=_slug(f"{source_id}-{requirement.dataset_id}-{runtime_route.value}"),
            source=source_id,
            dataset=requirement.dataset_id,
            transport=runtime_route.value,
            adapter=binding.adapter,
            endpoint=config.endpoint,
            capabilities=capabilities,
            adapter_status=adapter_status,
            status=status,
            notes=[
                f"discovered from sources.{source_id}.transports[{binding.route.value!r}]",
                *(
                    ["adapter instance does not report support for this transport"]
                    if transport_status is CheckStatus.NOT_IMPLEMENTED and binding.implemented
                    else []
                ),
            ],
            extensions={
                "routing.source_adapter": config.adapter,
                "routing.transport_implemented": binding.implemented,
                "routing.source_enabled": config.enabled,
            },
        )
        return route

    def _route_from_adapter(
        self,
        source_id: str,
        adapter: DataSourceAdapter,
        transport: str,
        requirement: DatasetRequest,
    ) -> Route:
        config = getattr(adapter, "config", None)
        endpoint = getattr(config, "endpoint", None) if isinstance(config, SourceConfig) else None
        return Route(
            id=_slug(f"{source_id}-{requirement.dataset_id}-{transport}"),
            source=source_id,
            dataset=requirement.dataset_id,
            transport=transport,
            adapter=getattr(adapter, "adapter_id", source_id),
            endpoint=endpoint,
            capabilities=RouteCapabilities(
                dataset=CheckStatus.PASS,
                transport=CheckStatus.PASS,
                authentication=CheckStatus.NOT_TESTED,
            ),
            adapter_status=AdapterImplementationStatus.IMPLEMENTED,
            status=RouteStatus.UNKNOWN,
            notes=[f"discovered from adapter {source_id!r}"],
            extensions={"routing.adapter_instance": True},
        )

    def _merge_registry(self, route: Route) -> Route:
        existing = self._registry.get(route.id)
        if existing is None:
            return route
        previous = existing.route
        qualification = previous.qualification
        current_invalid = (
            route.capability_summary in (CheckStatus.FAIL, CheckStatus.NOT_IMPLEMENTED)
            or previous.adapter != route.adapter
            or previous.endpoint != route.endpoint
        )
        if (
            current_invalid
            or route.adapter_status is AdapterImplementationStatus.NOT_IMPLEMENTED
            or route.capabilities.transport is CheckStatus.NOT_IMPLEMENTED
            or route.status is RouteStatus.DISABLED
        ):
            qualification = RouteQualification.DISCOVERED
        history = previous.history
        return route.model_copy(
            update={
                "capabilities": route.capabilities if current_invalid else previous.capabilities,
                "status": route.status if current_invalid else previous.status,
                "qualification": qualification,
                "benchmark": previous.benchmark or route.benchmark,
                "last_verified": previous.last_verified,
                "history": history,
                "notes": list(dict.fromkeys([*route.notes, *previous.notes]))[:32],
            }
        )

    def _qualification_expired(self, route: Route) -> bool:
        if route.qualification is RouteQualification.EXPIRED:
            return True
        if route.qualification not in (RouteQualification.QUALIFIED, RouteQualification.DEGRADED):
            return False
        last_verified = route.last_verified or route.history.last_verified
        if last_verified is None:
            return False
        return self._clock() - last_verified > self._qualification_ttl

    def _qualify_route(self, route: Route, request: DatasetRequest) -> Route:
        result = self._prober.probe(route, request, self._adapters.get(route.source))
        capabilities = result.capabilities or route.capabilities
        probe_route = route.model_copy(update={"capabilities": capabilities})
        can_qualify = (
            probe_route.capability_summary is CheckStatus.PASS
            and result.is_success
            and result.benchmark is not None
        )
        record = self._registry.record_probe(probe_route, result, qualify=can_qualify)
        return record.route

    def _decision(
        self,
        route: Route,
        request: DatasetRequest,
        tier: WorkloadTier,
        capability_factor: float,
        allow_degraded: bool,
    ) -> RouteDecision:
        summary = route.capability_summary
        qual_ok = route.qualification is RouteQualification.QUALIFIED or (
            allow_degraded and route.qualification is RouteQualification.DEGRADED
        )
        benchmark_ok = not (
            tier is WorkloadTier.LARGE
            and route.benchmark is None
            and route.history.benchmark is None
        )
        eligible = (
            summary is CheckStatus.PASS
            and qual_ok
            and benchmark_ok
            and route.status not in (RouteStatus.DISABLED, RouteStatus.UNAVAILABLE)
        )
        reliability = (
            route.history.success_rate
            if route.history.attempts > 0 and route.history.success_rate is not None
            else (0.72 if route.qualification is RouteQualification.QUALIFIED else 0.35)
        )
        benchmark = route.benchmark or route.history.benchmark
        speed = 0.35
        latency_factor = 0.5
        expected_seconds: float | None = None
        if benchmark is not None:
            speed = min(benchmark.throughput_bps / (50 * 1024**2), 1.0)
            latency_factor = 1.0 - min(benchmark.latency_ms / 2000.0, 1.0)
            if benchmark.throughput_bps > 0:
                expected_seconds = (
                    estimate_request_bytes(request) / benchmark.throughput_bps
                    + benchmark.latency_ms / 1000.0
                )
        age_days = 0.0
        fresh_anchor = route.last_verified or route.history.last_verified
        if fresh_anchor is not None:
            age_days = max(0.0, (self._clock() - fresh_anchor).total_seconds() / 86400.0)
        freshness = max(
            0.0, 1.0 - age_days / max(self._qualification_ttl.total_seconds() / 86400.0, 1.0)
        )
        subset_factor = 0.9 if route.capabilities.subset_supported is False else 1.0
        score = (
            100.0
            * capability_factor
            * reliability
            * (0.5 + 0.3 * speed + 0.1 * latency_factor + 0.1 * freshness)
            * subset_factor
        )
        reasons = [
            f"capability={summary.value}",
            f"qualification={route.qualification.value}",
            f"reliability={reliability:.2f}",
            f"throughput={((benchmark.throughput_bps if benchmark else 0.0) / 1024**2):.1f}MiB/s",
            f"latency={(benchmark.latency_ms if benchmark else 0.0):.0f}ms",
            f"workload={tier.value}",
        ]
        rejection: str | None = None
        if not eligible:
            if summary is not CheckStatus.PASS:
                rejection = f"capability={summary.value}"
            elif not qual_ok:
                rejection = f"qualification={route.qualification.value}"
            elif not benchmark_ok:
                rejection = "large workload requires a completed benchmark"
            else:
                rejection = f"route status={route.status.value}"
        return RouteDecision(
            route_id=route.id,
            eligible=eligible,
            score=score if eligible else 0.0,
            rank=0,
            rationale="; ".join(reasons),
            rejection=rejection,
            expected_seconds=expected_seconds,
        )


def _route_id_from(route: Route | RouteSelection | str | None, route_id: str | None) -> str | None:
    """兼容 route 对象、selection 对象与显式 route_id 三种调用形式。"""
    if route is None:
        return route_id
    if route_id is not None and route_id != (
        route.id
        if isinstance(route, Route)
        else route.route.id
        if isinstance(route, RouteSelection)
        else route
    ):
        raise RoutePolicyError("route and route_id refer to different routes")
    if isinstance(route, Route):
        return route.id
    if isinstance(route, RouteSelection):
        return route.route.id
    return route
