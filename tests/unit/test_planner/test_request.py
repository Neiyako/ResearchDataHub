"""需求加载器测试：§6.2 分段格式、扁平兼容、字段定位、时间/单位语义。

覆盖 task-5 的"YAML 解析 / 无效请求"两项；断言错误信息里出现 ``文件:行`` 与字段路径，
而不是 pydantic 的笼统 stack。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_planner.support import (
    BASE_DOCUMENT,
    EXAMPLE_REQUEST,
    document,
    load_doc,
    yaml_text,
)

from researchdatahub.core.models import IntegrityCheckKind, VerticalLevelKind
from researchdatahub.planner.request import (
    RequestLoadError,
    load_request,
    load_request_detailed,
    load_request_text_detailed,
)


def test_sectioned_yaml_maps_to_dataset_request() -> None:
    loaded = load_doc()
    request = loaded.request
    assert request.request_id == "era5_cloud_water_2011"
    assert request.dataset_id == "reanalysis-era5-pressure-levels"
    assert request.metadata.product == "ERA5"
    assert request.metadata.product_type == "reanalysis"
    assert request.metadata.extensions["rdh.provider"] == "cds"
    assert request.metadata.extensions["rdh.dataset_name"] == "reanalysis-era5-pressure-levels"
    assert request.metadata.time_definition == "hourly"
    assert [spec.name for spec in request.metadata.variables] == ["clwc", "t"]
    assert request.checks == [
        IntegrityCheckKind.SIZE_MATCH,
        IntegrityCheckKind.CHECKSUM_MATCH,
    ]
    assert request.preserve_original is True
    assert request.max_sources == 1
    assert loaded.warnings == ()


def test_end_date_is_expanded_to_next_midnight_utc() -> None:
    request = load_doc().request
    coverage = request.metadata.temporal_coverage
    assert coverage.start == datetime(2011, 1, 1, tzinfo=UTC)
    # 闭开区间：end=2011-12-31 → 2012-01-01T00:00:00Z，最后一天不丢。
    assert coverage.end == datetime(2012, 1, 1, tzinfo=UTC)


def test_flat_example_file_still_loads() -> None:
    loaded = load_request_detailed(str(EXAMPLE_REQUEST))
    request = loaded.request
    assert request.request_id == "era5-clwc-ciwc-t-q-pl-2011-2021"
    assert request.metadata.time_definition == "utc_hourly"
    assert len(request.metadata.variables) == 4
    assert request.metadata.vertical_levels.kind is VerticalLevelKind.PRESSURE


def test_string_variable_units_are_not_guessed() -> None:
    doc = document(variables=["clwc", {"name": "t", "units": "K"}])
    loaded = load_doc(doc)
    variables = {spec.name: spec.units for spec in loaded.request.metadata.variables}
    assert variables["clwc"] == "unknown"
    assert variables["t"] == "K"
    assert loaded.request.metadata.extensions["rdh.units_source"] == "unspecified_in_request"
    assert any("units" in warning and "clwc" in warning for warning in loaded.warnings)


def test_units_catalog_from_real_source_is_used() -> None:
    doc = document(variables=["clwc"])
    loaded = load_request_text_detailed(
        yaml_text(doc), source="<test>", units_catalog={"clwc": "kg kg**-1"}
    )
    assert loaded.request.metadata.variables[0].units == "kg kg**-1"
    assert loaded.request.metadata.extensions["rdh.units_source"] == "caller_supplied"


def test_invalid_field_reports_file_line_and_field_path(tmp_path: Path) -> None:
    text = """request:
  name: bad_request
  preferred_sources: [ncar]
dataset:
  provider: ncar
  name: era5-pressure-levels
  product: reanalysis
variables:
  - {name: clwc, units: "kg kg**-1"}
temporal:
  start: "2011-01-01"
  end: "2010-01-01"
  frequency: hourly
spatial: {type: global}
"""
    request_path = tmp_path / "bad.yaml"
    request_path.write_text(text, encoding="utf-8")
    with pytest.raises(RequestLoadError) as excinfo:
        load_request(request_path)
    message = str(excinfo.value)
    assert f"{request_path}:" in message
    assert "temporal.end" in message
    errors = excinfo.value.detail["errors"]
    assert isinstance(errors, list)
    assert errors
    assert any("temporal.end" in item for item in errors)


def test_unknown_section_reports_allowed_names() -> None:
    doc = document()
    doc["temporal-typo"] = {"start": "2011-01-01"}
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "temporal-typo" in str(excinfo.value)
    assert "temporal" in str(excinfo.value)


def test_unknown_field_inside_section_is_rejected_with_path() -> None:
    doc = document()
    doc["temporal"] = {"start": "2011-01-01", "end": "2011-12-31", "freq": "hourly"}
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "freq" in str(excinfo.value)
    assert "temporal" in str(excinfo.value)


def test_missing_required_section_reports_chinese_error() -> None:
    doc = document()
    del doc["temporal"]
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "temporal_coverage" in str(excinfo.value) or "temporal" in str(excinfo.value)


def test_empty_variables_rejected() -> None:
    doc = document(variables=[])
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "variables" in str(excinfo.value)


def test_frequency_shorter_than_interval_is_rejected() -> None:
    doc = document(
        temporal={
            "start": "2011-01-01T00:00:00Z",
            "end": "2011-01-01T00:00:01Z",
            "frequency": "hourly",
        }
    )
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "frequency" in str(excinfo.value)


def test_frequency_interval_not_multiple_is_rejected() -> None:
    doc = document(
        temporal={
            "start": "2011-01-01T00:00:00Z",
            "end": "2011-01-01T01:30:00Z",
            "frequency": "hourly",
        }
    )
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "整数倍" in str(excinfo.value)


def test_blank_interval_rejected() -> None:
    doc = document(temporal={"start": "2011-01-01", "end": "2010-12-31", "frequency": "daily"})
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "倒置" in str(excinfo.value) or "早于" in str(excinfo.value)


def test_vertical_levels_are_sorted_descending_and_duplicates_rejected() -> None:
    doc = document(vertical={"levels": [500, 1000, 850]})
    request = load_doc(doc).request
    assert request.metadata.vertical_levels.levels == [1000.0, 850.0, 500.0]
    assert request.metadata.vertical_levels.units == "hPa"

    duplicate = document(vertical={"levels": [1000, 1000]})
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(duplicate)
    assert "重复层" in str(excinfo.value)


def test_spatial_bbox_and_global() -> None:
    bbox = document(
        spatial={"bbox": {"west": -10, "east": 10, "south": 40, "north": 60, "grid_degrees": 0.25}}
    )
    extent = load_doc(bbox).request.metadata.spatial_extent
    assert (extent.west, extent.east, extent.south, extent.north) == (-10.0, 10.0, 40.0, 60.0)
    assert extent.grid_degrees == 0.25

    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(document(spatial={"type": "regional"}))
    assert "spatial.type" in str(excinfo.value)


def test_validation_flags_map_to_checks_and_all_false_rejected() -> None:
    doc = document(validation={"verify_size": True, "netcdf_openable": True})
    checks = load_doc(doc).request.checks
    assert IntegrityCheckKind.SIZE_MATCH in checks
    assert IntegrityCheckKind.NETCDF_OPENABLE in checks
    assert IntegrityCheckKind.CHECKSUM_MATCH not in checks

    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(document(validation={"verify_size": False, "calculate_sha256": False}))
    assert "validation" in str(excinfo.value)


def test_mixed_flat_and_sectioned_rejected() -> None:
    doc = document()
    doc["metadata"] = {"product": "ERA5"}
    with pytest.raises(RequestLoadError) as excinfo:
        load_doc(doc)
    assert "混用" in str(excinfo.value)


def test_yaml_syntax_error_reports_line() -> None:
    text = "request:\n  name: x\n   bad-indent: true\n"
    with pytest.raises(RequestLoadError) as excinfo:
        load_request_text_detailed(text, source="broken.yaml")
    assert "YAML 语法错误" in str(excinfo.value)
    assert excinfo.value.detail["line"] is not None


def test_missing_file_reports_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RequestLoadError) as excinfo:
        load_request(tmp_path / "does-not-exist.yaml")
    assert "不存在" in str(excinfo.value)


def test_defaults_are_not_hardcoded_sample_parameters() -> None:
    """换一套参数后加载结果必须跟着变（防止把示例值写死）。"""
    doc = document(
        request={"name": "other_request", "preferred_sources": ["ncar"], "max_sources": 2},
        variables=[{"name": "q", "units": "kg kg**-1"}],
        temporal={"start": "2001-01-01", "end": "2001-12-31", "frequency": "daily"},
        vertical={"kind": "none"},
    )
    request = load_doc(doc).request
    assert request.request_id == "other_request"
    assert request.dataset_id == BASE_DOCUMENT["dataset"]["name"]  # type: ignore[index]
    assert request.metadata.time_definition == "daily"
    assert [spec.name for spec in request.metadata.variables] == ["q"]
    assert request.metadata.vertical_levels.kind is VerticalLevelKind.NONE
    assert request.metadata.temporal_coverage.start == datetime(2001, 1, 1, tzinfo=UTC)
