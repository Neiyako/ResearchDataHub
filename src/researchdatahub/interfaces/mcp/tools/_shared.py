"""工具实现的公共工具：错误翻译、JSON 化、审批前置校验。

这里放"每个 handler 都要用、但不能放进 handler 里重复写"的东西：
把可选包/OpenAPI 形状的异常翻成 MCP 错误码、把结果 JSON 化、以及 ``submit_acquisition``
需要的计划/审批前置读取。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import AcquisitionPlan, JsonValue
from researchdatahub.interfaces.mcp.auth import (
    ApprovalRecord,
    ApprovalStatus,
    plan_fingerprint,
)
from researchdatahub.interfaces.mcp.capabilities import CapabilityUnavailable, to_json_value
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.errors import MCPError, NotAvailableError, not_found_error
from researchdatahub.manifest.repository import SqliteManifestRepository

__all__ = [
    "ToolHandler",
    "approval_summary",
    "ensure_plan_exists",
    "json_ready",
    "plan_fingerprint_of",
    "read_approval_record",
    "translate_capability_error",
]

type ToolHandler = Callable[[McpContext, Mapping[str, Any]], dict[str, Any]]
"""工具处理函数签名：``(context, arguments) -> data``；抛 ``MCPError``/``RdhError`` 表示失败。

返回的是**信封里的 data 部分**，信封由 ``server.py`` 统一包装（``ok`` / ``meta`` 等）。
handler 不需要（也不应该）自己算 ``duration_ms``。
"""


def json_ready(value: object) -> JsonValue:
    """把任意 service 返回值转成 JSON 安全结构。"""
    return to_json_value(value)


def translate_capability_error(exc: CapabilityUnavailable) -> MCPError:
    """把可选能力缺失翻成结构化 ``not_available``（附 capability 名称，便于 Agent 决策）。"""
    return NotAvailableError(
        f"capability {exc.capability!r} is not available: {exc.reason}",
        detail={"capability": exc.capability, "available": False},
    )


def ensure_plan_exists(repo: SqliteManifestRepository, plan_id: str) -> AcquisitionPlan:
    """读取计划；不存在时抛 ``not_found``（而不是让 ``ManifestError`` 冒成内部错误）。

    ``ManifestRepository.load_plan`` 的契约是"不存在 → ManifestError"；MCP 侧把它翻成
    更准确的 ``not_found``，让 Agent 知道该改 ID 而不是重试。
    """
    try:
        return repo.load_plan(plan_id)
    except RdhError as exc:
        if _is_missing(exc):
            raise not_found_error("plan", plan_id) from exc
        raise


def _is_missing(exc: RdhError) -> bool:
    """判断异常是否等价于"记录不存在"（按 detail 里的 id 与消息判定，避免误吞真故障）。"""
    detail = exc.detail or {}
    if detail.get("missing") is True:
        return True
    message = exc.message.lower()
    markers = ("not found", "does not exist", "no such", "is not registered", "not registered")
    return any(marker in message for marker in markers)


def plan_fingerprint_of(plan: AcquisitionPlan) -> str:
    """计划指纹（令牌绑定用）：与 ``auth.plan_fingerprint`` 共用同一实现。"""
    return plan_fingerprint(plan)


def read_approval_record(context: McpContext, plan_id: str) -> ApprovalRecord | None:
    """读取审批记录（不存在返回 ``None``）。"""
    with context.approval_store() as store:
        return store.get(plan_id)


def approval_summary(record: ApprovalRecord | None) -> dict[str, JsonValue]:
    """把审批记录整理成对外的摘要（**不含令牌**，只含 sha256 摘要与状态）。"""
    if record is None:
        return {"status": "none", "available": False}
    payload = record.as_dict()
    payload["available"] = record.status is ApprovalStatus.APPROVED
    return {str(key): json_ready(value) for key, value in payload.items()}
