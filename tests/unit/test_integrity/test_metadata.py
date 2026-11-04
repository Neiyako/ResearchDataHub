"""元数据一致性：变量/维度/格式期望；缺失期望只记 SKIPPED，不伪装通过。"""

from __future__ import annotations

from pathlib import Path

import pytest
from integrity_helpers import make_context, write_bytes, write_netcdf

from researchdatahub.core.errors import IntegrityError
from researchdatahub.core.models import IntegrityStatus
from researchdatahub.integrity import IntegrityChecker


def test_matching_metadata_passes(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "meta.nc", variables=("t2m", "clwc"))
    record, task = make_context(path)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=["metadata_consistent"],
        expected_metadata={
            "format": "hdf5",
            "variables": ["t2m", "clwc"],
            "dimensions": ["x"],
            "size_bytes": len(path.read_bytes()),
        },
    )
    assert report.status is IntegrityStatus.PASSED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["metadata_consistent"]["status"] == "passed"


def test_missing_variable_fails(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "missing-var.nc", variables=("t2m",))
    record, task = make_context(path)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=["metadata_consistent"],
        expected_metadata={"variables": ["t2m", "clwc"]},
    )
    assert report.status is IntegrityStatus.FAILED
    assert any("clwc" in failure for failure in report.failures)


def test_variable_present_check_reports_missing_names(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "vars.nc", variables=("t2m",))
    record, task = make_context(path)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=["variable_present"],
        expected_metadata={"variables": ["t2m", "q"]},
    )
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["variable_present"]["status"] == "failed"


def test_no_expected_metadata_is_skipped(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "no-meta.nc")
    record, task = make_context(path)
    report = IntegrityChecker().check(record, task=task, kinds=["metadata_consistent"])
    assert report.status is IntegrityStatus.SKIPPED
    assert report.permits_completion is False
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["metadata_consistent"]["status"] == "skipped"


def test_variable_present_without_expectation_is_skipped(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "no-expect.nc")
    record, task = make_context(path)
    report = IntegrityChecker().check(record, task=task, kinds=["variable_present"])
    assert report.status is IntegrityStatus.SKIPPED


def test_unsupported_expected_metadata_key_fails(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "meta.bin", b"payload")
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=["metadata_consistent"],
        expected_metadata={"made_up_key": "value"},
    )
    assert report.status is IntegrityStatus.FAILED
    assert any("unsupported expected metadata key" in failure for failure in report.failures)


def test_size_metadata_mismatch_fails_without_netcdf(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "size-meta.bin", b"0123456789")
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=["metadata_consistent"],
        expected_metadata={"size_bytes": 3},
    )
    assert report.status is IntegrityStatus.FAILED
    assert any("size_bytes mismatch" in failure for failure in report.failures)


def test_non_json_expected_value_is_rejected(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "bad-expected.bin", b"payload")
    record, task = make_context(path, content_type=None)
    with pytest.raises(IntegrityError) as excinfo:
        IntegrityChecker().check(
            record,
            task=task,
            kinds=["metadata_consistent"],
            expected_metadata={"format": object()},
        )
    assert "non-JSON" in excinfo.value.message


def test_missing_dependency_for_metadata_is_skipped(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "dep-meta.nc")
    record, task = make_context(path)
    checker = IntegrityChecker(netcdf_probe=lambda candidate: None)
    report = checker.check(
        record,
        task=task,
        kinds=["metadata_consistent"],
        expected_metadata={"variables": ["t2m"]},
    )
    assert report.status is IntegrityStatus.SKIPPED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert "not installed" in checks["metadata_consistent"]["detail"]
