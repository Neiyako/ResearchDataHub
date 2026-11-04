"""planner 测试的共享 fixture：真实 SQLite 临时库（绝不写 ~/.rdh）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from researchdatahub.config.models import ManifestConfig
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.db"


@pytest.fixture
def factory(db_path: Path) -> ManifestConnectionFactory:
    return ManifestConnectionFactory(db_path, config=ManifestConfig())


@pytest.fixture
def repo(factory: ManifestConnectionFactory) -> Iterator[SqliteManifestRepository]:
    factory.initialize()
    repository = SqliteManifestRepository(factory)
    try:
        yield repository
    finally:
        repository.close()
