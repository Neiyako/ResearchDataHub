"""计划器：把 :class:`DatasetRequest` 映射为可执行的 :class:`AcquisitionPlan`。

职责边界（ADR-0002 / ADR-0003）
------------------------------
* **只发现、不下载**：本模块 import 的模块集合里没有 ``transfer``，也没有任何网络客户端；
  远端字节搬运属于 transport 包。
* **只写状态机，不写数据库**：任务初始状态由 :func:`core.state.transition`
  ``PENDING -> PLANNED`` 得到；持久化由调用方（service 层 / CLI）用
  ``ManifestRepository.save_plan`` 完成。
* **不推断科学语义**：变量覆盖只看 ``adapter.discover_assets()`` 真实返回的资产元数据；
  缺失变量登记到 ``AcquisitionPlan.unmatched_variables``，绝不静默丢弃。

拆分策略（按数据源能力，而非拍脑袋）
------------------------------------
* **静态文件型源**（``https`` / ``s3`` / ``local`` …）：一个远端文件就是一个采集单元，
  ``granularity=file``。文件不可再切，因此计划规模等于去重后的文件数。
* **服务端生成型源**（``cds_api``，mode=submit→poll→result）：请求本身就是采集单元。
  ``auto`` 策略按时间跨度选择 ``request``（≤366 天）/ ``year`` / ``month``，并**先算数量
  再物化**，超过 ``planner.max_tasks_per_plan`` 直接拒绝（``ScheduleError``），不会先拆出
  数万个任务再报错。生成型资产的新 ``origin_uri`` 用 ``cds://`` 方案并携带
  ``cds.request_lifecycle`` 扩展，**不伪装成静态 URL**。
* 可通过 ``split_hints``（``{"source_id": "year"}``）或适配器可选属性
  ``split_granularity`` 覆盖 auto；配置 ``planner.preferred_split`` 只是建议，数据源能力优先。

去重
----
去重键 = ``(dataset_id, file_name, size_bytes, time_slice, level_slice, checksum)``，
**保留 preferred_sources 顺序里先出现的那个**；被丢弃的条目记入计划扩展
``planner.duplicates``（可审计，不静默）。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast

from researchdatahub.config.models import PlannerConfig, RetryConfig
from researchdatahub.core.errors import (
    MetadataError,
    ScheduleError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionPlan,
    AcquisitionTask,
    DatasetRequest,
    JsonValue,
    SourceAsset,
    TemporalExtent,
)
from researchdatahub.core.ports import DataSourceAdapter
from researchdatahub.core.state import TaskState, transition

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DefaultTaskPlanner",
    "DiscoveryAttempt",
    "SplitGranularity",
]

DEFAULT_MAX_ATTEMPTS: Final[int] = 3
"""``retry.max_attempts`` 缺省值；计划里每个任务都必须显式携带该值。"""

_GENERATED_ROUTES: Final[frozenset[AccessRoute]] = frozenset({AccessRoute.CDS_API})
"""服务端生成型访问路径：资产代表"一次远端请求"，不是静态文件。"""

_SECONDS_PER_YEAR: Final[float] = 365.25 * 86400.0
_SECONDS_PER_MONTH: Final[float] = 30.44 * 86400.0


class SplitGranularity(StrEnum):
    """采集单元拆分粒度。``file`` 只适用于静态文件，其余用于服务端生成型数据源。"""

    FILE = "file"
    REQUEST = "request"
    YEAR = "year"
    MONTH = "month"


@dataclass(frozen=True)
class DiscoveryAttempt:
    """一次 ``discover_assets`` 尝试的可审计结果（写进计划的扩展字段）。"""

    source_id: str
    status: str
    asset_count: int
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示。"""
        payload: dict[str, object] = {
            "source_id": self.source_id,
            "status": self.status,
            "asset_count": self.asset_count,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class DefaultTaskPlanner:
    """``core.ports.TaskPlanner`` 的实现：无状态、可并发调用（依赖的适配器须线程安全）。"""

    def __init__(
        self,
        config: PlannerConfig | None = None,
        *,
        retry: RetryConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        split_hints: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config or PlannerConfig()
        self._retry = retry or RetryConfig()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._split_hints = self._validated_hints(split_hints)
        if config is not None and config.max_tasks_per_plan < 1:
            raise ScheduleError("planner.max_tasks_per_plan must be >= 1")

    # -- 公开接口 -------------------------------------------------------- #
    def plan(
        self, request: DatasetRequest, adapters: Iterable[DataSourceAdapter]
    ) -> AcquisitionPlan:
        """为需求生成计划：源匹配 → 发现 → 拆分 → 去重 → 未满足变量登记。

        Raises
        ------
        SourceNotFoundError
            没有任何候选源能提供请求中的数据集，或 ``allow_partial=false`` 时存在未满足变量。
        ScheduleError
            计划规模超过 ``planner.max_tasks_per_plan``，或最小可执行计划也无法满足需求。
        """
        adapter_list = list(adapters)
        if not adapter_list:
            raise SourceNotFoundError(
                "no data source adapter is available; cannot plan",
                detail={"request_id": request.request_id},
            )
        generated_at = self._clock()
        ordered = self._order_candidates(request, adapter_list)
        required = request.required_variables()

        used_sources: list[str] = []
        assets: list[SourceAsset] = []
        attempts: list[DiscoveryAttempt] = []
        covered: set[str] = set()
        coverage_kinds: dict[str, str] = {}

        for adapter in ordered:
            if len(used_sources) >= request.max_sources:
                attempts.append(
                    DiscoveryAttempt(
                        source_id=adapter.source_id,
                        status="not_attempted",
                        asset_count=0,
                        reason=f"max_sources={request.max_sources} already reached",
                    )
                )
                continue
            if assets and required.issubset(covered):
                attempts.append(
                    DiscoveryAttempt(
                        source_id=adapter.source_id,
                        status="not_attempted",
                        asset_count=0,
                        reason="all required variables already covered",
                    )
                )
                continue
            attempt, found = self._discover(adapter, request)
            attempts.append(attempt)
            if found:
                used_sources.append(adapter.source_id)
                assets.extend(found)
                source_covered, coverage_kind = self._coverage_for(found, request)
                covered |= source_covered
                coverage_kinds[adapter.source_id] = coverage_kind

        if not assets:
            raise SourceNotFoundError(
                f"no data source can satisfy request {request.request_id!r}; "
                f"required variables: {sorted(required)}",
                detail={
                    "request_id": request.request_id,
                    "required_variables": sorted(required),
                    "attempts": [item.as_dict() for item in attempts],
                },
            )

        unmatched = sorted(required - covered)
        if unmatched and not self._config.allow_partial:
            raise SourceNotFoundError(
                f"variables {unmatched} cannot be satisfied by any candidate source and "
                "planner.allow_partial=false",
                detail={
                    "request_id": request.request_id,
                    "unmatched_variables": unmatched,
                    "attempts": [item.as_dict() for item in attempts],
                },
            )

        deduped, duplicates = self._dedupe(assets)
        split_plan, split_counts = self._plan_splits(request, deduped)
        total_units = sum(split_counts)
        if total_units > self._config.max_tasks_per_plan:
            raise ScheduleError(
                f"plan would create {total_units} acquisition units, exceeding "
                f"planner.max_tasks_per_plan={self._config.max_tasks_per_plan}; "
                "shrink the temporal range, use a coarser split, or raise the limit",
                detail={
                    "request_id": request.request_id,
                    "planned_units": total_units,
                    "max_tasks_per_plan": self._config.max_tasks_per_plan,
                    "split_counts": split_counts,
                },
            )

        tasks = self._build_tasks(
            request, split_plan, generated_at=generated_at, seed=request.fingerprint()
        )
        if not tasks:  # pragma: no cover - 上面 total_units 已保证；防御性分支
            raise ScheduleError(
                f"plan for request {request.request_id!r} contains no task",
                detail={"request_id": request.request_id},
            )

        plan_id = self._plan_id(request, used_sources, split_plan)
        tasks = [task.model_copy(update={"plan_id": plan_id}) for task in tasks]
        plan = AcquisitionPlan(
            plan_id=plan_id,
            request_id=request.request_id,
            policy=f"preferred-first/max={self._config.max_tasks_per_plan}",
            tasks=tasks,
            unmatched_variables=unmatched,
            generated_at=generated_at,
            extensions=cast(
                "dict[str, JsonValue]",
                self._plan_extensions(
                    request=request,
                    attempts=attempts,
                    used_sources=used_sources,
                    discovered=len(assets),
                    deduped=len(deduped),
                    duplicates=duplicates,
                    split_counts=split_counts,
                    unmatched=unmatched,
                    coverage_kinds=coverage_kinds,
                ),
            ),
        )
        return plan

    # -- 源匹配 ---------------------------------------------------------- #
    def _order_candidates(
        self, request: DatasetRequest, adapters: Sequence[DataSourceAdapter]
    ) -> list[DataSourceAdapter]:
        """源匹配顺序：``preferred_sources`` 声明顺序 → ``rdh.provider`` 提示 → 其余。"""
        by_id: dict[str, DataSourceAdapter] = {}
        for adapter in adapters:
            # 同一 source_id 重复注册时保留先出现者，避免两个实例处理同一源。
            by_id.setdefault(adapter.source_id, adapter)
        ordered: list[DataSourceAdapter] = []
        for source_id in request.preferred_sources:
            candidate = by_id.get(source_id)
            if candidate is not None and candidate not in ordered:
                ordered.append(candidate)
        provider = request.metadata.extensions.get("rdh.provider")
        if isinstance(provider, str):
            provider_candidate = by_id.get(provider)
            if provider_candidate is not None and provider_candidate not in ordered:
                ordered.append(provider_candidate)
        for adapter in by_id.values():
            if adapter not in ordered:
                ordered.append(adapter)
        return ordered

    def _discover(
        self, adapter: DataSourceAdapter, request: DatasetRequest
    ) -> tuple[DiscoveryAttempt, list[SourceAsset]]:
        """调用一次 ``discover_assets``；把可解释失败变成可审计的 attempt，不吞异常。"""
        source_id = adapter.source_id
        try:
            found = tuple(adapter.discover_assets(request))
        except SourceNotFoundError as exc:
            return DiscoveryAttempt(source_id, "not_found", 0, exc.message), []
        except SourceUnavailableError as exc:
            return DiscoveryAttempt(source_id, "unavailable", 0, exc.message), []
        except MetadataError as exc:
            return DiscoveryAttempt(source_id, "metadata_error", 0, exc.message), []
        if not found:
            return DiscoveryAttempt(source_id, "empty", 0, "discover_assets returned no asset"), []
        valid = [asset for asset in found if asset.source_id == source_id]
        if not valid:
            return (
                DiscoveryAttempt(
                    source_id,
                    "invalid",
                    0,
                    "every discovered asset belongs to a different source_id",
                ),
                [],
            )
        return DiscoveryAttempt(source_id, "used", len(valid), None), valid

    # -- 去重 ------------------------------------------------------------ #
    @staticmethod
    def _dedupe(assets: Sequence[SourceAsset]) -> tuple[list[SourceAsset], list[dict[str, object]]]:
        """按科学内容去重，保留 preferred 顺序里先出现的资产。"""
        seen: dict[tuple[object, ...], SourceAsset] = {}
        unique: list[SourceAsset] = []
        duplicates: list[dict[str, object]] = []
        for asset in assets:
            key = _dedupe_key(asset)
            keeper = seen.get(key)
            if keeper is None:
                seen[key] = asset
                unique.append(asset)
                continue
            duplicates.append(
                {
                    "file_name": asset.file_name,
                    "source_id": asset.source_id,
                    "kept_source_id": keeper.source_id,
                    "origin_uri": asset.origin_uri,
                    "reason": "same scientific content as an earlier asset",
                }
            )
        return unique, duplicates

    @staticmethod
    def _coverage_for(
        assets: Sequence[SourceAsset], request: DatasetRequest
    ) -> tuple[frozenset[str], str]:
        """一个源本次发现结果的变量覆盖。

        契约事实：``SourceAsset`` 只携带 :class:`SourceMetadata`，没有"文件包含哪些变量"
        的字段。因此覆盖判定按以下顺序，**不做无依据推断**：

        1. 资产元数据扩展里显式声明了变量覆盖（键名以 ``.variables`` /
           ``.variable_names`` / ``.variable_coverage`` 结尾且值为字符串列表）→
           用声明的合集，缺的变量进入 ``unmatched_variables``；
        2. 完全没有任何声明 → 只能把"适配器接受了该请求并返回资产"视为覆盖全部请求变量，
           并在计划扩展 ``planner.variable_coverage[source_id]="assumed_from_discovery"``
           中显式记录这是假设而非事实。

        Returns
        -------
        tuple[frozenset[str], str]
            ``(覆盖变量集合, 覆盖依据)``；依据取值 ``declared`` / ``assumed_from_discovery``。
        """
        declared: set[str] = set()
        has_declaration = False
        for asset in assets:
            declared_variables = _declared_variables(asset.metadata.extensions)
            if declared_variables is not None:
                has_declaration = True
                declared |= declared_variables
        if has_declaration:
            return frozenset(declared), "declared"
        return request.required_variables(), "assumed_from_discovery"

    # -- 拆分 ------------------------------------------------------------ #
    def _plan_splits(
        self, request: DatasetRequest, assets: Sequence[SourceAsset]
    ) -> tuple[list[tuple[SourceAsset, list[tuple[datetime, datetime]]]], list[int]]:
        """先算每个资产要拆成几个单元（不物化），返回拆分方案与计数。"""
        split_plan: list[tuple[SourceAsset, list[tuple[datetime, datetime]]]] = []
        counts: list[int] = []
        for asset in assets:
            windows = self._windows_for(request, asset)
            split_plan.append((asset, windows))
            counts.append(len(windows))
        return split_plan, counts

    def _windows_for(
        self, request: DatasetRequest, asset: SourceAsset
    ) -> list[tuple[datetime, datetime]]:
        """计算一个资产的采集单元时间窗。"""
        granularity = self._granularity_for(asset)
        start, end = _asset_span(asset, request)
        if granularity is SplitGranularity.FILE:
            return [(start, end)]
        return _chunk_windows(start, end, granularity, self._config.max_tasks_per_plan, request)

    def _granularity_for(self, asset: SourceAsset) -> SplitGranularity:
        """按"数据源能力优先"决定拆分粒度。"""
        generated = asset.access_route in _GENERATED_ROUTES
        hint = self._split_hints.get(asset.source_id)
        preferred = self._config.preferred_split
        if hint is not None:
            granularity = SplitGranularity(hint)
            if not generated and granularity in (SplitGranularity.YEAR, SplitGranularity.MONTH):
                # 静态文件不可切分：配置只是建议，能力优先。
                return SplitGranularity.FILE
            return granularity
        if not generated:
            return SplitGranularity.FILE
        if preferred == "auto":
            return _auto_granularity(_span_seconds(asset), self._config.max_tasks_per_plan)
        if preferred == "file":
            # 生成型源无法用"文件"表达；退化为 request（一次请求生成一个结果文件）。
            return SplitGranularity.REQUEST
        return SplitGranularity(preferred)

    # -- 任务构造 -------------------------------------------------------- #
    def _build_tasks(
        self,
        request: DatasetRequest,
        split_plan: Sequence[tuple[SourceAsset, list[tuple[datetime, datetime]]]],
        *,
        generated_at: datetime,
        seed: str,
    ) -> list[AcquisitionTask]:
        """把拆分方案物化成任务（初始 ``PENDING``，经状态机推进到 ``PLANNED``）。"""
        # 占位 plan_id 必须满足 Identifier 正则；真正的 plan_id 在计划摘要算出后回填。
        plan_stub = "plan-pending"
        tasks: list[AcquisitionTask] = []
        for asset, windows in split_plan:
            granularity = self._granularity_for(asset)
            units = (
                [asset]
                if len(windows) == 1 and granularity is SplitGranularity.FILE
                else [
                    _split_asset(asset, start, end, granularity=granularity)
                    for start, end in windows
                ]
            )
            for unit in units:
                task_id = _task_id(seed, unit)
                task = AcquisitionTask(
                    task_id=task_id,
                    plan_id=plan_stub,
                    request_id=request.request_id,
                    asset=unit,
                    state=TaskState.PENDING,
                    priority=request.priority,
                    max_attempts=self._max_attempts(),
                    scheduled_at=None,
                )
                tasks.append(
                    transition(
                        task,
                        TaskState.PLANNED,
                        reason=f"planned from request {request.request_id}",
                        now=generated_at,
                    )
                )
        return tasks

    def _max_attempts(self) -> int:
        attempts = getattr(self._retry, "max_attempts", DEFAULT_MAX_ATTEMPTS)
        if isinstance(attempts, int) and attempts >= 1:
            return attempts
        return DEFAULT_MAX_ATTEMPTS

    # -- 计划标识与扩展 -------------------------------------------------- #
    @staticmethod
    def _plan_id(
        request: DatasetRequest,
        used_sources: Sequence[str],
        split_plan: Sequence[tuple[SourceAsset, list[tuple[datetime, datetime]]]],
    ) -> str:
        """确定性 plan_id：同一需求 + 同一发现结果 → 同一计划（可幂等重放）。"""
        payload = {
            "request": request.fingerprint(),
            "sources": list(used_sources),
            "units": [
                [
                    asset.asset_id,
                    asset.source_id,
                    asset.file_name,
                    [[start.isoformat(), end.isoformat()] for start, end in windows],
                ]
                for asset, windows in split_plan
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"plan-{digest}"

    def _plan_extensions(
        self,
        *,
        request: DatasetRequest,
        attempts: Sequence[DiscoveryAttempt],
        used_sources: Sequence[str],
        discovered: int,
        deduped: int,
        duplicates: Sequence[Mapping[str, object]],
        split_counts: Sequence[int],
        unmatched: Sequence[str],
        coverage_kinds: Mapping[str, str],
    ) -> dict[str, object]:
        """计划的可审计元数据（全部 JSON 安全）。"""
        returned = sum(attempt.asset_count for attempt in attempts)
        return {
            "planner.version": "m4",
            "planner.max_tasks_per_plan": self._config.max_tasks_per_plan,
            "planner.allow_partial": self._config.allow_partial,
            "planner.preferred_split": self._config.preferred_split,
            "planner.sources_used": list(used_sources),
            "planner.discovery_attempts": [attempt.as_dict() for attempt in attempts],
            "planner.assets_returned": returned,
            "planner.assets_used": discovered,
            "planner.assets_deduped": deduped,
            "planner.duplicates": [dict(item) for item in duplicates],
            "planner.split_counts": list(split_counts),
            "planner.unmatched_variables": list(unmatched),
            "planner.requested_variables": sorted(request.required_variables()),
            "planner.variable_coverage": dict(coverage_kinds),
        }

    @staticmethod
    def _validated_hints(split_hints: Mapping[str, str] | None) -> dict[str, str]:
        """校验注入的拆分提示（只接受契约里的四种粒度）。"""
        if not split_hints:
            return {}
        allowed = {item.value for item in SplitGranularity}
        result: dict[str, str] = {}
        for source_id, granularity in split_hints.items():
            if granularity not in allowed:
                raise ScheduleError(
                    f"split hint for source {source_id!r} is {granularity!r}; "
                    f"allowed: {sorted(allowed)}"
                )
            result[source_id] = granularity
        return result


# --------------------------------------------------------------------------- #
# 纯函数：时间窗、去重键、ID、拆分资产
# --------------------------------------------------------------------------- #
_VARIABLE_COVERAGE_SUFFIXES: Final[tuple[str, ...]] = (
    ".variables",
    ".variable_names",
    ".variable_coverage",
)


def _declared_variables(extensions: Mapping[str, JsonValue]) -> frozenset[str] | None:
    """从数据源元数据扩展里读取"本文件覆盖哪些变量"的显式声明（没有则返回 ``None``）。"""
    declared: set[str] = set()
    found = False
    for key, value in extensions.items():
        if not isinstance(key, str) or not key.endswith(_VARIABLE_COVERAGE_SUFFIXES):
            continue
        if not isinstance(value, list):
            continue
        names = [item for item in value if isinstance(item, str) and item.strip()]
        if not names:
            continue
        found = True
        declared.update(names)
    return frozenset(declared) if found else None


def _dedupe_key(asset: SourceAsset) -> tuple[object, ...]:
    """去重键：同一科学内容的同一文件在多个源上只应采集一次。"""
    return (
        asset.dataset_id,
        asset.file_name,
        asset.size_bytes,
        None
        if asset.time_slice is None
        else (
            asset.time_slice.start.isoformat(),
            asset.time_slice.end.isoformat(),
        ),
        None if asset.level_slice is None else tuple(asset.level_slice),
        None if asset.checksum is None else asset.checksum.value,
    )


def _asset_span(asset: SourceAsset, request: DatasetRequest) -> tuple[datetime, datetime]:
    """资产覆盖的时间窗；资产没有声明时退回请求的时间范围。"""
    if asset.time_slice is not None:
        return asset.time_slice.start, asset.time_slice.end
    coverage = request.metadata.temporal_coverage
    return coverage.start, coverage.end


def _span_seconds(asset: SourceAsset) -> float:
    """资产自身声明的时间跨度（无 ``time_slice`` 时返回 0，由 auto 策略走 request 粒度）。"""
    if asset.time_slice is None:
        return 0.0
    return (asset.time_slice.end - asset.time_slice.start).total_seconds()


def _auto_granularity(span_seconds: float, max_tasks: int) -> SplitGranularity:
    """auto：跨度 ≤366 天用 request；否则取能塞进上限的最细粒度，再不行就拒绝。"""
    if span_seconds <= _SECONDS_PER_YEAR:
        return SplitGranularity.REQUEST
    years = max(1, math.ceil(span_seconds / _SECONDS_PER_YEAR))
    if years <= max_tasks:
        return SplitGranularity.YEAR
    months = max(1, math.ceil(span_seconds / _SECONDS_PER_MONTH))
    if months <= max_tasks:
        return SplitGranularity.MONTH
    raise ScheduleError(
        f"temporal span needs {months} monthly units, exceeding "
        f"planner.max_tasks_per_plan={max_tasks}; shrink the request or raise the limit",
        detail={"planned_units": months, "max_tasks_per_plan": max_tasks},
    )


def _chunk_windows(
    start: datetime,
    end: datetime,
    granularity: SplitGranularity,
    max_tasks: int,
    request: DatasetRequest,
) -> list[tuple[datetime, datetime]]:
    """按日历自然边界切分 ``[start, end)``；先算数量，超上限直接拒绝。"""
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        if granularity is SplitGranularity.YEAR:
            boundary = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        elif granularity is SplitGranularity.MONTH:
            if cursor.month == 12:
                boundary = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
            else:
                boundary = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        elif granularity is SplitGranularity.REQUEST:
            boundary = end
        else:  # pragma: no cover - FILE 在上层已拦截
            raise ScheduleError(f"cannot chunk static file asset with {granularity.value!r}")
        stop = min(boundary, end)
        windows.append((cursor, stop))
        cursor = stop
        if len(windows) > max_tasks:
            raise ScheduleError(
                f"request {request.request_id!r} would produce more than "
                f"planner.max_tasks_per_plan={max_tasks} {granularity.value} units; "
                "shrink the temporal range or raise the limit",
                detail={
                    "request_id": request.request_id,
                    "granularity": granularity.value,
                    "max_tasks_per_plan": max_tasks,
                },
            )
    if not windows:  # pragma: no cover - 调用方保证 start < end
        raise ScheduleError("empty temporal window")
    return windows


def _task_id(plan_stub: str, asset: SourceAsset) -> str:
    """确定性任务 ID（同一计划 + 同一采集单元 → 同一 ID，支撑幂等重放）。"""
    payload = [
        plan_stub,
        asset.source_id,
        asset.dataset_id,
        asset.file_name,
        asset.origin_uri,
        None
        if asset.time_slice is None
        else (
            asset.time_slice.start.isoformat(),
            asset.time_slice.end.isoformat(),
        ),
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"task-{digest}"


def _split_asset(
    asset: SourceAsset,
    start: datetime,
    end: datetime,
    *,
    granularity: SplitGranularity,
) -> SourceAsset:
    """把生成型资产切成一个采集单元：新的 origin_uri + 请求生命周期扩展。

    生成型数据源的单元代表"一次远端请求"，因此：
    * ``origin_uri`` 使用 ``cds://`` / ``generated://`` 方案，不是 http(s) 静态地址；
    * ``size_bytes`` 在结果生成前未知，记 0 并用 ``planner.size_bytes_unknown=true`` 标注
      （执行层不会把 0 当成"空文件"去跳过）；
    * 生命周期/时间窗写进 ``metadata.extensions``（``SourceAsset`` 没有 extensions 字段，
      数据源特有的信息按契约只能放在命名空间化的 metadata.extensions 里）。
    """
    suffix_start = f"{start.year:04d}{start.month:02d}{start.day:02d}"
    suffix_end = f"{end.year:04d}{end.month:02d}{end.day:02d}"
    scheme = "cds" if asset.access_route is AccessRoute.CDS_API else "generated"
    origin_uri = f"{scheme}://{asset.source_id}/{asset.dataset_id}/{suffix_start}-{suffix_end}"
    extensions: dict[str, JsonValue] = dict(asset.metadata.extensions)
    extensions["planner.split_granularity"] = granularity.value
    time_window: dict[str, JsonValue] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    extensions["planner.time_window"] = time_window
    extensions["planner.template_asset_id"] = asset.asset_id
    extensions["planner.template_origin_uri"] = asset.origin_uri
    extensions["planner.size_bytes_unknown"] = True
    if asset.access_route is AccessRoute.CDS_API:
        lifecycle: dict[str, JsonValue] = {
            "mode": "submit_poll_result",
            "submit": "cds_api.submit",
            "poll": "cds_api.poll",
            "result": "cds_api.result_file",
            "note": "服务端生成型数据：提交请求→轮询→结果文件；不是静态 URL",
        }
        extensions["cds.request_lifecycle"] = lifecycle
    metadata = asset.metadata.model_copy(
        update={"origin_uri": origin_uri, "extensions": extensions}
    )
    return asset.model_copy(
        update={
            "asset_id": f"{asset.asset_id}-{suffix_start}-{suffix_end}"[:63],
            "origin_uri": origin_uri,
            "metadata": metadata,
            "time_slice": TemporalExtent(start=start, end=end),
            "size_bytes": 0,
            "checksum": None,
            "remote_etag": None,
            "remote_last_modified": None,
        }
    )
