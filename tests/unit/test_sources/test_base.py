"""基类与元数据标准化测试：协议一致性、未知值处理、扩展命名空间、拒绝伪造。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import FIXED_NOW, make_request
from tests.unit.test_sources.conftest import make_source_config

from researchdatahub.core.errors import (
    MetadataError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import AccessRoute, SourceAsset
from researchdatahub.core.ports import DataSourceAdapter, assert_implements
from researchdatahub.sources.base import (
    SourceAdapter,
    as_utc,
    asset_id_for,
    build_asset,
    build_dataset_metadata,
    build_source_metadata,
    build_temporal_extent,
    build_variable_spec,
    build_vertical_levels,
    json_safe,
    namespaced_key,
    normalize_source_metadata,
    normalize_variable_specs,
    parse_size_bytes,
    parse_timestamp,
)


class _StaticAdapter(SourceAdapter):
    """测试用适配器：真实实现基类钩子，不访问网络。"""

    adapter_id = "static"

    def __init__(self, *, assets=(), describe_error=None, health_error=None, **kwargs):
        super().__init__(**kwargs)
        self._assets = tuple(assets)
        self._describe_error = describe_error
        self._health_error = health_error
        self.describe_calls = 0

    def _describe(self):
        self.describe_calls += 1
        if self._describe_error is not None:
            raise self._describe_error
        return build_source_metadata(
            source_id=self.source_id,
            display_name="Static Source",
            provider="Test Provider",
            access_route=AccessRoute.HTTPS,
            dataset_id="ds-test",
            origin_uri="https://example.invalid/ds-test/",
            retrieved_at=FIXED_NOW,
            extensions={"test.kind": "static"},
        )

    def _discover_assets(self, request):
        return self._assets

    def _health_check(self):
        if self._health_error is not None:
            raise self._health_error
        return True


def _asset(*, source_id: str = "static-src", asset_id: str = "asset-0001", dataset_id="era5"):
    return build_asset(
        source_id=source_id,
        dataset_id=dataset_id,
        access_route=AccessRoute.HTTPS,
        origin_uri=f"https://example.invalid/{asset_id}.nc",
        file_name=f"{asset_id}.nc",
        size_bytes=1024,
        metadata=build_source_metadata(
            source_id=source_id,
            display_name="Static Source",
            provider="Test Provider",
            access_route=AccessRoute.HTTPS,
            dataset_id="ds-test",
            origin_uri=f"https://example.invalid/{asset_id}.nc",
            retrieved_at=FIXED_NOW,
        ),
        discovered_at=FIXED_NOW,
        asset_id=asset_id,
    )


def test_adapter_satisfies_datasource_protocol():
    """基类实现满足运行时 Protocol 契约。"""
    adapter = _StaticAdapter(source_id="static-src")
    assert_implements(adapter, DataSourceAdapter)
    assert adapter.source_id == "static-src"
    assert adapter.adapter_id == "static"


def test_describe_is_cached_after_first_success():
    """describe 只触发一次远端元数据查询（后续读缓存）。"""
    adapter = _StaticAdapter(source_id="static-src")
    first = adapter.describe()
    second = adapter.describe()
    assert first is second
    assert adapter.describe_calls == 1
    assert first.license is None
    assert first.citation is None
    assert first.extensions == {"test.kind": "static"}


def test_discover_requires_declared_dataset():
    """配置声明了 datasets 时，其它数据集必须报 SourceNotFoundError。"""
    config = make_source_config(datasets=("era5-pressure-levels",))
    adapter = _StaticAdapter(source_id="static-src", config=config)
    with pytest.raises(SourceNotFoundError):
        adapter.discover_assets(make_request(dataset_id="another-dataset"))


def test_discover_rejects_asset_for_other_source():
    """适配器返回别的源的资产是缺陷，必须暴露而不是静默使用。"""
    adapter = _StaticAdapter(source_id="static-src", assets=[_asset(source_id="other-src")])
    with pytest.raises(MetadataError):
        adapter.discover_assets(make_request())


def test_discover_rejects_duplicate_asset_ids():
    """同一源内 asset_id 重复会导致 Manifest upsert 互相覆盖。"""
    adapter = _StaticAdapter(source_id="static-src", assets=[_asset(asset_id="asset-0001")] * 2)
    with pytest.raises(MetadataError):
        adapter.discover_assets(make_request())


def test_health_check_never_raises():
    """健康检查失败返回 False，而不是把异常抛给调度器。"""
    adapter = _StaticAdapter(source_id="static-src", health_error=SourceUnavailableError("down"))
    assert adapter.health_check() is False
    adapter_ok = _StaticAdapter(source_id="static-src-2")
    assert adapter_ok.health_check() is True


def test_closed_adapter_refuses_calls():
    """close 之后拒绝所有 I/O 方法（协议要求）。"""
    adapter = _StaticAdapter(source_id="static-src")
    with adapter as entered:
        assert entered is adapter
        assert adapter.describe() is not None
    with pytest.raises(RuntimeError):
        adapter.describe()
    with pytest.raises(RuntimeError):
        adapter.discover_assets(make_request())
    assert adapter.health_check() is False


def test_asset_id_is_deterministic_and_pattern_safe():
    """资产 ID 稳定、符合 Identifier 形状，空/超长 source 前缀也能兜底。"""
    first = asset_id_for("ncar", "https://example.invalid/a.nc")
    second = asset_id_for("ncar", "https://example.invalid/a.nc")
    assert first == second
    assert first.startswith("ncar-")
    assert len(first) <= 63
    fallback = asset_id_for("A.very long source id with spaces!!", "seed")
    assert fallback
    assert fallback.islower()


def test_build_asset_requires_known_size():
    """大小未知时拒绝构造资产：0 会把"未知"伪装成"空文件"。"""
    metadata = build_source_metadata(
        source_id="static-src",
        display_name="Static Source",
        provider="Test Provider",
        access_route=AccessRoute.HTTPS,
        dataset_id="ds-test",
        origin_uri="https://example.invalid/a.nc",
        retrieved_at=FIXED_NOW,
    )
    with pytest.raises(MetadataError):
        build_asset(
            source_id="static-src",
            dataset_id="era5",
            access_route=AccessRoute.HTTPS,
            origin_uri="https://example.invalid/a.nc",
            file_name="a.nc",
            size_bytes=None,
            metadata=metadata,
            discovered_at=FIXED_NOW,
        )


def test_build_asset_derives_file_name_from_uri():
    """未显式给文件名时从 URI path 推导；空文件名不会静默通过。"""
    metadata = build_source_metadata(
        source_id="static-src",
        display_name="Static Source",
        provider="Test Provider",
        access_route=AccessRoute.HTTPS,
        dataset_id="ds-test",
        origin_uri="https://example.invalid/path/data.nc?token=abc",
        retrieved_at=FIXED_NOW,
    )
    asset = build_asset(
        source_id="static-src",
        dataset_id="era5",
        access_route=AccessRoute.HTTPS,
        origin_uri="https://example.invalid/path/data.nc?token=abc",
        file_name=None,
        size_bytes=10,
        metadata=metadata,
        discovered_at=FIXED_NOW,
    )
    assert asset.file_name == "data.nc"
    assert asset.access_route is AccessRoute.HTTPS
    assert isinstance(asset, SourceAsset)


def test_extension_keys_must_be_namespaced():
    """核心模型要求 extensions 键命名空间化，标准化工具要在构造前就给出可操作错误。"""
    with pytest.raises(MetadataError):
        build_source_metadata(
            source_id="static-src",
            display_name="Static Source",
            provider="Test Provider",
            access_route=AccessRoute.HTTPS,
            dataset_id="ds-test",
            origin_uri="https://example.invalid/a.nc",
            retrieved_at=FIXED_NOW,
            extensions={"bucket": "not-namespaced"},
        )
    assert namespaced_key("rda", "gindex") == "rda.gindex"


def test_dataset_metadata_refuses_missing_science_fields():
    """缺失必填科学元数据时拒绝构造，并列出缺失项（不推断）。"""
    variables = [build_variable_spec(name="t", units="K", long_name="Temperature")]
    with pytest.raises(MetadataError) as caught:
        build_dataset_metadata(
            product="ERA5",
            product_type="reanalysis",
            variables=variables,
            temporal_coverage=None,
            spatial_extent=None,
        )
    assert set(caught.value.detail["missing"]) == {"temporal_coverage", "spatial_extent"}


def test_unknown_optional_fields_stay_none_or_unknown():
    """未知的可选字段保持 None / "unknown"，不补科学默认值。"""
    from researchdatahub.core.models import SpatialExtent

    dataset = build_dataset_metadata(
        product="ERA5",
        product_type="reanalysis",
        variables=[build_variable_spec(name="t", units="K")],
        temporal_coverage=build_temporal_extent(
            datetime(2011, 1, 1, tzinfo=UTC), datetime(2012, 1, 1, tzinfo=UTC)
        ),
        spatial_extent=SpatialExtent(west=-180, east=180, south=-90, north=90),
        time_definition=None,
        data_version=None,
    )
    assert dataset.time_definition == "unknown"
    assert dataset.data_version == "unknown"
    assert dataset.units_normalized is False
    assert dataset.regridded is False


def test_variable_units_are_required():
    """单位缺失就报错；同变量名不同单位不能判等。"""
    with pytest.raises(MetadataError):
        build_variable_spec(name="t", units=None)
    with pytest.raises(MetadataError):
        build_variable_spec(name="", units="K")


def test_parse_timestamp_accepts_observed_formats_and_rejects_naive():
    """时间解析覆盖实测格式；naive 时间戳拒绝猜测时区。"""
    assert parse_timestamp("2026-11-04T09:00:00Z", field="t") == datetime(
        2026, 11, 4, 9, 0, tzinfo=UTC
    )
    assert parse_timestamp("1940-01-01 00:00 +0000", field="t") == datetime(1940, 1, 1, tzinfo=UTC)
    assert parse_timestamp("2025-10-30", field="t") == datetime(2025, 10, 30, tzinfo=UTC)
    assert parse_timestamp("not-a-date", field="t") is None
    assert parse_timestamp(None, field="t") is None
    with pytest.raises(MetadataError):
        parse_timestamp("2025-10-30T00:00:00", field="t")
    with pytest.raises(MetadataError):
        as_utc(datetime(2026, 1, 1))


def test_parse_size_and_json_safe():
    """大小解析只接受明确的数字；extensions 的 datetime 转换为 ISO 字符串。"""
    assert parse_size_bytes(1024) == 1024
    assert parse_size_bytes("1,024") == 1024
    assert parse_size_bytes(-5) is None
    assert parse_size_bytes("unknown") is None
    assert json_safe({"when": datetime(2026, 1, 1, tzinfo=UTC), "n": [1, None]}) == {
        "when": "2026-01-01T00:00:00+00:00",
        "n": [1, None],
    }


def test_vertical_levels_pressure_must_be_decreasing():
    """压力层必须降序（模型不变式），构造工具不重排、不猜单位。"""
    levels = build_vertical_levels(kind="pressure", levels=[1000.0, 900.0], units="hPa")
    assert list(levels.levels) == [1000.0, 900.0]
    with pytest.raises(MetadataError):
        build_vertical_levels(kind="pressure", levels=[900.0, 1000.0], units="hPa")


def test_normalize_variable_specs_maps_explicit_keys_only():
    """远端变量数组按显式声明的键映射；不做同义词猜测，单位缺失报错。"""
    specs = normalize_variable_specs(
        [
            {"name": "t", "units": "K", "long_name": "Temperature", "unknown": 1},
            {"name": "q", "units": "kg kg**-1"},
        ]
    )
    assert [spec.name for spec in specs] == ["t", "q"]
    assert specs[0].units == "K"
    assert specs[0].long_name == "Temperature"
    with pytest.raises(MetadataError):
        normalize_variable_specs([{"name": "t"}])
    with pytest.raises(MetadataError):
        normalize_variable_specs([])


def test_normalize_source_metadata_keeps_unmapped_raw_fields():
    """未映射的远端字段进入命名空间 extensions，显式映射字段才填契约字段。"""
    raw = {
        "license_name": "CC-BY-4.0",
        "citation_text": "Smith et al. (2020)",
        "protocol_version": "1.0.0",
        "provider_note": "remote-only field",
    }
    metadata = normalize_source_metadata(
        raw,
        source_id="static-src",
        display_name="Static Source",
        provider="Test Provider",
        access_route=AccessRoute.HTTPS,
        dataset_id="ds-test",
        origin_uri="https://example.invalid/ds-test/",
        retrieved_at=FIXED_NOW,
        prefix="test",
        field_map={
            "license": "license_name",
            "citation": "citation_text",
            "protocol_version": "protocol_version",
        },
    )
    assert metadata.license == "CC-BY-4.0"
    assert metadata.citation == "Smith et al. (2020)"
    assert metadata.protocol_version == "1.0.0"
    assert metadata.extensions["test.provider_note"] == "remote-only field"
    assert all("." in key for key in metadata.extensions)
    with pytest.raises(MetadataError):
        normalize_source_metadata(
            raw,
            source_id="static-src",
            display_name="Static Source",
            provider="Test Provider",
            access_route=AccessRoute.HTTPS,
            dataset_id="ds-test",
            origin_uri="https://example.invalid/ds-test/",
            retrieved_at=FIXED_NOW,
            prefix="test",
            field_map={"unknown_contract_field": "license_name"},
        )
