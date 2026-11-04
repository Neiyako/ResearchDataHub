"""模型 ⇄ SQLite 行的编解码。

SQLite 没有 ``datetime`` / ``JSON`` / ``bool`` 原生类型，因此持久化格式必须由本模块
**唯一**规定，避免各处各写一份 ``json.dumps`` 导致格式漂移：

* 时间：tz-aware 的 UTC ``datetime``，序列化为 ISO 8601 字符串；读取时由 Pydantic 解析。
  解析失败不会被静默忽略——它会变成 :class:`ManifestError`（库被手工改坏必须暴露）。
* 结构化列：JSON 文本，键排序、无多余空格（相同内容得到相同文本，便于比对）。
* ``bool``：``INTEGER`` 0/1（SQLite 没有布尔类型）。
* 枚举：``StrEnum`` 的值字符串（``TaskState`` / ``AccessRoute`` / ``Priority`` 等）。

行解码函数与 SELECT 片段放在同一模块，防止“改了 SQL 别名却忘了改解码”的漂移。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, TypeAdapter, ValidationError

from researchdatahub.core.errors import ManifestError
from researchdatahub.core.models import (
    AcquisitionTask,
    FileRecord,
    IntegrityReport,
    JsonValue,
    SourceAsset,
    SourceMetadata,
    TransferLogEvent,
)
from researchdatahub.core.models import Priority as TaskPriority

__all__ = [
    "TASK_SELECT",
    "asset_from_row",
    "event_from_row",
    "file_record_from_row",
    "from_json_dict",
    "from_json_text",
    "integrity_report_from_row",
    "normalize_now",
    "priority_from_rank",
    "source_from_row",
    "task_from_row",
    "to_bool",
    "to_iso",
    "to_json_text",
    "to_json_value",
    "to_optional_iso",
    "utc_now",
    "validated",
]

_JSON_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)

TASK_SELECT: Final[str] = """
SELECT
    t.task_id, t.plan_id, t.request_id, t.asset_id, t.state, t.priority,
    t.attempt, t.max_attempts, t.scheduled_at, t.started_at, t.finished_at,
    t.last_transition_at, t.last_error, t.worker_id, t.heartbeat_at,
    t.lease_expires_at, t.notes,
    a.asset_id AS a_asset_id, a.source_id AS a_source_id, a.dataset_id AS a_dataset_id,
    a.access_route AS a_access_route, a.origin_uri AS a_origin_uri, a.file_name AS a_file_name,
    a.size_bytes AS a_size_bytes, a.checksum AS a_checksum, a.remote_etag AS a_remote_etag,
    a.remote_last_modified AS a_remote_last_modified, a.content_type AS a_content_type,
    a.time_slice AS a_time_slice, a.level_slice AS a_level_slice,
    a.metadata AS a_metadata, a.discovered_at AS a_discovered_at
FROM tasks AS t
JOIN assets AS a ON a.asset_id = t.asset_id
"""
"""任务查询的共同部分；:func:`task_from_row` 依赖这些列名（含 ``a_`` 前缀的资产列）。"""


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """当前 UTC 时间（tz-aware）。"""
    return datetime.now(UTC)


def normalize_now(now: datetime | None) -> datetime:
    """把可选时间戳规整为 UTC；``None`` 取当前时间，naive 时间直接拒绝。

    Raises
    ------
    ValueError
        ``now`` 缺少时区信息。时区不明的时间无法审计，和 ``core.state`` 保持一致。
    """
    if now is None:
        return utc_now()
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """把 tz-aware ``datetime`` 序列化为 UTC ISO 字符串。"""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def to_optional_iso(value: datetime | None) -> str | None:
    """``datetime | None`` 版本的时间序列化。"""
    return None if value is None else to_iso(value)


def parse_optional_iso(value: object, *, column: str) -> datetime | None:
    """读取时间列；``NULL`` 返回 ``None``，格式非法抛 :class:`ManifestError`。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(
            f"column {column!r} must be a TEXT timestamp, got {type(value).__name__}",
            detail={"column": column},
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestError(
            f"column {column!r} contains an invalid timestamp: {value!r}",
            detail={"column": column, "value": value},
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ManifestError(
            f"column {column!r} contains a naive timestamp: {value!r}",
            detail={"column": column, "value": value},
        )
    return parsed.astimezone(UTC)


# --------------------------------------------------------------------------- #
# JSON 与基础类型
# --------------------------------------------------------------------------- #
def _json_default(value: object) -> str:
    """``json.dumps`` 的兜底序列化：只接受可解释的类型，绝不把任意对象 ``str()`` 掉。"""
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(
        f"object of type {type(value).__name__} is not JSON-serializable; "
        "pass only JSON-compatible values in event payloads"
    )


def to_json_value(value: object) -> JsonValue:
    """把 Python 值规整为 :data:`JsonValue`（受控 JSON 类型），非法值抛 ManifestError。"""
    try:
        encoded = json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)
        return _JSON_ADAPTER.validate_json(encoded)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ManifestError(
            f"value is not JSON-serializable: {exc}",
            detail={"type": type(value).__name__},
        ) from exc


def to_json_text(value: object) -> str:
    """结构化值 → 稳定 JSON 文本（键排序，便于人工比对）。"""
    return json.dumps(
        to_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def from_json_text(value: object, *, column: str) -> JsonValue:
    """JSON 文本列 → :data:`JsonValue`；损坏的 JSON 抛 :class:`ManifestError`。"""
    if not isinstance(value, str):
        raise ManifestError(
            f"column {column!r} must be a JSON TEXT column, got {type(value).__name__}",
            detail={"column": column},
        )
    try:
        return _JSON_ADAPTER.validate_json(value)
    except ValidationError as exc:
        raise ManifestError(
            f"column {column!r} contains invalid JSON: {exc}",
            detail={"column": column},
        ) from exc


def from_json_dict(value: object, *, column: str) -> dict[str, JsonValue]:
    """JSON 文本列 → ``dict[str, JsonValue]``；顶层不是对象时抛 :class:`ManifestError`。"""
    decoded = from_json_text(value, column=column)
    if not isinstance(decoded, dict):
        raise ManifestError(
            f"column {column!r} must contain a JSON object",
            detail={"column": column, "actual_type": type(decoded).__name__},
        )
    return decoded


def to_bool(value: bool) -> int:
    """``bool`` → SQLite ``INTEGER``。"""
    return 1 if value else 0


def from_bool(value: object, *, column: str) -> bool:
    """SQLite ``INTEGER`` → ``bool``；只接受 0/1，其它值视为库被改坏。"""
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ManifestError(
        f"column {column!r} must be 0 or 1, got {value!r}", detail={"column": column}
    )


def priority_from_rank(rank: object) -> TaskPriority:
    """优先级整数（0–3，越大越优先）→ :class:`Priority` 枚举。

    ``tasks.priority`` 存的是 ``Priority.rank``：这样 SQL 可以直接按数值排序，
    与 ``Priority.rank`` 的语义一一对应（映射表在 :class:`Priority` 上，不在这里复制）。
    """
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise ManifestError(f"priority rank must be an integer, got {rank!r}")
    for candidate in TaskPriority:
        if candidate.rank == rank:
            return candidate
    raise ManifestError(f"unknown priority rank {rank!r}")


def validated[M: BaseModel](model: type[M], payload: object, *, context: str) -> M:
    """用 Pydantic 校验构造模型，失败时转成 :class:`ManifestError`（带原始原因）。"""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ManifestError(
            f"stored {context} does not satisfy the data contract: {exc.error_count()} error(s)",
            detail={"context": context, "errors": str(exc)},
        ) from exc


# --------------------------------------------------------------------------- #
# 行解码（与 SELECT 片段一一对应）
# --------------------------------------------------------------------------- #
def _row_mapping(row: sqlite3.Row) -> Mapping[str, object]:
    """``sqlite3.Row`` 转成 ``Mapping[str, object]`` 便于类型收敛。

    注意：``sqlite3.Row`` 的迭代产出**列值**而不是列名，因此必须显式用 ``.keys()``。
    """
    return {key: row[key] for key in row.keys()}  # noqa: SIM118 - Row 不是 dict


def source_from_row(row: sqlite3.Row) -> SourceMetadata:
    """解码一行 ``sources`` 记录。"""
    data: dict[str, object] = {
        "source_id": row["source_id"],
        "display_name": row["display_name"],
        "provider": row["provider"],
        "access_route": row["access_route"],
        "dataset_id": row["dataset_id"],
        "origin_uri": row["origin_uri"],
        "protocol_version": row["protocol_version"],
        "license": row["license"],
        "citation": row["citation"],
        "retrieved_at": row["retrieved_at"],
        "extensions": from_json_dict(row["extensions"], column="sources.extensions"),
    }
    return validated(SourceMetadata, data, context=f"source {row['source_id']!r}")


def _decode_asset_fields(mapping: Mapping[str, object], *, prefix: str) -> dict[str, object]:
    """把 ``prefix`` 前缀的资产列解码为 :class:`SourceAsset` 构造参数。"""

    def column(name: str) -> object:
        return mapping[f"{prefix}{name}"]

    checksum_raw = column("checksum")
    time_slice_raw = column("time_slice")
    level_slice_raw = column("level_slice")
    metadata = from_json_dict(column("metadata"), column=f"{prefix}metadata")
    return {
        "asset_id": column("asset_id"),
        "source_id": column("source_id"),
        "dataset_id": column("dataset_id"),
        "access_route": column("access_route"),
        "origin_uri": column("origin_uri"),
        "file_name": column("file_name"),
        "size_bytes": column("size_bytes"),
        "checksum": None if checksum_raw is None else {"value": checksum_raw},
        "remote_etag": column("remote_etag"),
        "remote_last_modified": column("remote_last_modified"),
        "content_type": column("content_type"),
        "time_slice": None
        if time_slice_raw is None
        else from_json_dict(time_slice_raw, column=f"{prefix}time_slice"),
        "level_slice": None
        if level_slice_raw is None
        else from_json_text(level_slice_raw, column=f"{prefix}level_slice"),
        "metadata": metadata,
        "discovered_at": column("discovered_at"),
    }


def asset_from_row(row: sqlite3.Row, *, prefix: str = "") -> SourceAsset:
    """解码一行资产；``prefix`` 用于 JOIN 查询里的别名（如 ``a_``）。"""
    mapping = _row_mapping(row)
    asset_id = mapping[f"{prefix}asset_id"]
    return validated(
        SourceAsset,
        _decode_asset_fields(mapping, prefix=prefix),
        context=f"asset {asset_id!r}",
    )


def task_from_row(row: sqlite3.Row) -> AcquisitionTask:
    """解码一行 :data:`TASK_SELECT` 查询结果（任务 + 内嵌资产）。"""
    asset = asset_from_row(row, prefix="a_")
    mapping = _row_mapping(row)
    data: dict[str, object] = {
        "task_id": mapping["task_id"],
        "plan_id": mapping["plan_id"],
        "request_id": mapping["request_id"],
        "asset": asset,
        "state": mapping["state"],
        "priority": priority_from_rank(mapping["priority"]),
        "attempt": mapping["attempt"],
        "max_attempts": mapping["max_attempts"],
        "scheduled_at": mapping["scheduled_at"],
        "started_at": mapping["started_at"],
        "finished_at": mapping["finished_at"],
        "last_transition_at": mapping["last_transition_at"],
        "last_error": mapping["last_error"],
        "worker_id": mapping["worker_id"],
        "heartbeat_at": mapping["heartbeat_at"],
        "lease_expires_at": mapping["lease_expires_at"],
        "notes": from_json_text(mapping["notes"], column="tasks.notes"),
    }
    return validated(AcquisitionTask, data, context=f"task {mapping['task_id']!r}")


def file_record_from_row(row: sqlite3.Row) -> FileRecord:
    """解码一行 ``files`` 记录。"""
    data: dict[str, object] = {
        "file_id": row["file_id"],
        "task_id": row["task_id"],
        "dataset_id": row["dataset_id"],
        "source_id": row["source_id"],
        "access_route": row["access_route"],
        "origin_uri": row["origin_uri"],
        "local_path": row["local_path"],
        "size_bytes": row["size_bytes"],
        "checksum": {"value": row["checksum"]},
        "content_type": row["content_type"],
        "remote_etag": row["remote_etag"],
        "remote_last_modified": row["remote_last_modified"],
        "retrieved_at": row["retrieved_at"],
        "is_read_only": from_bool(row["is_read_only"], column="files.is_read_only"),
        "extensions": from_json_dict(row["extensions"], column="files.extensions"),
    }
    return validated(FileRecord, data, context=f"file {row['file_id']!r}")


def integrity_report_from_row(row: sqlite3.Row) -> IntegrityReport:
    """解码一行 ``integrity_reports`` 记录。"""
    data: dict[str, object] = {
        "report_id": row["report_id"],
        "task_id": row["task_id"],
        "file_id": row["file_id"],
        "status": row["status"],
        "checks": from_json_text(row["checks"], column="integrity_reports.checks"),
        "failures": from_json_text(row["failures"], column="integrity_reports.failures"),
        "expected": from_json_dict(row["expected"], column="integrity_reports.expected"),
        "observed": from_json_dict(row["observed"], column="integrity_reports.observed"),
        "checked_at": row["checked_at"],
        "checker_id": row["checker_id"],
    }
    return validated(IntegrityReport, data, context=f"integrity report {row['report_id']!r}")


def event_from_row(row: sqlite3.Row) -> TransferLogEvent:
    """解码一行 ``transfer_logs`` 记录（审计事件）。"""
    data: dict[str, object] = {
        "task_id": row["task_id"],
        "event_type": row["event_type"],
        "message": row["message"],
        "bytes_transferred": row["bytes_transferred"],
        "metadata": from_json_dict(row["metadata"], column="transfer_logs.metadata"),
        "timestamp": row["timestamp"],
    }
    return validated(TransferLogEvent, data, context=f"transfer log {row['event_id']!r}")
