"""scheduler 单测 fixture：真实临时 SQLite + 真实 HTTP 服务器 + tmp 存储。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.unit.test_transfer.http_server import TransferTestServer

from researchdatahub.config.models import (
    DefaultsConfig,
    IntegrityConfig,
    ManifestConfig,
    PlannerConfig,
    RetryConfig,
    SchedulerConfig,
    StorageConfig,
    TransferConfig,
)
from researchdatahub.core.models import IntegrityCheckKind
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.storage.local import LocalStorageBackend
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import RetryPolicy

DB_FILENAME = "manifest.db"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / DB_FILENAME


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


@pytest.fixture
def repo_factory(factory: ManifestConnectionFactory) -> Callable[[], SqliteManifestRepository]:
    """每线程/每次调用一条独立连接（SQLite 连接不可跨线程共享）。"""
    factory.initialize()
    return lambda: SqliteManifestRepository(factory)


@pytest.fixture
def config(tmp_path: Path) -> DefaultsConfig:
    """测试配置：真实存储根在 tmp_path，磁盘阈值归零，退避毫秒级。"""
    root = tmp_path / "rdh"
    return DefaultsConfig(
        storage=StorageConfig(root=str(root), free_space_floor_gb=0.0),
        scheduler=SchedulerConfig(
            max_workers=2,
            free_space_floor_gb=0.0,
            heartbeat_interval_seconds=5,
            lease_seconds=60,
        ),
        retry=RetryConfig(
            max_attempts=3,
            backoff_seconds=0.001,
            backoff_max_seconds=0.002,
            jitter=0.0,
        ),
        integrity=IntegrityConfig(
            default_checks=[IntegrityCheckKind.SIZE_MATCH, IntegrityCheckKind.CHECKSUM_MATCH],
            stale_timeout_seconds=60,
        ),
        planner=PlannerConfig(max_tasks_per_plan=100),
        transfer=TransferConfig(
            connect_timeout_seconds=2.0,
            read_timeout_seconds=5.0,
            chunk_size_bytes=65536,
        ),
    )


@pytest.fixture
def storage(config: DefaultsConfig) -> LocalStorageBackend:
    return LocalStorageBackend(config.storage, create_dirs=True)


@pytest.fixture
def integrity(config: DefaultsConfig) -> IntegrityChecker:
    return IntegrityChecker(config.integrity)


@pytest.fixture
def server() -> Iterator[TransferTestServer]:
    instance = TransferTestServer()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def transport(config: DefaultsConfig) -> Iterator[HttpTransportAdapter]:
    """M2 真实 HTTP 适配器：内部重试关闭（max_attempts=1），把重试留给调度层测试。"""
    adapter = HttpTransportAdapter(
        config=config.transfer,
        retry=RetryPolicy(
            max_attempts=1,
            backoff_seconds=0.001,
            backoff_max_seconds=0.002,
            jitter=0.0,
        ),
        trust_env=False,
    )
    try:
        yield adapter
    finally:
        adapter.close()
