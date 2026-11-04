"""崩溃恢复与孤儿扫描：提交日志驱动的 FINALIZED / RETRIED / ABORTED / NEEDS_ATTENTION。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from storage_helpers import make_backend, sha256_hex, write_staging

from researchdatahub.storage import (
    COMMIT_JOURNAL_SUFFIX,
    CommitStatus,
    LocalStorageBackend,
    RecoveryAction,
)


def _write_journal(
    backend: LocalStorageBackend,
    *,
    target: Path,
    source: Path,
    size_bytes: int,
    sha256: str,
    status: CommitStatus = CommitStatus.PENDING,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "target_path": str(target),
        "source_path": str(source),
        "dataset_id": target.parent.name,
        "file_name": target.name,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "status": status.value,
        "created_at": "2026-11-04T00:00:00+00:00",
        "updated_at": "2026-11-04T00:00:00+00:00",
        "attempts": 0,
    }
    journal = target.with_name(target.name + COMMIT_JOURNAL_SUFFIX)
    journal.write_text(json.dumps(payload), encoding="utf-8")
    return journal


def test_pending_with_missing_target_is_retried(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"pending-payload"
    source = write_staging(backend, "task-0001", "retry.nc", content)
    target = backend.raw_dir / "era5" / "retry.nc"
    journal = _write_journal(
        backend,
        target=target,
        source=source,
        size_bytes=len(content),
        sha256=sha256_hex(content),
    )
    recoveries = backend.recover_pending_commits()
    assert len(recoveries) == 1
    assert recoveries[0].action is RecoveryAction.RETRIED
    assert target.read_bytes() == content
    assert not source.exists()
    assert json.loads(journal.read_text())["status"] == CommitStatus.COMMITTED.value


def test_pending_with_valid_target_is_finalized(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"already-moved"
    source = write_staging(backend, "task-0001", "done.nc", content)
    target = backend.raw_dir / "era5" / "done.nc"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    journal = _write_journal(
        backend,
        target=target,
        source=source,
        size_bytes=len(content),
        sha256=sha256_hex(content),
    )
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.FINALIZED
    assert not source.exists()
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o777 == 0o444
    assert json.loads(journal.read_text())["status"] == CommitStatus.COMMITTED.value


def test_pending_with_conflicting_target_needs_attention(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "conflict.nc", b"expected")
    target = backend.raw_dir / "era5" / "conflict.nc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"someone-else")
    journal = _write_journal(
        backend,
        target=target,
        source=source,
        size_bytes=len(b"expected"),
        sha256=sha256_hex(b"expected"),
    )
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.NEEDS_ATTENTION
    assert target.read_bytes() == b"someone-else", "recovery must never delete/modify raw data"
    assert json.loads(journal.read_text())["status"] == CommitStatus.PENDING.value


def test_pending_without_target_or_source_is_aborted(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = backend.allocate_staging("task-0001", "gone.nc")
    target = backend.raw_dir / "era5" / "gone.nc"
    journal = _write_journal(
        backend,
        target=target,
        source=source,
        size_bytes=4,
        sha256=sha256_hex(b"data"),
    )
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.ABORTED
    assert recoveries[0].target_path is None
    assert json.loads(journal.read_text())["status"] == CommitStatus.ABORTED.value


def test_committed_journal_with_missing_target_needs_attention(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    content = b"committed-content"
    source = write_staging(backend, "task-0001", "committed.nc", content)
    result = backend.commit_staging(source, dataset_id="era5", file_name="committed.nc")
    result.path.unlink()
    recoveries = backend.recover_pending_commits()
    assert len(recoveries) == 1
    assert recoveries[0].action is RecoveryAction.NEEDS_ATTENTION
    assert "missing" in recoveries[0].detail


def test_corrupt_journal_reported_not_ignored(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    target = backend.raw_dir / "era5" / "corrupt.nc"
    target.parent.mkdir(parents=True)
    journal = target.with_name(target.name + COMMIT_JOURNAL_SUFFIX)
    journal.write_text("{not-json", encoding="utf-8")
    recoveries = backend.recover_pending_commits()
    assert recoveries[0].action is RecoveryAction.NEEDS_ATTENTION
    assert "corrupt" in recoveries[0].detail


def test_valid_committed_journal_is_ignored(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "ok.nc", b"fine")
    backend.commit_staging(source, dataset_id="era5", file_name="ok.nc")
    assert backend.recover_pending_commits() == ()
    assert backend.pending_commits() == ()


def test_scan_orphans_classifies_raw_staging_and_partial(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    known_source = write_staging(backend, "task-0001", "known.nc", b"known")
    known = backend.commit_staging(known_source, dataset_id="era5", file_name="known.nc").path
    (backend.raw_dir / "era5" / "orphan.nc").write_bytes(b"orphan")
    pending_source = write_staging(backend, "task-0002", "pending.nc", b"pending")
    pending_target = backend.raw_dir / "era5" / "pending.nc"
    pending_target.parent.mkdir(parents=True, exist_ok=True)
    pending_target.write_bytes(b"pending")
    _write_journal(
        backend,
        target=pending_target,
        source=pending_source,
        size_bytes=len(b"pending"),
        sha256=sha256_hex(b"pending"),
    )
    unrelated_staging = write_staging(backend, "task-0003", "stale.nc", b"stale")
    old_temp = backend.allocate_temp("task-0004")
    old_temp.write_bytes(b"old-temp")
    old_time = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(old_temp, (old_time, old_time))
    partial = backend.raw_dir / ".rdh-partial-deadbeef-ghost.nc"
    partial.write_bytes(b"half-copied")

    scan = backend.scan_orphans(known_raw_paths=[known], max_temp_age_seconds=3600)
    assert [path.name for path in scan.raw_orphans] == ["orphan.nc"]
    assert [path.name for path in scan.pending_commit_targets] == ["pending.nc"]
    assert unrelated_staging in scan.staging_orphans
    assert pending_source not in scan.staging_orphans
    assert old_temp in scan.temp_files
    assert partial in scan.partial_files
    assert scan.total == (
        len(scan.raw_orphans)
        + len(scan.pending_commit_targets)
        + len(scan.staging_orphans)
        + len(scan.temp_files)
        + len(scan.partial_files)
    )
