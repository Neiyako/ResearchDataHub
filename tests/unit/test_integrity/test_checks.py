"""检查链核心行为：空文件、缺失、大小、截断、可读性、fail_fast 与报告不变式。"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from integrity_helpers import FIXED_NOW, make_checksum, make_context, write_bytes

from researchdatahub.config.models import IntegrityConfig
from researchdatahub.core.errors import IntegrityError
from researchdatahub.core.models import IntegrityStatus
from researchdatahub.integrity import CheckLevel, CheckStatus, IntegrityChecker

REPORT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def test_missing_file_raises_integrity_error(tmp_path: Path) -> None:
    record, task = make_context(tmp_path / "missing.nc")
    checker = IntegrityChecker()
    with pytest.raises(IntegrityError) as excinfo:
        checker.check(record, task=task, kinds=["size_match"])
    assert excinfo.value.detail["file_id"] == "file-0001"


def test_directory_path_raises_integrity_error(tmp_path: Path) -> None:
    directory = tmp_path / "data.nc"
    directory.mkdir()
    record, task = make_context(directory)
    with pytest.raises(IntegrityError) as excinfo:
        IntegrityChecker().check(record, task=task, kinds=["size_match"])
    assert "directory" in excinfo.value.message


def test_empty_file_fails_non_empty_check(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "empty.bin", b"")
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["non_empty", "size_match"])
    assert report.status is IntegrityStatus.FAILED
    assert any("non_empty" in failure for failure in report.failures)
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["non_empty"]["status"] == CheckStatus.FAILED.value


def test_size_mismatch_fails_and_checksum_still_checked(tmp_path: Path) -> None:
    content = b"0123456789"
    path = write_bytes(tmp_path / "small.bin", content)
    record, task = make_context(path, size_bytes=999, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["size_match", "checksum_match"])
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["size_match"]["status"] == CheckStatus.FAILED.value
    assert checks["checksum_match"]["status"] == CheckStatus.PASSED.value


def test_truncation_detected_when_actual_smaller(tmp_path: Path) -> None:
    content = b"only-five"
    path = write_bytes(tmp_path / "truncated.bin", content)
    record, task = make_context(path, size_bytes=100, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["truncation", "size_match"])
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["truncation"]["status"] == CheckStatus.FAILED.value
    assert "missing" in checks["truncation"]["detail"]
    assert report.status is IntegrityStatus.FAILED


def test_oversized_file_is_not_called_truncated(tmp_path: Path) -> None:
    content = b"0123456789"
    path = write_bytes(tmp_path / "big.bin", content)
    record, task = make_context(path, size_bytes=5, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["truncation", "size_match"])
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["size_match"]["status"] == CheckStatus.FAILED.value
    assert checks["truncation"]["status"] == CheckStatus.PASSED.value


def test_sha256_mismatch_fails(tmp_path: Path) -> None:
    content = b"real-bytes"
    path = write_bytes(tmp_path / "sha.bin", content)
    wrong = make_checksum(b"other-bytes")
    record, task = make_context(path, checksum=wrong, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED
    assert any("checksum mismatch" in failure for failure in report.failures)


def test_sha256_match_passes(tmp_path: Path) -> None:
    content = b"real-bytes"
    path = write_bytes(tmp_path / "sha-ok.bin", content)
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED
    assert report.observed["checksum"]["local_sha256_recorded"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_unreadable_file_fails_readable_and_skips_checksum(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "unreadable.bin", b"hidden-bytes")
    record, task = make_context(path, content_type=None)
    path.chmod(0o000)
    try:
        report = IntegrityChecker().check(record, task=task, kinds=["readable", "checksum_match"])
    finally:
        path.chmod(0o600)
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["readable"]["status"] == CheckStatus.FAILED.value
    assert checks["checksum_match"]["status"] == CheckStatus.SKIPPED.value


def test_unsupported_and_declared_unimplemented_kinds_raise(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "x.bin", b"bytes")
    record, task = make_context(path, content_type=None)
    checker = IntegrityChecker()
    with pytest.raises(IntegrityError) as excinfo:
        checker.check(record, task=task, kinds=["bogus_check"])
    assert "unsupported" in excinfo.value.message
    with pytest.raises(IntegrityError) as excinfo:
        checker.check(record, task=task, kinds=["time_coverage"])
    assert "not implemented" in excinfo.value.message


def test_empty_kinds_reports_unknown(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "unknown.bin", b"bytes")
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=[])
    assert report.status is IntegrityStatus.UNKNOWN
    assert report.checks == []
    assert report.failures == []
    assert report.permits_completion is False


def test_none_kinds_uses_config_defaults_and_skipped_is_not_passed(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "plain.bin", b"plain-text-payload")
    record, task = make_context(path, content_type=None, file_name="plain.bin")
    report = IntegrityChecker(IntegrityConfig()).check(record, task=task)
    assert report.status is IntegrityStatus.SKIPPED
    kinds = {item["kind"] for item in report.observed["checks"]}
    assert {"size_match", "checksum_match", "netcdf_openable"} <= kinds
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["netcdf_openable"]["status"] == CheckStatus.SKIPPED.value
    assert report.permits_completion is False


def test_fail_fast_stops_after_first_failure(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "empty-failfast.bin", b"")
    record, task = make_context(path, content_type=None)
    checker = IntegrityChecker(IntegrityConfig(fail_fast=True))
    report = checker.check(
        record,
        task=task,
        kinds=["exists", "readable", "non_empty", "size_match", "checksum_match"],
    )
    assert report.status is IntegrityStatus.FAILED
    executed = [item["kind"] for item in report.observed["checks"]]
    assert executed == ["exists", "readable", "non_empty"]
    assert checker.fail_fast is True


def test_report_is_json_safe_and_carries_expected_observed(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "json.bin", b"payload")
    record, task = make_context(path, content_type=None)
    checker = IntegrityChecker(clock=lambda: FIXED_NOW, checker_id="unit-test-checker")
    report = checker.check(
        record, task=task, kinds=["exists", "readable", "size_match", "checksum_match"]
    )
    assert REPORT_ID_PATTERN.fullmatch(report.report_id)
    assert report.checked_at == FIXED_NOW
    assert report.checker_id == "unit-test-checker"
    assert report.status is IntegrityStatus.PASSED
    assert report.failures == []
    assert report.expected["size_bytes"] == record.size_bytes
    assert report.observed["size_bytes"] == len(b"payload")
    json.dumps(report.observed)
    json.dumps(report.expected)
    levels = {item["kind"]: item["level"] for item in report.observed["checks"]}
    assert levels["size_match"] == CheckLevel.SIZE.value
    assert levels["checksum_match"] == CheckLevel.CHECKSUM.value


def test_report_invariants_hold_for_each_status(tmp_path: Path) -> None:
    checker = IntegrityChecker()
    valid = write_bytes(tmp_path / "ok.bin", b"valid")
    record, task = make_context(valid, content_type=None)
    passed = checker.check(record, task=task, kinds=["exists"])
    assert passed.status is IntegrityStatus.PASSED
    assert passed.failures == []
    broken = write_bytes(tmp_path / "broken.bin", b"")
    record2, task2 = make_context(broken, content_type=None)
    failed = checker.check(record2, task=task2, kinds=["non_empty"])
    assert failed.status is IntegrityStatus.FAILED
    assert failed.failures
    unknown = checker.check(record, task=task, kinds=[])
    assert unknown.status is IntegrityStatus.UNKNOWN
    assert unknown.permits_completion is False
    assert datetime.now(UTC) >= passed.checked_at


def test_implements_integrity_checker_protocol(tmp_path: Path) -> None:
    from researchdatahub.core.ports import IntegrityChecker as IntegrityCheckerProtocol
    from researchdatahub.core.ports import assert_implements

    path = write_bytes(tmp_path / "proto.bin", b"bytes")
    record, task = make_context(path, content_type=None)
    checker = IntegrityChecker()
    assert_implements(checker, IntegrityCheckerProtocol)
    report = checker.check(record, task=task, kinds=["size_match"])
    assert report.status is IntegrityStatus.PASSED


def test_skipped_result_is_not_passed(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "skip.bin", b"bytes")
    record, task = make_context(path, content_type=None, file_name="skip.bin")
    report = IntegrityChecker().check(record, task=task, kinds=["netcdf_openable"])
    checks = report.observed["checks"]
    assert all(item["passed"] is False for item in checks)
    assert checks[0]["status"] == CheckStatus.SKIPPED.value
