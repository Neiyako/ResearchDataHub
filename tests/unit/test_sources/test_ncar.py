"""NCAR GDEX/RDA 适配器测试：真实响应形状、dsid 映射、分组发现与"不虚构"边界。

fixtures 下的 GDEX JSON 是 2026-11-04 对 ``https://gdex.ucar.edu/api/...`` 的真实响应快照。
本地服务器用这些真实形状提供服务；没有 mock socket。
"""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import make_request
from tests.unit.test_sources.conftest import (
    LocalHttpServer,
    load_fixture,
    make_source_config,
)

from researchdatahub.config.models import SourceConfig
from researchdatahub.core.errors import (
    MetadataError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import AccessRoute
from researchdatahub.core.ports import DataSourceAdapter, assert_implements
from researchdatahub.sources.ncar import (
    GDEX_DATA_HOST,
    GDEX_ENDPOINT,
    NCARAdapter,
    normalize_dsid,
    parse_file_groups,
    parse_web_files,
)

DSID = "d633000"
API = "/api"


def _config(
    *,
    server: LocalHttpServer,
    datasets: tuple[str, ...] = ("era5-pressure-levels",),
    extensions: dict[str, object] | None = None,
) -> SourceConfig:
    merged: dict[str, object] = {"rda.dsid": DSID}
    merged.update(extensions or {})
    return make_source_config(
        adapter="rda",
        datasets=datasets,
        endpoint=server.url,
        extensions=merged,
    )


def install_gdex_routes(server: LocalHttpServer) -> None:
    """注册真实 GDEX 响应形状的路由。"""
    server.add_json(
        "GET", f"{API}/datasets/{DSID}/groups/", load_fixture("gdex_groups_d633000.json")
    )
    server.add_json(
        "GET", f"{API}/datasets/{DSID}/webfiles/-2/", load_fixture("gdex_webfiles_d633000_-2.json")
    )
    server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/data_license/",
        load_fixture("gdex_data_license_d633000.json"),
    )
    server.add_json(
        "GET", f"{API}/datasets/{DSID}/temporal/", load_fixture("gdex_temporal_d633000.json")
    )
    server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/data_formats/",
        load_fixture("gdex_data_formats_d633000.json"),
    )
    server.add_json(
        "GET",
        f"{API}/datasets/",
        {
            "status": "ok",
            "data": {
                "count": 1,
                "datasets": [
                    {"id": DSID, "title": "ERA5 Reanalysis (0.25 Degree Latitude-Longitude Grid)"}
                ],
            },
        },
    )


def _adapter(server: LocalHttpServer, **kwargs) -> NCARAdapter:
    config = kwargs.pop("config", None) or _config(server=server)
    return NCARAdapter(
        source_id="ncar",
        config=config,
        data_host=kwargs.pop("data_host", server.url),
        http_client=httpx.Client(timeout=2.0, follow_redirects=True),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# dsid 映射与解析
# --------------------------------------------------------------------------- #
def test_normalize_dsid_uses_observed_legacy_mapping():
    """dsid 规范化依据实测重定向 ds633.0 → d633000，不发明别的规则。"""
    assert normalize_dsid("d633000") == "d633000"
    assert normalize_dsid("ds633.0") == "d633000"
    assert normalize_dsid("ds633.12") == "d633012"
    with pytest.raises(MetadataError):
        normalize_dsid("era5-pressure-levels")


def test_parse_real_group_and_webfile_shapes():
    """真实 groups/webfiles 响应可解析，未知字段进命名空间扩展。"""
    groups = parse_file_groups(load_fixture("gdex_groups_d633000.json"))
    assert len(groups) >= 10
    catalogs = next(group for group in groups if group.gindex == -2)
    assert catalogs.grpid == "catalogs"
    assert catalogs.web_file_count == 6
    assert catalogs.raw["gindex"] == -2

    files = parse_web_files(load_fixture("gdex_webfiles_d633000_-2.json"))
    assert len(files) == 6
    first = files[0]
    assert first.wfile == "catalogs/d633000-osdf.csv"
    assert first.file_name == "d633000-osdf.csv"
    assert first.size_bytes == 106391
    assert first.data_format == "CSV"
    assert first.status == "P"
    assert first.date_modified is not None
    assert first.date_modified.isoformat() == "2025-10-30T00:00:00+00:00"
    assert first.size_source == "catalog_unverified"


# --------------------------------------------------------------------------- #
# describe / health_check
# --------------------------------------------------------------------------- #
def test_describe_uses_real_catalogue_responses(http_server: LocalHttpServer):
    """describe 读取真实形状的 license/temporal/formats/title。"""
    install_gdex_routes(http_server)
    adapter = _adapter(http_server)
    metadata = adapter.describe()
    assert metadata.source_id == "ncar"
    assert metadata.dataset_id == DSID
    assert metadata.license == "Creative Commons Attribution 4.0 International License"
    assert metadata.access_route is AccessRoute.HTTPS
    assert metadata.protocol_version == "rda-datasets-api/1.0.0"
    assert metadata.extensions["rda.dsid"] == DSID
    assert metadata.extensions["rda.temporal_start"].startswith("1940-01-01")
    assert metadata.extensions["rda.size_source"] == "catalog_unverified"
    assert metadata.extensions["rda.checksum"] == "unknown"
    assert all("." in key for key in metadata.extensions)


def test_health_check_ok_error_and_connection_refused(http_server: LocalHttpServer):
    """健康检查：200 → True；500 → False；真实连接失败 → False，均不抛异常。"""
    install_gdex_routes(http_server)
    assert _adapter(http_server).health_check() is True

    error_server = LocalHttpServer()
    try:
        error_server.add_json("GET", f"{API}/datasets/{DSID}/groups/", {}, status=500)
        assert _adapter(error_server, config=_config(server=error_server)).health_check() is False
    finally:
        error_server.stop()

    dead_server = LocalHttpServer()
    dead_url = dead_server.url
    dead_server.stop()
    config = make_source_config(adapter="rda", endpoint=dead_url, extensions={"rda.dsid": DSID})
    adapter = NCARAdapter(
        source_id="ncar",
        config=config,
        data_host=dead_url,
        timeout=0.2,
        http_client=httpx.Client(timeout=0.2),
    )
    assert adapter.health_check() is False


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def test_discover_requires_explicit_group_index(http_server: LocalHttpServer):
    """变量→文件分组的映射没有机器可读规则：缺 gindex 时明确拒绝，不猜。"""
    install_gdex_routes(http_server)
    adapter = _adapter(http_server)
    with pytest.raises(MetadataError) as caught:
        adapter.discover_assets(make_request())
    assert "rda.gindex" in str(caught.value)
    # 尚未发起任何目录请求（拒绝发生在 I/O 之前）。
    assert http_server.requests_to(f"{API}/datasets/{DSID}/webfiles/-2/") == ()


def test_discover_produces_https_assets_from_real_web_file_shape(
    http_server: LocalHttpServer,
):
    """按显式 gindex 发现真实形状文件，产出带 size/route/来源追踪的 SourceAsset。"""
    install_gdex_routes(http_server)
    adapter = _adapter(http_server)
    request = make_request(extensions={"rda.gindex": -2})
    assets = adapter.discover_assets(request)
    assert len(assets) == 6
    asset = assets[0]
    assert asset.source_id == "ncar"
    assert asset.dataset_id == "era5-pressure-levels"
    assert asset.access_route is AccessRoute.HTTPS
    assert asset.origin_uri == f"{http_server.url}/{DSID}/catalogs/d633000-osdf.csv"
    assert asset.file_name == "d633000-osdf.csv"
    assert asset.size_bytes == 106391
    assert asset.checksum is None
    assert asset.metadata.origin_uri == asset.origin_uri
    assert asset.metadata.extensions["rda.size_source"] == "catalog_unverified"
    assert asset.metadata.extensions["rda.checksum"] == "unknown"
    assert asset.metadata.extensions["rda.gindex"] == -2
    assert asset.metadata.extensions["rda.time_known"] is False


def test_discover_group_index_can_come_from_config(http_server: LocalHttpServer):
    """配置扩展可提供默认 gindex（请求扩展优先级更高）。"""
    install_gdex_routes(http_server)
    config = _config(server=http_server, extensions={"rda.gindex": -2})
    assets = _adapter(http_server, config=config).discover_assets(make_request())
    assert len(assets) == 6


def test_discover_refuses_oversized_group(http_server: LocalHttpServer):
    """没有时间过滤时，超大分组必须显式失败而不是悄悄生成上万任务。"""
    install_gdex_routes(http_server)
    adapter = _adapter(http_server, max_web_files=2)
    with pytest.raises(SourceUnavailableError):
        adapter.discover_assets(make_request(extensions={"rda.gindex": -2}))


def test_time_filter_uses_configured_filename_convention(http_server: LocalHttpServer):
    """按时间裁剪只在显式配置文件名规则时生效；规则要真实匹配到文件。"""
    payload = {
        "status": "ok",
        "data": [
            {
                "wfile": "e5.oper.an.pl.128_133_q.ll025sc.2011010100_2011123123.grb",
                "data_size": 100,
                "data_format": "GRIB1",
                "date_modified": "2020-01-01",
                "status": "P",
            },
            {
                "wfile": "e5.oper.an.pl.128_133_q.ll025sc.2000010100_2000123123.grb",
                "data_size": 200,
                "data_format": "GRIB1",
                "date_modified": "2020-01-01",
                "status": "P",
            },
        ],
    }
    http_server.add_json("GET", f"{API}/datasets/{DSID}/webfiles/26/", payload)
    http_server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/data_license/",
        load_fixture("gdex_data_license_d633000.json"),
    )
    config = _config(
        server=http_server,
        extensions={
            "rda.gindex": 26,
            "rda.file_time": {
                "pattern": r"\.(?P<start>\d{10})_(?P<end>\d{10})\.",
                "format": "%Y%m%d%H",
            },
        },
    )
    assets = _adapter(http_server, config=config).discover_assets(make_request())
    assert len(assets) == 1
    assert assets[0].file_name.endswith("2011010100_2011123123.grb")


def test_time_filter_pattern_must_match_named_groups(http_server: LocalHttpServer):
    """错误的文件名规则必须显式报错，而不是静默返回空列表。"""
    http_server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/webfiles/26/",
        {
            "status": "ok",
            "data": [
                {
                    "wfile": "no-timestamps-here.nc",
                    "data_size": 1,
                    "date_modified": "2020-01-01",
                }
            ],
        },
    )
    config = _config(
        server=http_server,
        extensions={
            "rda.gindex": 26,
            "rda.file_time": {
                "pattern": r"\.(?P<start>\d{10})_(?P<end>\d{10})\.",
                "format": "%Y%m%d%H",
            },
        },
    )
    with pytest.raises(MetadataError):
        _adapter(http_server, config=config).discover_assets(make_request())


def test_missing_size_is_refused_not_faked(http_server: LocalHttpServer):
    """webfiles 缺 data_size 时不生成 size=0 的假资产。"""
    http_server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/webfiles/-2/",
        {"status": "ok", "data": [{"wfile": "catalogs/x.csv", "data_format": "CSV"}]},
    )
    http_server.add_json(
        "GET",
        f"{API}/datasets/{DSID}/data_license/",
        load_fixture("gdex_data_license_d633000.json"),
    )
    with pytest.raises(MetadataError):
        _adapter(http_server).discover_assets(make_request(extensions={"rda.gindex": -2}))


# --------------------------------------------------------------------------- #
# 端点/映射的"已验证 vs 未验证"边界
# --------------------------------------------------------------------------- #
def test_verified_endpoint_hosts_and_data_host_rules():
    """只有实测过的 GDEX 主机（含旧域名重定向）才自动推导 data host。"""
    gdex = NCARAdapter(source_id="ncar", endpoint=GDEX_ENDPOINT)
    assert gdex.endpoint_verified is True
    assert gdex.data_host == GDEX_DATA_HOST

    legacy = NCARAdapter(source_id="ncar", endpoint="https://rda.ucar.edu")
    assert legacy.endpoint_verified is True
    assert legacy.data_host == GDEX_DATA_HOST

    unknown = NCARAdapter(source_id="ncar", endpoint="https://example.invalid")
    assert unknown.endpoint_verified is False
    with pytest.raises(SourceUnavailableError):
        _ = unknown.data_host
    assert unknown.capabilities().reason is not None

    explicit = NCARAdapter(
        source_id="ncar",
        endpoint="https://example.invalid",
        data_host="https://data.example.invalid",
    )
    assert explicit.data_host == "https://data.example.invalid"


def test_legacy_dataset_group_maps_all_declared_datasets():
    """配置示例里的 ncar.dataset_group 是显式映射（两个逻辑数据集都在 ds633.0）。"""
    config = make_source_config(
        adapter="rda",
        datasets=("era5-pressure-levels", "era5-single-levels"),
        extensions={"ncar.dataset_group": "ds633.0"},
    )
    adapter = NCARAdapter(source_id="ncar", config=config)
    assert adapter.resolve_dsid("era5-pressure-levels") == "d633000"
    assert adapter.resolve_dsid("era5-single-levels") == "d633000"
    with pytest.raises(SourceNotFoundError):
        adapter.resolve_dsid("unknown-dataset")


def test_capabilities_list_unverified_boundaries():
    """能力描述必须明确列出未验证项（变量→分组、大小来源、校验和、S3 等）。"""
    adapter = NCARAdapter(source_id="ncar", endpoint=GDEX_ENDPOINT)
    capabilities = adapter.capabilities()
    assert capabilities.available is True
    assert capabilities.routes == ("https",)
    rendered = " ".join(capabilities.unverified)
    assert "gindex" in rendered
    assert "catalog" in rendered
    assert "checksum" in rendered.lower()
    assert "S3" in rendered
    assert any("real GET 2026-11-04" in item for item in capabilities.verified)


def test_unknown_status_payload_becomes_source_unavailable(http_server: LocalHttpServer):
    """GDEX 返回 error status 时不把错误数据当成空目录。"""
    http_server.add_json(
        "GET", f"{API}/datasets/{DSID}/groups/", {"status": "error", "error_messages": ["nope"]}
    )
    adapter = _adapter(http_server)
    assert adapter.health_check() is False


def test_ncar_adapter_satisfies_datasource_protocol():
    """真实 NCAR 适配器实例满足运行时 DataSourceAdapter 契约。"""
    adapter = NCARAdapter(source_id="ncar", endpoint=GDEX_ENDPOINT)
    assert_implements(adapter, DataSourceAdapter)
    assert adapter.supported_routes == frozenset({"https"})
    assert adapter.adapter_id == "rda"


def test_configuration_validation_reports_missing_or_invalid_settings():
    """静态配置校验：缺 dsid 映射/未验证 host/坏文件名规则都报告。"""
    unconfigured = NCARAdapter(source_id="ncar", endpoint=GDEX_ENDPOINT)
    issues = unconfigured.validate_configuration()
    assert any("dataset mapping" in item for item in issues)

    unknown_host = NCARAdapter(
        source_id="ncar", endpoint="https://example.invalid", dataset_ids={"era5": "d633000"}
    )
    issues = unknown_host.validate_configuration()
    assert any("data host" in item for item in issues)

    bad_pattern = make_source_config(
        adapter="rda",
        endpoint=GDEX_ENDPOINT,
        extensions={
            "rda.dsid": "d633000",
            "rda.file_time": {"pattern": r"(?P<start>\d+)", "format": "%Y"},
        },
    )
    issues = NCARAdapter(source_id="ncar", config=bad_pattern).validate_configuration()
    assert any("named groups" in item for item in issues)

    ready = make_source_config(
        adapter="rda", endpoint=GDEX_ENDPOINT, extensions={"rda.dsid": "d633000"}
    )
    assert NCARAdapter(source_id="ncar", config=ready).validate_configuration() == ()
