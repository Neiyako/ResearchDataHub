"""采集报告：计数、失败原因聚合、校验统计、JSON 导出与终端表格渲染。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    FIXED_NOW,
    make_record,
    make_report,
    make_source,
    make_task,
    write_data,
)

from researchdatahub.core.models import IntegrityStatus
from researchdatahub.core.state import TaskState
from researchdatahub.reporting import (
    FailureRecord,
    QuarantinedRecord,
    build_collection_report,
    format_bytes,
    render_collection_report,
    write_collection_report,
)


def _report_for(tmp_path: Path):
    path = write_data(tmp_path / "raw" / "era5" / "data.nc", b"ten-bytes!")
    record = make_record(path)
    return path, record


def test_counts_and_total_bytes_from_tasks_and_files(tmp_path: Path) -> None:
    _, record = _report_for(tmp_path)
    tasks = [
        make_task("task-0001", TaskState.COMPLETED),
        make_task("task-0002", TaskState.FAILED),
        make_task("task-0003", TaskState.SKIPPED),
        make_task("task-0004", TaskState.QUARANTINED),
    ]
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=tasks,
        files=[record],
        integrity_reports=[make_report()],
        generated_at=FIXED_NOW,
    )
    assert report.request_count == 1
    assert report.discovered_tasks == 4
    assert (report.completed_tasks, report.failed_tasks) == (1, 1)
    assert (report.skipped_tasks, report.quarantined_tasks) == (1, 1)
    assert report.unfinished_tasks == 0
    assert report.total_bytes == len(b"ten-bytes!")
    assert report.integrity.passed == 1
    assert report.generated_at == FIXED_NOW


def test_failure_reasons_are_aggregated_and_failed_tasks_supplemented(tmp_path: Path) -> None:
    _, record = _report_for(tmp_path)
    tasks = [
        make_task("task-0001", TaskState.FAILED, last_error="timeout after 3 attempts"),
        make_task("task-0002", TaskState.FAILED, last_error="timeout again"),
        make_task("task-0003", TaskState.COMPLETED),
    ]
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=tasks,
        files=[record],
        integrity_reports=[make_report()],
        failures=[
            FailureRecord(task_id="task-0001", code="transport_timeout", message="timeout"),
            FailureRecord(
                task_id="task-0002",
                code="transport_timeout",
                message="timeout",
                source_id="ncar",
            ),
            FailureRecord(task_id="task-0003", code="bad_config", message="misconfigured"),
        ],
    )
    assert report.failed_tasks == 2
    assert report.failure_reasons() == (("transport_timeout", 2), ("bad_config", 1))
    assert all(
        failure.task_id != "task-0003" or failure.code == "bad_config"
        for failure in report.failures
    )


def test_failed_task_without_explicit_record_is_included(tmp_path: Path) -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.FAILED, last_error="boom")],
        files=[],
        integrity_reports=[],
    )
    assert [failure.code for failure in report.failures] == ["task_failed"]
    assert report.failures[0].message == "boom"


def test_integrity_tally_covers_all_statuses(tmp_path: Path) -> None:
    reports = [
        make_report(file_id="file-1", report_id="r1", status=IntegrityStatus.PASSED),
        make_report(file_id="file-2", report_id="r2", status=IntegrityStatus.FAILED),
        make_report(file_id="file-3", report_id="r3", status=IntegrityStatus.SKIPPED),
        make_report(file_id="file-4", report_id="r4", status=IntegrityStatus.UNKNOWN),
    ]
    files = [
        make_record(write_data(tmp_path / f"raw/file-{index}.bin", b"x"), file_id=f"file-{index}")
        for index in range(1, 6)
    ]
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.COMPLETED)],
        files=files,
        integrity_reports=reports,
    )
    assert report.integrity.passed == 1
    assert report.integrity.failed == 1
    assert report.integrity.skipped == 1
    assert report.integrity.unknown == 1
    assert report.integrity.missing == 1, "file-5 has no report"


def test_completed_without_file_record_warns(tmp_path: Path) -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.COMPLETED)],
        files=[],
        integrity_reports=[],
    )
    assert any("no FileRecord" in warning for warning in report.warnings)


def test_unfinished_task_warns(tmp_path: Path) -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.RUNNING)],
        files=[],
        integrity_reports=[],
    )
    assert report.unfinished_tasks == 1
    assert any("unfinished" in warning for warning in report.warnings)


def test_sources_and_delivery_manifest_path_in_json(tmp_path: Path) -> None:
    _, record = _report_for(tmp_path)
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task()],
        files=[record],
        integrity_reports=[make_report()],
        sources=[make_source()],
        dataset_id="era5-pressure-levels",
        delivery_manifest_path=tmp_path / "out" / "delivery.json",
        base_dir=tmp_path,
    )
    payload = report.as_json()
    assert payload["dataset_id"] == "era5-pressure-levels"
    assert payload["delivery_manifest_path"].endswith("delivery.json")
    sources = payload["sources"]
    assert isinstance(sources, list)
    assert sources[0]["source_id"] == "ncar"
    files = payload["files"]
    assert isinstance(files, list)
    assert files[0]["path"] == "raw/era5/data.nc"
    assert files[0]["integrity_status"] == "passed"


def test_quarantined_records_are_included_and_supplemented(tmp_path: Path) -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[
            make_task("task-0001", TaskState.QUARANTINED, last_error="checksum mismatch"),
            make_task("task-0002", TaskState.COMPLETED),
        ],
        files=[],
        integrity_reports=[],
        quarantined=[
            QuarantinedRecord(
                task_id="task-0002",
                reason="manual quarantine",
                path="quarantine/x.nc",
                file_id="file-2",
                size_bytes=10,
            )
        ],
    )
    reasons = {item.reason for item in report.quarantined}
    assert "manual quarantine" in reasons
    assert "checksum mismatch" in reasons
    payload = report.as_json()
    quarantined = payload["quarantined"]
    assert isinstance(quarantined, list)
    assert len(quarantined) == 2


def test_render_contains_expected_labels_and_numbers(tmp_path: Path) -> None:
    _, record = _report_for(tmp_path)
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.COMPLETED)],
        files=[record],
        integrity_reports=[make_report()],
        sources=[make_source()],
        delivery_manifest_path="delivery.json",
    )
    rendered = render_collection_report(report, width=120)
    assert "采集报告" in rendered
    assert "req-1" in rendered
    assert "请求量" in rendered
    assert "总下载量" in rendered
    assert "校验结果" in rendered
    assert "通过 1" in rendered
    assert "ncar" in rendered
    assert "delivery.json" in rendered
    assert "\x1b[" not in rendered


def test_render_failure_and_quarantine_tables(tmp_path: Path) -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task("task-0001", TaskState.FAILED, last_error="boom")],
        files=[],
        integrity_reports=[],
        failures=[
            FailureRecord(task_id="task-0001", code="RDH_TRANSPORT_RETRYABLE", message="timeout")
        ],
        quarantined=[QuarantinedRecord(task_id="task-0001", reason="bad hash", path="q/x")],
    )
    rendered = render_collection_report(report)
    assert "失败原因" in rendered
    assert "RDH_TRANSPORT_RETRYABLE" in rendered
    assert "错误码聚合" in rendered
    assert "隔离文件" in rendered
    assert "bad hash" in rendered


def test_render_empty_report_still_valid() -> None:
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[],
        files=[],
        integrity_reports=[],
    )
    rendered = render_collection_report(report)
    assert "采集报告" in rendered
    assert report.total_bytes == 0


def test_write_collection_report_json(tmp_path: Path) -> None:
    _, record = _report_for(tmp_path)
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task()],
        files=[record],
        integrity_reports=[make_report()],
    )
    target = write_collection_report(report, tmp_path / "out" / "report.json")
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["counts"]["completed"] == 1
    assert payload["total_bytes"] == len(b"ten-bytes!")
    assert payload["integrity"]["passed"] == 1
    assert not list(target.parent.glob("*.tmp"))


def test_invalid_request_count_rejected() -> None:
    from researchdatahub.core.errors import ModelValidationError

    with pytest.raises(ModelValidationError):
        build_collection_report(
            request_id="req-1",
            plan_id="plan-1",
            tasks=[],
            files=[],
            integrity_reports=[],
            request_count=0,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB (1024 B)"),
        (1536, "1.5 KiB (1536 B)"),
        (1024**2, "1.0 MiB (1048576 B)"),
    ],
)
def test_format_bytes(value: int, expected: str) -> None:
    assert format_bytes(value) == expected


def test_format_bytes_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_bytes(-1)


def test_report_inconsistency_is_surfaced_as_warning(tmp_path: Path) -> None:
    path = write_data(tmp_path / "raw" / "era5" / "data.nc", b"bytes")
    record = make_record(path)
    ghost_report = make_report(file_id="file-ghost", report_id="report-ghost")
    report = build_collection_report(
        request_id="req-1",
        plan_id="plan-1",
        tasks=[make_task()],
        files=[record],
        integrity_reports=[ghost_report],
    )
    assert any("report_for_unknown_file" in warning for warning in report.warnings)
    assert report.integrity.missing == 1
