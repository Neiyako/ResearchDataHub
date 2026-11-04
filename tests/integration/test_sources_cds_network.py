"""CDS 真实网络测试（默认不跑；显式 ``pytest -m network``）。

只做**元数据级**请求：公开 API 的 process schema / catalogue 元数据不需要凭证；
需要凭证的异步生命周期测试在 absence of ``CDSAPI_KEY`` 时明确 skip（NOT RUN），
绝不伪装成 PASS。所有结果都记录在测试名与断言里，便于报告引用。
"""

from __future__ import annotations

import os

import httpx
import pytest
from tests.conftest import make_metadata, make_request

from researchdatahub.core.errors import SourceUnavailableError
from researchdatahub.core.models import DatasetRequest, SpatialExtent
from researchdatahub.sources.cds import (
    CDS_API_ROOT,
    DEFAULT_COLLECTIONS,
    CDSAdapter,
)

pytestmark = [pytest.mark.network, pytest.mark.integration]

COLLECTION = "reanalysis-era5-pressure-levels"


def _public_adapter() -> CDSAdapter:
    return CDSAdapter(
        source_id="cds",
        api_root=CDS_API_ROOT,
        credentials={"url": CDS_API_ROOT, "key": ""},
        timeout=20.0,
        http_client=httpx.Client(timeout=20.0, follow_redirects=True, trust_env=True),
    )


def _skip_on_network_error(exc: BaseException) -> None:
    pytest.skip(f"network unavailable / remote error: {type(exc).__name__}: {exc}")


def test_real_cds_process_schema_has_verified_parameters():
    """实测：retrieve/v1 process schema 的输入参数与变量白名单。"""
    adapter = _public_adapter()
    try:
        schema = adapter.fetch_process_schema(COLLECTION)
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        adapter.close()
    assert set(schema.inputs) == {
        "product_type",
        "variable",
        "year",
        "month",
        "day",
        "time",
        "pressure_level",
        "area",
        "data_format",
        "download_format",
    }
    assert "specific_humidity" in schema.variable_names
    assert "temperature" in schema.variable_names
    assert {"grib", "netcdf"} <= set(schema.require("data_format").allowed)
    assert "1000" in schema.require("pressure_level").allowed


def test_real_cds_catalogue_metadata_license():
    """实测：catalogue/v1 collection 元数据包含 CC-BY-4.0 许可证。"""
    adapter = _public_adapter()
    try:
        catalogue = adapter.fetch_catalogue_metadata(COLLECTION)
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        adapter.close()
    assert catalogue.get("id") == COLLECTION
    assert catalogue.get("license") == "CC-BY-4.0"


def test_real_cds_translation_uses_live_remote_schema():
    """用**线上** schema 翻译 fixture 需求，验证参数名/枚举来自真实远端而非硬编码。"""
    adapter = _public_adapter()
    try:
        translation = adapter.translate_request(make_request())
    except (SourceUnavailableError, httpx.HTTPError) as exc:
        _skip_on_network_error(exc)
    finally:
        adapter.close()
    assert translation.collection_id == DEFAULT_COLLECTIONS["era5-pressure-levels"]
    assert translation.request["variable"] == [
        "specific_cloud_liquid_water_content",
        "specific_cloud_ice_water_content",
        "temperature",
        "specific_humidity",
    ]
    assert translation.request["data_format"] == "netcdf"
    assert translation.request["area"] == [90.0, -180.0, -90.0, 180.0]


def test_real_cds_health_check_public_endpoint():
    """实测：公开 process 端点可达时 health_check 为 True。"""
    adapter = _public_adapter()
    try:
        assert adapter.health_check() is True
    finally:
        adapter.close()


@pytest.mark.skipif(
    not os.environ.get("CDSAPI_KEY", "").strip(),
    reason="NOT RUN: CDSAPI_KEY is not configured; authenticated lifecycle test skipped",
)
def test_authenticated_async_lifecycle_produces_result_url():
    """有凭证时跑一次最小异步请求，验证 submit→poll→results 的真实生命周期。

    若账号未接受数据集许可，远端会拒绝；这里如实失败（不伪造 PASS）。
    """
    api_root = os.environ.get("CDSAPI_URL", CDS_API_ROOT)
    adapter = CDSAdapter(
        source_id="cds",
        api_root=api_root,
        credentials={
            "url": api_root,
            "key": os.environ["CDSAPI_KEY"],
        },
        request_timeout_seconds=900.0,
    )
    metadata = make_metadata(
        variables=[{"name": "t", "units": "K", "long_name": "Temperature"}],
        temporal_coverage={
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
        },
        spatial_extent=SpatialExtent(west=-10.0, east=10.0, south=50.0, north=60.0),
        vertical_levels={"kind": "pressure", "levels": [1000.0], "units": "hPa"},
        time_definition="utc_hourly",
    )
    request = DatasetRequest(
        request_id="req-cds-network-tiny",
        dataset_id="era5-pressure-levels",
        metadata=metadata,
    )
    try:
        assets = adapter.discover_assets(request)
    finally:
        adapter.close()
    assert len(assets) == 1
    assert assets[0].access_route.value == "cds_api"
    assert assets[0].origin_uri.startswith("http")
    assert assets[0].size_bytes > 0
    assert assets[0].metadata.extensions["cds.request_id"]
