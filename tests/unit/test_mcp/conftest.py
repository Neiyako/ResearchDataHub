"""MCP 单元测试的共享装配：临时状态根 + 临时配置 + 本地 HTTP 文件源适配器。

为什么自己造一套 ``McpContext``
-------------------------------
集成测试要真实跑"规划 → 审批 → 入队 → 传输 → 校验"，但不能用 cdsapi（用户明确要求），
也不能依赖仓库里 ``configs/sources.yaml`` 的取值（它需要真实凭证环境变量）。
因此这里用 ``tmp_path`` 构造：

* 一份真实的 SQLite Manifest 库（``rdh db init`` 等价的 ``ManifestConnectionFactory``）；
* 一份真实的 ``configs/defaults.yaml`` 拷贝（存储根改到 tmp）；
* 一个 :class:`StaticSource`（``DataSourceAdapter`` 形状），资产指向本地
  ``ThreadingHTTPServer``（复用 ``tests/unit/test_transfer/http_server.py``）。

这些都只是**装配**替换：规划、状态机、传输、校验仍然走真实的 Core/业务实现。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.config.loader import load_defaults
from researchdatahub.core.models import (
    AccessRoute,
    DatasetRequest,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.interfaces.mcp.auth import ApprovalKey, ApprovalStore, PlanApprovalAuthorizer
from researchdatahub.interfaces.mcp.context import McpContext, OptionalServices, PackageProbe
from researchdatahub.manifest.cli_service import DbTarget
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.sources.registry import SourceRegistry
from researchdatahub.storage.local import LocalStorageBackend

DATASET_ID = "reanalysis-era5-pressure-levels"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_TEMPLATE = REPO_ROOT / "configs" / "defaults.yaml"

REQUEST_DOCUMENT: dict[str, object] = {
    "request": {"name": "era5_mcp_test", "priority": "normal", "max_sources": 1},
    "dataset": {
        "provider": "ncar",
        "name": DATASET_ID,
        "product": "reanalysis",
        "product_family": "ERA5",
        "version": "e5.1",
    },
    "variables": [
        {"name": "t", "units": "K"},
        {"name": "clwc", "units": "kg kg**-1"},
    ],
    "temporal": {"start": "2020-01-01", "end": "2020-02-01", "frequency": "daily"},
    "spatial": {"type": "global"},
    "vertical": {"levels": [1000, 925], "kind": "pressure"},
}
"""一段合法的分段格式需求（与 ``tests/integration/test_e2e_pipeline.py`` 同构）。"""


class StaticSource:
    """``DataSourceAdapter`` 协议形状的测试源；资产由调用方显式给出（不伪造远端事实）。"""

    def __init__(self, source_id: str, assets: Sequence[SourceAsset]) -> None:
        self._source_id = source_id
        self._assets = tuple(assets)
        self.discover_calls: list[DatasetRequest] = []
        self.closed = False

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return frozenset({AccessRoute.HTTPS.value})

    def describe(self) -> SourceMetadata:
        return self._assets[0].metadata

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        self.discover_calls.append(request)
        return self._assets

    def health_check(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def capabilities(self) -> object:
        from researchdatahub.sources.base import AdapterCapabilities

        return AdapterCapabilities(
            adapter_id="static",
            source_id=self._source_id,
            available=True,
            routes=(AccessRoute.HTTPS.value,),
            verified=("local HTTP fixture",),
        )


class Harness:
    """一套 tmp 状态根 + 真实库 + 本地 HTTP 源。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.storage_root = root / "state"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.defaults_path = self._write_defaults()
        self.app_config = load_defaults(self.defaults_path)
        storage_config = self.app_config.defaults.storage.model_copy(
            update={"root": str(self.storage_root), "free_space_floor_gb": 0.0}
        )
        self.app_config = self.app_config.model_copy(
            update={
                "defaults": self.app_config.defaults.model_copy(update={"storage": storage_config})
            }
        )
        self.db_target = DbTarget(
            db_path=self.storage_root / "manifest.db", config=self.app_config.defaults.manifest
        )
        self.factory = ManifestConnectionFactory(
            self.db_target.db_path, config=self.db_target.config
        )
        self.factory.initialize()
        # 与 build_context 的生产路径一致：启动时建 mcp_approvals 表
        # （MCP 接口层自建，不占用 Manifest 迁移编号）。
        connection = self.factory.connect()
        try:
            ApprovalStore(connection).ensure_schema()
        finally:
            connection.close()
        self.storage = LocalStorageBackend(self.app_config.defaults.storage, create_dirs=True)
        self.integrity = IntegrityChecker(self.app_config.defaults.integrity)
        self.key_path = self.storage_root / "approval.key"
        self.http_server = TransferTestServer()
        self.sources: list[StaticSource] = []

    def _write_defaults(self) -> Path:
        text = DEFAULTS_TEMPLATE.read_text(encoding="utf-8")
        target = self.root / "defaults.yaml"
        target.write_text(text, encoding="utf-8")
        return target

    # -- 资产与源 ---------------------------------------------------------- #
    def serve(self, path: str, body: bytes) -> str:
        self.http_server.add(path, Resource(body=body))
        return self.http_server.url(path)

    def make_asset(
        self, *, http_path: str, body: bytes, file_name: str, source_id: str = "ncar"
    ) -> SourceAsset:
        origin = self.serve(http_path, body)
        metadata = SourceMetadata(
            source_id=source_id,
            display_name="local HTTP fixture",
            provider="local fixture",
            access_route=AccessRoute.HTTPS,
            dataset_id=DATASET_ID,
            origin_uri=origin,
            retrieved_at=datetime.now(UTC),
        )
        return SourceAsset(
            asset_id=f"asset-{file_name}".replace(".", "-").lower()[:63],
            source_id=source_id,
            dataset_id=DATASET_ID,
            access_route=AccessRoute.HTTPS,
            origin_uri=origin,
            file_name=file_name,
            size_bytes=len(body),
            checksum={"value": "sha256:" + hashlib.sha256(body).hexdigest()},
            metadata=metadata,
            discovered_at=datetime.now(UTC),
        )

    def add_source(self, assets: Sequence[SourceAsset], source_id: str = "ncar") -> StaticSource:
        source = StaticSource(source_id, assets)
        self.sources.append(source)
        return source

    # -- 上下文 ------------------------------------------------------------ #
    def context(
        self,
        *,
        optional: OptionalServices | None = None,
        adapters: Sequence[object] | None = None,
        key: ApprovalKey | None = None,
    ) -> McpContext:
        return McpContext(
            app_config=self.app_config,
            sources_config=None,
            storage_root=self.storage_root,
            db_target=self.db_target,
            registry=SourceRegistry.with_defaults(),
            adapters=tuple(adapters if adapters is not None else self.sources),
            storage=self.storage,
            integrity=self.integrity,
            transports=(),
            approval_authorizer=PlanApprovalAuthorizer(key_path=self.key_path, key=key),
            optional=optional if optional is not None else OptionalServices(),
        )

    def close(self) -> None:
        self.http_server.close()

    # -- 便捷断言 ---------------------------------------------------------- #
    def read_file(self, path: Path) -> bytes:
        return path.read_bytes()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    """一套独立的状态根（每个测试互不干扰，也不碰真实 ``~/.rdh``）。"""
    instance = Harness(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def body() -> bytes:
    """一个小的"NetCDF 头 + 数据"字节串（传输与校验只关心字节）。"""
    return b"CDF\x01\x00\x00\x00\x00" + bytes(range(256)) * 8


@pytest.fixture
def probe_factory():
    """构造一个"仅 environment 可用"的探测结果（供能力类断言）。"""

    def _make(**overrides: bool) -> dict[str, PackageProbe]:
        base = {
            "environment": PackageProbe("environment", available=False),
            "providers": PackageProbe("providers", available=False),
            "network": PackageProbe("network", available=False),
        }
        for key, value in overrides.items():
            base[key] = PackageProbe(key, available=value)
        return base

    return _make


def copy_repo_defaults(destination: Path) -> Path:
    """把仓库的 ``configs/defaults.yaml`` 拷到目标目录（集成测试的配置来源）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULTS_TEMPLATE, destination)
    return destination
