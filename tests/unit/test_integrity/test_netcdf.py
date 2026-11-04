"""NetCDF 检查：正常文件可打开、损坏/截断文件失败、依赖缺席只记 SKIPPED。"""

from __future__ import annotations

from pathlib import Path

import pytest
from integrity_helpers import make_context, write_bytes, write_netcdf

from researchdatahub.core.models import IntegrityStatus
from researchdatahub.integrity import IntegrityChecker, netcdf_backend_available


def test_netcdf_backend_available_in_this_environment() -> None:
    pytest.importorskip("netCDF4")
    assert netcdf_backend_available() is True


def test_valid_netcdf_passes_full_chain(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "valid.nc")
    record, task = make_context(path)
    report = IntegrityChecker().check(
        record,
        task=task,
        kinds=[
            "exists",
            "readable",
            "non_empty",
            "size_match",
            "truncation",
            "checksum_match",
            "format_sniff",
            "netcdf_openable",
        ],
    )
    assert report.status is IntegrityStatus.PASSED
    assert report.failures == []
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["format_sniff"]["actual"] == "hdf5"
    assert checks["netcdf_openable"]["status"] == "passed"
    assert report.observed["netcdf"]["engine"] == "netCDF4"
    assert "t2m" in report.observed["netcdf"]["variables"]


def test_corrupt_netcdf_fails_format_and_open_checks(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "corrupt.nc", b"this is not a netcdf file at all")
    record, task = make_context(path)
    report = IntegrityChecker().check(
        record, task=task, kinds=["size_match", "format_sniff", "netcdf_openable"]
    )
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["format_sniff"]["status"] == "failed"
    assert checks["netcdf_openable"]["status"] == "failed"
    assert any("format mismatch" in failure for failure in report.failures)


def test_truncated_netcdf_is_caught(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "truncated.nc")
    original_size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.truncate(40)
    record, task = make_context(path, size_bytes=original_size)
    report = IntegrityChecker().check(
        record, task=task, kinds=["size_match", "truncation", "netcdf_openable"]
    )
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["size_match"]["status"] == "failed"
    assert checks["truncation"]["status"] == "failed"


def test_missing_dependency_is_skipped_not_passed(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "dep.nc")
    record, task = make_context(path)
    checker = IntegrityChecker(netcdf_probe=lambda candidate: None)
    report = checker.check(record, task=task, kinds=["size_match", "netcdf_openable"])
    assert report.status is IntegrityStatus.SKIPPED
    assert report.permits_completion is False
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["netcdf_openable"]["status"] == "skipped"
    assert "not installed" in checks["netcdf_openable"]["detail"]


def test_netcdf_check_not_applicable_to_plain_text(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "notes.txt", b"plain text")
    record, task = make_context(path, content_type="text/plain", file_name="notes.txt")
    report = IntegrityChecker().check(record, task=task, kinds=["netcdf_openable"])
    assert report.status is IntegrityStatus.SKIPPED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["netcdf_openable"]["status"] == "skipped"
    assert "not applicable" in checks["netcdf_openable"]["detail"]
