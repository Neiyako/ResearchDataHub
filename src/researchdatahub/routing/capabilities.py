"""需求 × route 能力矩阵。

检查只使用“有证据的声明”。缺失证据返回 ``UNKNOWN`` 或 ``NOT_TESTED``，不会被
静默解释成 ``FAIL``；adapter/transport 未实现则返回 ``NOT_IMPLEMENTED``，不会变成
``DATA_UNAVAILABLE``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from researchdatahub.core.models import DatasetRequest, JsonValue
from researchdatahub.routing.models import CapabilityCheck, CheckStatus, Route, RouteCapabilities

__all__ = ["check_capabilities", "required_formats", "requires_subset"]


def _metadata_value(request: DatasetRequest, key: str) -> JsonValue | None:
    value = request.metadata.extensions.get(key)
    if value is not None:
        return value
    return request.extensions.get(key)


def required_formats(request: DatasetRequest) -> tuple[str, ...]:
    """读取显式格式需求（命名空间由调用方提供）。"""
    value = _metadata_value(request, "routing.required_formats")
    if isinstance(value, str):
        return (value.strip().lower(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(
            item.strip().lower() for item in value if isinstance(item, str) and item.strip()
        )
    return ()


def requires_subset(request: DatasetRequest) -> bool | None:
    """读取显式子集需求；未声明返回 ``None``。"""
    value = _metadata_value(request, "routing.requires_subset")
    return value if isinstance(value, bool) else None


def _check(
    dimension: str,
    status: CheckStatus,
    detail: str = "",
    evidence: Mapping[str, JsonValue] | None = None,
) -> CapabilityCheck:
    return CapabilityCheck(
        dimension=dimension,
        status=status,
        detail=detail,
        evidence=dict(evidence or {}),
    )


def _variable_checks(
    required: Iterable[str], declared: Mapping[str, CheckStatus]
) -> dict[str, CheckStatus]:
    result: dict[str, CheckStatus] = dict(declared)
    for name in required:
        result.setdefault(name, CheckStatus.UNKNOWN)
    return result


def _required_format_status(route: Route, requested: tuple[str, ...]) -> CheckStatus:
    if not requested:
        return route.capabilities.format
    if route.capabilities.format is CheckStatus.FAIL:
        return CheckStatus.FAIL
    if not route.capabilities.formats:
        return CheckStatus.UNKNOWN
    available = {item.lower() for item in route.capabilities.formats}
    return CheckStatus.PASS if set(requested).issubset(available) else CheckStatus.FAIL


def _temporal_resolution_status(route: Route, request: DatasetRequest) -> CheckStatus:
    declared = route.capabilities.temporal_resolution
    if declared is not CheckStatus.PASS:
        return declared
    requested = request.metadata.time_definition.strip().lower()
    if not route.capabilities.temporal_resolutions:
        return CheckStatus.PASS
    available = {item.lower() for item in route.capabilities.temporal_resolutions}
    return CheckStatus.PASS if requested in available else CheckStatus.FAIL


def _spatial_resolution_status(route: Route, request: DatasetRequest) -> CheckStatus:
    declared = route.capabilities.spatial_resolution
    if declared is not CheckStatus.PASS:
        return declared
    requested = request.metadata.grid.resolution_degrees
    if requested is None:
        requested = request.metadata.spatial_extent.grid_degrees
    if requested is None or not route.capabilities.spatial_resolutions:
        return CheckStatus.PASS
    return (
        CheckStatus.PASS
        if any(abs(item - requested) < 1e-9 for item in route.capabilities.spatial_resolutions)
        else CheckStatus.FAIL
    )


def _subset_status(route: Route, request: DatasetRequest) -> CheckStatus:
    requested = requires_subset(request)
    if requested is None or not requested:
        return route.capabilities.subset
    if route.capabilities.subset_supported is None:
        return CheckStatus.UNKNOWN
    return CheckStatus.PASS if route.capabilities.subset_supported else CheckStatus.FAIL


def check_capabilities(request: DatasetRequest, route: Route) -> RouteCapabilities:
    """把需求与 route 的声明能力比对，返回新的能力矩阵。

    这里不联网，也不推断远端事实。探活/基准的结果应写回 route 后再调用本函数。
    """
    declared = route.capabilities
    dataset_status = declared.dataset
    if route.dataset != request.dataset_id:
        dataset_status = CheckStatus.FAIL
    elif dataset_status is CheckStatus.PASS:
        dataset_status = CheckStatus.PASS

    variables = _variable_checks(request.required_variables(), declared.variables)
    format_status = _required_format_status(route, required_formats(request))
    temporal_resolution = _temporal_resolution_status(route, request)
    spatial_resolution = _spatial_resolution_status(route, request)
    subset = _subset_status(route, request)

    checks = [
        _check(
            "dataset",
            dataset_status,
            (
                "route dataset matches request"
                if dataset_status is CheckStatus.PASS
                else "route dataset does not provide the requested logical dataset"
            ),
            {
                "routing.requested_dataset": request.dataset_id,
                "routing.route_dataset": route.dataset,
            },
        ),
        _check(
            "temporal_coverage",
            declared.temporal_coverage,
            "route temporal coverage is declared by the adapter/probe",
        ),
        _check(
            "temporal_resolution",
            temporal_resolution,
            "requested time definition is checked against declared resolutions",
            {"routing.requested_resolution": request.metadata.time_definition},
        ),
        _check(
            "spatial_coverage",
            declared.spatial_coverage,
            "route spatial coverage is declared by the adapter/probe",
        ),
        _check(
            "spatial_resolution",
            spatial_resolution,
            "requested grid resolution is checked when the route declares resolutions",
        ),
        _check(
            "vertical_levels",
            declared.vertical_levels,
            "route vertical-level support is declared by the adapter/probe",
        ),
        _check("format", format_status, "requested format support"),
        _check("subset", subset, "requested subset capability"),
        _check("authentication", declared.authentication, "route authentication readiness"),
        _check("transport", declared.transport, "transport implementation/availability"),
    ]
    checks.extend(
        _check(
            f"variables.{name}",
            status,
            "variable support is declared by the adapter/probe",
        )
        for name, status in sorted(variables.items())
    )
    return declared.model_copy(
        update={
            "dataset": dataset_status,
            "variables": variables,
            "temporal_resolution": temporal_resolution,
            "spatial_resolution": spatial_resolution,
            "format": format_status,
            "subset": subset,
            "checks": checks,
        }
    )
