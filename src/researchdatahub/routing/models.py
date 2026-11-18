"""数据获取 Route Intelligence 的稳定契约。

本模块的 ``Route`` 与 :mod:`researchdatahub.network.routes.models` 的 worker route
不是同一个概念：后者描述“哪个执行节点通过哪种协议搬字节”，这里的 ``Route`` 描述
“某个逻辑数据集可以由哪个 source、transport 与 adapter 组合获取”。两者刻意分开，
避免把数据集能力、传输能力和 worker 可用性混成一个布尔值。

状态语义
--------
``CheckStatus`` 的五种结果不是同义词：

* ``PASS``：已有证据表明满足；
* ``FAIL``：已有证据表明不满足；
* ``UNKNOWN``：没有足够信息判断（**绝不能**降级成 ``FAIL``）；
* ``NOT_IMPLEMENTED``：RDH 当前没有该 adapter/transport 实现（**不等于**数据不存在）；
* ``NOT_TESTED``：实现存在，但还没有完成探活或基准测试。

``RouteQualification`` 单独记录路线是否获准用于大规模传输；``RouteStatus`` 只记录
运行时可观察到的可用性。两者不得互相替代。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator

from researchdatahub.core.models import DatasetRequest, JsonValue, RouteQualification, _Contract

__all__ = [
    "AdapterImplementationStatus",
    "BenchmarkResult",
    "CapabilityCheck",
    "CapabilitySummary",
    "CheckStatus",
    "DownloadDecision",
    "DownloadResult",
    "ProbeResult",
    "Route",
    "RouteCandidate",
    "RouteDecision",
    "RouteEvaluation",
    "RouteExperience",
    "RouteHistory",
    "RoutePlan",
    "RouteQualification",
    "RouteSelection",
    "RouteStatus",
    "WorkloadTier",
    "aggregate_checks",
]


class CheckStatus(StrEnum):
    """能力检查的五态结果。"""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    NOT_TESTED = "NOT_TESTED"


CapabilitySummary = CheckStatus
"""能力矩阵的聚合结果；与 :class:`CheckStatus` 使用同一五态枚举。"""


class AdapterImplementationStatus(StrEnum):
    """adapter 实现状态；与远端数据是否存在严格分开。"""

    IMPLEMENTED = "implemented"
    NOT_IMPLEMENTED = "not_implemented"
    UNKNOWN = "unknown"


class RouteStatus(StrEnum):
    """运行时可观察到的 route 可用性，不是 qualification。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class WorkloadTier(StrEnum):
    """下载规模策略分档。"""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class CapabilityCheck(_Contract):
    """一个能力维度的检查结果与证据。"""

    dimension: str = Field(min_length=1, max_length=64)
    status: CheckStatus = CheckStatus.UNKNOWN
    detail: str = Field(default="", max_length=2048)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("evidence")
    @classmethod
    def _namespaced_evidence(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(
                    f"capability evidence key {key!r} must be namespaced, e.g. 'probe.kind'"
                )
        return value


class RouteCapabilities(_Contract):
    """route 对需求各维度的声明能力；所有未知项默认 ``UNKNOWN``。"""

    dataset: CheckStatus = CheckStatus.UNKNOWN
    variables: dict[str, CheckStatus] = Field(default_factory=dict)
    temporal_coverage: CheckStatus = CheckStatus.UNKNOWN
    temporal_resolution: CheckStatus = CheckStatus.UNKNOWN
    spatial_coverage: CheckStatus = CheckStatus.UNKNOWN
    spatial_resolution: CheckStatus = CheckStatus.UNKNOWN
    vertical_levels: CheckStatus = CheckStatus.UNKNOWN
    format: CheckStatus = CheckStatus.UNKNOWN
    subset: CheckStatus = CheckStatus.UNKNOWN
    authentication: CheckStatus = CheckStatus.UNKNOWN
    transport: CheckStatus = CheckStatus.UNKNOWN
    formats: list[str] = Field(default_factory=list, max_length=32)
    temporal_resolutions: list[str] = Field(default_factory=list, max_length=32)
    spatial_resolutions: list[float] = Field(default_factory=list, max_length=32)
    subset_supported: bool | None = None
    checks: list[CapabilityCheck] = Field(default_factory=list, max_length=32)

    @field_validator("variables")
    @classmethod
    def _variable_keys(cls, value: dict[str, CheckStatus]) -> dict[str, CheckStatus]:
        for name in value:
            if not name.strip():
                raise ValueError("capability variable names must not be empty")
        return value

    def as_checks(self) -> list[CapabilityCheck]:
        """把每个维度展开为可审计的检查条目（不丢 UNKNOWN/NOT_TESTED）。"""
        scalars = {
            "dataset": self.dataset,
            "temporal_coverage": self.temporal_coverage,
            "temporal_resolution": self.temporal_resolution,
            "spatial_coverage": self.spatial_coverage,
            "spatial_resolution": self.spatial_resolution,
            "vertical_levels": self.vertical_levels,
            "format": self.format,
            "subset": self.subset,
            "authentication": self.authentication,
            "transport": self.transport,
        }
        checks = [
            CapabilityCheck(dimension=name, status=status) for name, status in scalars.items()
        ]
        checks.extend(
            CapabilityCheck(dimension=f"variables.{name}", status=status)
            for name, status in sorted(self.variables.items())
        )
        generated_dimensions = {item.dimension for item in checks}
        checks.extend(item for item in self.checks if item.dimension not in generated_dimensions)
        return checks

    @property
    def summary(self) -> CheckStatus:
        """当前能力矩阵的聚合五态结果。"""
        return aggregate_checks(self)


class BenchmarkResult(_Contract):
    """一次轻量传输基准；只记录实测值，不伪造速度。"""

    probe_size: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0, validation_alias=AliasChoices("latency_ms", "latency"))
    transfer_time_s: float = Field(
        ge=0.0, validation_alias=AliasChoices("transfer_time_s", "transfer_time")
    )
    throughput_bps: float = Field(
        ge=0.0, validation_alias=AliasChoices("throughput_bps", "throughput")
    )
    success: bool
    timestamp: datetime

    @property
    def latency(self) -> float:
        """兼容任务书字段名 ``latency``。"""
        return self.latency_ms

    @property
    def transfer_time(self) -> float:
        """兼容任务书字段名 ``transfer_time``。"""
        return self.transfer_time_s

    @property
    def throughput(self) -> float:
        """兼容任务书字段名 ``throughput``。"""
        return self.throughput_bps

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class RouteHistory(_Contract):
    """route 的历史经验；用于过期、降级与可解释排序。"""

    last_verified: datetime | None = None
    success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_failures: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    benchmark: BenchmarkResult | None = None
    updated_at: datetime | None = None

    @field_validator("last_verified", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)


class Route(_Contract):
    """一条可解释、可验证的数据获取路线。"""

    id: str = Field(min_length=2, max_length=180)
    source: str = Field(min_length=1, max_length=128)
    dataset: str = Field(min_length=1, max_length=128)
    transport: str = Field(min_length=1, max_length=64)
    adapter: str = Field(min_length=1, max_length=128)
    endpoint: str | None = Field(default=None, max_length=4096)
    capabilities: RouteCapabilities = Field(default_factory=RouteCapabilities)
    adapter_status: AdapterImplementationStatus = AdapterImplementationStatus.UNKNOWN
    status: RouteStatus = RouteStatus.UNKNOWN
    qualification: RouteQualification = RouteQualification.DISCOVERED
    benchmark: BenchmarkResult | None = None
    last_verified: datetime | None = None
    history: RouteHistory = Field(default_factory=RouteHistory)
    notes: list[str] = Field(default_factory=list, max_length=32)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_summary(cls, data: object) -> object:
        """允许 ``model_dump`` 后原样读回，同时仍拒绝其它未知字段。"""
        if isinstance(data, Mapping) and "capability_summary" in data:
            cleaned = dict(data)
            cleaned.pop("capability_summary", None)
            return cleaned
        return data

    @field_validator("last_verified")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)

    @field_validator("extensions")
    @classmethod
    def _namespaced(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(f"route extension key {key!r} must be namespaced")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capability_summary(self) -> CheckStatus:
        """聚合当前 route 能力（序列化时由当前 capabilities 实时计算）。"""
        return aggregate_checks(self.capabilities)

    @property
    def is_known_qualified(self) -> bool:
        """仅 ``QUALIFIED`` 可用于默认大规模下载。"""
        return self.qualification is RouteQualification.QUALIFIED


class RouteCandidate(_Contract):
    """route 计划的候选项；保留“未实现”与“不匹配”的区别。"""

    route: Route
    reason: str = Field(default="", max_length=2048)


class RoutePlan(_Contract):
    """Route discovery 的结果；不下载任何科研数据。"""

    requirement: DatasetRequest
    requirement_id: str
    dataset: str
    candidate_routes: list[Route] = Field(default_factory=list)
    source_attempts: list[dict[str, JsonValue]] = Field(default_factory=list)
    generated_at: datetime
    notes: list[str] = Field(default_factory=list)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("generated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("source_attempts")
    @classmethod
    def _attempt_maps(cls, value: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
        for item in value:
            if "source" not in item:
                raise ValueError("source attempt must include a 'source' key")
        return value

    @property
    def qualified_routes(self) -> tuple[Route, ...]:
        """当前计划中已获资质的 route。"""
        return tuple(item for item in self.candidate_routes if item.is_known_qualified)


class ProbeResult(_Contract):
    """低成本 connectivity probe / benchmark 的结果。"""

    route_id: str
    status: CheckStatus
    dns_ok: bool | None = None
    endpoint_ok: bool | None = None
    auth_ok: bool | None = None
    metadata_ok: bool | None = None
    object_ok: bool | None = None
    request_ok: bool | None = None
    response_valid: bool | None = None
    bytes_transferred: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    benchmark: BenchmarkResult | None = None
    capabilities: RouteCapabilities | None = None
    detail: str = Field(default="", max_length=2048)
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def is_success(self) -> bool:
        """探活是否提供了足以继续 qualification 的正面证据。"""
        return self.status is CheckStatus.PASS and self.response_valid is not False

    @property
    def success(self) -> bool:
        """``is_success`` 的短别名，便于 Agent/报告读取。"""
        return self.is_success


class RouteDecision(_Contract):
    """一条 route 的评估结果（可用与拒绝项都必须回传）。"""

    route_id: str
    eligible: bool
    score: float = Field(ge=0.0)
    rank: int = Field(ge=0)
    rationale: str = Field(default="", max_length=4096)
    rejection: str | None = Field(default=None, max_length=2048)
    expected_seconds: float | None = Field(default=None, ge=0.0)


class RouteEvaluation(_Contract):
    """一个需求的候选 route 排序与选择理由。"""

    requirement_id: str
    workload: WorkloadTier
    objective: str = Field(default="min_time", max_length=32)
    routes: list[Route] = Field(default_factory=list)
    decisions: list[RouteDecision] = Field(default_factory=list)
    selected: Route | None = None
    selection_reason: str = Field(default="", max_length=4096)
    needs_probe: list[str] = Field(default_factory=list)
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def ordered_routes(self) -> tuple[Route, ...]:
        """按排名返回 route。"""
        by_id = {route.id: route for route in self.routes}
        return tuple(
            by_id[item.route_id]
            for item in sorted(self.decisions, key=lambda item: item.rank)
            if item.route_id in by_id
        )


class RouteSelection(_Contract):
    """最终选中的 route 与可解释理由。"""

    route: Route
    reason: str = Field(min_length=1, max_length=4096)
    evaluated_at: datetime

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def id(self) -> str:
        """兼容 Agent 直接读取 ``selection.id`` 的调用形态。"""
        return self.route.id

    @property
    def route_id(self) -> str:
        """Route ID 的显式别名。"""
        return self.route.id


class RouteExperience(_Contract):
    """持久化的 route 历史记录（registry 一行）。"""

    route: Route
    history: RouteHistory = Field(default_factory=RouteHistory)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DownloadDecision(_Contract):
    """下载前的策略闸门结果。"""

    allowed: bool
    route: Route | None = None
    reason: str
    requires_probe: bool = False

    @model_validator(mode="after")
    def _allowed_has_route(self) -> DownloadDecision:
        if self.allowed and self.route is None:
            raise ValueError("an allowed download decision must include a route")
        return self


class DownloadResult(_Contract):
    """Route-aware 下载结果；实际执行由注入的 executor 负责。"""

    route_id: str
    file_ids: list[str] = Field(default_factory=list)
    status: Literal["succeeded", "failed", "partial"] = "succeeded"
    integrity_status: str = Field(default="unknown", max_length=64)
    detail: str = Field(default="", max_length=2048)


def aggregate_checks(capabilities: RouteCapabilities) -> CheckStatus:
    """按“不能把未知当失败”的规则聚合能力矩阵。

    优先级：``FAIL`` > ``NOT_IMPLEMENTED`` > ``UNKNOWN`` > ``NOT_TESTED`` > ``PASS``。
    ``FAIL`` 表示证据明确不匹配；``NOT_IMPLEMENTED`` 只表示 RDH 当前没有实现。
    """
    statuses = [check.status for check in capabilities.as_checks()]
    if not statuses:
        return CheckStatus.UNKNOWN
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL
    if CheckStatus.NOT_IMPLEMENTED in statuses:
        return CheckStatus.NOT_IMPLEMENTED
    if CheckStatus.UNKNOWN in statuses:
        return CheckStatus.UNKNOWN
    if CheckStatus.NOT_TESTED in statuses:
        return CheckStatus.NOT_TESTED
    return CheckStatus.PASS
