"""校验与交付工具：``verify_dataset`` / ``get_manifest``。

"未验证不能算通过"是硬约束
-------------------------
``verify_dataset`` 逐文件给出 ``status`` 与 ``verified`` 两个字段，**只有**当
``IntegrityChecker`` 的结论是 ``PASSED`` 且交付清单核对没有该文件的问题时，
``verified`` 才为 ``true``。``SKIPPED``（例如缺少期望校验和、NetCDF 后端未安装）与
``UNKNOWN`` 一律 ``verified=false``，并在 ``unverified_reason`` 里说明原因——
这与 ``docs/data-integrity.md`` 的口径一致，也是 Agent 最容易踩的坑。

``get_manifest`` 不覆盖既有文件
------------------------------
写出 JSON 时若目标已存在，返回 ``conflict`` 错误并附 ``existing_bytes``/``mtime``，
让调用方明确决定是否换路径。**没有** ``overwrite`` 开关：静默覆盖一份交付证据是
不可接受的行为。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import (
    DeliveryManifest,
    FileRecord,
    IntegrityStatus,
    JsonValue,
)
from researchdatahub.integrity.checker import IntegrityChecker, PathCheckResult
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.interfaces.mcp.errors import conflict_error, invalid_arguments, not_found_error
from researchdatahub.interfaces.mcp.schemas import (
    GET_MANIFEST_SCHEMA,
    VERIFY_DATASET_SCHEMA,
    coerce_arguments,
    optional_bool,
    optional_str,
    require_str_list,
)
from researchdatahub.interfaces.mcp.tools._shared import ensure_plan_exists, json_ready
from researchdatahub.reporting.delivery import (
    DeliveryVerification,
    build_delivery_manifest,
    delivery_manifest_to_json,
    latest_reports_by_file,
    verify_delivery_manifest,
)

__all__ = ["get_manifest", "verify_dataset"]

_MANIFEST_EXPORT_SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# verify_dataset
# --------------------------------------------------------------------------- #
def verify_dataset(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """逐文件校验：字节级检查 + 交付清单核对。"""
    args = coerce_arguments("verify_dataset", arguments, _fields(VERIFY_DATASET_SCHEMA))
    paths = require_str_list(args, "paths", max_items=200, item_max_length=4096)
    request_id = optional_str(args, "request_id", max_length=128)

    expected: dict[Path, FileRecord] = {}
    manifest_note: str | None = None
    if request_id is not None:
        expected, manifest_note = _expected_from_delivery(context, request_id)

    checker = context.integrity or IntegrityChecker(context.defaults.integrity)
    files: list[dict[str, Any]] = []
    for raw_path in paths:
        files.append(_verify_one(checker, raw_path, expected.get(_key(raw_path))))

    verified = sum(1 for item in files if item["verified"])
    return {
        "files": files,
        "total": len(files),
        "verified": verified,
        "unverified": len(files) - verified,
        "request_id": request_id,
        "manifest_available": request_id is not None and bool(expected),
        "manifest_note": manifest_note,
        "policy": "SKIPPED/UNKNOWN are never counted as verified",
    }


def _verify_one(
    checker: IntegrityChecker, raw_path: str, record: FileRecord | None
) -> dict[str, Any]:
    """校验单个路径；路径不存在等"无法进行"的情况如实记为 ``verified=false``。"""
    path = Path(raw_path).expanduser()
    expected_size = None if record is None else record.size_bytes
    expected_checksum = None if record is None else record.checksum
    try:
        result: PathCheckResult = checker.check_path(
            path,
            expected_size=expected_size,
            expected_checksum=expected_checksum,
        )
    except RdhError as exc:
        return {
            "path": str(path),
            "verified": False,
            "status": "unavailable",
            "unverified_reason": exc.message,
            "checks": [],
            "failures": [exc.message],
            "expected_source": "delivery_manifest" if record is not None else "none",
        }

    issues: list[str] = []
    if record is not None:
        issues.extend(_manifest_mismatches(result, record))
    verified = result.status is IntegrityStatus.PASSED and not issues
    payload: dict[str, Any] = {
        "path": str(path),
        "verified": verified,
        "status": result.status.value,
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "actual_format": result.actual_format,
        "checks": [json_ready(check.as_json()) for check in result.checks],
        "failures": list(result.failures),
        "skipped_checks": [
            check.kind.value for check in result.checks if check.status.value == "skipped"
        ],
        "expected_source": "delivery_manifest" if record is not None else "none",
        "manifest_issues": issues,
        "checked_at": result.checked_at.isoformat(),
    }
    if not verified:
        payload["unverified_reason"] = _unverified_reason(result, issues, record)
    return payload


def _manifest_mismatches(result: PathCheckResult, record: FileRecord) -> list[str]:
    """清单记录与磁盘事实的差异（大小/校验和）。"""
    issues: list[str] = []
    if result.size_bytes != record.size_bytes:
        issues.append(f"size_mismatch: disk={result.size_bytes} manifest={record.size_bytes}")
    expected_digest = record.checksum.value
    manifest_sha256 = (
        expected_digest.split(":", 1)[1] if expected_digest.startswith("sha256:") else None
    )
    if manifest_sha256 is not None and result.sha256 != manifest_sha256:
        issues.append("checksum_mismatch: sha256 differs from manifest record")
    return issues


def _unverified_reason(
    result: PathCheckResult, issues: Sequence[str], record: FileRecord | None
) -> str:
    if issues:
        return "; ".join(issues)
    if result.status is IntegrityStatus.FAILED:
        return "integrity checks failed: " + ("; ".join(result.failures) or "unspecified")
    if record is None:
        return (
            "no delivery manifest record for this path; size/checksum expectations are unknown "
            "so the result is not a pass"
        )
    return f"integrity status is {result.status.value}, which is not a pass"


def _expected_from_delivery(
    context: McpContext, request_id: str
) -> tuple[dict[Path, FileRecord], str | None]:
    """从 Manifest 读出该请求的交付文件记录（按路径索引）。"""
    with context.repository() as repo:
        try:
            manifest = repo.build_delivery_manifest(request_id)
        except RdhError as exc:
            if "not deliverable" in exc.message.lower() or "no files" in exc.message.lower():
                return {}, f"request {request_id!r} has no delivery-ready files yet"
            raise
    index: dict[Path, FileRecord] = {}
    for record in manifest.files:
        index[_key(record.local_path)] = record
    latest, issues = latest_reports_by_file(manifest.files, manifest.integrity)
    note = None
    if issues:
        note = f"manifest has {len(issues)} consistency issue(s); treat unverified files with care"
    if len(latest) != len(manifest.files):
        note = (note or "") + " some files lack integrity reports"
    return index, note


def _key(raw_path: str) -> Path:
    return Path(raw_path).expanduser()


# --------------------------------------------------------------------------- #
# get_manifest
# --------------------------------------------------------------------------- #
def get_manifest(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """返回交付清单（可按需写成 JSON；目标已存在则 conflict）。"""
    args = coerce_arguments("get_manifest", arguments, _fields(GET_MANIFEST_SCHEMA))
    request_id = optional_str(args, "request_id", max_length=128)
    plan_id = optional_str(args, "plan_id", max_length=128)
    output_path = optional_str(args, "output_path", max_length=4096)
    verify_checksums = optional_bool(args, "verify_checksums", default=False)

    if request_id is None and plan_id is None:
        raise invalid_arguments(
            "get_manifest requires either 'request_id' or 'plan_id'",
            detail={"required_one_of": ["request_id", "plan_id"]},
        )
    if request_id is not None and plan_id is not None:
        raise invalid_arguments(
            "get_manifest accepts only one of 'request_id' and 'plan_id'",
            detail={"fields": ["request_id", "plan_id"]},
        )

    resolved_request_id = request_id
    if resolved_request_id is None:
        with context.repository() as repo:
            plan = ensure_plan_exists(repo, plan_id or "")
        resolved_request_id = plan.request_id

    with context.repository() as repo:
        manifest = repo.build_delivery_manifest(resolved_request_id)
    if not manifest.files:
        raise not_found_error("deliverable request", resolved_request_id)

    latest, consistency_issues = latest_reports_by_file(manifest.files, manifest.integrity)
    verification: DeliveryVerification = verify_delivery_manifest(
        manifest, verify_checksums=verify_checksums
    )
    files_payload = _files_payload(context, manifest, latest, verification)
    payload: dict[str, Any] = {
        "delivery_id": manifest.delivery_id,
        "request_id": manifest.request_id,
        "plan_id": manifest.plan_id,
        "created_at": manifest.created_at.isoformat(),
        "tool_version": manifest.tool_version,
        "complete": manifest.complete and verification.ok,
        "total_bytes": manifest.total_bytes,
        "notes": manifest.notes,
        "manifest_path": None,
        "files": files_payload,
        "integrity": {
            "verified_files": sum(
                1 for item in files_payload if item["validation_status"] == "passed"
            ),
            "unverified_files": sum(
                1 for item in files_payload if item["validation_status"] != "passed"
            ),
            "consistency_issues": [issue.as_json() for issue in consistency_issues],
            "disk_verification": verification.as_json(),
        },
        "sources": [json_ready(source.to_json_dict()) for source in manifest.sources],
    }

    if output_path is not None:
        target = Path(output_path).expanduser()
        if not target.is_absolute():
            target = context.storage_root / target
        payload["manifest_path"] = _write_manifest_json(
            manifest,
            target,
            base=context.storage_root,
            verification=verification,
        )
    return payload


def _files_payload(
    context: McpContext,
    manifest: DeliveryManifest,
    latest: Mapping[str, Any],
    verification: DeliveryVerification,
) -> list[dict[str, Any]]:
    """逐文件给出路径/大小/sha256/provider/worker/route/校验结论。"""
    issue_codes: dict[str, list[str]] = {}
    for issue in verification.issues:
        if issue.file_id is not None:
            issue_codes.setdefault(issue.file_id, []).append(issue.code)
    workers = _worker_index(context, manifest)
    rows: list[dict[str, Any]] = []
    for record in manifest.files:
        report = latest.get(record.file_id)
        status = "missing" if report is None else report.status.value
        problems = issue_codes.get(record.file_id, [])
        rows.append(
            {
                "file_id": record.file_id,
                "task_id": record.task_id,
                "dataset_id": record.dataset_id,
                "provider": record.source_id,
                "worker": workers.get(record.task_id),
                "route": record.access_route.value,
                "origin_uri": record.origin_uri,
                "path": record.local_path,
                "size_bytes": record.size_bytes,
                "sha256": record.checksum.value,
                "validation_status": status,
                "verified": status == "passed" and not problems,
                "report_id": None if report is None else report.report_id,
                "disk_issues": problems,
                "retrieved_at": record.retrieved_at.isoformat(),
            }
        )
    return rows


def _worker_index(context: McpContext, manifest: DeliveryManifest) -> dict[str, str | None]:
    """``task_id -> worker_id``（从任务的租约归属读取；查不到就是 ``None``，不猜）。"""
    index: dict[str, str | None] = {}
    with context.repository() as repo:
        for record in manifest.files:
            try:
                task = repo.load_task(record.task_id)
            except RdhError:
                index[record.task_id] = None
                continue
            index[record.task_id] = task.worker_id
    return index


def _write_manifest_json(
    manifest: DeliveryManifest,
    target: Path,
    *,
    base: Path,
    verification: DeliveryVerification,
) -> str:
    """把清单写成 JSON；目标已存在时抛 ``conflict``（**不覆盖**）。"""
    if target.exists():
        stat_result = target.stat()
        raise conflict_error(
            f"output_path already exists: {target}",
            detail={
                "path": str(target),
                "existing_bytes": stat_result.st_size,
                "existing_mtime": datetime.fromtimestamp(stat_result.st_mtime, UTC).isoformat(),
                "hint": "choose another path; this server never overwrites delivery evidence",
            },
        )
    payload = delivery_manifest_to_json(manifest, relative_to=base, verification=verification)
    payload["schema_version"] = _MANIFEST_EXPORT_SCHEMA_VERSION
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        # ``os.link`` 是"不覆盖"语义的原子提交：目标已存在时直接 EEXIST。
        os.link(temporary, target)
    except FileExistsError as exc:
        if target.exists():
            raise conflict_error(
                f"output_path was created concurrently: {target}",
                detail={"path": str(target)},
            ) from exc
        raise
    except OSError as exc:
        raise invalid_arguments(
            f"cannot write manifest to {target}: {exc}",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _digest(text: str) -> str:  # pragma: no cover - 诊断辅助
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)


_ = (JsonValue, build_delivery_manifest)  # 保持与文档一致的显式引用
