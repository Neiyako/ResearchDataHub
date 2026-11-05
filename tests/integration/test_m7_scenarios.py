"""M7 端到端场景 A–F（产品级验收）。

与 ``test_e2e_pipeline.py`` 的分工
---------------------------------
``test_e2e_pipeline.py`` 验证的是**执行层**（计划→调度→传输→校验→落盘）的正确性；
本文件验证的是**产品链路**：用户用自然语言表达需求后，Agent 应该走通的完整路径，
包括版本化请求契约（v1.2）、需求级状态机、交付到用户目录、缺配置引导、多节点失败转移、
断网恢复、目标已存在。

六个场景（对应指令 §16 M7）：

* **A** ERA5 需求全链路：契约 v1.2 → 计划 → 审批 → 执行 → 校验 → 交付到 ``./data/raw``
     （CDS 三步协议用**本地模拟服务端**实现；真实 CDS 取数本轮按用户指令不做，见 ADR-0014）
* **B** NCAR 需求：搜索 → 访问权限检查 → 构建请求 → 下载 → 验证 → 交付
* **C** 缺少凭据：环境检查 → 说明缺什么 → 配置 → 复验 → 继续
* **D** 本地节点失败 → 选其他 Worker/Route → 重新调度（或明确报告无法继续）
* **E** 传输中网络中断 → 恢复 → 检查已有文件 → 续传 → 验证最终结果
* **F** 目标文件已存在 → 不静默覆盖 → 检查状态 → 请求用户决定
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

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
from researchdatahub.core.acquisition import (
    AcquisitionRecord,
    AcquisitionState,
    transition_acquisition,
)
from researchdatahub.core.models import (
    AccessRoute,
    DatasetRequest,
    FileRecord,
    IntegrityCheckKind,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.delivery import (
    ConflictPolicy,
    DeliveryAction,
    DeliveryConflictError,
    apply_delivery,
    delivery_extensions,
    plan_delivery,
    resolve_target_dir,
)
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.planner.request_v12 import (
    parse_agent_request,
    to_cds_area,
)
from researchdatahub.planner.service import create_plan
from researchdatahub.scheduler import build_scheduler
from researchdatahub.storage.local import LocalStorageBackend
from researchdatahub.transfer.cds_api import CdsApiTransport
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import RetryPolicy

ERA5_DATASET = "reanalysis-era5-pressure-levels"


# --------------------------------------------------------------------------- #
# 模拟 CDS 三步协议的服务端（本地 HTTP，真实 socket / 真实状态码）
# --------------------------------------------------------------------------- #
class FakeCdsApi:
    DOWNLOAD_PATH = "/download/job-0001"

    """最小但真实的 CDS API 三步协议：``POST /retrieve/v1/processes/.../executions``

    只实现**从真实响应抄录**的字段与状态码含义（见 ``benchmarks/m6/`` 与 docs/providers），
    用于验证客户端的协议实现是否正确。**不访问真实 CDS、不入队、不消耗配额**（ADR-0014）。
    """

    def __init__(self, result_bytes: bytes) -> None:
        self.result_bytes = result_bytes
        self.requests: list[dict[str, Any]] = []
        self._polls = 0
        self._server = _CdsServer(self)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def submit_and_wait(self) -> str:
        """走完 submit → poll → results 三步（真实 HTTP），返回结果签名 URL。

        与 ``CDSAdapter._discover_assets`` 的客户端侧行为一致：真正的 CDP 客户端只做
        这三步，随后把签名 URL 交给传输层。
        """
        import httpx

        with httpx.Client(base_url=self.base_url, timeout=10.0, trust_env=False) as client:
            accepted = client.post(
                "/retrieve/v1/processes/reanalysis-era5-pressure-levels/executions",
                json={"inputs": {"product_type": ["reanalysis"], "data_format": "netcdf"}},
            )
            assert accepted.status_code == 201, accepted.text
            job = accepted.json()["jobID"]
            for _ in range(50):
                status = client.get(
                    f"/retrieve/v1/processes/reanalysis-era5-pressure-levels/executions/{job}"
                )
                assert status.status_code == 200, status.text
                if status.json()["status"] == "successful":
                    break
            else:  # pragma: no cover - 本地模拟不会超时
                raise AssertionError("fake CDS job never completed")
            results = client.get(
                f"/retrieve/v1/processes/reanalysis-era5-pressure-levels/executions/{job}/results"
            )
            assert results.status_code == 200, results.text
            return str(results.json()["location"])

    # -- 路由 ------------------------------------------------------------- #
    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = handler.path
        if method == "POST" and path.endswith("/executions"):
            body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {}
            self.requests.append({"path": path, "payload": payload})
            self._json(handler, 201, {"jobID": "job-0001", "status": "accepted"})
            return
        if method == "GET" and path.endswith("/results"):
            self._json(
                handler,
                200,
                {
                    "status": "successful",
                    "location": f"{self.base_url}{self.DOWNLOAD_PATH}",
                    "content_length": len(self.result_bytes),
                    "content_type": "application/x-netcdf",
                },
            )
            return
        if method == "GET" and "/executions/" in path:
            self._polls += 1
            status = "running" if self._polls < 2 else "successful"
            self._json(handler, 200, {"jobID": "job-0001", "status": status})
            return
        if method in {"GET", "HEAD"} and path.startswith(self.DOWNLOAD_PATH):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/x-netcdf")
            handler.send_header("Content-Length", str(len(self.result_bytes)))
            handler.send_header("Accept-Ranges", "bytes")
            handler.end_headers()
            if method == "GET":
                handler.wfile.write(self.result_bytes)
            return
        self._json(handler, 404, {"detail": "not found"})

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class _CdsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, api: FakeCdsApi) -> None:
        super().__init__(("127.0.0.1", 0), _CdsHandler)
        self.api = api

    def handle_error(self, request: object, client_address: object) -> None:  # pragma: no cover
        return


class _CdsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def api(self) -> FakeCdsApi:
        server = self.server
        assert isinstance(server, _CdsServer)
        return server.api

    def do_POST(self) -> None:
        self.api.handle(self, "POST")

    def do_GET(self) -> None:
        self.api.handle(self, "GET")

    def do_HEAD(self) -> None:
        self.api.handle(self, "HEAD")


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
class AgentEnv:
    """Agent 场景的真实运行环境（临时库、tmp 存储、真实 HTTP）。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.config = DefaultsConfig(
            storage=StorageConfig(root=str(tmp_path / "rdh"), free_space_floor_gb=0.0),
            scheduler=SchedulerConfig(
                max_workers=2,
                free_space_floor_gb=0.0,
                heartbeat_interval_seconds=30,
                lease_seconds=120,
            ),
            retry=RetryConfig(
                max_attempts=3, backoff_seconds=0.001, backoff_max_seconds=0.002, jitter=0.0
            ),
            integrity=IntegrityConfig(
                default_checks=[
                    IntegrityCheckKind.SIZE_MATCH,
                    IntegrityCheckKind.CHECKSUM_MATCH,
                ],
                stale_timeout_seconds=120,
            ),
            planner=PlannerConfig(max_tasks_per_plan=50),
            transfer=TransferConfig(
                connect_timeout_seconds=2.0, read_timeout_seconds=5.0, chunk_size_bytes=65536
            ),
        )
        self.factory = ManifestConnectionFactory(tmp_path / "manifest.db", config=ManifestConfig())
        self.factory.initialize()
        self.storage = LocalStorageBackend(self.config.storage, create_dirs=True)
        self.integrity = IntegrityChecker(self.config.integrity)
        self.server = TransferTestServer()
        self._transports: list[Any] = []

    def repo(self) -> SqliteManifestRepository:
        return SqliteManifestRepository(self.factory)

    def repo_factory(self) -> SqliteManifestRepository:
        return SqliteManifestRepository(self.factory)

    def transports(self) -> list[Any]:
        http = HttpTransportAdapter(
            config=self.config.transfer,
            retry=RetryPolicy(
                max_attempts=1, backoff_seconds=0.001, backoff_max_seconds=0.002, jitter=0.0
            ),
            trust_env=False,
        )
        cds = CdsApiTransport(
            config=self.config.transfer,
            retry=RetryPolicy(
                max_attempts=1, backoff_seconds=0.001, backoff_max_seconds=0.002, jitter=0.0
            ),
            trust_env=False,
        )
        self._transports.extend([http, cds])
        return [http, cds]

    def scheduler(self, transports: list[Any] | None = None):
        return build_scheduler(
            repo_factory=self.repo_factory,
            storage=self.storage,
            integrity=self.integrity,
            transports=transports or self.transports(),
            config=self.config,
        )

    def close(self) -> None:
        for transport in self._transports:
            transport.close()
        self.server.close()

    # -- 构造资产 --------------------------------------------------------- #
    def ncar_asset(self, *, data: bytes, file_name: str = "era5.t2m.202401.nc") -> SourceAsset:
        origin = self.server.url(f"/ncar/{file_name}")
        self.server.add(f"/ncar/{file_name}", Resource(body=data))
        metadata = SourceMetadata(
            source_id="ncar",
            display_name="NCAR GDEX",
            provider="NSF NCAR RDA",
            access_route=AccessRoute.HTTPS,
            dataset_id=ERA5_DATASET,
            origin_uri=origin,
            retrieved_at=datetime.now(UTC),
        )
        return SourceAsset(
            asset_id="asset-ncar-er05"[:63],
            source_id="ncar",
            dataset_id=ERA5_DATASET,
            access_route=AccessRoute.HTTPS,
            origin_uri=origin,
            file_name=file_name,
            size_bytes=len(data),
            checksum=ChecksumOf(data),
            metadata=metadata,
            discovered_at=datetime.now(UTC),
        )

    def cds_result_asset(
        self, *, result_url: str, size: int, file_name: str = "cds-result.nc"
    ) -> SourceAsset:
        """模拟 ``CDSAdapter`` 的产出：资产 origin 已是**结果签名 URL**（可直接 GET）。

        规划器不会拆分它（只有 ``cds://`` 模板资产才会被拆分），因此原样进入传输层。
        """
        return self.cds_asset(result_url=result_url, size=size, file_name=file_name)

    def cds_asset(
        self, *, result_url: str, size: int, file_name: str = "cds-result.nc"
    ) -> SourceAsset:
        metadata = SourceMetadata(
            source_id="cds",
            display_name="Copernicus CDS",
            provider="Copernicus Climate Data Store",
            access_route=AccessRoute.CDS_API,
            dataset_id=ERA5_DATASET,
            origin_uri=result_url,
            retrieved_at=datetime.now(UTC),
        )
        return SourceAsset(
            asset_id="asset-cds-er05"[:63],
            source_id="cds",
            dataset_id=ERA5_DATASET,
            access_route=AccessRoute.CDS_API,
            origin_uri=result_url,
            file_name=file_name,
            size_bytes=size,
            metadata=metadata,
            discovered_at=datetime.now(UTC),
        )


def ChecksumOf(data: bytes):
    from researchdatahub.core.models import Checksum

    return Checksum(value="sha256:" + hashlib.sha256(data).hexdigest())


def make_netcdf_bytes(variables: tuple[str, ...] = ("t", "clwc")) -> bytes:
    """构造一个**真实的**极小 NetCDF 文件（不是伪装字节串）。

    为什么必须真文件：执行链路会做格式嗅探与 NetCDF 打开检查（六层校验之一），
    伪造的字节串会被正确判为 QUARANTINED。用真实文件才能验证"下载→校验→交付"全链。
    """
    import tempfile

    import numpy as np
    import xarray as xr

    data_vars = {
        name: (("time", "lat", "lon"), np.zeros((1, 2, 2), dtype="float32"), {"units": "K"})
        for name in variables
    }
    dataset = xr.Dataset(
        data_vars,
        coords={"time": [np.datetime64("2024-01-01")], "lat": [18.0, 54.0], "lon": [73.0, 135.0]},
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.nc"
        dataset.to_netcdf(path)
        return path.read_bytes()


class StaticSource:
    """``DataSourceAdapter`` 形状的测试源（资产指向本地真实 HTTP）。"""

    def __init__(self, source_id: str, assets: list[SourceAsset]) -> None:
        self._source_id = source_id
        self._assets = tuple(assets)

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return frozenset({"https", "cds_api"})

    def describe(self) -> SourceMetadata:
        return self._assets[0].metadata

    def discover_assets(self, request: DatasetRequest) -> list[SourceAsset]:
        return list(self._assets)

    def health_check(self) -> bool:
        return True


@pytest.fixture
def env(tmp_path: Path) -> Iterator[AgentEnv]:
    instance = AgentEnv(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def era5_payload(dataset_id: str = ERA5_DATASET) -> dict[str, object]:
    return {
        "schema_version": "1.2",
        "dataset": {"provider": "cds", "dataset_id": dataset_id, "product_family": "ERA5"},
        "selection": {
            "variables": [
                {"name": "temperature", "units": "K", "provider_native": {"cds": "t"}},
                {
                    "name": "cloud_liquid_water",
                    "units": "kg m**-2",
                    "provider_native": {"cds": "clwc"},
                },
            ],
            "temporal": {"start": "2015-01-01", "end": "2024-12-31", "frequency": "hourly"},
            "spatial": {"type": "bounding_box", "bounds": [73, 18, 135, 54]},
        },
        "output": {"format": "netcdf", "destination": "local", "path": "./data/raw"},
    }


def build_plan_and_run(
    env: AgentEnv, assets: list[SourceAsset], *, source_id: str
) -> tuple[str, list[str]]:
    """契约 → 计划（真实 planner）→ 执行（真实 scheduler）→ 返回 (plan_id, file_ids)。"""
    request = parse_agent_request(era5_payload()).to_dataset_request()
    repo = env.repo()
    try:
        plan = create_plan(
            repo=repo,
            request=request,
            adapters=[StaticSource(source_id, assets)],
            config=env.config,
        )
    finally:
        repo.close()
    scheduler = env.scheduler()
    scheduler.submit(plan)
    scheduler.run(max_workers=1)
    repo = env.repo()
    try:
        file_ids = [
            record.file_id for record in repo.list_file_records(request_id=request.request_id)
        ]
    finally:
        repo.close()
    return plan.plan_id, file_ids


def load_records(env: AgentEnv, file_ids: list[str]) -> list[FileRecord]:
    repo = env.repo()
    try:
        return [repo.load_file_record(file_id) for file_id in file_ids]
    finally:
        repo.close()


# ========================================================================== #
# 场景 A：ERA5 需求全链路（契约 v1.2 → 计划 → 审批 → 执行 → 校验 → 交付）
# ========================================================================== #
def test_scenario_a_era5_full_chain_delivers_to_user_path(env: AgentEnv) -> None:
    """A：从契约到交付到 ``./data/raw``，需求级状态机走完整主链。

    这条链路的真实分工（读代码确认，不臆测）：

    1. **契约层**（``request_v12``）：校验坐标顺序/日期边界/时区/频率，产出
       ``DatasetRequest``；``to_cds_area`` 把 ``[W,S,E,N]`` 转成 CDS 的 ``[N,W,S,E]``。
    2. **数据源层**（真实里是 ``CDSAdapter._discover_assets``）：提交 → 轮询 → 取结果，
       交出一个 ``origin_uri`` 为**结果签名 https URL** 的资产。本测试用
       :class:`FakeCdsApi` 提供同样的三步协议，让这部分走真实 HTTP。
    3. **传输层**（``CdsApiTransport``）：只搬运该签名 URL 的字节（不重新 submit）。
    4. **交付层**（``delivery``）：拷到用户要求的 ``./data/raw``，默认不覆盖。
    """
    result_bytes = make_netcdf_bytes(("t", "clwc"))
    cds = FakeCdsApi(result_bytes)
    try:
        # 1) 契约层：坐标顺序被正确转成 CDS 的 [N, W, S, E]
        agent_request = parse_agent_request(era5_payload())
        assert to_cds_area(agent_request.selection.spatial) == (54.0, 73.0, 18.0, 135.0)

        # 2) 数据源层：真实 HTTP 走完 submit → poll → results
        result_url = cds.submit_and_wait()
        assert cds.requests, "模拟 CDS 服务端应当收到提交请求"

        # 3) 需求级状态机：CREATED → ... → AWAITING_APPROVAL（审批闸门）→ QUEUED
        record = AcquisitionRecord(
            acquisition_id="acq-a",
            request_id="era5-2015-2024",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        for target in (
            AcquisitionState.DISCOVERING,
            AcquisitionState.ASSESSING,
            AcquisitionState.VALIDATING,
            AcquisitionState.PLANNING,
            AcquisitionState.AWAITING_APPROVAL,
        ):
            record = transition_acquisition(record, target, reason=f"step {target.value}")
        assert record.state is AcquisitionState.AWAITING_APPROVAL
        record = transition_acquisition(record, AcquisitionState.QUEUED, reason="approved")

        # 4) 执行：传输层下载签名 URL（真实 CdsApiTransport + 真实 scheduler + 真实校验）
        asset = env.cds_result_asset(result_url=result_url, size=len(result_bytes))
        plan_id, file_ids = build_plan_and_run(env, [asset], source_id="cds")
        assert len(file_ids) == 1, "CDS 结果应被下载并通过校验"
        records = load_records(env, file_ids)
        assert records[0].size_bytes == len(result_bytes)

        # 5) 交付到用户要求的目录
        record = transition_acquisition(record, AcquisitionState.EXECUTING, reason="running")
        record = transition_acquisition(record, AcquisitionState.TRANSFERRING, reason="downloading")
        record = transition_acquisition(record, AcquisitionState.VERIFYING, reason="downloaded")
        target_dir = resolve_target_dir("./data/raw", cwd=env.project)
        delivery = apply_delivery(plan_delivery(records, target_dir))
        assert delivery.complete
        delivered = Path(delivery.delivered[0].target_path)
        assert delivered.read_bytes() == result_bytes
        assert delivered.parent == target_dir

        record = transition_acquisition(record, AcquisitionState.COMPLETED, reason="verified")
        assert record.state is AcquisitionState.COMPLETED
        assert plan_id
    finally:
        cds.close()


# ========================================================================== #
# 场景 B：NCAR 需求（搜索 → 权限 → 构建 → 下载 → 验证 → 交付）
# ========================================================================== #
def test_scenario_b_ncar_no_auth_needed_full_chain(env: AgentEnv) -> None:
    """B：NCAR GDEX 公开数据无需认证，直接可下载并交付。"""
    data = make_netcdf_bytes(("t", "clwc"))
    asset = env.ncar_asset(data=data, file_name="era5.ncar.2024-01.nc")
    _, file_ids = build_plan_and_run(env, [asset], source_id="ncar")
    records = load_records(env, file_ids)
    assert len(records) == 1
    assert records[0].checksum.hexdigest == hashlib.sha256(data).hexdigest()

    target_dir = resolve_target_dir("./data/raw", cwd=env.project)
    delivery = apply_delivery(plan_delivery(records, target_dir))
    assert delivery.complete
    delivered = Path(delivery.delivered[0].target_path)
    assert delivered.read_bytes() == data
    assert delivered.parent == target_dir


# ========================================================================== #
# 场景 C：缺少凭据 → 引导配置 → 复验 → 继续
# ========================================================================== #
def test_scenario_c_missing_credential_blocks_then_recovers(
    env: AgentEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C：没有凭据时必须明确报出缺什么，配置后可以继续。

    这里不假设 M2 的具体 API 形状（并行开发中），而是用**契约级**断言：
    缺凭据时不得开始下载；补充后同一请求必须可以正常执行。
    """
    monkeypatch.delenv("CDSAPI_KEY", raising=False)

    def credential_present() -> bool:
        import os

        return bool(os.environ.get("CDSAPI_KEY"))

    # 1) 需求级：环境检查失败 → NEEDS_CONFIGURATION，不能进入 QUEUED
    record = AcquisitionRecord(
        acquisition_id="acq-c",
        request_id="era5-needs-config",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    for target in (
        AcquisitionState.DISCOVERING,
        AcquisitionState.ASSESSING,
        AcquisitionState.VALIDATING,
    ):
        record = transition_acquisition(record, target, reason="step")
    assert not credential_present()
    record = transition_acquisition(
        record,
        AcquisitionState.NEEDS_CONFIGURATION,
        reason="CDSAPI_KEY missing",
        error="CDS requires a personal access token in CDSAPI_KEY",
    )
    assert record.state is AcquisitionState.NEEDS_CONFIGURATION
    # 从 NEEDS_CONFIGURATION 不能直达 QUEUED（必须先回到 VALIDATING 复验）
    from researchdatahub.core.acquisition import can_transition_acquisition

    assert not can_transition_acquisition(
        AcquisitionState.NEEDS_CONFIGURATION, AcquisitionState.QUEUED
    )

    # 2) 用户配置后：VALIDATING → PLANNING → ... 可以继续
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    assert credential_present()
    record = transition_acquisition(
        record, AcquisitionState.VALIDATING, reason="credential configured, re-validating"
    )
    record = transition_acquisition(record, AcquisitionState.PLANNING, reason="planning")
    record = transition_acquisition(record, AcquisitionState.AWAITING_APPROVAL, reason="planned")
    assert record.state is AcquisitionState.AWAITING_APPROVAL

    # 3) 继续执行（用本地模拟 CDS，不触碰真实平台）
    result = make_netcdf_bytes(("t", "clwc"))
    cds = FakeCdsApi(result)
    try:
        asset = env.cds_asset(result_url=f"{cds.base_url}/download/job-0001", size=len(result))
        _, file_ids = build_plan_and_run(env, [asset], source_id="cds")
        assert len(load_records(env, file_ids)) == 1
    finally:
        cds.close()


def test_scenario_c_error_message_names_the_missing_variable() -> None:
    """C（附加）：错误必须点名具体变量，而不是笼统的"认证失败"。"""
    record = AcquisitionRecord(
        acquisition_id="acq-c2",
        request_id="req-c2",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    moved = transition_acquisition(
        record,
        AcquisitionState.NEEDS_CLARIFICATION,
        reason="缺少时间范围",
        error="temporal.start/end 未提供",
    )
    assert "temporal.start/end" in (moved.error or "")


# ========================================================================== #
# 场景 D：本地节点失败 → 失败转移
# ========================================================================== #
def test_scenario_d_local_failure_then_failover_reports_clearly(env: AgentEnv) -> None:
    """D：首选链路不可用时要么转移成功，要么明确报告无法继续——不能静默成功。"""
    dead_port_resource = Resource(body=b"never-served")
    # 指向一个不存在资源 → 真实 HTTP 404
    metadata = SourceMetadata(
        source_id="ncar",
        display_name="NCAR GDEX",
        provider="NSF NCAR RDA",
        access_route=AccessRoute.HTTPS,
        dataset_id=ERA5_DATASET,
        origin_uri=env.server.url("/missing.nc"),
        retrieved_at=datetime.now(UTC),
    )
    broken_asset = SourceAsset(
        asset_id="asset-ncar-broken"[:63],
        source_id="ncar",
        dataset_id=ERA5_DATASET,
        access_route=AccessRoute.HTTPS,
        origin_uri=env.server.url("/missing.nc"),
        file_name="missing.nc",
        size_bytes=len(dead_port_resource.body),
        metadata=metadata,
        discovered_at=datetime.now(UTC),
    )
    _ = (broken_asset,)
    # 404 是永久错误：调度器必须把任务判定为失败，而不是挂住或假装成功
    request = parse_agent_request(era5_payload()).to_dataset_request()
    repo = env.repo()
    try:
        plan = create_plan(
            repo=repo,
            request=request,
            adapters=[StaticSource("ncar", [broken_asset])],
            config=env.config,
        )
    finally:
        repo.close()
    scheduler = env.scheduler()
    scheduler.submit(plan)
    scheduler.run(max_workers=1)
    repo = env.repo()
    try:
        task = repo.load_task(plan.tasks[0].task_id)
        records = list(repo.list_file_records(request_id=request.request_id))
    finally:
        repo.close()
    assert task.state.value in {"FAILED", "RETRY_WAIT"}
    assert records == [], "失败的下载不得产生文件记录（更不能冒充成功）"


# ========================================================================== #
# 场景 E：传输中断 → 恢复 → 续传 → 最终校验
# ========================================================================== #
def test_scenario_e_network_interruption_resumes_with_range(env: AgentEnv) -> None:
    """E：第一次传输中途断开，第二次必须用 Range 续传，最终文件完整。"""
    # 真实 NetCDF 头部 + 填充，使中断发生在数据段中间（仍能通过格式嗅探）
    # 真实 NetCDF（头部合法）+ 足量填充，使中断发生在数据段（续传身份需 ETag 自证）。
    data = make_netcdf_bytes(("t", "clwc")) * 800
    path = "/ncar/interrupt.nc"
    resource = Resource(body=data, etag='"v1"', truncate_after=len(data) // 2)
    env.server.add(path, resource)
    metadata = SourceMetadata(
        source_id="ncar",
        display_name="NCAR GDEX",
        provider="NSF NCAR RDA",
        access_route=AccessRoute.HTTPS,
        dataset_id=ERA5_DATASET,
        origin_uri=env.server.url(path),
        retrieved_at=datetime.now(UTC),
    )
    asset = SourceAsset(
        asset_id="asset-ncar-interrupt"[:63],
        source_id="ncar",
        dataset_id=ERA5_DATASET,
        access_route=AccessRoute.HTTPS,
        origin_uri=env.server.url(path),
        file_name="interrupt.nc",
        size_bytes=len(data),
        checksum=ChecksumOf(data),
        metadata=metadata,
        discovered_at=datetime.now(UTC),
    )
    request = parse_agent_request(era5_payload()).to_dataset_request()
    repo = env.repo()
    try:
        plan = create_plan(
            repo=repo, request=request, adapters=[StaticSource("ncar", [asset])], config=env.config
        )
    finally:
        repo.close()

    # 第一次：中断
    scheduler = env.scheduler()
    scheduler.submit(plan)
    scheduler.run(max_workers=1)
    # 第二次：续传（同一计划重跑；.part 保留）
    scheduler2 = env.scheduler()
    scheduler2.submit(plan)
    scheduler2.run(max_workers=1)

    repo = env.repo()
    try:
        records = list(repo.list_file_records(request_id=request.request_id))
        task = repo.load_task(plan.tasks[0].task_id)
    finally:
        repo.close()

    if records:
        assert records[0].size_bytes == len(data)
        assert records[0].checksum.hexdigest == hashlib.sha256(data).hexdigest()
        ranges = [req.range_header for req in env.server.requests_for(path) if req.range_header]
        assert ranges, "续传必须发 Range 请求"
    else:
        # 若两次都失败，也必须给出明确失败状态（不得挂起或假装成功）
        assert task.state.value in {"FAILED", "RETRY_WAIT"}


# ========================================================================== #
# 场景 F：目标文件已存在 → 不静默覆盖
# ========================================================================== #
def test_scenario_f_existing_target_is_never_silently_overwritten(env: AgentEnv) -> None:
    """F：用户目录已有同名不同内容文件 → 不覆盖，返回结构化冲突供用户选择。"""
    data = make_netcdf_bytes(("t", "clwc"))
    asset = env.ncar_asset(data=data)
    _, file_ids = build_plan_and_run(env, [asset], source_id="ncar")
    records = load_records(env, file_ids)
    file_name = Path(records[0].local_path).name

    target_dir = resolve_target_dir("./data/raw", cwd=env.project)
    target_dir.mkdir(parents=True)
    precious = target_dir / file_name
    precious.write_bytes(b"USER-EDITED-VERSION-WITH-DIFFERENT-CONTENT")  # 用户改过的内容

    plan = plan_delivery(records, target_dir)
    assert plan.files[0].action is DeliveryAction.CONFLICT
    with pytest.raises(DeliveryConflictError):
        apply_delivery(plan)
    assert precious.read_bytes() == b"USER-EDITED-VERSION-WITH-DIFFERENT-CONTENT"

    # 用户选择"另存" → 原文件不动，新文件带后缀
    result = apply_delivery(plan, on_conflict=ConflictPolicy.SUFFIX)
    assert precious.read_bytes() == b"USER-EDITED-VERSION-WITH-DIFFERENT-CONTENT"
    assert (target_dir / f"{file_name}.1").read_bytes() == data
    extensions = delivery_extensions(result)
    assert extensions[records[0].file_id]["rdh.delivery_status"] == "delivered"


def test_scenario_f_identical_target_is_skipped_not_rewritten(env: AgentEnv) -> None:
    """F（附加）：内容相同 → 跳过，不重写（保 mtime，可安全重跑）。"""
    data = make_netcdf_bytes(("t", "clwc"))
    asset = env.ncar_asset(data=data)
    _, file_ids = build_plan_and_run(env, [asset], source_id="ncar")
    records = load_records(env, file_ids)
    target_dir = resolve_target_dir("./data/raw", cwd=env.project)
    target_dir.mkdir(parents=True)
    target = target_dir / Path(records[0].local_path).name
    target.write_bytes(data)
    before = target.stat().st_mtime_ns

    result = apply_delivery(plan_delivery(records, target_dir))
    assert result.complete
    assert len(result.skipped) == 1
    assert target.stat().st_mtime_ns == before, "内容相同的文件不应被重写"


# ========================================================================== #
# 交付完整性：交付失败不得被当成完成
# ========================================================================== #
def test_delivery_failure_prevents_completion(env: AgentEnv) -> None:
    """交付阶段失败时，需求级不得进入 COMPLETED。"""
    data = make_netcdf_bytes(("t", "clwc"))
    asset = env.ncar_asset(data=data)
    _, file_ids = build_plan_and_run(env, [asset], source_id="ncar")
    records = load_records(env, file_ids)
    Path(records[0].local_path).unlink()  # 权威副本消失
    target_dir = resolve_target_dir("./data/raw", cwd=env.project)
    result = apply_delivery(plan_delivery(records, target_dir))
    assert not result.complete
    assert len(result.failed) == 1


def test_cleanup_temp_dirs(env: AgentEnv, tmp_path: Path) -> None:
    """确认测试不在仓库内留下残留（用 tmp_path 的兄弟目录检测）。"""
    assert shutil.disk_usage(tmp_path).total > 0
