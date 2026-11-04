"""端到端集成测试：真实 SQLite + 真实本地 HTTP 服务器 + tmp 存储跑完整采集链路。

链路：需求 YAML → 计划（PLANNED）→ 入队（QUEUED）→ 原子领取 → 真实 HTTP 传输 →
完整性校验 → 提交 raw → 文件记录 → 交付清单（DeliveryManifest + JSON 导出）。

故障注入（真实发生，不是 mock）：
1. 传输中断后续传：服务端只发一部分 body 就断连，第二次运行必须用 ``Range`` 续传；
2. 进程重启恢复：任务被"已崩溃 worker"领取（心跳过期），启动恢复后重新排队并完成；
3. 校验失败隔离：远端声明的校验和与落盘字节不符 → 隔离 + ``QUARANTINED``，绝不交付；
4. 目标已存在跳过：正式区已有内容相同的文件 → ``SKIPPED_IDENTICAL``，不覆盖、不重复记录。

依赖全部本地：``127.0.0.1`` 随机端口的 ``ThreadingHTTPServer``，不访问外网。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

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
from researchdatahub.core.errors import ManifestError
from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionPlan,
    Checksum,
    DatasetRequest,
    IntegrityCheckKind,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.core.state import TaskState, transition
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.planner.request import load_request_detailed
from researchdatahub.planner.service import create_plan, plan_overview
from researchdatahub.reporting import write_delivery_manifest
from researchdatahub.scheduler import DEFAULT_WORKER_LIMIT, build_scheduler
from researchdatahub.storage.local import LocalStorageBackend
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import RetryPolicy

DATASET_ID = "reanalysis-era5-pressure-levels"

REQUEST_YAML = """
request:
  name: era5_cloud_water_2011
  priority: high
  preferred_sources: [cds, ncar]
  max_sources: 1
dataset:
  provider: ncar
  name: reanalysis-era5-pressure-levels
  product: reanalysis
  product_family: ERA5
  version: e5.1
variables:
  - {name: clwc, units: "kg kg**-1"}
  - {name: t, units: K}
temporal:
  start: "2011-01-01"
  end: "2011-12-31"
  frequency: daily
spatial:
  type: global
vertical:
  levels: [1000, 925, 850]
storage:
  preserve_original: true
validation:
  verify_size: true
  calculate_sha256: true
"""


class StaticSource:
    """``DataSourceAdapter`` 协议形状的测试数据源（资产指向本地真实 HTTP 服务器）。"""

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


class E2EEnv:
    """一套真实运行环境：临时库、tmp 存储、真实 HTTP 服务器、真实传输/校验实现。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.config = DefaultsConfig(
            storage=StorageConfig(root=str(tmp_path / "rdh"), free_space_floor_gb=0.0),
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
                default_checks=[
                    IntegrityCheckKind.SIZE_MATCH,
                    IntegrityCheckKind.CHECKSUM_MATCH,
                ],
                stale_timeout_seconds=60,
            ),
            planner=PlannerConfig(max_tasks_per_plan=50),
            transfer=TransferConfig(
                connect_timeout_seconds=2.0,
                read_timeout_seconds=5.0,
                chunk_size_bytes=65536,
            ),
        )
        self.factory = ManifestConnectionFactory(tmp_path / "manifest.db", config=ManifestConfig())
        self.factory.initialize()
        self.storage = LocalStorageBackend(self.config.storage, create_dirs=True)
        self.integrity = IntegrityChecker(self.config.integrity)
        self.server = TransferTestServer()
        self.transport = HttpTransportAdapter(
            config=self.config.transfer,
            retry=RetryPolicy(
                max_attempts=1,
                backoff_seconds=0.001,
                backoff_max_seconds=0.002,
                jitter=0.0,
            ),
            trust_env=False,
        )

    def repo(self) -> SqliteManifestRepository:
        """独立连接（模拟 CLI 的每次命令一条连接）。"""
        return SqliteManifestRepository(self.factory)

    def repo_factory(self) -> SqliteManifestRepository:
        return SqliteManifestRepository(self.factory)

    def scheduler(self):
        return build_scheduler(
            repo_factory=self.repo_factory,
            storage=self.storage,
            integrity=self.integrity,
            transports=[self.transport],
            config=self.config,
        )

    def write_request(self, text: str = REQUEST_YAML, name: str = "request.yaml") -> Path:
        path = self.tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def serve_file(self, path: str, data: bytes, **kwargs: object) -> Resource:
        resource = Resource(body=data, **kwargs)  # type: ignore[arg-type]
        self.server.add(path, resource)
        return resource

    def make_asset(
        self,
        *,
        http_path: str,
        data: bytes,
        file_name: str,
        checksum: bool = True,
    ) -> SourceAsset:
        origin = self.server.url(http_path)
        metadata = SourceMetadata(
            source_id="ncar",
            display_name="NCAR test source",
            provider="NCAR test provider",
            access_route=AccessRoute.HTTPS,
            dataset_id=DATASET_ID,
            origin_uri=origin,
            retrieved_at=datetime.now(UTC),
        )
        return SourceAsset(
            asset_id=f"asset-ncar-{file_name}".lower().replace(".", "-")[:63],
            source_id="ncar",
            dataset_id=DATASET_ID,
            access_route=AccessRoute.HTTPS,
            origin_uri=origin,
            file_name=file_name,
            size_bytes=len(data),
            checksum=(
                Checksum(value="sha256:" + hashlib.sha256(data).hexdigest()) if checksum else None
            ),
            metadata=metadata,
            discovered_at=datetime.now(UTC),
        )

    def close(self) -> None:
        self.transport.close()
        self.server.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[E2EEnv]:
    instance = E2EEnv(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def _plan_request(env: E2EEnv, assets: Sequence[SourceAsset]) -> tuple[AcquisitionPlan, Path]:
    """需求 YAML → 计划（真实 loader + 真实落库）。"""
    request_path = env.write_request()
    loaded = load_request_detailed(str(request_path))
    assert loaded.warnings == ()
    repo = env.repo()
    try:
        plan = create_plan(
            repo=repo,
            request=loaded.request,
            adapters=[StaticSource("ncar", assets)],
            config=env.config,
        )
    finally:
        repo.close()
    return plan, request_path


def _raw_bytes(env: E2EEnv, file_name: str) -> bytes:
    return (env.storage.raw_dir / DATASET_ID / file_name).read_bytes()


def test_full_pipeline_request_to_delivery_manifest(env: E2EEnv) -> None:
    data = b"netcdf-fake-payload" * 5000
    env.serve_file("/ncar/e5.1-2011.nc", data)
    asset = env.make_asset(http_path="/ncar/e5.1-2011.nc", data=data, file_name="e5.1-2011.nc")
    plan, request_path = _plan_request(env, [asset])

    assert plan.tasks[0].state is TaskState.PLANNED
    assert plan.unmatched_variables == []
    overview = plan_overview(plan)
    assert overview["task_count"] == 1
    assert overview["request_id"] == "era5_cloud_water_2011"

    scheduler = env.scheduler()
    submitted = scheduler.submit(plan)
    assert submitted == 1
    summary = scheduler.run()

    assert summary.counts == {"completed": 1}, summary.as_dict()
    assert summary.workers == DEFAULT_WORKER_LIMIT == 2
    repo = env.repo()
    try:
        task = repo.load_task(plan.task_ids()[0])
        assert task.state is TaskState.COMPLETED
        assert task.priority.value == "high"
        assert task.max_attempts == 3
        records = list(repo.list_file_records(task_id=task.task_id))
        assert len(records) == 1
        raw_path = Path(records[0].local_path)
        assert raw_path == env.storage.raw_dir / DATASET_ID / "e5.1-2011.nc"
        assert raw_path.read_bytes() == data
        assert raw_path.parent == env.storage.raw_dir / DATASET_ID

        delivery = repo.build_delivery_manifest("era5_cloud_water_2011")
        assert delivery.complete is True
        assert delivery.total_bytes == len(data)
        assert delivery.plan_id == plan.plan_id
        assert delivery.dataset_metadata.temporal_coverage.start.year == 2011

        manifest_path = write_delivery_manifest(
            delivery,
            env.tmp_path / "delivery.json",
            relative_to=env.storage.root,
        )
    finally:
        repo.close()

    exported = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert exported["request_id"] == "era5_cloud_water_2011"
    # 相对路径基于 storage.root，因此包含 raw 区前缀；跨机器可移植。
    assert exported["files"][0]["path"] == f"raw/{DATASET_ID}/e5.1-2011.nc"
    assert exported["complete"] is True
    assert request_path.exists()


def test_fault_injection_transfer_interruption_then_resume(env: E2EEnv) -> None:
    data = bytes(range(256)) * 1563  # 400128 字节
    env.serve_file("/ncar/interrupted.nc", data, etag='"v1"', truncate_after=200_000)
    asset = env.make_asset(http_path="/ncar/interrupted.nc", data=data, file_name="interrupted.nc")
    plan, _ = _plan_request(env, [asset])
    scheduler = env.scheduler()
    assert scheduler.submit(plan) == 1
    task_id = plan.task_ids()[0]

    first = scheduler.run()
    assert first.counts == {"retry_wait": 1}, first.as_dict()
    second = scheduler.run()

    assert second.counts == {"completed": 1}, second.as_dict()
    assert second.records[0].resumed is True
    requests = env.server.requests_for("/ncar/interrupted.nc")
    assert any(item.range_header for item in requests), requests
    assert _raw_bytes(env, "interrupted.nc") == data
    repo = env.repo()
    try:
        assert repo.load_task(task_id).state is TaskState.COMPLETED
        event_types = [event.event_type for event in repo.list_events(task_id)]
        assert "retry_scheduled" in event_types
    finally:
        repo.close()


def test_fault_injection_process_restart_recovery(env: E2EEnv) -> None:
    data = b"restart-payload" * 700
    env.serve_file("/ncar/restart.nc", data)
    asset = env.make_asset(http_path="/ncar/restart.nc", data=data, file_name="restart.nc")
    plan, _ = _plan_request(env, [asset])
    task_id = plan.task_ids()[0]
    repo = env.repo()
    try:
        planned = repo.load_task(task_id)
        queued = transition(planned, TaskState.QUEUED, reason="e2e enqueue", now=datetime.now(UTC))
        repo.save_task(queued, expected_state=TaskState.PLANNED)
        # 模拟"worker 领取后进程崩溃"：两分钟前领取，租约 1 秒，心跳已过期。
        claimed = repo.claim_tasks(
            owner="crashed-worker",
            limit=1,
            now=datetime.now(UTC) - timedelta(seconds=120),
            lease_seconds=1,
        )
        assert [task.task_id for task in claimed] == [task_id]
    finally:
        repo.close()

    scheduler = env.scheduler()
    summary = scheduler.run()

    assert any(result.new_state == "RETRY_WAIT" for result in summary.recovered), summary.as_dict()
    assert summary.counts == {"completed": 1}, summary.as_dict()
    repo = env.repo()
    try:
        task = repo.load_task(task_id)
        assert task.state is TaskState.COMPLETED
        assert task.attempt == 2
        assert "lease_recovered" in [item.event_type for item in repo.list_events(task_id)]
    finally:
        repo.close()
    assert _raw_bytes(env, "restart.nc") == data


def test_fault_injection_integrity_failure_is_quarantined(env: E2EEnv) -> None:
    data = b"bad-remote-checksum" * 500
    env.serve_file("/ncar/bad.nc", data)
    asset = env.make_asset(http_path="/ncar/bad.nc", data=data, file_name="bad.nc")
    wrong = asset.model_copy(update={"checksum": Checksum(value="sha256:" + "f" * 64)})
    plan, _ = _plan_request(env, [wrong])
    scheduler = env.scheduler()
    assert scheduler.submit(plan) == 1

    summary = scheduler.run()

    assert summary.counts == {"quarantined": 1}, summary.as_dict()
    repo = env.repo()
    try:
        task = repo.load_task(plan.task_ids()[0])
        assert task.state is TaskState.QUARANTINED
        assert list(repo.list_file_records(task_id=task.task_id)) == []
        # 没有文件记录或有非 PASSED 报告时，交付清单必须拒绝生成。
        with pytest.raises(ManifestError, match=r"no file records|non-PASSED"):
            repo.build_delivery_manifest("era5_cloud_water_2011")
    finally:
        repo.close()
    assert not (env.storage.raw_dir / DATASET_ID / "bad.nc").exists()
    quarantined = [
        path
        for path in env.storage.quarantine_dir.rglob("*")
        if path.is_file() and path.name.endswith("bad.nc")
    ]
    assert quarantined  # 证据保留


def test_fault_injection_identical_target_is_skipped(env: E2EEnv) -> None:
    data = b"already-in-raw" * 300
    env.serve_file("/ncar/dup.nc", data)
    asset = env.make_asset(http_path="/ncar/dup.nc", data=data, file_name="dup.nc")
    target_dir = env.storage.raw_dir / DATASET_ID
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "dup.nc"
    target.write_bytes(data)
    plan, _ = _plan_request(env, [asset])
    scheduler = env.scheduler()
    assert scheduler.submit(plan) == 1

    summary = scheduler.run()

    assert summary.counts == {"skipped_identical": 1}, summary.as_dict()
    repo = env.repo()
    try:
        assert repo.load_task(plan.task_ids()[0]).state is TaskState.COMPLETED
        assert len(list(repo.list_file_records(task_id=plan.task_ids()[0]))) == 1
    finally:
        repo.close()
    assert target.read_bytes() == data
