"""核心数据模型测试：合法构造、边界、必填、类型错误。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError
from tests.conftest import (
    FIXTURES,
    make_asset,
    make_file_record,
    make_integrity_report,
    make_metadata,
    make_source_metadata,
    make_transfer_result,
)

from researchdatahub.core.models import (
    MODELS_VERSION,
    AcquisitionPlan,
    AcquisitionTask,
    Checksum,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
    FileRecord,
    GridSpec,
    IntegrityReport,
    IntegrityStatus,
    Priority,
    SourceAsset,
    SourceMetadata,
    SpatialExtent,
    TemporalExtent,
    TransferResult,
    TransferStatus,
    VariableSpec,
    VerticalLevelKind,
    VerticalLevels,
)

MODEL_CLASSES = [
    DatasetRequest,
    SourceAsset,
    AcquisitionPlan,
    AcquisitionTask,
    TransferResult,
    FileRecord,
    IntegrityReport,
    DeliveryManifest,
    SourceMetadata,
    DatasetMetadata,
]


def test_all_ten_models_are_frozen() -> None:
    """十个契约模型都必须不可变（本轮任务书 §5.2）。"""
    assert len(MODEL_CLASSES) == 10
    for cls in MODEL_CLASSES:
        assert cls.model_config["frozen"] is True, cls.__name__
        assert cls.model_config["extra"] == "forbid", cls.__name__


def test_every_model_carries_schema_version() -> None:
    """模型版本管理：每个模型都能读取 schema_version 且默认 1.0。"""
    for cls in MODEL_CLASSES:
        assert "schema_version" in cls.model_fields, cls.__name__
        assert cls.model_fields["schema_version"].default == MODELS_VERSION


def test_forward_compatibility_requires_migration() -> None:
    """未知 schema_version 必须拒绝，而不是静默接受。"""
    with pytest.raises(ValidationError, match="schema_version"):
        make_metadata(schema_version="2.0")


# --------------------------------------------------------------------------- #
# 合法构造
# --------------------------------------------------------------------------- #
def test_dataset_metadata_from_fixture(metadata: DatasetMetadata) -> None:
    assert metadata.product == "ERA5"
    assert [v.name for v in metadata.variables] == ["clwc", "ciwc", "t", "q"]
    assert metadata.vertical_levels.kind is VerticalLevelKind.PRESSURE
    assert metadata.vertical_levels.levels == [1000.0, 975.0, 950.0, 925.0, 900.0]


def test_dataset_request_required_variables(request_: DatasetRequest) -> None:
    assert request_.required_variables() == frozenset({"clwc", "ciwc", "t", "q"})
    assert request_.preferred_sources == ["ncar", "cds"]
    assert request_.preserve_original is True


def test_era5_example_yaml_parses_to_dataset_request() -> None:
    """examples/era5-request.yaml 必须能被 DatasetRequest 直接校验（任务书 §6）。"""
    path = FIXTURES.parents[1] / "examples" / "era5-request.yaml"
    if not path.exists():
        pytest.fail(f"缺少任务书要求的示例文件: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    req = DatasetRequest.model_validate(payload)
    meta = req.metadata
    assert meta.product == "ERA5"
    assert meta.product_type == "reanalysis"
    assert {v.name for v in meta.variables} == {"clwc", "ciwc", "t", "q"}
    assert meta.temporal_coverage.start == datetime(2011, 1, 1, tzinfo=UTC)
    assert meta.temporal_coverage.end.year == 2021
    assert meta.spatial_extent.is_global
    assert meta.vertical_levels.kind is VerticalLevelKind.PRESSURE
    assert 1000.0 in meta.vertical_levels.levels
    assert set(req.preferred_sources) >= {"ncar", "cds"}
    assert req.preserve_original is True


def test_asset_and_task_construction(asset: SourceAsset, task: AcquisitionTask) -> None:
    assert asset.size_bytes == 536870912
    assert task.state.value == "PENDING"
    assert task.attempt == 0


def test_plan_requires_matching_plan_id(task: AcquisitionTask) -> None:
    plan = AcquisitionPlan(
        plan_id="plan-0001",
        request_id=task.request_id,
        tasks=[task],
        generated_at=datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert plan.task_ids() == ["task-0001"]

    with pytest.raises(ValidationError, match="belongs to plan"):
        AcquisitionPlan(
            plan_id="plan-9999",
            request_id=task.request_id,
            tasks=[task],
            generated_at=datetime(2026, 11, 4, tzinfo=UTC),
        )


def test_priority_rank_ordering() -> None:
    ranks = [Priority.LOW.rank, Priority.NORMAL.rank, Priority.HIGH.rank, Priority.CRITICAL.rank]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 4


# --------------------------------------------------------------------------- #
# 边界与约束
# --------------------------------------------------------------------------- #
def test_global_extent_detection() -> None:
    assert SpatialExtent(west=-180, east=180, south=-90, north=90).is_global is True
    assert SpatialExtent(west=0, east=1, south=0, north=1).is_global is False


def test_temporal_duration_days() -> None:
    extent = TemporalExtent(
        start=datetime(2011, 1, 1, tzinfo=UTC), end=datetime(2011, 1, 11, tzinfo=UTC)
    )
    assert extent.duration_days() == pytest.approx(10.0)


def test_timezone_is_normalized_to_utc() -> None:
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=8))
    extent = TemporalExtent(
        start=datetime(2020, 1, 1, 8, tzinfo=tz), end=datetime(2020, 1, 2, 8, tzinfo=tz)
    )
    assert extent.start.tzinfo is UTC
    assert extent.start.hour == 0


def test_zero_byte_file_is_legal_but_boundary() -> None:
    record = make_file_record(size_bytes=0)
    assert record.size_bytes == 0


def test_checksum_algorithm_and_digest() -> None:
    checksum = Checksum(value="sha256:" + "f" * 64)
    assert checksum.algorithm == "sha256"
    assert checksum.hexdigest == "f" * 64


def test_scientific_identity_separates_units() -> None:
    """同名变量、不同单位 → 科学身份不同（数据契约的核心断言）。"""
    kelvin = make_metadata()
    celsius = make_metadata(
        variables=[
            VariableSpec(name="clwc", units="kg kg**-1"),
            VariableSpec(name="ciwc", units="kg kg**-1"),
            VariableSpec(name="t", units="degC"),
            VariableSpec(name="q", units="kg kg**-1"),
        ]
    )
    assert kelvin.scientific_identity() != celsius.scientific_identity()


def test_scientific_identity_separates_vertical_levels() -> None:
    a = make_metadata()
    b = make_metadata(vertical_levels={"kind": "pressure", "levels": [500.0], "units": "hPa"})
    assert a.scientific_identity() != b.scientific_identity()


def test_scientific_identity_stable_for_equal_metadata() -> None:
    assert make_metadata().scientific_identity() == make_metadata().scientific_identity()


def test_integrity_report_permits_completion_semantics() -> None:
    passed = make_integrity_report()
    assert passed.permits_completion is True
    unknown = make_integrity_report(status=IntegrityStatus.UNKNOWN)
    assert unknown.permits_completion is False, "UNKNOWN 不得被视为通过"


def test_transfer_result_requires_path_on_success() -> None:
    ok = make_transfer_result()
    assert ok.status is TransferStatus.SUCCEEDED


def test_delivery_manifest_with_totals() -> None:
    record = make_file_record()
    manifest = DeliveryManifest(
        delivery_id="delivery-0001",
        request_id="req-era5-clwc-2011-2020",
        plan_id="plan-0001",
        dataset_metadata=make_metadata(),
        files=[record],
        integrity=[make_integrity_report()],
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        tool_version="0.1.0",
    )
    completed = manifest.with_totals()
    assert completed.total_bytes == record.size_bytes
    assert completed.complete is False  # 需要显式确认


def test_asset_rejects_inconsistent_metadata(asset: SourceAsset) -> None:
    """asset.source_id 与其 metadata.source_id 不一致时必须拒绝（来源追踪完整性）。"""
    meta = make_source_metadata(source_id="cds", origin_uri="https://cds.example/x")
    with pytest.raises(ValidationError, match="source_id"):
        make_asset(metadata=meta, source_id="ncar")


def test_asset_rejects_inconsistent_origin_uri(asset: SourceAsset) -> None:
    """origin_uri 与 metadata.origin_uri 分叉会让来源追踪失真。"""
    meta = make_source_metadata()
    with pytest.raises(ValidationError, match="origin_uri"):
        make_asset(metadata=meta, origin_uri="s3://other-bucket/elsewhere/file.nc")


def test_grid_spec_optional_but_typed() -> None:
    assert GridSpec().projection == "regular_latlon"
    with pytest.raises(ValidationError):
        GridSpec(resolution_degrees=-1)


def test_vertical_levels_none_kind_rejects_levels() -> None:
    with pytest.raises(ValidationError, match="none"):
        VerticalLevels(kind=VerticalLevelKind.NONE, levels=[1000.0])
