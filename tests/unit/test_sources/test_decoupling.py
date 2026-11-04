"""解耦证明：sources 包不依赖 transfer；传输层无需知道数据源是谁。

对应 ADR-0002 的验证方式：
* ``grep``/AST 层面：``sources`` 不 import 其它执行层模块；
* 契约层面：只凭 ``SourceAsset`` 的 ``origin_uri`` / ``access_route`` / ``size_bytes``
  / ``checksum``，一个**与源无关**的传输实现就能处理 CDS 与 NCAR 的资产。
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
from tests.conftest import make_request
from tests.unit.test_sources.conftest import LocalHttpServer, make_source_config
from tests.unit.test_sources.test_cds_adapter import install_cds_routes
from tests.unit.test_sources.test_ncar import install_gdex_routes

from researchdatahub.core.models import AccessRoute, SourceAsset
from researchdatahub.sources.cds import CDSAdapter, probe_cds_dependencies
from researchdatahub.sources.ncar import NCARAdapter

SOURCES_DIR = Path("src/researchdatahub/sources")
FORBIDDEN_MODULES = (
    "transfer",
    "manifest",
    "integrity",
    "storage",
    "planner",
    "scheduler",
    "catalog",
    "reporting",
    "cli",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_sources_package_does_not_import_other_execution_layers():
    """sources 只依赖 core/config；横向 import 会破坏 ADR-0002 的依赖矩阵。"""
    for path in sorted(SOURCES_DIR.glob("*.py")):
        imported = _imported_modules(path)
        for module in imported:
            if not module.startswith("researchdatahub."):
                continue
            parts = module.split(".")
            assert len(parts) >= 2
            layer = parts[1]
            assert layer not in FORBIDDEN_MODULES, f"{path} imports {module}"


def test_sources_never_download_or_transfer_bytes():
    """发现层禁止调用下载/传输原语：只产出 SourceAsset。"""
    forbidden_calls = {"download", "transfer", "read_bytes", "write_bytes", "copy"}
    offenders: list[str] = []
    for path in sorted(SOURCES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr in forbidden_calls:
                offenders.append(f"{path.name}:{node.lineno}:{function.attr}")
    assert offenders == []


class RouteOnlyTransport:
    """与数据源无关的传输实现（真实可运行：只做 URI 解析与字段读取）。

    它**不知道** ``source_id``，只依赖 ``SourceAsset`` 契约；这就是"换源不改传输"的证明。
    """

    def __init__(self) -> None:
        self.received: list[tuple[str, str, int]] = []

    def supports(self, asset: SourceAsset) -> bool:
        """依据 access_route 判断能否处理（不知道源）。"""
        return asset.access_route in {AccessRoute.HTTPS, AccessRoute.CDS_API}

    def plan_download(self, asset: SourceAsset) -> dict[str, object]:
        """模拟一次下载规划：仅使用 origin_uri / size / checksum / file_name。"""
        assert self.supports(asset)
        self.received.append((asset.origin_uri, asset.file_name, asset.size_bytes))
        return {
            "uri": asset.origin_uri,
            "target_name": asset.file_name,
            "expected_size": asset.size_bytes,
            "expected_checksum": None if asset.checksum is None else asset.checksum.value,
        }


def _cds_adapter(server: LocalHttpServer) -> CDSAdapter:
    return CDSAdapter(
        source_id="cds",
        api_root=f"{server.url}/api",
        credentials={"url": f"{server.url}/api", "key": "test-key"},
        dependency_probe=probe_cds_dependencies,
        request_timeout_seconds=5.0,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(timeout=2.0, follow_redirects=True),
    )


def _ncar_adapter(server: LocalHttpServer) -> NCARAdapter:
    config = make_source_config(
        adapter="rda",
        datasets=("era5-pressure-levels",),
        endpoint=server.url,
        extensions={"rda.dsid": "d633000"},
    )
    return NCARAdapter(
        source_id="ncar",
        config=config,
        data_host=server.url,
        http_client=httpx.Client(timeout=2.0, follow_redirects=True),
    )


def test_one_transport_handles_assets_from_both_sources():
    """同一个传输实现消费 CDS 与 NCAR 资产，代码中没有 source_id 分支。"""
    cds_server = LocalHttpServer()
    ncar_server = LocalHttpServer()
    try:
        install_cds_routes(cds_server)
        install_gdex_routes(ncar_server)
        cds_asset = _cds_adapter(cds_server).discover_assets(make_request())[0]
        ncar_asset = _ncar_adapter(ncar_server).discover_assets(
            make_request(extensions={"rda.gindex": -2})
        )[0]
    finally:
        cds_server.stop()
        ncar_server.stop()

    transport = RouteOnlyTransport()
    assert not hasattr(transport, "source_id")
    assert transport.supports(cds_asset) is True
    assert transport.supports(ncar_asset) is True
    cds_plan = transport.plan_download(cds_asset)
    ncar_plan = transport.plan_download(ncar_asset)
    assert cds_plan["uri"] == cds_asset.origin_uri
    assert ncar_plan["uri"] == ncar_asset.origin_uri
    assert transport.received[0][0].startswith("http")
    assert transport.received[1][0].startswith("http")
    # 两种 route 都通过契约表达，传输层无需回查任何适配器/配置。
    assert {asset.access_route for asset in (cds_asset, ncar_asset)} == {
        AccessRoute.CDS_API,
        AccessRoute.HTTPS,
    }


def test_source_asset_carries_everything_transport_needs():
    """SourceAsset 自带 origin_uri/size/route/checksum，传输层不需要 source 知识。"""
    fields = set(SourceAsset.model_fields)
    for required in ("origin_uri", "access_route", "size_bytes", "checksum", "file_name"):
        assert required in fields
    assert "source_id" in fields
