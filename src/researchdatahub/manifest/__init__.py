"""Manifest 持久化：SQLite 记录任务状态、文件记录与审计事件（ADR-0003）。

对外公开
--------
* :class:`~researchdatahub.manifest.connection.ManifestConnectionFactory`：
  连接工厂（每线程一条连接）+ 版本化迁移；
* :class:`~researchdatahub.manifest.repository.SqliteManifestRepository`：
  :class:`~researchdatahub.core.ports.ManifestRepository` 的完整实现；
* :mod:`researchdatahub.manifest.cli_service`：``rdh db init/status/migrate`` 的
  service 层（CLI 只负责参数解析与呈现，命令注册由 ``cli`` 模块负责）。
"""

from __future__ import annotations

from researchdatahub.manifest.connection import (
    ManifestConnectionFactory,
    MigrationReport,
    read_schema_version,
)
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.manifest.schema import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    Migration,
    latest_version,
)

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "ManifestConnectionFactory",
    "Migration",
    "MigrationReport",
    "SqliteManifestRepository",
    "latest_version",
    "read_schema_version",
]
