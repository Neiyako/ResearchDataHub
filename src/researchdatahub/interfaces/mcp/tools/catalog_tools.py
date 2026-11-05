"""目录与能力类工具：``search_datasets`` / ``inspect_dataset`` / ``compare_datasets`` /
``get_provider_capabilities`` / ``get_route_status``。

数据来源的诚实边界
------------------
这五个工具只描述**本地配置与适配器自报的能力**（``configs/sources.yaml`` +
``AdapterCapabilities``），不发任何网络请求去"发现数据集"。原因：外部 Agent 需要的是
"现在能不能规划/执行"，而不是"远端理论上有什么"。因此返回里的每个数据集都带
``configured`` 标记，每个能力都区分 ``verified`` / ``unverified``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from researchdatahub.config.models import SourceConfig
from researchdatahub.interfaces.mcp.capabilities import describe_available_capabilities
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.errors import invalid_arguments, not_found_error
from researchdatahub.interfaces.mcp.schemas import (
    COMPARE_DATASETS_SCHEMA,
    GET_PROVIDER_CAPABILITIES_SCHEMA,
    GET_ROUTE_STATUS_SCHEMA,
    INSPECT_DATASET_SCHEMA,
    SEARCH_DATASETS_SCHEMA,
    coerce_arguments,
    optional_bool,
    optional_int,
    optional_str,
    optional_str_list,
    require_str,
    require_str_list,
)
from researchdatahub.interfaces.mcp.tools._shared import json_ready
from researchdatahub.sources.base import AdapterCapabilities, SourceAdapter

__all__ = [
    "compare_datasets",
    "get_provider_capabilities",
    "get_route_status",
    "inspect_dataset",
    "search_datasets",
]

_COMPARABLE_DIMENSIONS: tuple[str, ...] = (
    "variables",
    "temporal_coverage",
    "spatial_extent",
    "vertical_levels",
    "providers",
    "grid",
)


def _source_configs(context: McpContext, provider: str | None) -> dict[str, SourceConfig]:
    """当前配置里的数据源（按 provider 过滤）；无配置时返回空字典。"""
    if context.sources_config is None:
        return {}
    selected = {
        source_id: config
        for source_id, config in context.sources_config.sources.items()
        if provider is None or source_id == provider
    }
    return selected


def _dataset_entries(
    sources: Mapping[str, SourceConfig],
) -> list[dict[str, Any]]:
    """把 ``source_id -> SourceConfig`` 展开成 ``数据集作为键`` 的扁平行。"""
    rows: list[dict[str, Any]] = []
    for source_id, config in sources.items():
        for dataset_id in config.datasets:
            rows.append(
                {
                    "dataset_id": dataset_id,
                    "provider": source_id,
                    "display_name": config.display_name,
                    "adapter": config.adapter,
                    "implemented": config.implemented,
                    "routes": sorted(route.value for route in config.routes),
                    "usable_routes": sorted(route.value for route in config.usable_routes),
                }
            )
    return rows


def _configuration_note(context: McpContext) -> str | None:
    if context.sources_config is None:
        return "no sources configuration is loaded; dataset discovery is unavailable"
    return None


def search_datasets(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """按关键词/变量/Provider 搜索配置中声明的逻辑数据集。"""
    args = coerce_arguments("search_datasets", arguments, _names(SEARCH_DATASETS_SCHEMA))
    query = optional_str(args, "query")
    variables = optional_str_list(args, "variables")
    provider = optional_str(args, "provider")
    limit = optional_int(args, "limit", default=50, minimum=1, maximum=200) or 50

    sources = _source_configs(context, provider)
    if provider is not None and not sources:
        raise not_found_error("provider", provider)

    rows = _dataset_entries(sources)
    needle = (query or "").lower()
    matched: list[dict[str, Any]] = []
    for row in rows:
        haystack = f"{row['dataset_id']} {row['provider']} {row['display_name']}".lower()
        if needle and needle not in haystack:
            continue
        row_copy = dict(row)
        row_copy["variables"] = _declared_variables(row_copy["dataset_id"])
        if variables and not set(variables).issubset(set(row_copy["variables"])):
            continue
        matched.append(row_copy)

    note = _configuration_note(context)
    payload: dict[str, Any] = {
        "datasets": matched[:limit],
        "total": len(matched),
        "truncated": len(matched) > limit,
        "source": "configuration",
    }
    if note is not None:
        payload["note"] = note
    if variables:
        payload["variables_note"] = (
            "variable filtering uses variables declared in request metadata; the configuration "
            "does not enumerate per-dataset variables"
        )
    return payload


_DECLARED_VARIABLES: dict[str, tuple[str, ...]] = {}


def _declared_variables(dataset_id: str) -> tuple[str, ...]:
    """配置层没有逐数据集变量清单，返回空元组（**不猜测**）。

    独立函数便于将来接入 ``providers`` 包时替换，而不改工具逻辑。
    """
    return _DECLARED_VARIABLES.get(dataset_id, ())


def inspect_dataset(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """查看单个逻辑数据集的 Provider/路线/适配器能力。"""
    args = coerce_arguments("inspect_dataset", arguments, _names(INSPECT_DATASET_SCHEMA))
    dataset_id = require_str(args, "dataset_id")
    provider = optional_str(args, "provider")

    sources = _source_configs(context, provider)
    matching = [row for row in _dataset_entries(sources) if row["dataset_id"] == dataset_id]
    if not matching:
        raise not_found_error("dataset", dataset_id)

    providers: list[dict[str, Any]] = []
    for row in matching:
        adapter = _adapter_for(context, row["provider"])
        entry: dict[str, Any] = {
            "provider": row["provider"],
            "display_name": row["display_name"],
            "adapter": row["adapter"],
            "implemented": row["implemented"],
            "routes": row["routes"],
            "usable_routes": row["usable_routes"],
        }
        entry["capabilities"] = _capabilities_payload(adapter)
        providers.append(entry)

    payload: dict[str, Any] = {"dataset_id": dataset_id, "providers": providers}
    note = _configuration_note(context)
    if note is not None:
        payload["note"] = note
    return payload


def _adapter_for(context: McpContext, source_id: str) -> SourceAdapter | None:
    for entry in context.registry.entries:
        if entry.source_id == source_id and isinstance(entry.adapter, SourceAdapter):
            return entry.adapter
    return None


def _capabilities_payload(adapter: SourceAdapter | None) -> dict[str, Any] | None:
    """适配器能力；适配器缺席时返回 ``None``（不伪造能力）。"""
    if adapter is None:
        return None
    capabilities: AdapterCapabilities = adapter.capabilities()
    return json_ready(capabilities.to_dict())  # type: ignore[return-value]


def compare_datasets(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """按维度比较多个逻辑数据集（配置层可比项）。"""
    args = coerce_arguments("compare_datasets", arguments, _names(COMPARE_DATASETS_SCHEMA))
    dataset_ids = require_str_list(args, "dataset_ids")
    if len(dataset_ids) < 2:
        raise invalid_arguments(
            "dataset_ids must contain at least 2 dataset ids",
            detail={"field": "dataset_ids", "received": len(dataset_ids)},
        )
    raw_dimensions = optional_str_list(args, "dimensions")
    dimensions = raw_dimensions or _COMPARABLE_DIMENSIONS
    unknown = [item for item in dimensions if item not in _COMPARABLE_DIMENSIONS]
    if unknown:
        raise invalid_arguments(
            f"unsupported comparison dimension(s): {', '.join(unknown)}",
            detail={"field": "dimensions", "allowed": list(_COMPARABLE_DIMENSIONS)},
        )

    sources = _source_configs(context, None)
    rows = {row["dataset_id"]: row for row in _dataset_entries(sources)}
    missing = [dataset_id for dataset_id in dataset_ids if dataset_id not in rows]
    if missing:
        raise not_found_error("dataset", ", ".join(missing))

    comparison: dict[str, Any] = {}
    for dimension in dimensions:
        comparison[dimension] = {
            dataset_id: _dimension_value(rows[dataset_id], dimension) for dataset_id in dataset_ids
        }
    return {
        "dataset_ids": list(dataset_ids),
        "dimensions": list(dimensions),
        "comparison": comparison,
        "source": "configuration",
        "note": "各 Provider 声明的能力也不同，见 inspect_dataset 的 capabilities 字段",
    }


def _dimension_value(row: Mapping[str, Any], dimension: str) -> Any:
    if dimension == "variables":
        return list(_declared_variables(str(row["dataset_id"])))
    if dimension == "providers":
        return row["provider"]
    # temporal_coverage / spatial_extent / vertical_levels / grid 需要远端元数据或请求上下文，
    # 配置层没有权威值：如实给 null，而不是编一个默认全球/全年。
    return None


def get_provider_capabilities(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """列出数据源适配器能力边界与可选包可用性。"""
    args = coerce_arguments(
        "get_provider_capabilities", arguments, _names(GET_PROVIDER_CAPABILITIES_SCHEMA)
    )
    provider = optional_str(args, "provider")

    sources = _source_configs(context, provider)
    if provider is not None and not sources:
        raise not_found_error("provider", provider)

    providers: list[dict[str, Any]] = []
    for source_id, config in sources.items():
        adapter = _adapter_for(context, source_id)
        providers.append(
            {
                "provider": source_id,
                "adapter": config.adapter,
                "implemented": config.implemented,
                "enabled": config.enabled,
                "datasets": list(config.datasets),
                "routes": [
                    {"route": binding.route.value, "implemented": binding.implemented}
                    for binding in config.transports
                ],
                "capabilities": _capabilities_payload(adapter),
                "available": bool(adapter is not None and adapter.capabilities().available),
            }
        )

    payload: dict[str, Any] = {
        "providers": providers,
        "optional_capabilities": describe_available_capabilities(),
        "credential_policy": (
            "credentials are never returned; only missing credential KEY NAMES are reported"
        ),
    }
    note = _configuration_note(context)
    if note is not None:
        payload["note"] = note
    return payload


def get_route_status(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """列出访问路线及其可用性；``probe=true`` 时发起真实端点探测。"""
    args = coerce_arguments("get_route_status", arguments, _names(GET_ROUTE_STATUS_SCHEMA))
    dataset_id = optional_str(args, "dataset_id")
    probe = optional_bool(args, "probe", default=False)
    timeout = optional_int(args, "timeout_seconds", default=3, minimum=1, maximum=60) or 3

    sources = _source_configs(context, None)
    if dataset_id is not None and not any(
        dataset_id in config.datasets for config in sources.values()
    ):
        raise not_found_error("dataset", dataset_id)

    routes: list[dict[str, Any]] = []
    for source_id, config in sources.items():
        if dataset_id is not None and dataset_id not in config.datasets:
            continue
        adapter = _adapter_for(context, source_id)
        capabilities = None if adapter is None else adapter.capabilities()
        for binding in config.transports:
            route_name = binding.route.value
            supported_by_adapter = capabilities is None or route_name in capabilities.routes
            routes.append(
                {
                    "provider": source_id,
                    "route": route_name,
                    "adapter": binding.adapter,
                    "configured": binding.implemented,
                    "supported_by_adapter": supported_by_adapter,
                    "usable": binding.implemented and supported_by_adapter,
                    "endpoint": config.endpoint,
                    "reason": _route_reason(binding.implemented, supported_by_adapter, adapter),
                }
            )

    payload: dict[str, Any] = {
        "routes": routes,
        "total": len(routes),
        "probe": {"requested": probe, "performed": False},
    }
    if probe:
        payload["probe"] = _probe_routes(routes, timeout_seconds=timeout)
    note = _configuration_note(context)
    if note is not None:
        payload["note"] = note
    return payload


def _route_reason(
    configured: bool, supported_by_adapter: bool, adapter: SourceAdapter | None
) -> str | None:
    if not configured:
        return "route is declared in configuration but marked implemented=false"
    if not supported_by_adapter:
        return "adapter does not report support for this access route"
    if adapter is None:
        return "no adapter instance is constructed for this source"
    return None


def _probe_routes(routes: Sequence[dict[str, Any]], *, timeout_seconds: int) -> dict[str, Any]:
    """对可用路线的端点做真实 HEAD 探测（只读；失败如实记为 unreachable）。

    只探测 https 端点：s3/cds_api/globus 需要各自的客户端与凭证，不能用 HEAD 冒充其可用性。
    """
    import httpx

    results: list[dict[str, Any]] = []
    probed = 0
    for route in routes:
        endpoint = route.get("endpoint")
        if not route["usable"] or not isinstance(endpoint, str) or not endpoint.startswith("https"):
            results.append(
                {
                    "provider": route["provider"],
                    "route": route["route"],
                    "probed": False,
                    "reason": "probe is only defined for usable https endpoints",
                }
            )
            continue
        probed += 1
        try:
            response = httpx.head(endpoint, timeout=float(timeout_seconds), follow_redirects=True)
            status_code = response.status_code
            reachable = status_code < 500
        except Exception as exc:
            results.append(
                {
                    "provider": route["provider"],
                    "route": route["route"],
                    "endpoint": endpoint,
                    "probed": True,
                    "reachable": False,
                    "error": type(exc).__name__,
                }
            )
            continue
        results.append(
            {
                "provider": route["provider"],
                "route": route["route"],
                "endpoint": endpoint,
                "probed": True,
                "reachable": reachable,
                "status_code": status_code,
            }
        )
    return {"requested": True, "performed": probed > 0, "results": results}


def _names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)
