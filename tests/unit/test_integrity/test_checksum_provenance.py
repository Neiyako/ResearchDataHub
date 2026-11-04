"""校验和来源标记：远端声明 vs 本地计算，ETag 绝不做 MD5。"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

import pytest
from integrity_helpers import make_checksum, make_context, write_bytes

from researchdatahub.core.models import Checksum, IntegrityStatus
from researchdatahub.integrity import CheckLevel, IntegrityChecker


def test_remote_declared_checksum_is_verified_locally(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "remote.bin", b"remote-verified-bytes")
    record, task = make_context(path, content_type=None)
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED
    checksum = report.observed["checksum"]
    assert checksum["origin"] == "remote_trusted"
    assert checksum["remote_checksum_verified"] is True
    assert checksum["local_sha256_recorded"] is True


def test_local_only_checksum_is_recorded_not_called_remote_verified(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "local.bin", b"local-only-bytes")
    record, task = make_context(path, content_type=None, asset_checksum_matches=False)
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    checksum = report.observed["checksum"]
    assert checksum["origin"] == "local_computed"
    assert checksum["remote_checksum_verified"] is False
    assert checksum["local_sha256_recorded"] is True


def test_explicit_etag_provenance_is_rejected(tmp_path: Path) -> None:
    content = b"etag-payload"
    path = write_bytes(tmp_path / "etag.bin", content)
    md5 = hashlib.md5(content).hexdigest()
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=f"md5:{md5}"),
        remote_etag=md5,
        extensions={"integrity.checksum_origin": "remote_etag"},
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED
    checks = {item["kind"]: item for item in report.observed["checks"]}
    assert checks["checksum_match"]["level"] == CheckLevel.PROTOCOL.value
    assert any("ETag" in failure for failure in report.failures)


def test_etag_masquerading_as_md5_is_inferred_and_rejected(tmp_path: Path) -> None:
    content = b"etag-looks-like-md5"
    path = write_bytes(tmp_path / "etag-md5.bin", content)
    md5 = hashlib.md5(content).hexdigest()
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=f"md5:{md5}"),
        remote_etag=f'"{md5}"',
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED
    assert any("ETag" in failure for failure in report.failures)


def test_md5_with_trusted_provenance_marker_passes(tmp_path: Path) -> None:
    content = b"trusted-md5"
    path = write_bytes(tmp_path / "trusted-md5.bin", content)
    md5 = hashlib.md5(content).hexdigest()
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=f"md5:{md5}"),
        remote_etag=md5,
        extensions={"integrity.checksum_origin": "remote_trusted"},
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED
    assert report.observed["checksum"]["origin"] == "remote_trusted"


def test_md5_mismatch_fails(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "md5-bad.bin", b"actual")
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value="md5:" + "0" * 32),
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED


def test_invalid_origin_marker_is_rejected(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "marker.bin", b"bytes")
    record, task = make_context(
        path,
        content_type=None,
        extensions={"integrity.checksum_origin": "trust_me_bro"},
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED
    assert any("invalid" in failure for failure in report.failures)


def test_sha256_16_prefix_is_supported(tmp_path: Path) -> None:
    content = b"short-digest"
    path = write_bytes(tmp_path / "short.bin", content)
    prefix = hashlib.sha256(content).hexdigest()[:16]
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=f"sha256-16:{prefix}"),
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED


def test_crc32_is_supported(tmp_path: Path) -> None:
    content = b"crc-content"
    path = write_bytes(tmp_path / "crc.bin", content)
    crc = f"{zlib.crc32(content):08x}"
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=f"crc32:{crc}"),
        asset_checksum_matches=False,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED


@pytest.mark.parametrize(
    ("algorithm", "value"),
    [("sha256", "sha256:" + "a" * 64), ("md5", "md5:" + "b" * 32)],
)
def test_wrong_remote_checksum_fails(tmp_path: Path, algorithm: str, value: str) -> None:
    path = write_bytes(tmp_path / f"wrong-{algorithm}.bin", b"payload")
    record, task = make_context(
        path,
        content_type=None,
        checksum=Checksum(value=value),
        asset_checksum_matches=True,
    )
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.FAILED


def test_make_checksum_helper_matches_record(tmp_path: Path) -> None:
    content = b"helper"
    path = write_bytes(tmp_path / "helper.bin", content)
    record, task = make_context(path, content_type=None, checksum=make_checksum(content))
    report = IntegrityChecker().check(record, task=task, kinds=["checksum_match"])
    assert report.status is IntegrityStatus.PASSED
