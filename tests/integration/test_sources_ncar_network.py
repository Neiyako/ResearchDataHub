"""NCAR GDEX/RDA 真实网络测试（默认不跑；显式 ``pytest -m network``）。

GDEX 的目录 API 与数据文件都是公开的（无需登录），因此这些断言是真实的远程事实：
分组 index/标题、许可证、时间覆盖、文件大小声明与 HTTPS 可达性。
"""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import make_request
from tests.unit.test_sources.conftest import make_source_config

from researchdatahub.core.errors import SourceUnavailableError
from researchdatahub.sources.ncar import (
    GDEX_DATA_HOST,
    GDEX_ENDPOINT,
    GdexCatalogClient,
    NCARAdapter,
)

pytestmark = [pytest.mark.network, pytest.mark.integration]

DSID = "d633000"


def _skip_on_network_error(exc: BaseException) -> None:
    pytest.skip(f"network unavailable / remote error: {type(exc).__name__}: {exc}")


def _adapter() -> NCARAdapter:
    config = make_source_config(
        adapter="rda",
        datasets=("era5-pressure-levels",),
        endpoint=GDEX_ENDPOINT,
        extensions={"rda.dsid": DSID},
    )
    return NCARAdapter(
        source_id="ncar",
        config=config,
        http_client=httpx.Client(timeout=30.0, follow_redirects=True, trust_env=True),
    )


def test_real_gdex_groups_and_temporal_coverage():
    """实测：ds633.0 的 GDEX 分组与时间覆盖（1940 起）。"""
    client = GdexCatalogClient(endpoint=GDEX_ENDPOINT, timeout=30.0)
    try:
        groups = client.list_groups(DSID)
        start, end = client.dataset_temporal(DSID)
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        client.close()
    catalogs = [group for group in groups if group.gindex == -2]
    assert len(catalogs) == 1
    assert catalogs[0].grpid == "catalogs"
    assert catalogs[0].web_file_count is not None
    assert catalogs[0].web_file_count >= 1
    assert start is not None
    assert start.isoformat().startswith("1940-01-01")
    assert end is not None
    assert end > start


def test_real_gdex_license_and_formats():
    """实测：GDEX 数据集许可证为 CC-BY-4.0，格式声明包含 netCDF4。"""
    client = GdexCatalogClient(endpoint=GDEX_ENDPOINT, timeout=30.0)
    try:
        license_text = client.dataset_license(DSID)
        formats = client.dataset_formats(DSID)
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        client.close()
    assert license_text is not None
    assert "Creative Commons Attribution 4.0" in license_text
    assert any("netCDF4" in item for item in formats)


def test_real_gdex_discovery_of_catalog_group():
    """实测：用显式 gindex=-2 真实发现 6 个 HTTPS 资产（catalogs 分组）。"""
    adapter = _adapter()
    try:
        assets = adapter.discover_assets(make_request(extensions={"rda.gindex": -2}))
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        adapter.close()
    assert len(assets) == 6
    assert all(asset.access_route.value == "https" for asset in assets)
    assert all(asset.origin_uri.startswith(GDEX_DATA_HOST) for asset in assets)
    first = assets[0]
    assert first.file_name == "d633000-osdf.csv"
    assert first.size_bytes == 106391
    assert first.metadata.extensions["rda.size_source"] == "catalog_unverified"
    assert first.metadata.extensions["rda.checksum"] == "unknown"


def test_real_gdex_file_url_is_reachable_and_catalog_size_is_not_trusted():
    """实测：目录声明的文件 URL 可达；但目录声明大小与实测字节可能不一致。"""
    adapter = _adapter()
    try:
        assets = adapter.discover_assets(make_request(extensions={"rda.gindex": -2}))
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        adapter.close()
    asset = next(item for item in assets if item.file_name == "d633000-osdf.csv")
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, trust_env=True) as client:
            response = client.head(asset.origin_uri)
    except httpx.HTTPError as exc:
        _skip_on_network_error(exc)
    assert response.status_code == 200
    actual = response.headers.get("Content-Length")
    assert actual is not None
    # 目录声明值与实际字节不一定相等（实测样本不同）；这里只记录差异，不做"相等"断言。
    if int(actual) == asset.size_bytes:
        assert asset.metadata.extensions["rda.size_source"] == "catalog_unverified"


def test_real_gdex_health_check():
    """实测：默认 GDEX endpoint + 已配置 dsid 时 health_check 为 True。"""
    adapter = _adapter()
    try:
        assert adapter.health_check() is True
    finally:
        adapter.close()


def test_legacy_rda_host_redirects_to_gdex():
    """实测：旧域名 rda.ucar.edu 的 /api 路径 301 到 gdex.ucar.edu 并返回 JSON。"""
    client = GdexCatalogClient(endpoint="https://rda.ucar.edu", timeout=30.0)
    try:
        groups = client.list_groups(DSID)
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        client.close()
    assert len(groups) > 0
