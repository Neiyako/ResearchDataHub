"""交付清单（``DeliveryManifest``）的构建、校验与可移植 JSON 导出。

Integrity Before Delivery
-------------------------
交付清单的 ``complete`` **只**在以下条件全部满足时为 ``True``：

1. 至少有一个 :class:`~researchdatahub.core.models.FileRecord`；
2. 每个文件都有一份**最新的** :class:`~researchdatahub.core.models.IntegrityReport`，
   且其状态是 ``PASSED``（``FAILED`` / ``SKIPPED`` / ``UNKNOWN`` 一律不算通过）；
3. 报告引用的 ``file_id`` 都真实存在于文件列表（不存在 = “Manifest 与文件状态不一致”）；
4. 文件总字节数 > 0；
5. 可选地：磁盘上的文件仍存在、大小（与可选校验和）与记录一致。

失败、跳过、隔离的文件不会从导出中消失：它们通过 :class:`ExcludedFile` 保留在
``excluded`` 区段，``notes`` 里也会写明不完整原因——报告不得只统计成功数。

相对路径
--------
导出 JSON 时文件位置一律转换为相对 ``relative_to`` 的路径（``path`` 字段），便于清单
复制给别人或在论文附录里复现；如果文件在 ``relative_to`` 之外，会带
``path_escapes_base: true`` 而不是把绝对路径悄悄写进去。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from researchdatahub import __version__
from researchdatahub.core.errors import ModelValidationError
from researchdatahub.core.models import (
    DatasetMetadata,
    DeliveryManifest,
    FileRecord,
    IntegrityReport,
    IntegrityStatus,
    JsonValue,
    SourceMetadata,
)

__all__ = [
    "DeliveryIssue",
    "DeliveryVerification",
    "ExcludedDisposition",
    "ExcludedFile",
    "build_delivery_manifest",
    "delivery_manifest_to_json",
    "latest_reports_by_file",
    "verify_delivery_manifest",
    "write_delivery_manifest",
]

_EXPORT_SCHEMA_VERSION = "1.0"
_NOTES_LIMIT = 4096
_HASH_CHUNK_SIZE = 1024 * 1024


class ExcludedDisposition(StrEnum):
    """未进入交付清单的文件去向；隔离与失败都必须能出现在报告里。"""

    FAILED = "failed"
    SKIPPED = "skipped"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ExcludedFile:
    """一个没有进入交付的文件（失败/跳过/隔离），保留原因与位置供审计。"""

    disposition: ExcludedDisposition
    file_id: str
    task_id: str
    reason: str
    path: str | None = None
    size_bytes: int = 0
    integrity_status: IntegrityStatus | None = None

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "disposition": self.disposition.value,
            "file_id": self.file_id,
            "task_id": self.task_id,
            "reason": self.reason,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "integrity_status": (
                self.integrity_status.value if self.integrity_status is not None else None
            ),
        }


@dataclass(frozen=True)
class DeliveryIssue:
    """交付不完整/不一致的一条结构化原因。"""

    code: str
    detail: str
    file_id: str | None = None

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {"code": self.code, "detail": self.detail, "file_id": self.file_id}


@dataclass(frozen=True)
class DeliveryVerification:
    """磁盘事实与清单记录的一致性校验结果。"""

    ok: bool
    checked_files: int
    issues: tuple[DeliveryIssue, ...] = ()

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "ok": self.ok,
            "checked_files": self.checked_files,
            "issues": [issue.as_json() for issue in self.issues],
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _tool_version() -> str:
    return __version__


def latest_reports_by_file(
    files: Sequence[FileRecord], reports: Sequence[IntegrityReport]
) -> tuple[dict[str, IntegrityReport], list[DeliveryIssue]]:
    """按文件挑出**最新**的完整性报告，并返回引用不一致问题。

    “最新”按 ``checked_at`` 排序（同一时间按 ``report_id`` 稳定排序）。历史失败报告仍保留在
    清单的 ``integrity`` 列表里，但只有最新一份决定该文件能否交付——重试成功不应被
    旧失败永久拖住，重试后仍失败也不应因旧成功而通过。
    """
    issues: list[DeliveryIssue] = []
    file_ids = {record.file_id for record in files}
    duplicates = {file_id for file_id in file_ids if _count_file_ids(files, file_id) > 1}
    for file_id in sorted(duplicates):
        issues.append(
            DeliveryIssue("duplicate_file_id", f"file_id {file_id!r} appears more than once")
        )
    latest: dict[str, IntegrityReport] = {}
    for report in reports:
        if report.file_id is None:
            issues.append(
                DeliveryIssue(
                    "report_without_file_id",
                    f"report {report.report_id!r} has no file_id; it cannot prove a specific file",
                )
            )
            continue
        if report.file_id not in file_ids:
            issues.append(
                DeliveryIssue(
                    "report_for_unknown_file",
                    f"report {report.report_id!r} references unknown file {report.file_id!r}",
                    file_id=report.file_id,
                )
            )
            continue
        current = latest.get(report.file_id)
        if current is None or (report.checked_at, report.report_id) > (
            current.checked_at,
            current.report_id,
        ):
            latest[report.file_id] = report
    return latest, issues


def _count_file_ids(files: Sequence[FileRecord], file_id: str) -> int:
    return sum(1 for record in files if record.file_id == file_id)


def _collect_issues(
    files: Sequence[FileRecord], reports: Sequence[IntegrityReport]
) -> tuple[list[DeliveryIssue], dict[str, IntegrityReport]]:
    latest, issues = latest_reports_by_file(files, reports)
    for record in files:
        report = latest.get(record.file_id)
        if report is None:
            issues.append(
                DeliveryIssue(
                    "missing_integrity_report",
                    f"file {record.file_id!r} has no integrity report",
                    file_id=record.file_id,
                )
            )
            continue
        if not report.permits_completion:
            issues.append(
                DeliveryIssue(
                    "integrity_not_passed",
                    f"file {record.file_id!r} latest report status is {report.status.value}",
                    file_id=record.file_id,
                )
            )
    return issues, latest


def build_delivery_manifest(
    *,
    delivery_id: str | None = None,
    request_id: str,
    plan_id: str,
    dataset_metadata: DatasetMetadata,
    files: Sequence[FileRecord],
    integrity_reports: Sequence[IntegrityReport],
    sources: Sequence[SourceMetadata] = (),
    excluded: Sequence[ExcludedFile] = (),
    created_at: datetime | None = None,
    tool_version: str | None = None,
    notes: str | None = None,
    verify_files: bool = False,
    base_dir: Path | None = None,
    verify_checksums: bool = False,
) -> DeliveryManifest:
    """构建交付清单；``complete`` 由完整性证据决定，不由调用方指定。

    Parameters
    ----------
    delivery_id:
        ``None`` 时生成 ``delivery-<16 hex>``。
    files:
        成功落盘的文件记录（空列表直接拒绝：没有文件的“交付”不是交付）。
    integrity_reports:
        完整性报告（可包含历史失败报告，最新一份决定每个文件是否通过）。
    sources:
        数据源元数据（交付清单需自证数据来自哪里）。
    excluded:
        失败/跳过/隔离文件，写入 ``notes`` 并可由导出函数单独保留。
    verify_files:
        为 ``True`` 时额外核对磁盘上的文件存在且大小（与可选校验和）一致。
    base_dir:
        相对路径基准；``verify_files`` 时用于解析相对 ``local_path``。

    Returns
    -------
    DeliveryManifest
        ``complete=True`` 当且仅当所有文件都有 PASSED 的最新报告且无任何不一致。

    Raises
    ------
    ModelValidationError
        ``files`` 为空、``request_id``/``plan_id`` 为空，或构造清单时违反模型不变式。
    """
    if len(files) == 0:
        raise ModelValidationError(
            "cannot build a delivery manifest without files",
            detail={"request_id": request_id, "plan_id": plan_id},
        )
    if request_id == "" or plan_id == "":
        raise ModelValidationError(
            "delivery manifest requires request_id and plan_id",
            detail={"request_id": request_id, "plan_id": plan_id},
        )
    issues, latest = _collect_issues(files, integrity_reports)
    verification: DeliveryVerification | None = None
    if verify_files:
        pending = _manifest_for_verification(
            delivery_id=delivery_id,
            request_id=request_id,
            plan_id=plan_id,
            dataset_metadata=dataset_metadata,
            files=files,
            integrity_reports=integrity_reports,
            sources=sources,
            created_at=created_at,
        )
        verification = verify_delivery_manifest(
            pending,
            base_dir=base_dir,
            verify_checksums=verify_checksums,
        )
        issues.extend(verification.issues)
    total_bytes = sum(record.size_bytes for record in files)
    all_passed = all(report.permits_completion for report in latest.values()) and len(
        latest
    ) == len(files)
    complete = bool(files) and total_bytes > 0 and not issues and all_passed
    combined_notes = _compose_notes(notes, issues, excluded, complete)
    manifest = DeliveryManifest(
        delivery_id=delivery_id if delivery_id is not None else f"delivery-{uuid.uuid4().hex[:16]}",
        request_id=request_id,
        plan_id=plan_id,
        dataset_metadata=dataset_metadata,
        files=list(files),
        integrity=_reports_for_manifest(files, integrity_reports, latest),
        sources=list(sources),
        created_at=created_at if created_at is not None else _utc_now(),
        tool_version=tool_version if tool_version is not None else _tool_version(),
        total_bytes=total_bytes,
        complete=complete,
        notes=combined_notes,
    )
    return manifest


def _reports_for_manifest(
    files: Sequence[FileRecord],
    reports: Sequence[IntegrityReport],
    latest: Mapping[str, IntegrityReport],
) -> list[IntegrityReport]:
    """清单只携带每个文件的**最新**报告，以及无法归属到文件的历史报告。

    旧失败/重试报告留在 SQLite 的不可变报告表里（append-only），交付清单只固化当前
    证据：否则 ``DeliveryManifest`` 的模型不变式（``complete`` 时所有报告必须 PASSED）
    会与“重试后成功”的正常业务流程冲突。
    """
    selected: list[IntegrityReport] = []
    seen: set[str] = set()
    file_ids = {record.file_id for record in files}
    for record in files:
        report = latest.get(record.file_id)
        if report is not None and report.report_id not in seen:
            selected.append(report)
            seen.add(report.report_id)
    for report in reports:
        if (report.file_id is None or report.file_id not in file_ids) and (
            report.report_id not in seen
        ):
            selected.append(report)
            seen.add(report.report_id)
    return selected


def _manifest_for_verification(
    *,
    delivery_id: str | None,
    request_id: str,
    plan_id: str,
    dataset_metadata: DatasetMetadata,
    files: Sequence[FileRecord],
    integrity_reports: Sequence[IntegrityReport],
    sources: Sequence[SourceMetadata],
    created_at: datetime | None,
) -> DeliveryManifest:
    """构造一个 ``complete=False`` 的临时清单，仅用于磁盘一致性校验。"""
    return DeliveryManifest(
        delivery_id=delivery_id if delivery_id is not None else f"delivery-{uuid.uuid4().hex[:16]}",
        request_id=request_id,
        plan_id=plan_id,
        dataset_metadata=dataset_metadata,
        files=list(files),
        integrity=list(integrity_reports),
        sources=list(sources),
        created_at=created_at if created_at is not None else _utc_now(),
        tool_version=_tool_version(),
        total_bytes=sum(record.size_bytes for record in files),
        complete=False,
        notes=None,
    )


def _compose_notes(
    notes: str | None,
    issues: Sequence[DeliveryIssue],
    excluded: Sequence[ExcludedFile],
    complete: bool,
) -> str | None:
    parts: list[str] = []
    if notes is not None and notes.strip() != "":
        parts.append(notes.strip())
    if not complete:
        reason = "; ".join(f"[{issue.code}] {issue.detail}" for issue in issues)
        parts.append(
            f"INCOMPLETE: {reason}" if reason else "INCOMPLETE: no passing integrity evidence"
        )
    if excluded:
        summary = ", ".join(f"{item.disposition.value}:{item.file_id}" for item in excluded)
        parts.append(f"excluded files: {summary}")
    if not parts:
        return None
    combined = " | ".join(parts)
    if len(combined) > _NOTES_LIMIT:
        combined = combined[: _NOTES_LIMIT - 3] + "..."
    return combined


def _resolve_local_path(local_path: str, base_dir: Path | None) -> Path:
    candidate = Path(local_path)
    if candidate.is_absolute() or base_dir is None:
        return candidate
    return base_dir / candidate


def verify_delivery_manifest(
    manifest: DeliveryManifest,
    *,
    base_dir: Path | None = None,
    verify_checksums: bool = False,
) -> DeliveryVerification:
    """核对清单记录的本地文件是否仍存在、大小/校验和是否一致。

    这是“Manifest 与文件状态不一致”检测入口：文件被删除、被截断、被替换都会被识别，
    返回的 ``ok=False`` 意味着该清单不应被当成可交付证据。
    """
    issues: list[DeliveryIssue] = []
    checked = 0
    for record in manifest.files:
        path = _resolve_local_path(record.local_path, base_dir)
        if not path.exists():
            issues.append(
                DeliveryIssue(
                    "file_missing",
                    f"recorded file does not exist: {path}",
                    file_id=record.file_id,
                )
            )
            continue
        if not path.is_file():
            issues.append(
                DeliveryIssue(
                    "file_not_regular",
                    f"recorded path is not a regular file: {path}",
                    file_id=record.file_id,
                )
            )
            continue
        checked += 1
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            issues.append(
                DeliveryIssue(
                    "file_unreadable",
                    f"cannot stat {path}: {exc}",
                    file_id=record.file_id,
                )
            )
            continue
        if actual_size != record.size_bytes:
            issues.append(
                DeliveryIssue(
                    "size_mismatch",
                    f"file {path} has {actual_size} bytes, manifest says {record.size_bytes}",
                    file_id=record.file_id,
                )
            )
            continue
        if verify_checksums:
            try:
                actual_digest = _digest_file(path, record.checksum.algorithm)
            except OSError as exc:
                issues.append(
                    DeliveryIssue(
                        "file_unreadable",
                        f"cannot hash {path}: {exc}",
                        file_id=record.file_id,
                    )
                )
                continue
            expected = record.checksum.hexdigest
            if record.checksum.algorithm == "sha256-16":
                actual_digest = actual_digest[:16]
            if actual_digest != expected:
                issues.append(
                    DeliveryIssue(
                        "checksum_mismatch",
                        f"file {path} checksum differs from the manifest record",
                        file_id=record.file_id,
                    )
                )
    return DeliveryVerification(ok=not issues, checked_files=checked, issues=tuple(issues))


def _digest_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new("sha256" if algorithm == "sha256-16" else algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(local_path: str, base: Path) -> tuple[str, bool]:
    candidate = Path(local_path)
    if not candidate.is_absolute():
        return candidate.as_posix(), False
    # 两边都做 strict=False 解析，避免 macOS /var → /private/var 这类符号链接基准不一致，
    # 把同一目录误判成「逃出基准目录」并写出 ../../ 垃圾路径。
    resolved_candidate = candidate.resolve(strict=False)
    resolved_base = base.resolve(strict=False)
    try:
        relative = Path(os.path.relpath(resolved_candidate, resolved_base))
    except ValueError:  # pragma: no cover - 仅 Windows 跨盘符
        return candidate.name, True
    escaped = relative.is_absolute() or ".." in relative.parts
    return relative.as_posix(), escaped


def delivery_manifest_to_json(
    manifest: DeliveryManifest,
    *,
    relative_to: Path,
    excluded: Sequence[ExcludedFile] = (),
    verification: DeliveryVerification | None = None,
) -> dict[str, JsonValue]:
    """把交付清单导出为可移植 JSON 字典（相对路径 + 逐文件校验状态与证据）。

    若传入 ``verification`` 且其 ``ok=False``，导出结果会把 ``complete`` 强制为 ``False``
    —— 清单是在文件被删除/截断之前生成的这一事实，不能让导出假装仍然完整。
    """
    latest, _ = latest_reports_by_file(manifest.files, manifest.integrity)
    files_payload: list[JsonValue] = []
    for record in manifest.files:
        relative, escaped = _relative_path(record.local_path, relative_to)
        report = latest.get(record.file_id)
        details = _report_details(report)
        files_payload.append(
            {
                "file_id": record.file_id,
                "task_id": record.task_id,
                "dataset_id": record.dataset_id,
                "source_id": record.source_id,
                "access_route": record.access_route.value,
                "path": relative,
                "path_escapes_base": escaped,
                "size_bytes": record.size_bytes,
                "checksum": record.checksum.value,
                "content_type": record.content_type,
                "remote_etag": record.remote_etag,
                "retrieved_at": record.retrieved_at.isoformat(),
                "validation_status": report.status.value if report is not None else "missing",
                "validation_details": details,
                "report_id": report.report_id if report is not None else None,
            }
        )
    integrity_payload: list[JsonValue] = []
    for report in manifest.integrity:
        report_payload = _model_json(report)
        report_payload["validation_status"] = report.status.value
        report_payload["details"] = _report_details(report)
        integrity_payload.append(report_payload)
    sources_payload: list[JsonValue] = []
    for source in manifest.sources:
        sources_payload.append(_model_json(source))
    payload: dict[str, JsonValue] = {
        "schema_version": _EXPORT_SCHEMA_VERSION,
        "delivery_id": manifest.delivery_id,
        "request_id": manifest.request_id,
        "plan_id": manifest.plan_id,
        "created_at": manifest.created_at.isoformat(),
        "tool_version": manifest.tool_version,
        "complete": manifest.complete,
        "total_bytes": manifest.total_bytes,
        "notes": manifest.notes,
        "relative_to": str(relative_to),
        "dataset_metadata": _model_json(manifest.dataset_metadata),
        "files": files_payload,
        "integrity": integrity_payload,
        "excluded": [item.as_json() for item in excluded],
        "sources": sources_payload,
        "verification": verification.as_json() if verification is not None else None,
    }
    if verification is not None and not verification.ok:
        payload["complete"] = False
    return payload


def write_delivery_manifest(
    manifest: DeliveryManifest,
    path: Path,
    *,
    relative_to: Path,
    excluded: Sequence[ExcludedFile] = (),
    verification: DeliveryVerification | None = None,
) -> Path:
    """把交付清单原子写到 ``path``（同目录临时文件 + ``os.replace``），返回该路径。"""
    payload = delivery_manifest_to_json(
        manifest,
        relative_to=relative_to,
        excluded=excluded,
        verification=verification,
    )
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        raise ModelValidationError(
            "cannot write delivery manifest",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _report_details(report: IntegrityReport | None) -> list[JsonValue]:
    if report is None:
        return []
    raw = report.observed.get("checks")
    if not isinstance(raw, list):
        return []
    details: list[JsonValue] = []
    for item in raw:
        if isinstance(item, Mapping):
            mapping: dict[str, JsonValue] = {}
            for key, value in item.items():
                mapping[str(key)] = _to_json_value(value)
            details.append(mapping)
    return details


def _model_json(model: object) -> dict[str, JsonValue]:
    """Pydantic 模型 → JSON 字典（走 ``model_dump_json`` 保证 enum/datetime 正确）。"""
    dump_json = getattr(model, "model_dump_json", None)
    if dump_json is None:  # pragma: no cover - 只接受 pydantic 模型
        raise ModelValidationError(
            "expected a pydantic model", detail={"type": type(model).__name__}
        )
    payload = json.loads(str(dump_json()))
    if not isinstance(payload, dict):
        raise ModelValidationError(
            "model did not serialise to a JSON object", detail={"type": type(model).__name__}
        )
    converted: dict[str, JsonValue] = {}
    for key, value in payload.items():
        converted[str(key)] = _to_json_value(value)
    return converted


def _to_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_to_json_value(item) for item in value]
    if isinstance(value, Mapping):
        nested: dict[str, JsonValue] = {}
        for key, item in value.items():
            nested[str(key)] = _to_json_value(item)
        return nested
    raise ModelValidationError(
        "value is not JSON serialisable", detail={"type": type(value).__name__}
    )
