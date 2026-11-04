"""staging → raw 提交：同内容跳过、不同内容拒绝覆盖、期望值校验与提交日志。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from storage_helpers import (
    assert_no_temporary_files,
    make_backend,
    make_checksum,
    sha256_hex,
    write_staging,
)

from researchdatahub.core.errors import StorageError
from researchdatahub.core.models import Checksum
from researchdatahub.storage import COMMIT_JOURNAL_SUFFIX, CommitStatus, PublishStatus


def test_staging_to_raw_commit(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"science-bytes" * 10
    source = write_staging(backend, "task-0001", "e5.nc", content)
    result = backend.commit_staging(source, dataset_id="era5", file_name="e5.nc")
    assert result.status is PublishStatus.COMMITTED
    assert result.path == backend.raw_dir / "era5" / "e5.nc"
    assert result.path.read_bytes() == content
    assert result.size_bytes == len(content)
    assert result.sha256 == sha256_hex(content)
    assert not source.exists(), "staging source must be moved, not copied"
    assert result.path.stat().st_mode & 0o777 == 0o444
    journal_path = result.path.with_name(result.path.name + COMMIT_JOURNAL_SUFFIX)
    journal = json.loads(journal_path.read_text())
    assert journal["status"] == CommitStatus.COMMITTED.value
    assert journal["sha256"] == sha256_hex(content)
    assert backend.pending_commits() == ()


def test_publish_original_protocol_method_returns_path(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "proto.nc", b"bytes")
    path = backend.publish_original(source, dataset_id="era5", file_name="proto.nc")
    assert isinstance(path, Path)
    assert path.exists()


def test_identical_republish_is_skipped_without_touching_target(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"identical-content"
    first = write_staging(backend, "task-0001", "same.nc", content)
    committed = backend.commit_staging(first, dataset_id="era5", file_name="same.nc")
    original_mode = committed.path.stat().st_mode
    second = write_staging(backend, "task-0002", "same.nc", content)
    result = backend.commit_staging(second, dataset_id="era5", file_name="same.nc")
    assert result.status is PublishStatus.SKIPPED_IDENTICAL
    assert result.path == committed.path
    assert result.path.read_bytes() == content
    assert second.exists(), "identical source must not be deleted on skip"
    assert result.path.stat().st_mode == original_mode


def test_conflicting_republish_refuses_to_overwrite(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    first = write_staging(backend, "task-0001", "conflict.nc", b"original")
    committed = backend.commit_staging(first, dataset_id="era5", file_name="conflict.nc")
    second = write_staging(backend, "task-0002", "conflict.nc", b"different-and-longer")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(second, dataset_id="era5", file_name="conflict.nc")
    assert "refusing to overwrite" in excinfo.value.message
    assert committed.path.read_bytes() == b"original"
    assert second.read_bytes() == b"different-and-longer"


def test_expected_size_mismatch_rejected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "size.nc", b"12345")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="size.nc", expected_size=99)
    assert excinfo.value.detail["expected_size"] == 99
    assert source.exists()
    assert not (backend.raw_dir / "era5" / "size.nc").exists()


def test_expected_checksum_mismatch_rejected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "sha.nc", b"payload")
    wrong = Checksum(value="sha256:" + "0" * 64)
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(
            source, dataset_id="era5", file_name="sha.nc", expected_checksum=wrong
        )
    assert "checksum" in excinfo.value.message
    assert source.exists()


def test_expected_md5_checksum_supported(tmp_path: Path) -> None:
    import hashlib

    backend = make_backend(tmp_path)
    content = b"md5-payload"
    source = write_staging(backend, "task-0001", "md5.bin", content)
    expected = Checksum(value="md5:" + hashlib.md5(content).hexdigest())
    result = backend.commit_staging(
        source, dataset_id="era5", file_name="md5.bin", expected_checksum=expected
    )
    assert result.path.read_bytes() == content


def test_missing_and_directory_sources_rejected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    with pytest.raises(StorageError):
        backend.commit_staging(
            backend.staging_dir / "nope.nc", dataset_id="era5", file_name="nope.nc"
        )
    directory = backend.staging_dir / "task-0001" / "adir"
    directory.mkdir(parents=True)
    with pytest.raises(StorageError):
        backend.commit_staging(directory, dataset_id="era5", file_name="adir")


def test_publish_from_temp_dir_works(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    temp = backend.allocate_temp("task-0001", file_name="tempfile.nc")
    temp.write_bytes(b"temp-bytes")
    result = backend.commit_staging(temp, dataset_id="era5", file_name="tempfile.nc")
    assert result.path.read_bytes() == b"temp-bytes"
    assert not temp.exists()


def test_target_directory_conflict_rejected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    target = backend.raw_dir / "era5" / "conflict.nc"
    target.mkdir(parents=True)
    source = write_staging(backend, "task-0001", "conflict.nc", b"payload")
    with pytest.raises(StorageError):
        backend.commit_staging(source, dataset_id="era5", file_name="conflict.nc")


def test_pending_commit_journal_blocks_new_commit(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    target = backend.raw_dir / "era5" / "pending.nc"
    target.parent.mkdir(parents=True, exist_ok=True)
    journal_dir = backend.raw_dir / "era5"
    pending_source = backend.allocate_staging("task-0000", "pending.nc")
    pending_source.write_bytes(b"previous")
    journal = {
        "schema_version": 1,
        "target_path": str(target),
        "source_path": str(pending_source),
        "dataset_id": "era5",
        "file_name": "pending.nc",
        "size_bytes": 8,
        "sha256": sha256_hex(b"previous"),
        "status": "pending",
        "created_at": "2026-11-04T00:00:00+00:00",
        "updated_at": "2026-11-04T00:00:00+00:00",
        "attempts": 0,
    }
    journal_path = journal_dir / ("pending.nc" + COMMIT_JOURNAL_SUFFIX)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    source = write_staging(backend, "task-0001", "pending.nc", b"new")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="pending.nc")
    assert "recover_pending_commits" in excinfo.value.message
    journal_path.unlink()


def test_aborted_journal_is_cleared_for_new_attempt(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    target = backend.raw_dir / "era5" / "retry.nc"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = backend.allocate_staging("task-0001", "retry.nc")
    source.write_bytes(b"payload")
    journal = {
        "schema_version": 1,
        "target_path": str(target),
        "source_path": str(source),
        "dataset_id": "era5",
        "file_name": "retry.nc",
        "size_bytes": 7,
        "sha256": sha256_hex(b"payload"),
        "status": "aborted",
        "created_at": "2026-11-04T00:00:00+00:00",
        "updated_at": "2026-11-04T00:00:00+00:00",
        "attempts": 1,
    }
    journal_path = target.with_name(target.name + COMMIT_JOURNAL_SUFFIX)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    result = backend.commit_staging(source, dataset_id="era5", file_name="retry.nc")
    assert result.status is PublishStatus.COMMITTED
    assert result.path.read_bytes() == b"payload"
    assert_no_temporary_files(backend.root)


def test_checksum_object_matches_content(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "check.nc", b"payload")
    result = backend.commit_staging(
        source,
        dataset_id="era5",
        file_name="check.nc",
        expected_checksum=make_checksum(source.read_bytes()),
    )
    assert result.sha256 == sha256_hex(b"payload")


def test_expected_crc32_checksum_supported(tmp_path: Path) -> None:
    import zlib

    backend = make_backend(tmp_path)
    content = b"crc32-payload"
    source = write_staging(backend, "task-0001", "crc.bin", content)
    expected = Checksum(value=f"crc32:{zlib.crc32(content):08x}")
    result = backend.commit_staging(
        source, dataset_id="era5", file_name="crc.bin", expected_checksum=expected
    )
    assert result.path.read_bytes() == content
