"""``list_workers``：分布式 worker 列表（依赖 M4 ``researchdatahub.network``）。

诚实降级
--------
M4 不可用、或它没有提供可调用的 worker 入口时，本工具返回结构化 ``not_available``
（``ok=false`` + ``error.code=not_available``），**不会**返回空列表冒充"没有 worker"
——两者对 Agent 的含义完全不同：空列表意味着"注册表为空，可以注册"，而不可用意味着
"这套能力现在不存在，别据此判断"。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from researchdatahub.interfaces.mcp.capabilities import (
    NETWORK_CANDIDATES,
    NETWORK_ENTRYPOINTS,
    CapabilityUnavailable,
    import_first_available,
    to_json_value,
)
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.errors import NotAvailableError
from researchdatahub.interfaces.mcp.schemas import (
    LIST_WORKERS_SCHEMA,
    coerce_arguments,
    optional_int,
    optional_str,
)

__all__ = ["WORKERS_DB_FILENAME", "list_workers"]

WORKERS_DB_FILENAME = "workers.db"
"""worker 控制面库文件名（与 M4 ``network.workers.store`` 的约定一致：``<状态根>/workers.db``）。"""


def list_workers(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """列出 worker 记录（能力不可用时抛 ``not_available``）。"""
    args = coerce_arguments("list_workers", arguments, _fields(LIST_WORKERS_SCHEMA))
    status_filter = optional_str(args, "status")
    limit = optional_int(args, "limit", default=50, minimum=1, maximum=200) or 50

    try:
        # 优先用包顶层（M4 已把 WorkerRegistry / WorkerStore 再导出）；子模块作回退。
        module, module_name = import_first_available(NETWORK_CANDIDATES, NETWORK_ENTRYPOINTS)
    except CapabilityUnavailable as exc:
        raise NotAvailableError(
            f"worker registry is not available: {exc.reason}",
            detail={
                "capability": "network",
                "available": False,
                "package": "researchdatahub.network",
            },
        ) from exc

    db_path = context.storage_root / WORKERS_DB_FILENAME
    registry = _open_registry(module, db_path)
    rows: list[dict[str, Any]] = []
    for record in registry.list_workers():
        row = to_json_value(record)
        if not isinstance(row, dict):
            continue
        if status_filter is not None and not _matches_status(row, status_filter):
            continue
        rows.append(row)

    return {
        "workers": rows[:limit],
        "total": len(rows),
        "truncated": len(rows) > limit,
        "registry": module_name,
        "database": str(db_path),
        "available": True,
        "filter": {"status": status_filter},
    }


def _matches_status(row: Mapping[str, Any], wanted: str) -> bool:
    """健康状态既可能在顶层（``WorkerSummary.status``），也可能在 ``health.status``。"""
    if row.get("status") == wanted:
        return True
    health = row.get("health")
    return isinstance(health, Mapping) and health.get("status") == wanted


def _open_registry(module: Any, db_path: Path) -> Any:
    """按 M4 的约定装配 ``WorkerRegistry(WorkerStore(db_path))``。

    这里只做装配；建库失败（目录只读等）如实转成 ``not_available``，不伪造空列表。
    """
    store_cls = getattr(module, "WorkerStore", None)
    registry_cls = getattr(module, "WorkerRegistry", None)
    if store_cls is None or registry_cls is None:
        raise NotAvailableError(
            "worker registry is not available: WorkerStore/WorkerRegistry are both required",
            detail={"capability": "network", "available": False},
        )
    try:
        return registry_cls(store_cls(db_path))
    except Exception as exc:
        raise NotAvailableError(
            f"cannot open worker registry at {db_path}: {type(exc).__name__}",
            detail={"capability": "network", "available": False, "database": str(db_path)},
        ) from exc


def _fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)
