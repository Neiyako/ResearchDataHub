"""交付清单：complete 只由 PASSED 证据决定、JSON 相对路径、失败/跳过/隔离保留。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from reporting_helpers import (
    FIXED_NOW,
    make_dataset_metadata,
    make_record,
    make_report,
    make_source,
    write_data,
)

from researchdatahub import __version__
from researchdatahub.core.errors import ModelValidationError
from researchdatahub.core.models import IntegrityStatus
from researchdatahub.reporting import (
    ExcludedDisposition,
    ExcludedFile,
    build_delivery_manifest,
    delivery_manifest_to_json,
    verify_delivery_manifest,
    write_delivery_manifest,
)


def _fixture(tmp_path: Path) -> tuple[Path, object]:
    path = write_data(tmp_path / "raw" / "era5" / "data.nc", b"delivered-bytes")
    record = make_record(path)
    return path, record


def test_complete_delivery_requires_passed_reports(tmp_path: Path) -> None:
    _path, record = _fixture(tmp_path)
    report = make_report()
    manifest = build_delivery_manifest(
        request_id="req-era5-clwc-2011-2020",
        plan_id="plan-0001",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[report],
        sources=[make_source()],
        created_at=FIXED_NOW,
    )
    assert manifest.complete is True
    assert manifest.total_bytes == len(b"delivered-bytes")
    assert manifest.tool_version == __version__
    assert manifest.notes is None
    assert len(manifest.files) == 1


def test_missing_report_marks_delivery_incomplete(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[],
    )
    assert manifest.complete is False
    assert "missing_integrity_report" in (manifest.notes or "")


@pytest.mark.parametrize(
    "status",
    [IntegrityStatus.FAILED, IntegrityStatus.SKIPPED, IntegrityStatus.UNKNOWN],
)
def test_non_passed_report_marks_delivery_incomplete(
    tmp_path: Path, status: IntegrityStatus
) -> None:
    _, record = _fixture(tmp_path)
    report = make_report(status=status)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[report],
    )
    assert manifest.complete is False
    assert "integrity_not_passed" in (manifest.notes or "")


def test_report_for_unknown_file_is_an_inconsistency(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    ghost = make_report(file_id="file-9999", report_id="report-ghost")
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[ghost],
    )
    assert manifest.complete is False
    assert "report_for_unknown_file" in (manifest.notes or "")


def test_report_without_file_id_cannot_prove_completion(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    task_report = make_report(file_id=None)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[task_report],
    )
    assert manifest.complete is False
    assert "report_without_file_id" in (manifest.notes or "")


def test_latest_report_decides_and_history_is_retained(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    failed = make_report(
        status=IntegrityStatus.FAILED, report_id="report-old", checked_at=FIXED_NOW
    )
    from datetime import timedelta

    passed = make_report(
        status=IntegrityStatus.PASSED,
        report_id="report-new",
        checked_at=FIXED_NOW + timedelta(minutes=5),
    )
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[failed, passed],
    )
    assert manifest.complete is True
    assert [report.report_id for report in manifest.integrity] == ["report-new"]


def test_build_requires_files(tmp_path: Path) -> None:
    with pytest.raises(ModelValidationError):
        build_delivery_manifest(
            request_id="req-1",
            plan_id="plan-1",
            dataset_metadata=make_dataset_metadata(),
            files=[],
            integrity_reports=[],
        )


def test_verify_detects_missing_file(tmp_path: Path) -> None:
    path, record = _fixture(tmp_path)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
    )
    path.unlink()
    verification = verify_delivery_manifest(manifest)
    assert verification.ok is False
    assert [issue.code for issue in verification.issues] == ["file_missing"]


def test_verify_detects_size_and_checksum_mismatch(tmp_path: Path) -> None:
    path, _record = _fixture(tmp_path)
    wrong_size = make_record(path, size_bytes=1)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[wrong_size],
        integrity_reports=[make_report()],
    )
    size_check = verify_delivery_manifest(manifest)
    assert [issue.code for issue in size_check.issues] == ["size_mismatch"]
    # 显式构造错误校验和：
    from researchdatahub.core.models import Checksum

    bad = make_record(path, checksum=Checksum(value="sha256:" + "0" * 64))
    manifest_bad = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[bad],
        integrity_reports=[make_report()],
    )
    checksum_check = verify_delivery_manifest(manifest_bad, verify_checksums=True)
    assert [issue.code for issue in checksum_check.issues] == ["checksum_mismatch"]


def test_build_with_verify_files_flags_inconsistency(tmp_path: Path) -> None:
    path, record = _fixture(tmp_path)
    path.unlink()
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
        verify_files=True,
    )
    assert manifest.complete is False
    assert "file_missing" in (manifest.notes or "")


def test_export_uses_relative_paths_and_validation_details(tmp_path: Path) -> None:
    path, record = _fixture(tmp_path)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
        sources=[make_source()],
    )
    payload = delivery_manifest_to_json(manifest, relative_to=tmp_path / "raw")
    assert payload["complete"] is True
    assert payload["tool_version"] == __version__
    file_payload = payload["files"][0]
    assert isinstance(file_payload, dict)
    assert file_payload["path"] == "era5/data.nc"
    assert file_payload["path_escapes_base"] is False
    assert file_payload["validation_status"] == "passed"
    details = file_payload["validation_details"]
    assert isinstance(details, list)
    assert details
    assert details[0]["kind"] == "checksum_match"
    integrity_payload = payload["integrity"][0]
    assert isinstance(integrity_payload, dict)
    assert integrity_payload["validation_status"] == "passed"
    assert integrity_payload["details"]
    serialised = json.dumps(payload)
    assert str(tmp_path) not in serialised.replace(str(tmp_path / "raw"), "")
    assert path.exists()


def test_export_flags_paths_outside_base(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
    )
    payload = delivery_manifest_to_json(manifest, relative_to=tmp_path / "somewhere-else")
    file_payload = payload["files"][0]
    assert isinstance(file_payload, dict)
    assert file_payload["path_escapes_base"] is True


def test_export_preserves_failed_skipped_quarantined(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    excluded = [
        ExcludedFile(
            disposition=ExcludedDisposition.FAILED,
            file_id="file-failed",
            task_id="task-failed",
            reason="transport gave up",
        ),
        ExcludedFile(
            disposition=ExcludedDisposition.SKIPPED,
            file_id="file-skipped",
            task_id="task-skipped",
            reason="source not applicable",
        ),
        ExcludedFile(
            disposition=ExcludedDisposition.QUARANTINED,
            file_id="file-bad",
            task_id="task-bad",
            reason="checksum mismatch",
            path="quarantine/20261104-bad.nc",
            size_bytes=123,
            integrity_status=IntegrityStatus.FAILED,
        ),
    ]
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
        excluded=excluded,
    )
    payload = delivery_manifest_to_json(manifest, relative_to=tmp_path, excluded=excluded)
    exported = payload["excluded"]
    assert isinstance(exported, list)
    assert [item["disposition"] for item in exported] == [
        "failed",
        "skipped",
        "quarantined",
    ]
    assert "excluded files" in (manifest.notes or "")


def test_manifest_model_invariant_rejects_complete_with_failed_report(
    tmp_path: Path,
) -> None:
    _, record = _fixture(tmp_path)
    from researchdatahub.core.models import DeliveryManifest

    with pytest.raises(ValidationError):
        DeliveryManifest(
            delivery_id="delivery-1",
            request_id="req-1",
            plan_id="plan-1",
            dataset_metadata=make_dataset_metadata(),
            files=[record],
            integrity=[make_report(status=IntegrityStatus.FAILED)],
            created_at=FIXED_NOW,
            tool_version=__version__,
            total_bytes=record.size_bytes,
            complete=True,
        )


def test_write_delivery_manifest_is_atomic_and_complete(tmp_path: Path) -> None:
    _, record = _fixture(tmp_path)
    manifest = build_delivery_manifest(
        request_id="req-1",
        plan_id="plan-1",
        dataset_metadata=make_dataset_metadata(),
        files=[record],
        integrity_reports=[make_report()],
    )
    target = write_delivery_manifest(
        manifest, tmp_path / "out" / "delivery.json", relative_to=tmp_path / "raw"
    )
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["delivery_id"] == manifest.delivery_id
    assert payload["complete"] is True
    assert not list(target.parent.glob("*.tmp"))
