"""可选能力探测：M2/M3/M4 是否可用，以及把它们的结果解码成 JSON。

设计原则（为什么不是直接 import 调用）
------------------------------------
``researchdatahub.environment``（M2）、``researchdatahub.providers``（M3）、
``researchdatahub.network``（M4）由其它成员并行开发，**API 仍在变化**。若 M5 的模型层
直接 ``from researchdatahub.environment import EnvironmentReport``：

* 对方改名/移动类，M5 连 import 都过不了，整个 MCP 服务起不来；
* 对方尚未提交时同样起不来。

因此本模块只做两件事：

1. **能力探测**：``importlib.import_module`` + ``hasattr``，检查入口函数/类是否存在，
   **不执行**任何检查逻辑，也不在模块顶层 import 它们；
2. **结果解码**：把可选包返回的 pydantic 模型 / dataclass 安全转成 JSON 可序列化结构。

真正的调用发生在 ``tools/`` 层（那里才决定传什么参数），且每条调用都经过
:class:`CapabilityUnavailable` → ``not_available`` 的降级路径。

子模块回退
----------
并行成员可能先落地子模块（``network.workers.registry``）再补 ``__init__`` 的再导出。
探测时按"包顶层 → 已知子模块"顺序尝试，避免"代码写了但没导出"被误判为不可用。
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol, TypeGuard

from researchdatahub.core.models import JsonValue

__all__ = [
    "ENVIRONMENT_CANDIDATES",
    "NETWORK_CANDIDATES",
    "CapabilityUnavailable",
    "describe_available_capabilities",
    "import_first_available",
    "to_json_value",
]

logger = logging.getLogger(__name__)

ENVIRONMENT_CANDIDATES: Final[tuple[str, ...]] = (
    "researchdatahub.environment",
    "researchdatahub.environment.validation",
    "researchdatahub.environment.validation.validator",
    "researchdatahub.environment.registry",
)
"""M2 的候选模块（按优先级：包顶层 → 子模块）。"""

NETWORK_CANDIDATES: Final[tuple[str, ...]] = (
    "researchdatahub.network.workers",
    "researchdatahub.network.workers.registry",
    "researchdatahub.network.workers.store",
)
"""M4 控制面的候选模块。"""

NETWORK_ENTRYPOINTS: Final[tuple[str, ...]] = ("WorkerRegistry", "WorkerStore")


class _EnumLike(Protocol):
    """带 ``value`` 的枚举成员形状（供 :func:`_is_enum` 的类型守卫使用）。"""

    @property
    def value(self) -> object:
        """枚举成员的稳定取值。"""
        ...


class CapabilityUnavailable(Exception):
    """可选能力不可用（工具层据此返回 ``not_available``，不伪装成功）。"""

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(reason)
        self.capability = capability
        self.reason = reason


def _optional_import(module_name: str) -> Any | None:
    """尝试导入可选模块；异常一律视为"不可用"（不吞 KeyboardInterrupt/SystemExit）。"""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None
    except Exception as exc:
        logger.info("optional module %s raised on import: %s", module_name, type(exc).__name__)
        return None


def _find_symbol(candidates: Sequence[str], names: Sequence[str]) -> tuple[Any, str] | None:
    """在候选模块里找第一个同时具备所需符号的模块，返回 ``(module, module_name)``。"""
    for module_name in candidates:
        module = _optional_import(module_name)
        if module is None:
            continue
        if all(hasattr(module, name) for name in names):
            return module, module_name
    return None


def import_first_available(candidates: Sequence[str], names: Sequence[str]) -> tuple[Any, str]:
    """在候选模块里找同时具备 ``names`` 的模块。

    Raises
    ------
    CapabilityUnavailable
        没有任何候选模块同时具备全部符号。
    """
    found = _find_symbol(candidates, names)
    if found is None:
        raise CapabilityUnavailable(
            "optional",
            f"none of {list(candidates)} exports all of {list(names)}",
        )
    return found


def _probe(candidates: Sequence[str], names: Sequence[str]) -> dict[str, Any]:
    """探测一组符号是否可用（只回答"在不在"，不代表检查会通过）。"""
    for module_name in candidates:
        module = _optional_import(module_name)
        if module is None:
            continue
        present = [name for name in names if hasattr(module, name)]
        if present:
            return {"available": True, "module": module_name, "entrypoints": present}
    return {"available": False, "reason": "no candidate module exports the required symbols"}


def describe_available_capabilities() -> dict[str, Any]:
    """静态能力摘要（供能力工具展示；**不执行**任何检查逻辑）。"""
    return {
        "environment_check": _probe(ENVIRONMENT_CANDIDATES, ("validate_environment",)),
        "environment_requirements": _probe(
            ENVIRONMENT_CANDIDATES, ("get_requirements", "get_environment_requirements")
        ),
        "network": _probe(NETWORK_CANDIDATES, NETWORK_ENTRYPOINTS),
    }


def to_json_value(value: object) -> JsonValue:
    """把可选包返回值转成 JSON 安全结构（**不猜测**：未知类型降级为 ``str``）。

    支持：``None`` / 标量 / ``Mapping`` / ``Sequence`` / pydantic ``model_dump`` /
    ``dataclasses.asdict`` / 带 ``as_json`` / ``to_dict`` / ``as_dict`` 的对象。
    其它对象只有**公开且全是标量**的属性时才展开，否则用 ``str()``。
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json_value(item) for item in value]
    if _is_enum(value):
        return str(value.value)
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return to_json_value(dumper(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json_value(dataclasses.asdict(value))
    for method_name in ("as_json", "to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return to_json_value(method())
    public = _scalar_public_attributes(value)
    if public is not None:
        return public
    return str(value)


def _is_enum(value: object) -> TypeGuard[_EnumLike]:
    """判断对象是否是枚举成员（``TypeGuard`` 让后续访问 ``.value`` 通过类型检查）。"""
    return hasattr(value, "value") and hasattr(type(value), "__members__")


def _scalar_public_attributes(value: object) -> dict[str, JsonValue] | None:
    try:
        attributes = {name: item for name, item in vars(value).items() if not name.startswith("_")}
    except TypeError:
        return None
    if not attributes:
        return None
    converted: dict[str, JsonValue] = {}
    for name, item in attributes.items():
        if item is None or isinstance(item, bool | int | float | str):
            converted[name] = item
            continue
        return None
    return converted
