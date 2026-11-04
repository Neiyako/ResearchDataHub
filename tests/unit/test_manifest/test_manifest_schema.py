"""Schema、PRAGMA 与版本化迁移的测试（真实 SQLite 文件）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.conftest import make_request, make_source_metadata
from tests.unit.test_manifest.helpers import seed_queued_task

from researchdatahub.core.errors import ManifestError
from researchdatahub.manifest.connection import ManifestConnectionFactory, read_schema_version
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.manifest.schema import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    latest_version,
)

EXPECTED_TABLES = {
    "sources",
    "datasets",
    "requests",
    "assets",
    "tasks",
    "files",
    "transfer_logs",
    "integrity_reports",
    "schema_version",
    "plans",
    "plan_tasks",
}

EXPECTED_INDEXES = {
    "idx_tasks_dispatch",
    "idx_tasks_request",
    "idx_tasks_plan",
    "idx_tasks_lease",
    "idx_files_task",
    "idx_assets_dataset",
    "idx_assets_source",
    "idx_transfer_logs_task",
    "idx_integrity_task",
    "idx_integrity_file",
    "idx_plans_request",
    "idx_plan_tasks_order",
}


def _table_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


def _index_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


def _schema_version_rows(db_path: Path) -> list[tuple[int, str]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]
    finally:
        connection.close()


def test_migrations_are_contiguous_unique_and_non_empty() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1))
    assert latest_version() == LATEST_SCHEMA_VERSION == SCHEMA_VERSION == len(MIGRATIONS)
    for migration in MIGRATIONS:
        assert migration.description.strip()
        assert migration.statements
        assert all(statement.strip() for statement in migration.statements)


def test_initialize_creates_schema_version_and_all_tables(
    factory: ManifestConnectionFactory, db_path: Path
) -> None:
    factory.initialize()
    tables = _table_names(db_path)
    assert tables >= EXPECTED_TABLES
    assert _index_names(db_path) >= EXPECTED_INDEXES
    rows = _schema_version_rows(db_path)
    assert [version for version, _ in rows] == list(range(1, len(MIGRATIONS) + 1))
    assert all(description for _, description in rows)
    connection = factory.connect()
    try:
        assert read_schema_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_initialize_sets_wal_foreign_keys_and_busy_timeout(
    factory: ManifestConnectionFactory,
) -> None:
    factory.initialize()
    connection = factory.connect()
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()
    finally:
        connection.close()
    assert journal is not None
    assert str(journal[0]).lower() == "wal"
    assert foreign_keys is not None
    assert int(foreign_keys[0]) == 1
    assert busy_timeout is not None
    assert int(busy_timeout[0]) == factory.config.busy_timeout_ms


def test_initialize_is_idempotent_and_preserves_data(
    factory: ManifestConnectionFactory,
) -> None:
    factory.initialize()
    repository = SqliteManifestRepository(factory)
    try:
        source = make_source_metadata()
        repository.upsert_source(source, adapter_id="ncar-http")
        seed_queued_task(repository, task_id="task-idem")
        first = factory.migrate()
        second = factory.migrate()
        assert first.applied == ()
        assert second.applied == ()
        assert repository.schema_version == SCHEMA_VERSION
        assert repository.get_source(source.source_id) == source
        loaded = repository.load_task("task-idem")
        assert loaded.task_id == "task-idem"
    finally:
        repository.close()


def test_migrate_v1_database_upgrades_and_keeps_rows(
    factory: ManifestConnectionFactory, db_path: Path
) -> None:
    """手工造一个只有 v1 schema 且有数据的库，``initialize()`` 必须升级且不丢数据。"""
    source = make_source_metadata()
    request = make_request()
    created_at = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC).isoformat()

    connection = sqlite3.connect(db_path)
    try:
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
            (1, "hand-built v1", created_at),
        )
        connection.execute(
            "INSERT INTO sources (source_id, display_name, provider, access_route, dataset_id, "
            "origin_uri, protocol_version, license, citation, retrieved_at, extensions, "
            "adapter_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                "ncar-http",
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

    report = factory.migrate()
    assert report.previous_version == 1
    assert report.schema_version == SCHEMA_VERSION
    assert [migration.version for migration in report.applied] == [2]

    repository = SqliteManifestRepository(factory)
    try:
        assert repository.schema_version == SCHEMA_VERSION
        assert repository.get_source(source.source_id) == source
        assert repository.get_request(request.request_id) == request
        seed_queued_task(repository, task_id="task-after-migration")
        heartbeat_task = repository.load_task("task-after-migration")
        assert heartbeat_task.heartbeat_at is None
        assert heartbeat_task.lease_expires_at is None
    finally:
        repository.close()

    tables = _table_names(db_path)
    assert {"plans", "plan_tasks"} <= tables
    inspector = sqlite3.connect(db_path)
    try:
        columns = {str(row[1]) for row in inspector.execute("PRAGMA table_info(tasks)")}
    finally:
        inspector.close()
    assert {"heartbeat_at", "lease_expires_at"} <= columns


def test_status_on_uninitialized_database_raises_and_reports_version_zero(
    factory: ManifestConnectionFactory,
) -> None:
    repository = SqliteManifestRepository(factory)
    try:
        assert repository.schema_version == 0
        with pytest.raises(ManifestError, match="not initialized"):
            repository.status()
    finally:
        repository.close()


def test_connect_requires_existing_parent_directory(tmp_path: Path) -> None:
    missing_parent = tmp_path / "does" / "not" / "exist"
    factory = ManifestConnectionFactory(missing_parent / "manifest.db")
    with pytest.raises(ManifestError, match="directory does not exist"):
        factory.connect()


def test_repository_initialize_is_idempotent(repo: SqliteManifestRepository) -> None:
    first = repo.schema_version
    repo.initialize()
    repo.initialize()
    assert repo.schema_version == first == SCHEMA_VERSION


def test_repository_can_initialize_schema_after_open(
    factory: ManifestConnectionFactory,
) -> None:
    """先开连接再 initialize 也必须可用（连接创建时 schema 还不在）。"""
    repository = SqliteManifestRepository(factory)
    try:
        assert repository.schema_version == 0
        repository.initialize()
        assert repository.schema_version == SCHEMA_VERSION
        seed_queued_task(repository, task_id="task-late-init")
        assert repository.load_task("task-late-init").state.value == "QUEUED"
    finally:
        repository.close()


def test_migrate_report_lists_applied_migrations(
    factory: ManifestConnectionFactory,
) -> None:
    report = factory.migrate()
    assert report.previous_version == 0
    assert report.schema_version == SCHEMA_VERSION
    assert [migration.version for migration in report.applied] == [
        migration.version for migration in MIGRATIONS
    ]
