"""请求契约 v1.2 的测试（ADR-0012）。

重点覆盖三类"静默出错"风险：

1. **坐标顺序**：契约是 ``[W, S, E, N]``，CDS 是 ``[N, W, S, E]`` —— 写错不报错、只拿错区域。
2. **日期边界**：``end`` 为纯日期时必须展开到次日 00:00，否则最后一天静默丢失。
3. **时区**：带时刻且无时区的输入必须拒绝，不得猜。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import IntegrityCheckKind, VerticalLevelKind
from researchdatahub.planner.request_v12 import (
    COORDINATE_ORDER,
    REQUEST_SCHEMA_VERSION,
    AgentDataRequestV12,
    DatasetRef,
    DatasetRefError,
    ExecutionMode,
    Frequency,
    RequestContractError,
    SpatialSpec,
    SpatialSpecError,
    TemporalSpec,
    TemporalSpecError,
    describe_contract,
    parse_agent_request,
    to_cds_area,
)

GOLDEN_PAYLOAD: dict[str, object] = {
    "schema_version": "1.2",
    "request_id": "era5-clouds-2015-2024",
    "dataset": {
        "provider": "cds",
        "dataset_id": "reanalysis-era5-pressure-levels",
        "product_family": "ERA5",
    },
    "selection": {
        "variables": [
            {"name": "temperature", "units": "K", "provider_native": {"cds": "t"}},
            {
                "name": "wind_speed",
                "units": "m s**-1",
                "provider_native": {"cds": "sqrt(u**2+v**2)"},
            },
            {"name": "cloud_liquid_water", "units": "kg m**-2", "provider_native": {"cds": "clwc"}},
            {"name": "cloud_ice_water", "units": "kg m**-2", "provider_native": {"cds": "ciwc"}},
        ],
        "temporal": {"start": "2015-01-01", "end": "2024-12-31", "frequency": "hourly"},
        "spatial": {
            "type": "bounding_box",
            "bounds": [
                73,
                18,
                135,
                54,
            ],
        },
        "vertical": {"kind": "pressure", "levels": [1000.0, 850.0, 500.0]},
    },
    "output": {"format": "netcdf", "destination": "local", "path": "./data/raw"},
    "execution": {"mode": "automatic", "max_retries": 3},
    "validation": {"checksum": True, "metadata": True, "coverage": True},
}


def payload(**overrides: object) -> dict[str, object]:
    """深拷贝黄金 payload 并支持顶层覆盖（避免测试互相污染）。"""
    import copy

    data = copy.deepcopy(GOLDEN_PAYLOAD)
    for key, value in overrides.items():
        if key in {"selection", "dataset", "output"}:
            data[key] = {**data[key], **value}  # type: ignore[dict-item]
        else:
            data[key] = value
    return data


# --------------------------------------------------------------------------- #
# 契约元信息
# --------------------------------------------------------------------------- #
def test_schema_version_constant() -> None:
    assert REQUEST_SCHEMA_VERSION == "1.2"


def test_coordinate_order_is_declared_machine_readably() -> None:
    """坐标顺序必须可机读（不能只写在文档里）。"""
    assert COORDINATE_ORDER == ("west", "south", "east", "north")
    assert describe_contract()["coordinate_order"] == ["west", "south", "east", "north"]


def test_describe_contract_lists_frequencies() -> None:
    assert describe_contract()["frequency"] == [item.value for item in Frequency]
    assert describe_contract()["time_zone"] == "UTC"


# --------------------------------------------------------------------------- #
# 黄金路径
# --------------------------------------------------------------------------- #
def test_golden_request_parses() -> None:
    request = parse_agent_request(GOLDEN_PAYLOAD)
    assert request.schema_version == REQUEST_SCHEMA_VERSION
    assert request.selection.variable_names == (
        "temperature",
        "wind_speed",
        "cloud_liquid_water",
        "cloud_ice_water",
    )
    assert request.selection.spatial.bounds == (73.0, 18.0, 135.0, 54.0)
    assert request.selection.vertical.kind is VerticalLevelKind.PRESSURE


def test_golden_request_converts_to_execution_contract() -> None:
    request = parse_agent_request(GOLDEN_PAYLOAD)
    dataset_request = request.to_dataset_request()
    assert dataset_request.dataset_id == "reanalysis-era5-pressure-levels"
    assert dataset_request.preferred_sources == ["cds"]
    assert dataset_request.metadata.product == "ERA5"
    assert dataset_request.metadata.product_type == "reanalysis"
    assert dataset_request.metadata.time_definition == "utc_hourly"
    assert dataset_request.metadata.temporal_coverage.start == datetime(2015, 1, 1, tzinfo=UTC)
    assert dataset_request.metadata.temporal_coverage.end == datetime(2025, 1, 1, tzinfo=UTC)
    assert dataset_request.metadata.spatial_extent.west == 73.0
    assert dataset_request.metadata.vertical_levels.levels == [1000.0, 850.0, 500.0]
    assert dataset_request.metadata.extensions["rdh.contract_version"] == "1.2"
    assert dataset_request.metadata.extensions["rdh.coordinate_order"] == "west,south,east,north"
    assert dataset_request.metadata.extensions["rdh.output_path"] == "./data/raw"


# --------------------------------------------------------------------------- #
# 1. 坐标顺序
# --------------------------------------------------------------------------- #
def test_to_cds_area_reorders_bounds() -> None:
    """契约 [W,S,E,N] → CDS [N,W,S,E]：这是最容易静默出错的一处。"""
    spatial = SpatialSpec(bounds=(73.0, 18.0, 135.0, 54.0))
    assert to_cds_area(spatial) == (54.0, 73.0, 18.0, 135.0)


def test_to_cds_area_differs_from_bounds() -> None:
    """确保转序真的发生了（防止"实现成恒等函数"而测试恰好通过）。"""
    spatial = SpatialSpec(bounds=(73.0, 18.0, 135.0, 54.0))
    assert to_cds_area(spatial) != spatial.bounds


def test_bounds_rejects_inverted_longitudes() -> None:
    with pytest.raises(SpatialSpecError, match="west, south, east, north"):
        SpatialSpec(bounds=(135.0, 18.0, 73.0, 54.0))


def test_bounds_rejects_inverted_latitudes() -> None:
    with pytest.raises(SpatialSpecError, match="west, south, east, north"):
        SpatialSpec(bounds=(73.0, 54.0, 135.0, 18.0))


def test_bounds_rejects_out_of_range() -> None:
    with pytest.raises(SpatialSpecError):
        SpatialSpec(bounds=(-200.0, 18.0, 135.0, 54.0))
    with pytest.raises(SpatialSpecError):
        SpatialSpec(bounds=(73.0, -95.0, 135.0, 54.0))


def test_global_type_requires_full_bounds() -> None:
    ok = SpatialSpec(type="global", bounds=(-180.0, -90.0, 180.0, 90.0))
    assert ok.type == "global"  # "global" 是显式声明，不被折叠成 bounding_box
    with pytest.raises(SpatialSpecError, match="global"):
        SpatialSpec(type="global", bounds=(73.0, 18.0, 135.0, 54.0))


def test_unsupported_spatial_type_rejected() -> None:
    with pytest.raises(SpatialSpecError, match="not supported"):
        SpatialSpec(type="polygon", bounds=(73.0, 18.0, 135.0, 54.0))


# --------------------------------------------------------------------------- #
# 2. 日期边界
# --------------------------------------------------------------------------- #
def test_date_only_end_expands_to_next_day() -> None:
    """2024-12-31 作为 end 必须覆盖到 2025-01-01T00:00Z（闭开区间）。"""
    spec = TemporalSpec(start="2015-01-01", end="2024-12-31")
    assert spec.end_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert spec.start_at == datetime(2015, 1, 1, tzinfo=UTC)


def test_date_object_end_expands_too() -> None:
    spec = TemporalSpec(start=date(2020, 1, 1), end=date(2020, 1, 31))
    assert spec.end_at == datetime(2020, 2, 1, tzinfo=UTC)


def test_datetime_end_is_not_expanded() -> None:
    """带时刻的 end 按给定瞬间解释，不展开。"""
    spec = TemporalSpec(
        start="2020-01-01T00:00:00Z",
        end="2020-01-02T06:00:00Z",
    )
    assert spec.end_at == datetime(2020, 1, 2, 6, tzinfo=UTC)


def test_end_semantics_is_recorded_in_contract() -> None:
    assert describe_contract()["temporal_interval"] == "half-open [start, end)"
    assert describe_contract()["end_semantics"] == ["exclusive_next_day"]


def test_start_must_precede_end() -> None:
    with pytest.raises(TemporalSpecError, match="earlier than"):
        TemporalSpec(start="2024-01-01", end="2024-01-01")


def test_interval_shorter_than_frequency_step_rejected() -> None:
    """要逐时数据却只给了一个小时区间 → 至少有一个时间步才行。"""
    with pytest.raises(TemporalSpecError, match="shorter than one"):
        TemporalSpec(start="2024-01-01T00:00:00Z", end="2024-01-01T00:30:00Z", frequency="hourly")
    # daily 需要至少一天
    TemporalSpec(start="2024-01-01", end="2024-01-02", frequency="daily")


# --------------------------------------------------------------------------- #
# 3. 时区
# --------------------------------------------------------------------------- #
def test_non_utc_time_zone_rejected() -> None:
    with pytest.raises(TemporalSpecError, match="only UTC"):
        TemporalSpec(start="2024-01-01", end="2024-01-02", time_zone="Asia/Shanghai")


def test_utc_time_zone_accepted() -> None:
    assert TemporalSpec(start="2024-01-01", end="2024-01-02", time_zone="UTC").time_zone == "UTC"


def test_offset_datetime_normalised_to_utc() -> None:
    spec = TemporalSpec(start="2024-01-01T08:00:00+08:00", end="2024-01-02T08:00:00+08:00")
    assert spec.start_at == datetime(2024, 1, 1, 0, tzinfo=UTC)


def test_naive_datetime_rejected() -> None:
    """2024-01-01T12:00 没有时区 → 必须拒绝，不得猜。"""
    with pytest.raises(TemporalSpecError, match="ambiguous"):
        TemporalSpec(start="2024-01-01T12:00", end="2024-01-02T12:00")


def test_naive_datetime_object_rejected() -> None:
    with pytest.raises(TemporalSpecError, match="ambiguous"):
        TemporalSpec(start=datetime(2024, 1, 1, 12), end=datetime(2024, 1, 2, 12))


def test_garbage_date_rejected() -> None:
    with pytest.raises(TemporalSpecError, match="cannot parse"):
        TemporalSpec(start="not-a-date", end="2024-01-02")


# --------------------------------------------------------------------------- #
# 变量与原生映射
# --------------------------------------------------------------------------- #
def test_variable_name_is_semantic_not_native() -> None:
    request = parse_agent_request(GOLDEN_PAYLOAD)
    native = request.selection.variables[2].provider_native
    assert native == {"cds": "clwc"}
    assert request.selection.variables[2].name == "cloud_liquid_water"
    assert request.to_dataset_request().metadata.extensions["rdh.provider_native"][
        "cloud_liquid_water"
    ] == {"cds": "clwc"}


def test_variable_name_must_be_snake_case() -> None:
    with pytest.raises(RequestContractError, match="snake_case"):
        parse_agent_request(payload(selection={"variables": [{"name": "Cloud Liquid Water"}]}))


def test_duplicate_variables_rejected() -> None:
    with pytest.raises(RequestContractError, match="duplicate"):
        parse_agent_request(
            payload(
                selection={
                    "variables": [{"name": "temperature"}, {"name": "temperature"}],
                }
            )
        )


def test_empty_variables_rejected() -> None:
    with pytest.raises(RequestContractError):
        parse_agent_request(payload(selection={"variables": []}))


# --------------------------------------------------------------------------- #
# 数据集引用 / 解析
# --------------------------------------------------------------------------- #
def test_auto_dataset_requires_resolution() -> None:
    """``auto`` 不允许进入执行层（否则执行契约里的 dataset_id 会是假的）。"""
    request = parse_agent_request(
        payload(dataset={"provider": "auto", "dataset_id": "auto_discover"})
    )
    assert request.dataset.needs_discovery
    with pytest.raises(DatasetRefError, match="unresolved"):
        request.to_dataset_request()


def test_auto_dataset_resolves_when_given() -> None:
    request = parse_agent_request(
        payload(
            dataset={"provider": "auto", "dataset_id": "auto_discover", "product_family": "ERA5"}
        )
    )
    resolved = request.to_dataset_request(
        resolved_provider="cds", resolved_dataset_id="reanalysis-era5-single-levels"
    )
    assert resolved.dataset_id == "reanalysis-era5-single-levels"
    assert resolved.preferred_sources == ["cds"]


def test_product_falls_back_to_provider_when_family_missing() -> None:
    """未声明 product_family 时回退到 provider 名（执行契约要求非空，但不许编造产品族）。

    回退链：显式 ``product=`` > ``dataset.product_family`` > ``provider``。
    ``to_dataset_request`` 之外无法再猜，因此这里断言"不抛错、且 product == provider"。
    """
    import copy

    data = copy.deepcopy(GOLDEN_PAYLOAD)
    data["dataset"] = {"provider": "cds", "dataset_id": "x-1"}
    request = parse_agent_request(data)
    resolved = request.to_dataset_request()
    assert resolved.metadata.product == "cds"


def test_product_type_is_not_guessed() -> None:
    """``product_type`` 必须显式给：默认值是声明过的 ``reanalysis``，
    若调用方想记录别的类型必须传参，不允从 provider 推断。"""
    request = parse_agent_request(payload(dataset={"provider": "cds", "dataset_id": "x-1"}))
    assert (
        request.to_dataset_request(product="ERA5", product_type="reanalysis").metadata.product_type
        == "reanalysis"
    )
    assert (
        request.to_dataset_request(product="ERA5", product_type="observation").metadata.product_type
        == "observation"
    )


def test_dataset_ref_accepts_auto_aliases() -> None:
    assert DatasetRef(provider="AUTO").provider == "auto"
    assert DatasetRef(dataset_id="auto").dataset_id == "auto_discover"


def test_invalid_provider_rejected() -> None:
    with pytest.raises(DatasetRefError):
        DatasetRef(provider="CDS Provider!")


# --------------------------------------------------------------------------- #
# 输出 / 执行 / 校验
# --------------------------------------------------------------------------- #
def test_unknown_format_rejected() -> None:
    with pytest.raises(RequestContractError, match="not supported"):
        parse_agent_request(payload(output={"format": "parquet"}))


def test_netcdf4_alias_normalised() -> None:
    request = parse_agent_request(payload(output={"format": "netcdf4"}))
    assert request.output.format == "netcdf"


def test_preview_only_mode_available() -> None:
    request = parse_agent_request(payload(execution={"mode": "preview_only"}))
    assert request.execution.mode is ExecutionMode.PREVIEW_ONLY


def test_validation_flags_map_to_checks() -> None:
    request = parse_agent_request(GOLDEN_PAYLOAD)
    checks = request.selection and request.validation.to_checks()
    assert IntegrityCheckKind.SIZE_MATCH in checks
    assert IntegrityCheckKind.CHECKSUM_MATCH in checks
    assert IntegrityCheckKind.VARIABLE_PRESENT in checks
    assert IntegrityCheckKind.TIME_COVERAGE in checks


def test_validation_can_be_disabled_explicitly() -> None:
    request = parse_agent_request(payload(validation={"checksum": False, "coverage": False}))
    checks = request.validation.to_checks()
    assert IntegrityCheckKind.CHECKSUM_MATCH not in checks
    assert IntegrityCheckKind.VARIABLE_PRESENT not in checks


# --------------------------------------------------------------------------- #
# schema_version 版本守卫
# --------------------------------------------------------------------------- #
def test_wrong_schema_version_rejected() -> None:
    with pytest.raises(RequestContractError, match="schema_version"):
        parse_agent_request(payload(schema_version="1.1"))


def test_unknown_top_level_field_rejected() -> None:
    """``extra="forbid"``：未知字段必须失败，不能悄悄忽略。"""
    data = payload()
    data["analysis"] = {"do_eda": True}  # 越界功能应被契约挡住
    with pytest.raises(RequestContractError):
        parse_agent_request(data)


def test_parse_error_locates_field() -> None:
    data = payload()
    del data["selection"]  # type: ignore[misc]
    with pytest.raises(RequestContractError) as excinfo:
        parse_agent_request(data)
    errors = excinfo.value.detail["errors"]
    assert any("selection" in item["path"] for item in errors)


def test_contract_error_is_rdh_error() -> None:
    assert issubclass(RequestContractError, RdhError)


def test_request_without_request_id_synthesises_stable_id() -> None:
    data = payload()
    del data["request_id"]
    first = AgentDataRequestV12.model_validate(data).to_dataset_request()
    second = AgentDataRequestV12.model_validate(data).to_dataset_request()
    assert first.request_id == second.request_id  # 可复现
    assert len(first.request_id) <= 63


def test_round_trip_via_model_validate() -> None:
    """序列化 → 反序列化必须等价（契约可用于落库与跨进程传递）。"""
    request = parse_agent_request(GOLDEN_PAYLOAD)
    again = AgentDataRequestV12.model_validate(request.model_dump(mode="json"))
    assert again == request
