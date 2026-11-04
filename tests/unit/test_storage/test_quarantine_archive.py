"""隔离与归档：移动不删除、原因侧车、重名唯一化与禁止覆盖。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from storage_helpers import install_exdev, make_backend, sha256_hex, write_staging

from researchdatahub.core.errors import StorageError


def test_quarantine_moves_file_and_writes_reason_sidecar(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"corrupted-bytes"
    source = write_staging(backend, "task-0001", "bad.nc", content)
    quarantined = backend.quarantine(source, reason="sha256 mismatch")
    assert quarantined.parent == backend.quarantine_dir
    assert not source.exists()
    assert quarantined.read_bytes() == content
    sidecar = quarantined.with_name(quarantined.name + ".reason.json")
    payload = json.loads(sidecar.read_text())
    assert payload["reason"] == "sha256 mismatch"
    assert payload["original_path"] == str(source)
    assert payload["sha256"] == sha256_hex(content)
    assert payload["size_bytes"] == len(content)


def test_quarantine_is_idempotent_for_already_quarantined_file(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "bad.nc", b"bytes")
    first = backend.quarantine(source, reason="first")
    second = backend.quarantine(first, reason="second")
    assert first == second
    assert first.exists()


def test_quarantine_requires_reason_and_existing_file(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    with pytest.raises(StorageError):
        backend.quarantine(backend.staging_dir / "missing.nc", reason="anything")
    source = write_staging(backend, "task-0001", "exists.nc", b"bytes")
    with pytest.raises(StorageError):
        backend.quarantine(source, reason="   ")


def test_quarantine_name_collisions_are_unique(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    first_source = write_staging(backend, "task-0001", "same.nc", b"first")
    second_source = write_staging(backend, "task-0002", "same.nc", b"second")
    first = backend.quarantine(first_source, reason="bad-1")
    second = backend.quarantine(second_source, reason="bad-2")
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_quarantine_across_devices_copies_before_removing_source(
    monkeypatch, tmp_path: Path
) -> None:
    backend = make_backend(tmp_path)
    content = b"cross-device-quarantine"
    source = write_staging(backend, "task-0001", "cross.nc", content)
    cross_calls = install_exdev(monkeypatch)
    quarantined = backend.quarantine(source, reason="cross-device")
    assert cross_calls
    assert quarantined.read_bytes() == content
    assert not source.exists()
    sidecar = quarantined.with_name(quarantined.name + ".reason.json")
    assert json.loads(sidecar.read_text())["size_bytes"] == len(content)


def test_quarantine_rejects_symlink(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = backend.allocate_staging("task-0001", "link.nc")
    link.symlink_to(outside)
    with pytest.raises(StorageError):
        backend.quarantine(link, reason="suspicious link")


def test_archive_moves_file(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "bundle.tar", b"archived")
    target = backend.archive(source)
    assert target == backend.archive_dir / "bundle.tar"
    assert target.read_bytes() == b"archived"
    assert not source.exists()


def test_archive_identical_target_is_skipped_and_source_kept(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "bundle.tar", b"same")
    first = backend.archive(source)
    second_source = write_staging(backend, "task-0002", "bundle.tar", b"same")
    second = backend.archive(second_source)
    assert first == second
    assert second_source.exists(), "identical archive input must be left in place"


def test_archive_conflicting_target_refuses_overwrite(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "bundle.tar", b"original")
    target = backend.archive(source)
    conflicting = write_staging(backend, "task-0002", "bundle.tar", b"different")
    with pytest.raises(StorageError):
        backend.archive(conflicting)
    assert target.read_bytes() == b"original"
    assert conflicting.read_bytes() == b"different"


def test_archive_custom_name_and_symlink_rejection(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "raw-name.bin", b"x")
    target = backend.archive(source, archive_name="renamed.tar")
    assert target.name == "renamed.tar"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = backend.allocate_staging("task-0002", "link.bin")
    link.symlink_to(outside)
    with pytest.raises(StorageError):
        backend.archive(link)
