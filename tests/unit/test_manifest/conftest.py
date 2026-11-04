"""Manifest 测试的共享 fixture：真实 SQLite 临时库 + 已初始化仓库。

测试数据一律落在 pytest 的 ``tmp_path`` 下，绝不写 ``~/.rdh``。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from researchdatahub.config.models import ManifestConfig
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository

DB_FILENAME = "manifest.db"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """临时数据库文件路径（每个测试独立文件，互不干扰）。"""
    return tmp_path / DB_FILENAME


@pytest.fixture
def factory(db_path: Path) -> ManifestConnectionFactory:
    """连接工厂（尚未初始化 schema）。"""
    return ManifestConnectionFactory(db_path, config=ManifestConfig())


@pytest.fixture
def repo(factory: ManifestConnectionFactory) -> Iterator[SqliteManifestRepository]:
    """已初始化 schema 的仓库实例；测试结束关闭连接。"""
    factory.initialize()
    repository = SqliteManifestRepository(factory)
    try:
        yield repository
    finally:
        repository.close()
