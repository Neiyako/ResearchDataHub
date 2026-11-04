"""CDS 请求翻译测试：真实 process schema 形状 + 不猜测规则。

远端 schema 来自 2026-11-04 对 CDS 公开 API 的真实响应快照
（``fixtures/cds_process_era5_pressure_levels.json``）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import make_metadata, make_request
from tests.unit.test_sources.conftest import load_fixture

from researchdatahub.core.errors import MetadataError, SourceNotFoundError
from researchdatahub.core.models import SpatialExtent
from researchdatahub.core.ports import DataSourceAdapter, assert_implements
from researchdatahub.sources.cds import (
    DEFAULT_COLLECTIONS,
    CDSAdapter,
    CdsProcessSchema,
    CdsRequestTranslator,
)


def _schema() -> CdsProcessSchema:
    payload = load_fixture("cds_process_era5_pressure_levels.json")
    return CdsProcessSchema.from_payload(
        payload,
        collection_id="reanalysis-era5-pressure-levels",
        source_url="https://cds.climate.copernicus.eu/api/retrieve/v1/processes/"
        "reanalysis-era5-pressure-levels",
        fetched_at=datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
    )


def _translator() -> CdsRequestTranslator:
    return CdsRequestTranslator(_schema(), clock=lambda: datetime(2026, 11, 4, tzinfo=UTC))


def test_schema_parses_real_process_payload():
    """真实 inputs 形状可解析，并保留带 enum 的参数白名单。"""
    schema = _schema()
    assert schema.collection_id == "reanalysis-era5-pressure-levels"
    assert schema.version == "1.0.0"
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
    assert schema.require("data_format").allowed == ("grib", "netcdf")
    assert set(schema.require("download_format").allowed) == {"unarchived", "zip"}
    assert "1000" in schema.require("pressure_level").allowed


def test_fixture_request_translates_to_verified_cds_parameters():
    """用 fixture 需求翻译出的请求逐字段符合 CDS retrieve/v1 的实测参数与枚举。"""
    translation = _translator().translate(make_request())
    request = translation.request
    assert translation.collection_id == "reanalysis-era5-pressure-levels"
    assert request["product_type"] == ["reanalysis"]
    assert request["variable"] == [
        "specific_cloud_liquid_water_content",
        "specific_cloud_ice_water_content",
        "temperature",
        "specific_humidity",
    ]
    assert request["year"] == [str(year) for year in range(2011, 2021)]
    assert request["month"] == [f"{month:02d}" for month in range(1, 13)]
    assert request["day"] == [f"{day:02d}" for day in range(1, 32)]
    assert request["time"] == [f"{hour:02d}:00" for hour in range(24)]
    assert request["pressure_level"] == ["1000", "975", "950", "925", "900"]
    assert request["area"] == [90.0, -180.0, -90.0, 180.0]
    assert request["data_format"] == "netcdf"
    assert request["download_format"] == "unarchived"


def test_area_uses_declared_order_and_is_marked_unverified():
    """bbox 按 [north, west, south, east] 输出；该顺序尚未被官方文档复核。"""
    metadata = make_metadata(
        spatial_extent=SpatialExtent(west=-10.0, east=30.0, south=50.0, north=60.0)
    )
    request = make_request(metadata=metadata.model_dump(mode="json"))
    translated = _translator().translate(request)
    assert translated.request["area"] == [60.0, -10.0, 50.0, 30.0]
    assert CdsRequestTranslator.area_order == ("north", "west", "south", "east")
    assert CdsRequestTranslator.area_order_verified is False


def test_temporal_range_is_expanded_by_union_and_end_is_exclusive():
    """CDS 的 year/month/day 是笛卡尔积：区间按覆盖集展开，end 为开区间。"""
    metadata = make_metadata(
        temporal_coverage={
            "start": "2011-03-01T00:00:00Z",
            "end": "2012-06-15T00:00:00Z",
        }
    )
    request = make_request(metadata=metadata.model_dump(mode="json"))
    translated = _translator().translate(request)
    assert translated.request["year"] == ["2011", "2012"]
    # CDS 的 year/month/day 是笛卡尔积，因此 month 取区间覆盖到的月份并集：
    # 2011-03..2012-06 覆盖了全部 12 个月；days 取覆盖月份中最大天数 31。
    assert translated.request["month"] == [f"{month:02d}" for month in range(1, 13)]
    assert translated.request["day"] == [f"{day:02d}" for day in range(1, 32)]


def test_variables_are_matched_by_long_name_not_guessed():
    """变量通过 long_name 规范化为远端 enum 长名；匹配不上就失败并列出可用值。"""
    metadata = make_metadata(
        variables=[{"name": "t", "units": "K", "long_name": "Temperature"}],
    )
    translated = _translator().translate(make_request(metadata=metadata.model_dump(mode="json")))
    assert translated.request["variable"] == ["temperature"]

    unknown = make_metadata(
        variables=[{"name": "xyz", "units": "K", "long_name": "Not A CDS Variable"}],
    )
    with pytest.raises(MetadataError) as caught:
        _translator().translate(make_request(metadata=unknown.model_dump(mode="json")))
    assert "available" in caught.value.detail


def test_schema_without_variable_enum_is_rejected():
    """远端不给变量白名单时不能"按名字猜"，必须失败。"""
    payload = {
        "id": "reanalysis-era5-pressure-levels",
        "inputs": {
            "product_type": {"schema": {"type": "array", "items": {"type": "string"}}},
            "variable": {"schema": {"type": "array", "items": {"type": "string"}}},
            "year": {"schema": {"type": "array", "items": {"type": "string"}}},
            "month": {"schema": {"type": "array", "items": {"type": "string"}}},
            "day": {"schema": {"type": "array", "items": {"type": "string"}}},
            "time": {"schema": {"type": "array", "items": {"type": "string"}}},
            "area": {"schema": {"type": "array"}},
            "data_format": {"schema": {"type": "string"}},
            "download_format": {"schema": {"type": "string"}},
        },
    }
    schema = CdsProcessSchema.from_payload(
        payload,
        collection_id="x",
        source_url="https://example.invalid/processes/x",
        fetched_at=datetime(2026, 11, 4, tzinfo=UTC),
    )
    with pytest.raises(MetadataError):
        CdsRequestTranslator(schema).translate(make_request())


def test_unknown_temporal_granularity_is_refused():
    """time_definition 无法映射到 CDS 时间粒度时拒绝，不默认按小时猜。"""
    metadata = make_metadata(time_definition="utc")
    request = make_request(metadata=metadata.model_dump(mode="json"))
    with pytest.raises(MetadataError):
        _translator().translate(request)


def test_daily_granularity_with_hourly_dataset_is_refused():
    """ERA5 小时产品需要 time 参数；daily 粒度无法表达时不猜测。"""
    metadata = make_metadata(time_definition="utc_daily")
    request = make_request(metadata=metadata.model_dump(mode="json"))
    with pytest.raises(MetadataError):
        _translator().translate(request)


def test_pressure_level_must_exist_in_remote_enum():
    """压力层取值必须命中远端白名单（1234 hPa 不存在 → 报错）。"""
    metadata = make_metadata(
        vertical_levels={"kind": "pressure", "levels": [1234.0, 1000.0], "units": "hPa"}
    )
    request = make_request(metadata=metadata.model_dump(mode="json"))
    with pytest.raises(MetadataError):
        _translator().translate(request)


def test_non_pressure_vertical_kind_is_refused():
    """模型层/高度层没有已验证的 CDS 参数，必须显式拒绝。"""
    metadata = make_metadata(
        vertical_levels={"kind": "model_level", "levels": [1.0, 2.0], "units": "1"}
    )
    request = make_request(metadata=metadata.model_dump(mode="json"))
    with pytest.raises(MetadataError):
        _translator().translate(request)


def test_data_format_override_is_validated_against_remote_enum():
    """请求扩展可指定输出格式，但必须在远端 enum 内。"""
    request = make_request(extensions={"cds.data_format": "grib"})
    assert _translator().translate(request).request["data_format"] == "grib"
    bad = make_request(extensions={"cds.data_format": "csv"})
    with pytest.raises(MetadataError):
        _translator().translate(bad)


def test_collection_mapping_uses_verified_defaults_and_config_override():
    """默认映射是实测过的 collection；配置可覆盖；未知数据集明确报错。"""
    assert DEFAULT_COLLECTIONS["era5-pressure-levels"] == "reanalysis-era5-pressure-levels"
    adapter = CDSAdapter(source_id="cds")
    assert adapter.resolve_collection("era5-single-levels") == "reanalysis-era5-single-levels"
    overridden = CDSAdapter(
        source_id="cds",
        dataset_names={"my-dataset": "reanalysis-era5-pressure-levels"},
    )
    assert overridden.resolve_collection("my-dataset") == "reanalysis-era5-pressure-levels"
    with pytest.raises(SourceNotFoundError):
        adapter.resolve_collection("unknown-dataset")


def test_capabilities_report_availability_without_fabricating():
    """缺依赖/缺凭证时 available=False 并说明缺什么（不伪造可用）。"""
    missing = CDSAdapter(source_id="cds", dependency_probe=lambda: None)
    capabilities = missing.capabilities()
    assert capabilities.available is False
    assert "cdsapi>=0.7.7" in capabilities.missing_dependencies
    assert any("area array ordering" in item for item in capabilities.unverified)

    no_credentials = CDSAdapter(source_id="cds", dependency_probe=lambda: object())
    assert no_credentials.capabilities().available is False
    assert no_credentials.capabilities().missing_credentials == ("key",)

    ready = CDSAdapter(
        source_id="cds",
        dependency_probe=lambda: object(),
        credentials={"url": "https://example.invalid/api", "key": "super-secret-token"},
    )
    assert ready.capabilities().available is True
    rendered = str(ready.capabilities().to_dict())
    assert "super-secret-token" not in rendered
    assert "area array ordering" in str(ready.capabilities().unverified)


def test_cds_adapter_satisfies_datasource_protocol():
    """真实 CDS 适配器实例满足运行时 DataSourceAdapter 契约。"""
    adapter = CDSAdapter(source_id="cds")
    assert_implements(adapter, DataSourceAdapter)
    assert adapter.source_id == "cds"
    assert adapter.supported_routes == frozenset({"cds_api"})


def test_configuration_validation_reports_missing_or_invalid_settings():
    """静态配置校验：URL/凭证/数据集映射问题逐条报告（不发网络请求）。"""
    empty = CDSAdapter(source_id="cds", dependency_probe=lambda: object())
    issues = empty.validate_configuration()
    assert any("credentials.key" in item for item in issues)

    broken = CDSAdapter(
        source_id="cds",
        api_root="ftp://example.invalid/api",
        dependency_probe=lambda: object(),
        credentials={"key": "test-key"},
        dataset_names={"era5": "bad collection/id"},
    )
    issues = broken.validate_configuration()
    assert any("http(s)" in item for item in issues)
    assert any("invalid CDS collection id" in item for item in issues)

    ready = CDSAdapter(
        source_id="cds",
        api_root="https://example.invalid/api",
        dependency_probe=lambda: object(),
        credentials={"key": "test-key"},
    )
    assert ready.validate_configuration() == ()
