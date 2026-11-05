"""环境类工具：``get_environment_requirements`` / ``check_environment`` / ``validate_environment``。

依赖 M2 ``researchdatahub.environment``，**可选**并如实降级
-----------------------------------------------------------
三个工具都在调用前检查 M2 的入口是否存在；不存在时抛 ``NotAvailableError``
（对外 ``not_available`` + ``data.capability="environment"``），不返回空结果冒充"环境没问题"。

三个工具的分工（不要混用）
--------------------------
* ``get_environment_requirements``：**只读声明**——这个 Provider 要求什么（依赖/凭据/端点），
  以及可操作的配置步骤。不探测任何东西。
* ``check_environment``：**真实探测**——依赖是否可导入、凭据是否存在、端点是否可达、
  磁盘是否够。会发起网络请求（只读 HEAD/GET）。
* ``validate_environment``：**结论折叠**——把上面的探测结果整理成"能不能现在执行"，
  并列出阻断项。用于 Agent 在做决定前的一次确认。

安全：M2 的约定是"凭据只以引用形式存在"，M5 只转述它的结论；即便 M2 报告里出现
``observed`` 字段，也只会是计数/文件名/端点这类可公开信息（M2 自带
``assert_report_has_no_secret`` 兜底）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from researchdatahub.interfaces.mcp.capabilities import (
    ENVIRONMENT_CANDIDATES,
    CapabilityUnavailable,
    import_first_available,
    to_json_value,
)
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.errors import NotAvailableError
from researchdatahub.interfaces.mcp.schemas import (
    CHECK_ENVIRONMENT_SCHEMA,
    GET_ENVIRONMENT_REQUIREMENTS_SCHEMA,
    VALIDATE_ENVIRONMENT_SCHEMA,
    coerce_arguments,
    optional_bool,
    optional_str,
    optional_str_list,
    require_str,
)

__all__ = [
    "check_environment",
    "get_environment_requirements",
    "validate_environment",
]

_SUPPORTED_CHECKS: frozenset[str] = frozenset({"dependencies", "credentials", "network", "disk"})
_DEFAULT_TIMEOUT_SECONDS = 5

_READY_STATUSES: frozenset[str] = frozenset({"ok"})
"""M2 ``OverallStatus.ok`` 是**唯一**的"可以执行"；``needs_configuration`` / ``unavailable``
都不是（后者尤其不能被当成"稍微差点但能跑"）。"""


def get_environment_requirements(
    context: McpContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """返回 Provider 的环境要求与可操作配置步骤（不探测）。"""
    args = coerce_arguments(
        "get_environment_requirements",
        arguments,
        _fields(GET_ENVIRONMENT_REQUIREMENTS_SCHEMA),
    )
    provider = require_str(args, "provider", max_length=128)
    module, module_name = _environment_module()

    requirements: Any = None
    get_requirements = getattr(module, "get_requirements", None)
    if callable(get_requirements):
        requirements = get_requirements(provider)
    guide: Any = None
    get_guide = getattr(module, "get_environment_requirements", None)
    if callable(get_guide):
        try:
            guide = get_guide(provider)
        except KeyError:
            guide = None

    if requirements is None and guide is None:
        raise NotAvailableError(
            "researchdatahub.environment exposes no requirements entrypoint in this version",
            detail={"capability": "environment", "provider": provider},
        )
    payload: dict[str, Any] = {
        "provider": provider,
        "module": module_name,
        "tool": "get_environment_requirements",
        "requirements": to_json_value(requirements),
        "guide": to_json_value(guide),
        "probed": False,
    }
    return payload


def check_environment(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """真实探测 Provider 的执行前提（依赖/凭据/网络/磁盘）。"""
    args = coerce_arguments("check_environment", arguments, _fields(CHECK_ENVIRONMENT_SCHEMA))
    provider = require_str(args, "provider", max_length=128)
    checks = _checks(args)
    timeout = _timeout(args)
    module, module_name = _environment_module()

    report = _call_validate(module, provider, checks=checks, timeout=timeout, context=context)
    payload: dict[str, Any] = {
        "provider": provider,
        "module": module_name,
        "tool": "check_environment",
        "report": to_json_value(report),
        "probed": True,
        "checks_requested": list(checks) if checks else list(_SUPPORTED_CHECKS),
    }
    summary = to_json_value(report)
    if isinstance(summary, dict):
        status_value = summary.get("status")
        payload["status"] = status_value
        payload["summary"] = summary.get("summary")
        # 与 validate_environment 同一口径：只有 OverallStatus.ok 才算"现在可执行"。
        payload["usable"] = status_value in _READY_STATUSES
    return payload


def validate_environment(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """折叠探测结果，回答"现在能不能执行"。"""
    args = coerce_arguments("validate_environment", arguments, _fields(VALIDATE_ENVIRONMENT_SCHEMA))
    provider = require_str(args, "provider", max_length=128)
    require_network = optional_bool(args, "require_network", default=False)
    checks = _checks(args)
    timeout = _timeout(args)
    if require_network and checks is not None and "network" not in checks:
        checks = (*checks, "network")
    module, module_name = _environment_module()

    report = _call_validate(module, provider, checks=checks, timeout=timeout, context=context)
    raw = to_json_value(report)
    blocking: list[dict[str, Any]] = []
    status_value: Any = None
    if isinstance(raw, dict):
        status_value = raw.get("status")
        requirements = raw.get("requirements")
        if isinstance(requirements, list):
            for item in requirements:
                if not isinstance(item, dict):
                    continue
                item_status = str(item.get("status", ""))
                required = bool(item.get("required", True))
                if required and item_status in {"missing", "failed", "error", "unverified"}:
                    blocking.append(
                        {
                            "name": item.get("name"),
                            "kind": item.get("kind"),
                            "status": item_status,
                            "guidance": item.get("guidance"),
                        }
                    )
    usable = status_value in _READY_STATUSES
    if require_network and not _network_ok(raw):
        usable = False
        if not any(item.get("kind") == "network" for item in blocking):
            blocking.append(
                {
                    "name": "network",
                    "kind": "network",
                    "status": "not_verified",
                    "guidance": "require_network=true but the network check did not report ok",
                }
            )
    return {
        "provider": provider,
        "module": module_name,
        "tool": "validate_environment",
        "usable": usable,
        "status": status_value,
        "blocking": blocking,
        "require_network": require_network,
        "report": raw,
        "probed": True,
    }


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _environment_module() -> tuple[Any, str]:
    """解析 M2 模块（``validate_environment`` 是必需入口）。

    包已落地，因此这里只在**真的导入不到**时降级；降级理由里带模块名，便于运维定位
    （例如装了不含 M2 的旧版本 wheel）。
    """
    try:
        return import_first_available(ENVIRONMENT_CANDIDATES, ("validate_environment",))
    except CapabilityUnavailable as exc:
        raise NotAvailableError(
            f"environment validation is not available: {exc.reason}",
            detail={
                "capability": "environment",
                "available": False,
                "package": "researchdatahub.environment",
            },
        ) from exc


def _call_validate(
    module: Any,
    provider: str,
    *,
    checks: tuple[str, ...] | None,
    timeout: int | None,
    context: McpContext,
) -> Any:
    """调用 M2 的 ``validate_environment``，只传双方都认的关键字。

    M2 的签名仍在演进，因此这里按"探测到的参数名"传：``check_network`` 由 ``checks``
    推出（没请求 network 就不联网），``storage_root`` 用本服务的状态根，
    ``free_space_floor_gb`` 用配置值。M2 若新增/删减参数，这里不会因为位置参数错位而炸。
    """
    validate = getattr(module, "validate_environment", None)
    if validate is None:
        raise NotAvailableError(
            "researchdatahub.environment.validate_environment is missing in this version",
            detail={"capability": "environment", "provider": provider},
        )
    kwargs: dict[str, Any] = {
        "storage_root": str(context.storage_root),
        "free_space_floor_gb": float(context.defaults.storage.free_space_floor_gb),
        "check_network": (checks is None) or ("network" in checks),
    }
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    sources_path = _sources_path(context)
    if sources_path is not None:
        kwargs["sources_path"] = sources_path
    accepted = _accepted_kwargs(validate, kwargs)
    try:
        return validate(provider, **accepted)
    except KeyError as exc:
        raise NotAvailableError(
            f"unknown provider {provider!r} for environment checks",
            detail={"capability": "environment", "provider": provider, "reason": str(exc)},
        ) from exc


def _accepted_kwargs(func: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
    """只保留目标函数签名里存在的关键字（``**kwargs`` 时全传）。"""
    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建/C 函数
        return dict(candidate)
    parameters = signature.parameters
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return dict(candidate)
    return {key: value for key, value in candidate.items() if key in parameters}


def _sources_path(context: McpContext) -> str | None:
    """数据源配置路径（供 M2 核对"配置里的凭据引用"）；没有配置就返回 ``None``。"""
    if context.sources_config is None:
        return None
    from researchdatahub.interfaces.mcp.context import DEFAULT_SOURCES_PATH, LOCAL_SOURCES_PATH

    for candidate in (LOCAL_SOURCES_PATH, DEFAULT_SOURCES_PATH):
        if candidate.is_file():
            return str(candidate)
    return None


def _network_ok(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    requirements = raw.get("requirements")
    if not isinstance(requirements, list):
        return False
    for item in requirements:
        if isinstance(item, dict) and item.get("kind") == "network":
            return str(item.get("status", "")) in {"ok", "present", "satisfied"}
    return False


def _checks(arguments: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = optional_str_list(arguments, "checks", max_items=8, item_max_length=32)
    if raw is None:
        return None
    from researchdatahub.interfaces.mcp.errors import invalid_arguments

    unknown = [item for item in raw if item not in _SUPPORTED_CHECKS]
    if unknown:
        raise invalid_arguments(
            f"unsupported check(s): {', '.join(unknown)}",
            detail={"field": "checks", "allowed": sorted(_SUPPORTED_CHECKS)},
        )
    return raw


def _timeout(arguments: Mapping[str, Any]) -> int | None:
    raw = arguments.get("timeout_seconds")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        from researchdatahub.interfaces.mcp.errors import invalid_arguments

        raise invalid_arguments(
            "timeout_seconds must be a number",
            detail={"field": "timeout_seconds", "expected": "number"},
        )
    if not 0.1 <= float(raw) <= 120:
        from researchdatahub.interfaces.mcp.errors import invalid_arguments

        raise invalid_arguments(
            "timeout_seconds must be within [0.1, 120]",
            detail={"field": "timeout_seconds", "minimum": 0.1, "maximum": 120},
        )
    return int(float(raw))


def _fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)


_ = optional_str  # 保持导入显式：其余工具共用同一组取值函数
