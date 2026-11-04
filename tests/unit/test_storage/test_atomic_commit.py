"""原子提交策略：同设备 rename、跨设备（EXDEV）复制 + fsync + 同目录 rename。"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from storage_helpers import (
    assert_no_temporary_files,
    install_exdev,
    make_backend,
    sha256_hex,
    write_staging,
)

from researchdatahub.core.errors import StorageError
from researchdatahub.storage import CommitStatus, PublishStatus, RecoveryAction


def test_same_filesystem_uses_direct_rename(monkeypatch, tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"direct-rename-payload"
    source = write_staging(backend, "task-0001", "direct.nc", content)
    real_replace = os.replace
    calls: list[tuple[Path, Path]] = []

    def recorder(src: Path | str, dst: Path | str) -> None:
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recorder)
    result = backend.commit_staging(source, dataset_id="era5", file_name="direct.nc")
    assert result.status is PublishStatus.COMMITTED
    assert (source, result.path) in calls, "same-device commit must use os.replace move"
    assert result.path.read_bytes() == content
    assert_no_temporary_files(backend.root)


def test_cross_device_commit_copies_fsyncs_and_renames(monkeypatch, tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"cross-device-payload" * 128
    source = write_staging(backend, "task-0001", "cross.nc", content)
    cross_calls = install_exdev(monkeypatch)
    result = backend.commit_staging(source, dataset_id="era5", file_name="cross.nc")
    assert result.status is PublishStatus.COMMITTED
    assert result.path.read_bytes() == content
    assert result.sha256 == sha256_hex(content)
    assert not source.exists(), "cross-device move must clean up the staging source"
    assert cross_calls, "the EXDEV fallback must have been exercised"
    assert_no_temporary_files(backend.root)
    journal_path = result.path.with_name(result.path.name + ".commit.json")
    import json

    assert json.loads(journal_path.read_text())["status"] == CommitStatus.COMMITTED.value


def test_cross_device_copy_failure_leaves_recoverable_state(monkeypatch, tmp_path: Path) -> None:
    backend = make_backend(tmp_path)

    state = {"failed": False}

    def unreadable(source: Path, target: Path) -> None:
        if not state["failed"]:
            state["failed"] = True
            source.chmod(0o000)

    install_exdev(monkeypatch, callback=unreadable)
    content = b"copy-failure-payload"
    source = write_staging(backend, "task-0001", "broken.nc", content)
    target = backend.raw_dir / "era5" / "broken.nc"
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="broken.nc")
    assert "cross-device copy" in excinfo.value.message
    assert source.exists()
    assert not target.exists(), "a failed cross-device copy must never expose a raw target"
    assert_no_temporary_files(backend.root)
    pending = backend.pending_commits()
    assert len(pending) == 1
    assert pending[0].status is CommitStatus.PENDING

    source.chmod(0o600)
    recoveries = backend.recover_pending_commits()
    assert len(recoveries) == 1
    assert recoveries[0].action is RecoveryAction.RETRIED
    assert target.read_bytes() == content
    assert not source.exists()
    assert backend.pending_commits() == ()


def test_cross_device_content_tampering_is_detected(monkeypatch, tmp_path: Path) -> None:
    backend = make_backend(tmp_path)

    def tamper(source: Path, target: Path) -> None:
        with source.open("ab") as handle:
            handle.write(b"TAMPERED")

    install_exdev(monkeypatch, callback=tamper)
    source = write_staging(backend, "task-0001", "tampered.nc", b"original")
    target = backend.raw_dir / "era5" / "tampered.nc"
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="tampered.nc")
    assert "mismatch" in excinfo.value.message
    assert not target.exists()
    assert_no_temporary_files(backend.root)
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.NEEDS_ATTENTION
    assert "mismatch" in recoveries[0].detail


def test_transient_move_failure_is_recoverable(monkeypatch, tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    real_replace = os.replace
    state = {"failed": False}

    def fail_once(src: Path | str, dst: Path | str) -> None:
        source_path = Path(src)
        target_path = Path(dst)
        if (
            source_path.parent != target_path.parent
            and not state["failed"]
            and not source_path.name.startswith(".rdh-partial-")
        ):
            state["failed"] = True
            raise OSError(errno.EACCES, "simulated permission problem")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_once)
    source = write_staging(backend, "task-0001", "flaky.nc", b"flaky-payload")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="flaky.nc")
    assert "atomic move into raw failed" in excinfo.value.message
    assert source.exists()
    assert len(backend.pending_commits()) == 1
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.RETRIED
    assert (backend.raw_dir / "era5" / "flaky.nc").read_bytes() == b"flaky-payload"
