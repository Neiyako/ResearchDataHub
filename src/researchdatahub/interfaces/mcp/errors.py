"""MCP 统一错误信封：稳定错误码 + 可执行提示 + 可重试标记。

为什么要单独一层错误码
----------------------
MCP 是**外部 AI Agent 的唯一正式执行接口**，调用方是一个不会读堆栈的模型。它需要的是：

1. **稳定机器可读的 code**（可以写进 if 分支，不随文案变化）；
2. **可执行的 hint**（下一步该改参数、还是等一会儿再试）；
3. **retryable 标记**（决定是重试还是换方案）。

因此本模块把 ``core.errors.RdhError.code`` 映射为 MCP 侧的错误信封，并补充本项目
特有的三个「接口层」错误码（``invalid_arguments`` / ``not_available`` / ``internal_error``）。
未预期的异常一律折叠为 ``internal_error``：**不带堆栈、不带异常字符串**，因为那既可能
泄露密钥，也会让调用方误以为可以按具体异常类型分支。

错误码是全大写下划线字符串（例如 ``approval_invalid``），不是 ``RDH_*``：``RDH_*`` 是
Core 的内部码，MCP 侧刻意用另一套命名空间，避免"改一个内部错误码就破坏外部契约"。
两者的对应关系见 :data:`CORE_CODE_TO_MCP_CODE`，并在 ``docs/mcp.md`` 里列表维护。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from researchdatahub.core.errors import RdhError

__all__ = [
    "APPROVAL_INVALID_CODE",
    "CORE_CODE_TO_MCP_CODE",
    "ENVELOPE_SCHEMA_VERSION",
    "ERROR_HINTS",
    "ApprovalInvalidError",
    "MCPError",
    "NotAvailableError",
    "approval_required_error",
    "conflict_error",
    "envelope_error",
    "envelope_success",
    "error_from_exception",
    "invalid_arguments",
    "invalid_state_error",
    "not_found_error",
]

APPROVAL_INVALID_CODE: Final[str] = "approval_invalid"
"""审批令牌被拒绝时**唯一**使用的错误码（缺失/伪造/过期/重放/计划已改都归到这里）。"""

ENVELOPE_SCHEMA_VERSION: Final[str] = "1.0"
"""返回信封的 schema 版本。破坏性变更时才升，调用方据此判断是否需要迁移。"""

ERROR_CODES: Final[tuple[str, ...]] = (
    "invalid_arguments",
    "not_found",
    "conflict",
    "approval_invalid",
    "approval_required",
    "not_available",
    "invalid_state",
    "config_error",
    "model_invalid",
    "source_not_found",
    "source_unavailable",
    "metadata_error",
    "storage_error",
    "manifest_error",
    "schedule_error",
    "integrity_error",
    "transport_error",
    "not_implemented",
    "internal_error",
)
"""全部 MCP 错误码（``docs/mcp.md`` 的错误码表与本元组必须一致，由单测守护）。"""

CORE_CODE_TO_MCP_CODE: Final[Mapping[str, str]] = {
    "RDH_CONFIG": "config_error",
    "RDH_MODEL_INVALID": "model_invalid",
    "RDH_SOURCE": "source_unavailable",
    "RDH_SOURCE_NOT_FOUND": "source_not_found",
    "RDH_SOURCE_UNAVAILABLE": "source_unavailable",
    "RDH_METADATA": "metadata_error",
    "RDH_TRANSPORT": "transport_error",
    "RDH_TRANSPORT_RETRYABLE": "transport_error",
    "RDH_TRANSPORT_PERMANENT": "transport_error",
    "RDH_INTEGRITY": "integrity_error",
    "RDH_STORAGE": "storage_error",
    "RDH_MANIFEST": "manifest_error",
    "RDH_SCHEDULE": "schedule_error",
    "RDH_STATE_TRANSITION": "invalid_state",
    "RDH_NOT_IMPLEMENTED": "not_implemented",
    "RDH_REQUEST": "invalid_arguments",
}
"""Core 内部错误码 → MCP 对外错误码。未登记的 Core 码落到 ``internal_error`` 之外的
``source_unavailable`` 是不行的（会撒谎），因此未登记一律 ``internal_error`` 并在单测里
禁止把新 Core 码悄悄漏掉。"""

RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {"source_unavailable", "transport_error", "manifest_error"}
)
"""接口层口径：这些错误**可能**因为环境变化而恢复。

注意 ``manifest_error`` 之所以算可重试：SQLite ``SQLITE_BUSY`` 在并发写时是暂时的；
但错误信封里的 ``retryable`` 只是**建议**，不代表重试一定成立（例如数据库被删除）。
``approval_invalid`` 永远不是可重试的——重试同一个坏令牌不会变好。
"""

ERROR_HINTS: Final[Mapping[str, str]] = {
    "invalid_arguments": "检查参数名与取值是否符合 tools/list 的 inputSchema；未知字段一律拒绝。",
    "not_found": "用 list 类工具（如 get_execution_plan / get_task_status）确认 ID 是否存在。",
    "conflict": "目标已存在，请换一个路径或自行确认后覆盖（本服务不覆盖既有文件）。",
    "approval_invalid": "重新调用 create_execution_plan 拿新的 approval_token，并在过期前提交。",
    "approval_required": "submit_acquisition 需同时提供 plan_id 与计划生成的审批令牌。",
    "not_available": "该能力依赖的可选包/组件当前未安装或未注入；查看 data.capability 字段。",
    "invalid_state": "先查询当前状态（get_task_status / get_execution_plan）再决定动作。",
    "config_error": "检查配置（rdh config validate）与 --root 指向的状态根。",
    "model_invalid": "按返回的 data.detail 修正字段后重试。",
    "source_not_found": "检查 dataset_id / preferred_sources 是否在数据源配置中声明。",
    "source_unavailable": "数据源暂时不可用（凭证/配额/网络），稍后重试或换源。",
    "metadata_error": "远端元数据与声明不一致，检查需求文件。",
    "storage_error": "检查磁盘空间与目录权限。",
    "manifest_error": "Manifest 数据库操作失败，检查库文件是否存在并可写。",
    "schedule_error": "计划规模或调度约束不满足，缩小时间范围或调整并发。",
    "integrity_error": "文件完整性检查无法进行（缺失/不可读），不要把它当成通过。",
    "transport_error": "传输失败，按 retryable 决定是否重试。",
    "not_implemented": "该功能尚未实现，不要假设成功。",
    "internal_error": "服务端内部错误，详情已写 stderr 日志；请向维护者报告。",
}


def _coerce_retryable(code: str, exc: RdhError | None) -> bool:
    """计算 ``retryable``：先看接口层规则，再尊重 Core 的显式语义。"""
    if code not in RETRYABLE_CODES:
        return False
    explicit = getattr(exc, "retryable", None)
    if isinstance(explicit, bool):
        return explicit
    return True


def make_error(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    retryable: bool | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造标准 ``error`` 对象；``detail`` 必须已脱敏（由调用方保证）。"""
    resolved_retryable = code in RETRYABLE_CODES if retryable is None else retryable
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "hint": hint if hint is not None else ERROR_HINTS.get(code, ""),
        "retryable": resolved_retryable,
    }
    if detail:
        error["detail"] = dict(detail)
    return error


def error_from_exception(exc: BaseException) -> dict[str, Any]:
    """把异常转换成 ``error`` 对象。

    * :class:`MCPError` 子类与:class:`~researchdatahub.core.errors.RdhError` 子类保留
      **错误码与安全 detail**；
    * 其它异常（含 ``pydantic.ValidationError`` 的意外形态、``KeyError`` 等）一律
      ``internal_error``，message 使用固定文案——**不回显** ``str(exc)``，因为那可能包含
      路径、令牌片段或 SQL 语句。
    """
    if isinstance(exc, MCPError):
        return make_error(
            exc.code,
            exc.message,
            hint=exc.hint,
            retryable=exc.retryable,
            detail=exc.detail or None,
        )
    if isinstance(exc, RdhError):
        code = CORE_CODE_TO_MCP_CODE.get(exc.code, "internal_error")
        detail = _sanitize_detail(exc.detail)
        return make_error(
            code,
            exc.message,
            retryable=_coerce_retryable(code, exc),
            detail=detail or None,
        )
    return make_error(
        "internal_error",
        f"{type(exc).__name__} raised inside the tool handler; details withheld",
    )


_SECRET_MARKERS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "signature",
    "private_key",
)
"""detail 键名里出现这些片段时，值一律替换为 ``"[redacted]"``。"""


def _sanitize_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏 detail：按**键名**屏蔽疑似密钥字段。

    只处理键名而不扫描字符串内容，是因为内容扫描既会误伤（文件路径里出现 "token"）
    又不可靠；真正的保证是"代码里根本不把令牌放进 detail"（见 ``auth.py``）。
    """
    return {key: _sanitize_value(key, value) for key, value in detail.items()}


def _sanitize_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return _sanitize_detail(value)
    if isinstance(value, list | tuple):
        return [_sanitize_value(key, item) for item in value]
    return value


class MCPError(Exception):
    """MCP 接口层预期异常基类：自带对外错误码，不会被折叠成 ``internal_error``。"""

    code: str = "internal_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        detail: Mapping[str, Any] | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint if hint is not None else ERROR_HINTS.get(code or self.code, "")
        self.detail: dict[str, Any] = dict(detail or {})
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class ApprovalInvalidError(MCPError):
    """审批令牌缺失/不匹配/过期/已被消费；**任何**这一族都不得触发副作用。"""

    code = "approval_invalid"


class NotAvailableError(MCPError):
    """可选能力（``environment`` / ``providers`` / ``network`` 包）当前不可用。"""

    code = "not_available"


def approval_required_error(message: str) -> MCPError:
    """构造 ``approval_required`` 错误：没有任何令牌/审批子系统时拒绝执行。"""
    return MCPError(message, code="approval_required")


def invalid_state_error(message: str, *, detail: Mapping[str, Any] | None = None) -> MCPError:
    """构造 ``invalid_state`` 错误（当前状态不允许该动作）。"""
    return MCPError(message, code="invalid_state", detail=detail)


def invalid_arguments(message: str, *, detail: Mapping[str, Any] | None = None) -> MCPError:
    """构造 ``invalid_arguments`` 错误（供 schema 校验失败使用）。"""
    return MCPError(message, code="invalid_arguments", detail=detail)


def not_found_error(what: str, identifier: str) -> MCPError:
    """构造 ``not_found`` 错误。"""
    return MCPError(
        f"{what} {identifier!r} does not exist",
        code="not_found",
        detail={"kind": what, "id": identifier},
    )


def conflict_error(message: str, *, detail: Mapping[str, Any] | None = None) -> MCPError:
    """构造 ``conflict`` 错误（目标已存在且本服务不覆盖）。"""
    return MCPError(message, code="conflict", detail=detail)


def envelope_success(data: Mapping[str, Any], *, tool: str, duration_ms: float) -> dict[str, Any]:
    """构造成功信封。"""
    return {
        "ok": True,
        "data": dict(data),
        "error": None,
        "meta": {
            "tool": tool,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "duration_ms": round(duration_ms, 3),
        },
    }


def envelope_error(
    error: Mapping[str, Any],
    *,
    tool: str,
    duration_ms: float,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造失败信封。``data`` 允许携带**与错误无关的安全上下文**（例如当前状态）。"""
    return {
        "ok": False,
        "data": dict(data or {}),
        "error": dict(error),
        "meta": {
            "tool": tool,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "duration_ms": round(duration_ms, 3),
        },
    }
