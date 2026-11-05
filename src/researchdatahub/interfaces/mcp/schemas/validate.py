"""18 个 MCP 工具的参数校验（手写、零依赖、错误可定位）。

为什么不用 ``jsonschema`` 库校验
--------------------------------
``mcp`` SDK 已带 ``jsonschema``，但接口层的校验还要负责**类型收敛**（YAML 里
``timeout: "30"`` 是字符串）与**中文定位错误**（``arguments.timeout_seconds: 必须是整数``）。
把这两件事和纯 schema 校验分开写会让错误码来源分散。这里用一份集中实现：

* ``reject_unknown`` 拒绝未知字段（对齐 ``additionalProperties: false``）；
* 每个取值函数在类型/范围/枚举不合法时抛 ``invalid_arguments``，
  ``detail`` 里带 ``field`` 与 ``expected``，让 Agent 能自动纠错。

注意：本模块**不做业务判断**（例如"这个 plan 是否存在"），那属于 service 层。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from researchdatahub.interfaces.mcp.errors import invalid_arguments

__all__ = [
    "MAX_LIMIT",
    "Arguments",
    "coerce_arguments",
    "optional_bool",
    "optional_int",
    "optional_str",
    "optional_str_list",
    "require_mapping",
    "require_str",
    "require_str_list",
]

type Arguments = Mapping[str, Any]
"""校验/收敛后的参数类型别名。

用 ``Mapping``（而不是 ``dict``）是有意的：工具 handler 接收的是 ``Mapping[str, Any]``，
若 helper 要求 ``dict``，每个调用点都得先转换或写 ``cast``。这里只读参数表，
不修改它，``Mapping`` 更准确也更省事。
"""

MAX_LIMIT: Final[int] = 200
"""列表类工具的 ``limit`` 上限。上限存在的意义是防止一次调用把整个库读进内存。"""


def coerce_arguments(
    name: str,
    arguments: Mapping[str, Any] | None,
    allowed: Sequence[str],
) -> Arguments:
    """校验顶层形状：必须是对象、键必须都在白名单内。

    Raises
    ------
    MCPError
        ``invalid_arguments``：参数不是对象、或包含 schema 未声明的字段。
    """
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise invalid_arguments(
            f"arguments for tool {name!r} must be a JSON object",
            detail={"tool": name, "received_type": type(arguments).__name__},
        )
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise invalid_arguments(
            f"unknown argument(s) for tool {name!r}: {', '.join(unknown)}",
            detail={"tool": name, "unknown": unknown, "allowed": list(allowed)},
        )
    return dict(arguments)


def _type_name(value: Any) -> str:
    return type(value).__name__


def require_str(arguments: Arguments, field: str, *, max_length: int = 4096) -> str:
    """取必填字符串；去首尾空白后不得为空。

    Raises
    ------
    MCPError
        ``invalid_arguments``：字段缺失、类型不是字符串或为空。
    """
    if field not in arguments or arguments[field] is None:
        raise invalid_arguments(
            f"missing required argument {field!r}",
            detail={"field": field, "expected": "string"},
        )
    value = arguments[field]
    if not isinstance(value, str):
        raise invalid_arguments(
            f"argument {field!r} must be a string, got {_type_name(value)}",
            detail={"field": field, "expected": "string", "received_type": _type_name(value)},
        )
    stripped = value.strip()
    if not stripped:
        raise invalid_arguments(
            f"argument {field!r} must not be empty", detail={"field": field, "expected": "string"}
        )
    if len(stripped) > max_length:
        raise invalid_arguments(
            f"argument {field!r} exceeds {max_length} characters",
            detail={"field": field, "max_length": max_length, "length": len(stripped)},
        )
    return stripped


def optional_str(
    arguments: Arguments,
    field: str,
    *,
    default: str | None = None,
    max_length: int = 4096,
) -> str | None:
    """取可选字符串（``None`` 与缺席等价，返回 ``default``）。"""
    value = arguments.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise invalid_arguments(
            f"argument {field!r} must be a string or null, got {_type_name(value)}",
            detail={"field": field, "expected": "string", "received_type": _type_name(value)},
        )
    stripped = value.strip()
    if not stripped:
        return default
    if len(stripped) > max_length:
        raise invalid_arguments(
            f"argument {field!r} exceeds {max_length} characters",
            detail={"field": field, "max_length": max_length, "length": len(stripped)},
        )
    return stripped


def optional_int(
    arguments: Arguments,
    field: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """取可选整数（拒绝 ``bool``——在 Python 里 ``True`` 是 ``int`` 子类，极易误传）。"""
    value = arguments.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_arguments(
            f"argument {field!r} must be an integer, got {_type_name(value)}",
            detail={"field": field, "expected": "integer", "received_type": _type_name(value)},
        )
    if minimum is not None and value < minimum:
        raise invalid_arguments(
            f"argument {field!r} must be >= {minimum}, got {value}",
            detail={"field": field, "minimum": minimum, "received": value},
        )
    if maximum is not None and value > maximum:
        raise invalid_arguments(
            f"argument {field!r} must be <= {maximum}, got {value}",
            detail={"field": field, "maximum": maximum, "received": value},
        )
    narrowed: int = value
    return narrowed


def optional_bool(arguments: Arguments, field: str, *, default: bool = False) -> bool:
    """取可选布尔。"""
    value = arguments.get(field)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise invalid_arguments(
            f"argument {field!r} must be a boolean, got {_type_name(value)}",
            detail={"field": field, "expected": "boolean", "received_type": _type_name(value)},
        )
    return value


def require_str_list(
    arguments: Arguments,
    field: str,
    *,
    max_items: int = 128,
    item_max_length: int = 256,
) -> tuple[str, ...]:
    """取必填字符串数组；空数组被视为非法（"什么都不选"通常不是调用方本意）。"""
    value = arguments.get(field)
    if value is None:
        raise invalid_arguments(
            f"missing required argument {field!r}",
            detail={"field": field, "expected": "array of strings"},
        )
    return _str_list(value, field, max_items=max_items, item_max_length=item_max_length)


def optional_str_list(
    arguments: Arguments,
    field: str,
    *,
    max_items: int = 128,
    item_max_length: int = 256,
) -> tuple[str, ...] | None:
    """取可选字符串数组；缺席返回 ``None``（与"空数组"区分开）。"""
    value = arguments.get(field)
    if value is None:
        return None
    return _str_list(value, field, max_items=max_items, item_max_length=item_max_length)


def _str_list(value: Any, field: str, *, max_items: int, item_max_length: int) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise invalid_arguments(
            f"argument {field!r} must be an array of strings, got {_type_name(value)}",
            detail={"field": field, "expected": "array of strings"},
        )
    if len(value) == 0:
        raise invalid_arguments(
            f"argument {field!r} must not be empty",
            detail={"field": field, "expected": "non-empty array of strings"},
        )
    if len(value) > max_items:
        raise invalid_arguments(
            f"argument {field!r} accepts at most {max_items} items, got {len(value)}",
            detail={"field": field, "max_items": max_items, "received": len(value)},
        )
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise invalid_arguments(
                f"argument {field}[{index}] must be a string, got {_type_name(item)}",
                detail={"field": f"{field}[{index}]", "expected": "string"},
            )
        stripped = item.strip()
        if not stripped:
            raise invalid_arguments(
                f"argument {field}[{index}] must not be empty",
                detail={"field": f"{field}[{index}]", "expected": "non-empty string"},
            )
        if len(stripped) > item_max_length:
            raise invalid_arguments(
                f"argument {field}[{index}] exceeds {item_max_length} characters",
                detail={"field": f"{field}[{index}]", "max_length": item_max_length},
            )
        items.append(stripped)
    return tuple(items)


def require_mapping(arguments: Arguments, field: str) -> Mapping[str, Any]:
    """取必填对象（用于 ``request`` / ``dataset`` 这类结构化入参）。"""
    value = arguments.get(field)
    if value is None:
        raise invalid_arguments(
            f"missing required argument {field!r}",
            detail={"field": field, "expected": "object"},
        )
    if not isinstance(value, Mapping):
        raise invalid_arguments(
            f"argument {field!r} must be an object, got {_type_name(value)}",
            detail={"field": field, "expected": "object", "received_type": _type_name(value)},
        )
    return value
