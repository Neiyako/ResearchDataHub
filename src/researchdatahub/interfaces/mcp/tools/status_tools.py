"""任务状态与控制工具：``get_task_status`` / ``cancel_task``。

两条硬约束
----------
1. **状态只从 SQLite 读**（``ManifestRepository.load_task`` / ``TaskControlService.status``），
   MCP 进程内存里没有任何任务状态副本，因此服务重启后 Agent 仍然能查到同一结果。
2. **取消经由状态机**（``scheduler.service.TaskControlService.cancel``）：本模块不改
   状态字段，也不捕获 ``StateTransitionError`` 后"顺手改一下"；终态任务逐条返回
   ``skipped`` 与原因，让 Agent 知道为什么没变。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from researchdatahub.core.errors import ManifestError
from researchdatahub.core.state import TERMINAL_STATES
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.schemas import (
    CANCEL_TASK_SCHEMA,
    GET_TASK_STATUS_SCHEMA,
    coerce_arguments,
    optional_str,
    require_str_list,
)
from researchdatahub.interfaces.mcp.tools._shared import json_ready
from researchdatahub.scheduler.service import TaskControlService

__all__ = ["cancel_task", "get_task_status"]

_DEFAULT_EVENT_LIMIT = 10


def _status_view(context: McpContext, task_id: str) -> dict[str, Any]:
    """读取单个任务的状态视图（每次调用一条新连接，跨线程安全）。"""
    with context.repository() as repo:
        view = TaskControlService(repo).status(task_id)
    payload = view.as_dict()
    return {str(key): json_ready(value) for key, value in payload.items()}


def get_task_status(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """批量查询任务状态（逐条给出成功/失败，不让一个坏 ID 拖垮整批）。"""
    args = coerce_arguments("get_task_status", arguments, _fields(GET_TASK_STATUS_SCHEMA))
    task_ids = require_str_list(args, "task_ids", max_items=200)
    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for task_id in task_ids:
        try:
            tasks.append(_status_view(context, task_id))
        except ManifestError as exc:
            errors.append(
                {
                    "task_id": task_id,
                    "code": "not_found" if _is_missing(exc) else "manifest_error",
                    "message": exc.message,
                }
            )
    payload: dict[str, Any] = {
        "tasks": tasks,
        "errors": errors,
        "found": len(tasks),
        "missing": len(errors),
        "source": "sqlite",
    }
    if errors and not tasks:
        # 整批都不存在：这是调用方 ID 用错了，应该让 Agent 看到错误码而不是空列表。
        from researchdatahub.interfaces.mcp.errors import not_found_error

        raise not_found_error("task", ", ".join(item["task_id"] for item in errors))
    return payload


def cancel_task(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """取消任务（状态机 + 乐观并发）；终态任务返回 skipped。"""
    args = coerce_arguments("cancel_task", arguments, _fields(CANCEL_TASK_SCHEMA))
    task_ids = require_str_list(args, "task_ids", max_items=200)
    reason = optional_str(args, "reason", default="mcp cancel_task") or "mcp cancel_task"
    cancelled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    with context.repository() as repo:
        service = TaskControlService(repo, clock=None)
        for task_id in task_ids:
            try:
                task = repo.load_task(task_id)
            except ManifestError as exc:
                skipped.append(
                    {
                        "task_id": task_id,
                        "reason": "not_found" if _is_missing(exc) else "manifest_error",
                        "detail": exc.message,
                    }
                )
                continue
            if task.state in TERMINAL_STATES:
                skipped.append(
                    {
                        "task_id": task_id,
                        "reason": "terminal",
                        "state": task.state.value,
                        "detail": "terminal tasks are immutable; nothing was changed",
                    }
                )
                continue
            try:
                updated = service.cancel(task_id, reason=reason)
            except Exception as exc:
                skipped.append(
                    {
                        "task_id": task_id,
                        "reason": "transition_rejected",
                        "detail": getattr(exc, "message", type(exc).__name__),
                    }
                )
                continue
            cancelled.append(
                {
                    "task_id": task_id,
                    "previous_state": task.state.value,
                    "state": updated.state.value,
                }
            )
    return {
        "cancelled": cancelled,
        "skipped": skipped,
        "cancelled_count": len(cancelled),
        "skipped_count": len(skipped),
    }


def _is_missing(exc: ManifestError) -> bool:
    message = exc.message.lower()
    return "not found" in message or "does not exist" in message or "no such" in message


def _fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)
