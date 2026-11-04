"""交付清单构建的测试：证据链、非 PASSED 拒绝、重启后一致、状态快照联动。"""

from __future__ import annotations

import pytest
from tests.conftest import (
    FIXED_NOW,
    make_file_record,
    make_integrity_report,
    make_request,
    make_source_metadata,
)
from tests.unit.test_manifest.helpers import seed_running_task

from researchdatahub.core.errors import ManifestError
from researchdatahub.core.models import DeliveryManifest, FileRecord, IntegrityReport
from researchdatahub.core.state import TaskState, transition
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository

REQUEST_ID = "req-era5-clwc-2011-2020"


def _complete_task_with_evidence(
    repo: SqliteManifestRepository,
    *,
    task_id: str = "task-0001",
    file_id: str = "file-0001",
    report_id: str = "report-0001",
) -> tuple[FileRecord, IntegrityReport]:
    """走完 RUNNING → VERIFYING → COMPLETED 的真实状态机，并留下文件与 PASSED 报告。"""
    running = seed_running_task(repo, task_id=task_id)
    record = make_file_record(file_id=file_id, task_id=task_id)
    repo.save_file_record(record)
    verifying = transition(running, TaskState.VERIFYING, reason="transfer done", now=FIXED_NOW)
    repo.save_task(verifying, expected_state=TaskState.RUNNING)
    report = make_integrity_report(report_id=report_id, task_id=task_id, file_id=file_id)
    repo.save_integrity_report(report)
    completed = transition(verifying, TaskState.COMPLETED, reason="integrity passed", now=FIXED_NOW)
    repo.save_task_and_event(
        completed,
        expected_state=TaskState.VERIFYING,
        event="task_completed",
        payload={"report_id": report_id},
    )
    return record, report


def test_build_delivery_manifest_from_real_evidence(repo: SqliteManifestRepository) -> None:
    request = make_request()
    repo.save_request(request)
    record, report = _complete_task_with_evidence(repo)

    manifest = repo.build_delivery_manifest(REQUEST_ID)
    assert isinstance(manifest, DeliveryManifest)
    assert manifest.request_id == REQUEST_ID
    assert manifest.plan_id == "plan-0001"
    assert manifest.files == [record]
    assert manifest.integrity == [report]
    assert manifest.dataset_metadata == request.metadata
    assert manifest.sources == [make_source_metadata()]
    assert manifest.total_bytes == record.size_bytes
    assert manifest.complete is True
    assert manifest.delivery_id.startswith("dlv-")
    assert manifest.tool_version


def test_delivery_id_is_deterministic(repo: SqliteManifestRepository) -> None:
    repo.save_request(make_request())
    _complete_task_with_evidence(repo)
    first = repo.build_delivery_manifest(REQUEST_ID)
    second = repo.build_delivery_manifest(REQUEST_ID)
    assert first.delivery_id == second.delivery_id
    assert first.files == second.files


def test_delivery_manifest_round_trips_through_json(repo: SqliteManifestRepository) -> None:
    repo.save_request(make_request())
    _complete_task_with_evidence(repo)
    manifest = repo.build_delivery_manifest(REQUEST_ID)
    assert DeliveryManifest.model_validate(manifest.model_dump(mode="json")) == manifest


def test_build_delivery_manifest_rejects_non_passed_report(
    repo: SqliteManifestRepository,
) -> None:
    repo.save_request(make_request())
    _complete_task_with_evidence(repo)
    repo.save_integrity_report(
        make_integrity_report(
            report_id="report-late-failure",
            task_id="task-0001",
            file_id=None,
            status="failed",
            failures=["post-delivery checksum mismatch"],
        )
    )
    with pytest.raises(ManifestError, match="non-PASSED"):
        repo.build_delivery_manifest(REQUEST_ID)


def test_build_delivery_manifest_requires_passed_report_for_every_file(
    repo: SqliteManifestRepository,
) -> None:
    repo.save_request(make_request())
    seed_running_task(repo, task_id="task-0001")
    repo.save_file_record(make_file_record())
    with pytest.raises(ManifestError, match="without a PASSED"):
        repo.build_delivery_manifest(REQUEST_ID)


def test_build_delivery_manifest_without_files_raises(repo: SqliteManifestRepository) -> None:
    repo.save_request(make_request())
    with pytest.raises(ManifestError, match="no file records"):
        repo.build_delivery_manifest(REQUEST_ID)


def test_build_delivery_manifest_for_unknown_request_raises(
    repo: SqliteManifestRepository,
) -> None:
    with pytest.raises(ManifestError, match="not registered"):
        repo.build_delivery_manifest("req-ghost-0001")


def test_build_delivery_manifest_requires_registered_source(
    repo: SqliteManifestRepository,
) -> None:
    """手工把 sources 行删掉（绕过仓库 API）后，交付清单必须明确报错而不是编造来源。"""
    repo.save_request(make_request())
    _complete_task_with_evidence(repo)
    connection = repo.factory.connect()
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM sources WHERE source_id = 'ncar'")
    finally:
        connection.close()
    with pytest.raises(ManifestError, match="source"):
        repo.build_delivery_manifest(REQUEST_ID)


def test_delivery_manifest_survives_process_restart(
    factory: ManifestConnectionFactory,
) -> None:
    factory.initialize()
    first_repo = SqliteManifestRepository(factory)
    try:
        first_repo.save_request(make_request())
        record, report = _complete_task_with_evidence(first_repo)
        first_manifest = first_repo.build_delivery_manifest(REQUEST_ID)
    finally:
        first_repo.close()

    restarted = SqliteManifestRepository(factory)
    try:
        second_manifest = restarted.build_delivery_manifest(REQUEST_ID)
    finally:
        restarted.close()

    assert second_manifest.delivery_id == first_manifest.delivery_id
    assert second_manifest.files == [record]
    assert second_manifest.integrity == [report]
    assert second_manifest.complete is True


def test_status_delivery_ready_requests_tracks_evidence(
    repo: SqliteManifestRepository,
) -> None:
    repo.save_request(make_request())
    task = seed_running_task(repo, task_id="task-0001")
    repo.save_file_record(make_file_record())

    assert repo.status().delivery_ready_requests == []

    verifying = transition(task, TaskState.VERIFYING, reason="verify", now=FIXED_NOW)
    repo.save_task(verifying, expected_state=TaskState.RUNNING)
    repo.save_integrity_report(make_integrity_report())
    assert repo.status().delivery_ready_requests == [REQUEST_ID]

    repo.save_integrity_report(
        make_integrity_report(
            report_id="report-failed",
            status="failed",
            failures=["size mismatch"],
            file_id=None,
        )
    )
    assert repo.status().delivery_ready_requests == []
