"""scheduler 测试的共享装配：真实 SQLite + 真实 HTTP 服务器 + tmp 存储。

原则（与仓库其它测试一致）：不 Mock 核心语义。
* 数据库是真实临时 SQLite（``tmp_path``，绝不写 ``~/.rdh``）；
* HTTP 是 ``tests/unit/test_transfer/http_server.py`` 的真实 ``ThreadingHTTPServer``；
* 存储是真实 ``LocalStorageBackend``，文件真的被移动/隔离；
* 数据源适配器只实现 ``DataSourceAdapter`` 协议形状（发现资产），不含下载逻辑。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import make_request
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.config.models import DefaultsConfig
from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionPlan,
    AcquisitionTask,
    Checksum,
    DatasetRequest,
    IntegrityCheckKind,
    Priority,
    SourceAsset,
    SourceMetadata,
    TemporalExtent,
)
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.planner.service import create_plan
from researchdatahub.scheduler import DefaultTaskScheduler

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
DATASET_ID = "era5-pressure-levels"


def sha256_checksum(data: bytes) -> str:
    """``sha256:<hex>`` 形式的校验和（与 ``core.models.Checksum`` 约定一致）。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def serve_bytes(
    server: TransferTestServer, path: str, data: bytes, **resource_kwargs: object
) -> Resource:
    """在真实测试服务器上注册一个资源；返回 Resource 以便注入故障。"""
    resource = Resource(body=data, **resource_kwargs)  # type: ignore[arg-type]
    server.add(path, resource)
    return resource


def make_https_asset(
    server: TransferTestServer,
    *,
    path: str,
    data: bytes,
    source_id: str = "ncar",
    file_name: str | None = None,
    checksum: bool = True,
    variables: Sequence[str] | None = None,
    asset_id: str | None = None,
    time_slice: TemporalExtent | None = None,
) -> SourceAsset:
    """构造一个指向真实本地 HTTP 服务器的资产。"""
    url = server.url(path)
    name = file_name or path.rsplit("/", 1)[-1]
    extensions: dict[str, object] = {}
    if variables is not None:
        extensions["test.variables"] = list(variables)
    metadata = SourceMetadata(
        source_id=source_id,
        display_name=f"{source_id} test source",
        provider=f"{source_id} test provider",
        access_route=AccessRoute.HTTPS,
        dataset_id=DATASET_ID,
        origin_uri=url,
        retrieved_at=NOW,
        extensions=extensions,  # type: ignore[arg-type]
    )
    return SourceAsset(
        asset_id=asset_id or f"asset-{source_id}-{name}".lower().replace(".", "-")[:63],
        source_id=source_id,
        dataset_id=DATASET_ID,
        access_route=AccessRoute.HTTPS,
        origin_uri=url,
        file_name=name,
        size_bytes=len(data),
        checksum=Checksum(value=sha256_checksum(data)) if checksum else None,
        metadata=metadata,
        discovered_at=NOW,
        time_slice=time_slice,
    )


class StaticSource:
    """符合 ``DataSourceAdapter`` 协议形状的测试数据源（返回预置资产）。"""

    def __init__(self, source_id: str, assets: Sequence[SourceAsset]) -> None:
        self._source_id = source_id
        self._assets = tuple(assets)
        self.calls: list[DatasetRequest] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return frozenset({"https"})

    def describe(self) -> SourceMetadata:
        return self._assets[0].metadata

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        self.calls.append(request)
        return self._assets

    def health_check(self) -> bool:
        return True


def new_request(
    *,
    request_id: str = "req-scheduler-pipeline",
    preferred_sources: Sequence[str] = ("ncar",),
    max_sources: int = 1,
    priority: Priority = Priority.NORMAL,
) -> DatasetRequest:
    """构造一个校验链为 size+checksum 的需求（避免 netcdf 依赖影响 PASSED 判定）。"""
    return make_request(
        request_id=request_id,
        preferred_sources=list(preferred_sources),
        max_sources=max_sources,
        priority=priority,
        checks=[IntegrityCheckKind.SIZE_MATCH, IntegrityCheckKind.CHECKSUM_MATCH],
    )


def create_and_submit(
    *,
    scheduler: DefaultTaskScheduler,
    repo: SqliteManifestRepository,
    request: DatasetRequest,
    assets: Sequence[SourceAsset],
    config: DefaultsConfig,
    source_id: str = "ncar",
) -> AcquisitionPlan:
    """真实链路：登记请求 → 计划 → 落库 → submit(QUEUED)。"""
    plan = create_plan(
        repo=repo,
        request=request,
        adapters=[StaticSource(source_id, assets)],
        config=config,
    )
    submitted = scheduler.submit(plan)
    assert submitted == len(plan.tasks)
    return plan


def reload_task(repo: SqliteManifestRepository, task_id: str) -> AcquisitionTask:
    """读取任务最新快照。"""
    return repo.load_task(task_id)


def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.01
) -> bool:
    """有界等待条件成立（测试用；失败返回 False 由断言给出清晰信息）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def utc_now() -> datetime:
    """真实当前时间（调度器在集成测试里用真实时钟跑传输）。"""
    return datetime.now(UTC)


def directory_files(path: Path) -> list[Path]:
    """递归列出普通文件（排除提交日志侧车）。"""
    if not path.exists():
        return []
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and not item.name.endswith(".commit.json")
    )


TASK_TERMINAL_REASON = "seeded by test"


def make_scheduler(
    *,
    config: DefaultsConfig,
    repo_factory: Callable[[], SqliteManifestRepository],
    storage: object,
    integrity: object,
    transports: Sequence[object],
    **overrides: object,
) -> DefaultTaskScheduler:
    """按测试配置装配调度器（与 CLI 共用 ``build_scheduler`` 路径）。"""
    from researchdatahub.scheduler import build_scheduler

    return build_scheduler(
        repo_factory=repo_factory,
        storage=storage,  # type: ignore[arg-type]
        integrity=integrity,  # type: ignore[arg-type]
        transports=transports,  # type: ignore[arg-type]
        config=config,
        **overrides,  # type: ignore[arg-type]
    )
