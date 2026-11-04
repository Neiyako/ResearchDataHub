"""``check_path``：对任意路径体检，不伪造 task/file/dataset 元数据。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from integrity_helpers import make_checksum, write_bytes, write_netcdf

from researchdatahub.core.errors import IntegrityError
from researchdatahub.core.models import IntegrityStatus
from researchdatahub.integrity import CheckStatus, IntegrityChecker, PathCheckResult


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_normal_file_passes_with_expectations(tmp_path: Path) -> None:
    content = b"plain-file-payload"
    path = write_bytes(tmp_path / "data.bin", content)
    result = IntegrityChecker().check_path(
        path,
        kinds=["exists", "readable", "non_empty", "size_match", "truncation", "checksum_match"],
        expected_size=len(content),
        expected_checksum=make_checksum(content),
    )
    assert isinstance(result, PathCheckResult)
    assert result.status is IntegrityStatus.PASSED
    assert result.ok is True
    assert result.path == path
    assert result.size_bytes == len(content)
    assert result.sha256 == _sha256(content)
    assert result.failures == ()
    assert result.expected_size == len(content)
    assert result.expected_checksum == make_checksum(content).value
    statuses = {check.kind.value: check.status for check in result.checks}
    assert statuses["size_match"] is CheckStatus.PASSED
    assert statuses["checksum_match"] is CheckStatus.PASSED
    # JSON 可序列化，且不含伪造的 task/file/dataset 字段
    payload = result.as_json()
    json.dumps(payload)
    assert not {"task_id", "file_id", "dataset_id"} & set(payload)
    assert payload["path"] == str(path)


def test_missing_path_raises_integrity_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.nc"
    with pytest.raises(IntegrityError) as excinfo:
        IntegrityChecker().check_path(missing)
    assert excinfo.value.detail["path"] == str(missing)


def test_directory_path_raises_integrity_error(tmp_path: Path) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()
    with pytest.raises(IntegrityError) as excinfo:
        IntegrityChecker().check_path(directory)
    assert "directory" in excinfo.value.message


def test_empty_file_fails_non_empty(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "empty.bin", b"")
    result = IntegrityChecker().check_path(
        path, kinds=["exists", "readable", "non_empty"], expected_size=0
    )
    assert result.status is IntegrityStatus.FAILED
    assert any("non_empty" in failure for failure in result.failures)
    assert result.sha256 == _sha256(b"")


def test_expected_size_mismatch_fails(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "size.bin", b"0123456789")
    result = IntegrityChecker().check_path(
        path, kinds=["size_match", "truncation"], expected_size=999
    )
    assert result.status is IntegrityStatus.FAILED
    checks = {check.kind.value: check for check in result.checks}
    assert checks["size_match"].status is CheckStatus.FAILED
    assert checks["truncation"].status is CheckStatus.FAILED
    assert "999" in checks["size_match"].detail


def test_expected_checksum_mismatch_fails(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "checksum.bin", b"actual-content")
    wrong = make_checksum(b"different-content")
    result = IntegrityChecker().check_path(path, kinds=["checksum_match"], expected_checksum=wrong)
    assert result.status is IntegrityStatus.FAILED
    assert any("checksum mismatch" in failure for failure in result.failures)


def test_without_expectations_records_sha256_but_does_not_pass(tmp_path: Path) -> None:
    content = b"unverified-but-hashed"
    path = write_bytes(tmp_path / "no-expect.bin", content)
    result = IntegrityChecker().check_path(
        path, kinds=["size_match", "truncation", "checksum_match"]
    )
    assert result.status is IntegrityStatus.SKIPPED
    assert result.ok is False
    assert result.expected_size is None
    assert result.expected_checksum is None
    assert result.sha256 == _sha256(content)
    checks = {check.kind.value: check for check in result.checks}
    assert checks["checksum_match"].status is CheckStatus.SKIPPED
    assert checks["checksum_match"].actual == f"sha256:{_sha256(content)}"
    assert checks["size_match"].status is CheckStatus.SKIPPED
    assert checks["truncation"].status is CheckStatus.SKIPPED


def test_valid_netcdf_passes_path_check(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "valid.nc", variables=("t2m",))
    content = path.read_bytes()
    result = IntegrityChecker().check_path(
        path,
        kinds=[
            "exists",
            "readable",
            "non_empty",
            "size_match",
            "checksum_match",
            "format_sniff",
            "netcdf_openable",
        ],
        expected_size=len(content),
        expected_checksum=make_checksum(content),
    )
    assert result.status is IntegrityStatus.PASSED
    assert result.actual_format == "hdf5"
    assert result.netcdf is not None
    assert "t2m" in result.netcdf.variables
    payload = result.as_json()
    assert payload["netcdf"] is not None
    assert payload["actual_format"] == "hdf5"


def test_corrupt_netcdf_fails_path_check(tmp_path: Path) -> None:
    content = b"this is definitely not a netcdf container"
    path = write_bytes(tmp_path / "corrupt.nc", content)
    result = IntegrityChecker().check_path(
        path,
        kinds=["size_match", "checksum_match", "format_sniff", "netcdf_openable"],
        expected_size=len(content),
        expected_checksum=make_checksum(content),
    )
    assert result.status is IntegrityStatus.FAILED
    checks = {check.kind.value: check for check in result.checks}
    assert checks["checksum_match"].status is CheckStatus.PASSED
    assert checks["format_sniff"].status is CheckStatus.FAILED
    assert checks["netcdf_openable"].status is CheckStatus.FAILED


def test_missing_dependency_is_skipped(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "dep.nc")
    checker = IntegrityChecker(netcdf_probe=lambda candidate: None)
    result = checker.check_path(path, kinds=["netcdf_openable"])
    assert result.status is IntegrityStatus.SKIPPED
    assert result.checks[0].status is CheckStatus.SKIPPED
    assert "not installed" in result.checks[0].detail
    assert result.ok is False


def test_expected_metadata_variables_checked(tmp_path: Path) -> None:
    path = write_netcdf(tmp_path / "meta.nc", variables=("t2m", "clwc"))
    result = IntegrityChecker().check_path(
        path,
        kinds=["metadata_consistent"],
        expected_metadata={"format": "hdf5", "variables": ["t2m", "clwc"]},
    )
    assert result.status is IntegrityStatus.PASSED
    failing = IntegrityChecker().check_path(
        path,
        kinds=["metadata_consistent"],
        expected_metadata={"variables": ["q"]},
    )
    assert failing.status is IntegrityStatus.FAILED
    assert any("q" in failure for failure in failing.failures)


def test_empty_kinds_reports_unknown_but_records_sha256(tmp_path: Path) -> None:
    content = b"bytes"
    path = write_bytes(tmp_path / "unknown.bin", content)
    result = IntegrityChecker().check_path(path, kinds=[])
    assert result.status is IntegrityStatus.UNKNOWN
    assert result.checks == ()
    assert result.ok is False
    assert result.sha256 == _sha256(content)


def test_negative_expected_size_rejected(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "negative.bin", b"bytes")
    with pytest.raises(IntegrityError) as excinfo:
        IntegrityChecker().check_path(path, expected_size=-1)
    assert "expected_size" in excinfo.value.message


def test_clock_and_checker_id_are_injected(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    fixed = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
    path = write_bytes(tmp_path / "clock.bin", b"bytes")
    result = IntegrityChecker(clock=lambda: fixed, checker_id="path-checker").check_path(
        path, kinds=["exists"]
    )
    assert result.checked_at == fixed
    assert result.checker_id == "path-checker"
