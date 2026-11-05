"""``tests/unit/test_network`` 的公共装配：真实控制面库 + 真实本地 HTTP 服务器。

原则
----
* **不用 mock 顶替被测逻辑**：SQLite 是真的（``tmp_path/workers.db``），HTTP 服务器是真的
  （``TransferTestServer`` 起真实 socket），传输适配器是真的（``HttpTransportAdapter``）。
* 只用可注入的**时钟**控制健康判定时间，其它全部真实运行。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.config.models import StorageConfig, TransferConfig
from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionTask,
    Checksum,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.network.routes.manager import RouteManager
from researchdatahub.network.routes.stats import RouteStatsStore
from researchdatahub.network.workers.local import LocalWorker
from researchdatahub.network.workers.registry import WorkerRegistry
from researchdatahub.network.workers.store import WorkerStore
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import RetryPolicy

DATASET_ID = "era5-pressure-levels"
SOURCE_ID = "ncar"
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """可手动推进的时钟（同一对象同时提供 ``now`` 与单调秒数）。"""

    def __init__(self, start: datetime = NOW) -> None:
        self._current = start
        self._monotonic = 1000.0

    def now(self) -> datetime:
        """当前（可注入）UTC 时间。"""
        return self._current

    def monotonic(self) -> float:
        """当前（可注入）单调秒数。"""
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """把两个时钟同时推进 ``seconds`` 秒。"""
        self._current += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleeper(self, seconds: float) -> None:
        """模拟"睡 ``seconds`` 秒"：只推进单调时钟，不推进墙钟。

        用它替换真实 ``time.sleep`` 时，调用方的超时判定与实测耗时都基于单调时钟，
        因此不会被墙钟推进干扰；真实线程调度仍会发生（不会忙等）。
        """
        self._monotonic += seconds
        time.sleep(0.002)


@pytest.fixture
def clock() -> FakeClock:
    """测试时钟（健康判定/链路耗时用）。"""
    return FakeClock()


@pytest.fixture
def workers_db(tmp_path: Path) -> Path:
    """控制面库路径（独立于 manifest.db）。"""
    return tmp_path / "workers.db"


@pytest.fixture
def store(workers_db: Path) -> WorkerStore:
    """控制面库句柄（已迁移）。"""
    instance = WorkerStore(workers_db)
    instance.initialize()
    return instance


@pytest.fixture
def registry(store: WorkerStore, clock: FakeClock) -> WorkerRegistry:
    """真实注册表（心跳超时 30s / 离线 120s）。"""
    return WorkerRegistry(store, clock=clock.now)


@pytest.fixture
def stats_store(store: WorkerStore) -> RouteStatsStore:
    """链路统计存储（与注册表共用控制面库）。"""
    return RouteStatsStore(store)


@pytest.fixture
def route_manager(registry: WorkerRegistry, stats_store: RouteStatsStore) -> RouteManager:
    """选路管理器。"""
    return RouteManager(registry=registry, stats=stats_store)


@pytest.fixture
def http_server() -> Iterator[TransferTestServer]:
    """真实本地 HTTP 服务器（提供被下载的源文件）。"""
    instance = TransferTestServer()
    try:
        yield instance
    finally:
        instance.close()


def file_bytes(size: int, *, seed: int = 7) -> bytes:
    """确定性的二进制内容（重复调用同一 seed 得到同一字节）。"""
    block = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return (block * (size // len(block) + 1))[:size]


def serve(
    server: TransferTestServer, path: str, data: bytes, **resource_kwargs: object
) -> Resource:
    """在真实服务器上注册一个资源。"""
    resource = Resource(body=data, **resource_kwargs)  # type: ignore[arg-type]
    server.add(path, resource)
    return resource


def sha256_checksum(data: bytes) -> str:
    """``sha256:<hex>`` 形式的校验和。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_asset(
    *,
    uri: str,
    data: bytes,
    file_name: str = "e5.1-pressure-2020-01.nc",
    source_id: str = SOURCE_ID,
    dataset_id: str = DATASET_ID,
    checksum: bool = True,
) -> SourceAsset:
    """构造指向真实本地 HTTP 服务器的资产。"""
    metadata = SourceMetadata(
        source_id=source_id,
        display_name=f"{source_id} test source",
        provider=f"{source_id} test provider",
        access_route=AccessRoute.HTTPS,
        dataset_id=dataset_id,
        origin_uri=uri,
        retrieved_at=NOW,
    )
    return SourceAsset(
        asset_id=f"asset-{source_id}-{file_name}".lower().replace(".", "-"),
        source_id=source_id,
        dataset_id=dataset_id,
        access_route=AccessRoute.HTTPS,
        origin_uri=uri,
        file_name=file_name,
        size_bytes=len(data),
        checksum=Checksum(value=sha256_checksum(data)) if checksum else None,
        metadata=metadata,
        discovered_at=NOW,
    )


def make_task(asset: SourceAsset, *, task_id: str = "task-net-0001") -> AcquisitionTask:
    """构造指向该资产的采集任务。"""
    return AcquisitionTask(
        task_id=task_id,
        plan_id="plan-net-0001",
        request_id="req-net-0001",
        asset=asset,
    )


@pytest.fixture
def transfer_config() -> TransferConfig:
    """毫秒级重试与超时的传输配置（测试不能真等 60 秒读超时）。"""
    return TransferConfig(
        connect_timeout_seconds=2.0,
        read_timeout_seconds=5.0,
        chunk_size_bytes=65536,
    )


@pytest.fixture
def http_adapter(transfer_config: TransferConfig) -> Iterator[HttpTransportAdapter]:
    """真实 HTTP 传输适配器（内部重试关闭，把重试留给上层）。"""
    adapter = HttpTransportAdapter(
        config=transfer_config,
        retry=RetryPolicy(
            max_attempts=1, backoff_seconds=0.001, backoff_max_seconds=0.002, jitter=0.0
        ),
        trust_env=False,
    )
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def local_worker(tmp_path: Path, http_adapter: HttpTransportAdapter) -> Iterator[LocalWorker]:
    """真实本地 worker（存储根在 tmp，传输走真实 HTTP）。"""
    worker = LocalWorker(
        worker_id="local-a",
        storage_root=tmp_path / "local-a",
        transports=[http_adapter],
        max_concurrent=2,
    )
    try:
        yield worker
    finally:
        worker.close()


def storage_config(root: Path) -> StorageConfig:
    """测试存储配置（磁盘阈值归零，避免 CI 磁盘波动影响判定）。"""
    return StorageConfig(root=str(root), free_space_floor_gb=0.0)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    """忙等到 ``predicate`` 为真；超时返回 ``False``（供异步执行断言使用）。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
