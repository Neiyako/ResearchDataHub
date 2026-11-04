"""``rdh db init/status/migrate`` 的 service 层测试（真实临时库，不启动子进程）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from tests.conftest import make_request, make_source_metadata

from researchdatahub.config.models import ManifestConfig
from researchdatahub.core.errors import ManifestError
from researchdatahub.core.ports import ManifestRepository, assert_implements
from researchdatahub.manifest.cli_service import (
    DbTarget,
    db_init,
    db_migrate,
    db_status,
    open_manifest,
    resolve_db_target,
)
from researchdatahub.manifest.schema import MIGRATIONS, SCHEMA_VERSION


def _target(tmp_path: Path) -> DbTarget:
    """临时目标：父目录故意不存在，用来验证 ``db init`` 会创建目录。"""
    return DbTarget(db_path=tmp_path / "state" / "manifest.db", config=ManifestConfig())


def _write_defaults_config(tmp_path: Path, *, db_filename: str) -> Path:
    """写一个最小可用的 ``defaults.yaml``（root 指向临时目录，绝不污染 ~/.rdh）。"""
    root = tmp_path / "rdh-home"
    document = {
        "defaults": {
            "storage": {"root": str(root)},
            "manifest": {"db_filename": db_filename},
        }
    }
    path = tmp_path / "defaults.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def _create_v1_database(db_path: Path) -> None:
    """手工构造一个只有 v1 schema 的库（用于验证升级路径）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    source = make_source_metadata()
    request = make_request()
    created_at = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, description, applied_at) VALUES (1, ?, ?)",
            ("hand-built v1", created_at),
        )
        connection.execute(
            "INSERT INTO sources (source_id, display_name, provider, access_route, dataset_id, "
            "origin_uri, protocol_version, license, citation, retrieved_at, extensions, "
            "adapter_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                source.source_id,
                source.display_name,
                source.provider,
                source.access_route.value,
                source.dataset_id,
                source.origin_uri,
                source.protocol_version,
                source.license,
                source.citation,
                source.retrieved_at.isoformat(),
                json.dumps(source.extensions),
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO requests (request_id, dataset_id, priority, payload, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.dataset_id,
                request.priority.rank,
                json.dumps(request.model_dump(mode="json")),
                created_at,
                created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# db_init
# --------------------------------------------------------------------------- #
def test_db_init_creates_directory_database_and_schema(tmp_path: Path) -> None:
    target = _target(tmp_path)
    assert not target.db_path.parent.exists()

    result = db_init(target)

    assert result.created is True
    assert result.previous_version == 0
    assert result.schema_version == SCHEMA_VERSION
    assert result.applied_versions == tuple(migration.version for migration in MIGRATIONS)
    assert target.db_path.is_file()


def test_db_init_is_idempotent(tmp_path: Path) -> None:
    target = _target(tmp_path)
    db_init(target)
    second = db_init(target)
    assert second.created is False
    assert second.previous_version == SCHEMA_VERSION
    assert second.schema_version == SCHEMA_VERSION
    assert second.applied_versions == ()


# --------------------------------------------------------------------------- #
# db_status
# --------------------------------------------------------------------------- #
def test_db_status_rejects_missing_database(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="does not exist"):
        db_status(_target(tmp_path))


def test_db_status_rejects_uninitialized_database(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    target.db_path.touch()
    with pytest.raises(ManifestError, match="not initialized"):
        db_status(target)


def test_db_status_reports_wal_and_counts(tmp_path: Path) -> None:
    target = _target(tmp_path)
    db_init(target)
    status = db_status(target)
    assert status.db_path == str(target.db_path)
    assert status.db_schema_version == SCHEMA_VERSION
    assert status.journal_mode == "wal"
    assert status.foreign_keys is True
    assert status.file_count == 0
    assert status.integrity_failures == 0
    assert set(status.task_counts) >= {"QUEUED", "RUNNING", "COMPLETED", "FAILED"}
    assert all(count == 0 for count in status.task_counts.values())


# --------------------------------------------------------------------------- #
# db_migrate
# --------------------------------------------------------------------------- #
def test_db_migrate_applies_all_migrations_then_noop(tmp_path: Path) -> None:
    target = _target(tmp_path)
    first = db_migrate(target)
    assert first.previous_version == 0
    assert first.schema_version == SCHEMA_VERSION
    assert len(first.applied) == len(MIGRATIONS)
    assert first.applied[0].startswith("v1: ")

    second = db_migrate(target)
    assert second.previous_version == SCHEMA_VERSION
    assert second.applied == ()


def test_db_migrate_upgrades_v1_database(tmp_path: Path) -> None:
    target = _target(tmp_path)
    _create_v1_database(target.db_path)

    result = db_migrate(target)

    assert result.previous_version == 1
    assert result.schema_version == SCHEMA_VERSION
    assert len(result.applied) == 1
    assert result.applied[0].startswith("v2: ")

    repository = open_manifest(target)
    try:
        assert repository.get_source("ncar") == make_source_metadata()
        assert repository.get_request("req-era5-clwc-2011-2020") == make_request()
    finally:
        repository.close()


# --------------------------------------------------------------------------- #
# resolve_db_target / open_manifest
# --------------------------------------------------------------------------- #
def test_resolve_db_target_from_config_file(tmp_path: Path) -> None:
    config_path = _write_defaults_config(tmp_path, db_filename="rdh-manifest.db")
    target = resolve_db_target(defaults_path=config_path)
    assert target.db_path == (tmp_path / "rdh-home" / "rdh-manifest.db").resolve()
    assert target.config.db_filename == "rdh-manifest.db"

    db_init(target)
    assert db_status(target).db_schema_version == SCHEMA_VERSION


def test_resolve_db_target_storage_root_override(tmp_path: Path) -> None:
    config_path = _write_defaults_config(tmp_path, db_filename="custom.db")
    override = tmp_path / "override"
    target = resolve_db_target(defaults_path=config_path, storage_root=override)
    assert target.db_path == (override / "custom.db").resolve()
    assert target.db_path.parent == override.resolve()


def test_resolve_db_target_uses_repository_defaults() -> None:
    """仓库自带 ``configs/defaults.yaml`` 必须能被解析（默认 root = ~/.rdh，不创建文件）。"""
    target = resolve_db_target()
    assert target.db_path.name == "manifest.db"
    assert str(target.db_path).endswith("manifest.db")


def test_open_manifest_returns_protocol_repository(tmp_path: Path) -> None:
    target = _target(tmp_path)
    db_init(target)
    repository = open_manifest(target)
    try:
        assert_implements(repository, ManifestRepository)
        assert isinstance(repository, ManifestRepository)
        assert repository.schema_version == SCHEMA_VERSION
    finally:
        repository.close()
