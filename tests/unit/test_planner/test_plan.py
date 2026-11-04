"""计划器测试：源匹配顺序、资源发现、拆分（file/year/month/request）、去重、
未满足变量登记、计划规模上限、计划可序列化/可持久化。

数据源用协议形状的 :class:`support.FakeAdapter`（真实返回 ``SourceAsset``），
计划器本身不下载、不访问网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.test_planner.support import (
    FIXED_NOW,
    FakeAdapter,
    document,
    load_doc,
    make_asset,
)

from researchdatahub.config.models import PlannerConfig, RetryConfig
from researchdatahub.core.errors import MetadataError, ScheduleError, SourceNotFoundError
from researchdatahub.core.models import (
    AcquisitionPlan,
    Priority,
    SourceAsset,
    TemporalExtent,
)
from researchdatahub.core.state import TaskState
from researchdatahub.planner.planner import DefaultTaskPlanner

PLANNER_SOURCE = Path(__file__).resolve().parents[3] / "src" / "researchdatahub" / "planner"


def _planner(**config: object) -> DefaultTaskPlanner:
    return DefaultTaskPlanner(
        PlannerConfig(**config),  # type: ignore[arg-type]
        retry=RetryConfig(max_attempts=4),
        clock=lambda: FIXED_NOW,
    )


def _request(**overrides: object):
    doc = document(**overrides)
    return load_doc(doc).request


def _span(start: str, end: str) -> TemporalExtent:
    from datetime import UTC, datetime

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)

    return TemporalExtent(start=parse(start), end=parse(end))


# --------------------------------------------------------------------------- #
# 源匹配 / 资源发现
# --------------------------------------------------------------------------- #
def test_preferred_source_order_wins_and_max_sources_limits_attempts() -> None:
    request = _request(
        request={"name": "order_test", "preferred_sources": ["cds", "ncar"], "max_sources": 1}
    )
    ncar = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc", "t"])])
    cds = FakeAdapter("cds", assets=[make_asset(source_id="cds", variables=["clwc", "t"])])
    plan = _planner().plan(request, [ncar, cds])
    assert ncar.calls == []
    assert len(cds.calls) == 1
    assert [task.asset.source_id for task in plan.tasks] == ["cds"]
    statuses = {
        item["source_id"]: item["status"] for item in plan.extensions["planner.discovery_attempts"]
    }
    assert statuses == {"cds": "used", "ncar": "not_attempted"}


def test_partial_coverage_uses_second_source_and_fills_variables() -> None:
    request = _request(
        request={"name": "partial_cover", "preferred_sources": ["ncar", "cds"], "max_sources": 2},
        variables=[{"name": "clwc", "units": "x"}, {"name": "t", "units": "K"}],
    )
    ncar = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc"])])
    cds = FakeAdapter(
        "cds", assets=[make_asset(source_id="cds", variables=["t"], file_name="cds.nc")]
    )
    plan = _planner().plan(request, [ncar, cds])
    assert {task.asset.source_id for task in plan.tasks} == {"ncar", "cds"}
    assert plan.unmatched_variables == []
    assert plan.extensions["planner.variable_coverage"] == {
        "ncar": "declared",
        "cds": "declared",
    }


def test_unmatched_variables_come_from_real_discovery() -> None:
    request = _request(
        request={"name": "unmatched", "preferred_sources": ["ncar"], "max_sources": 1},
        variables=[{"name": "clwc", "units": "x"}, {"name": "t", "units": "K"}],
    )
    ncar = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc"])])
    plan = _planner(allow_partial=True).plan(request, [ncar])
    assert plan.unmatched_variables == ["t"]
    assert plan.extensions["planner.unmatched_variables"] == ["t"]
    assert plan.extensions["planner.requested_variables"] == ["clwc", "t"]


def test_allow_partial_false_rejects_incomplete_coverage() -> None:
    request = _request(variables=[{"name": "clwc", "units": "x"}, {"name": "q", "units": "y"}])
    ncar = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc"])])
    with pytest.raises(SourceNotFoundError) as excinfo:
        _planner(allow_partial=False).plan(request, [ncar])
    assert "q" in str(excinfo.value)


def test_no_usable_source_raises_with_attempt_reasons() -> None:
    request = _request()
    ncar = FakeAdapter("ncar", error=SourceNotFoundError("no such dataset", source_id="ncar"))
    cds = FakeAdapter("cds", error=MetadataError("bad metadata", source_id="cds"))
    with pytest.raises(SourceNotFoundError) as excinfo:
        _planner().plan(request, [ncar, cds])
    attempts = excinfo.value.detail["attempts"]
    assert [item["status"] for item in attempts] == ["metadata_error", "not_found"]


def test_empty_adapter_list_rejected() -> None:
    with pytest.raises(SourceNotFoundError):
        _planner().plan(_request(), [])


def test_adapter_returning_no_asset_is_skipped() -> None:
    request = _request(
        request={"name": "empty_source", "preferred_sources": ["ncar", "cds"], "max_sources": 2}
    )
    ncar = FakeAdapter("ncar", assets=[])
    cds = FakeAdapter("cds", assets=[make_asset(source_id="cds", variables=["clwc", "t"])])
    plan = _planner().plan(request, [ncar, cds])
    statuses = {
        item["source_id"]: item["status"] for item in plan.extensions["planner.discovery_attempts"]
    }
    assert statuses["ncar"] == "empty"
    assert statuses["cds"] == "used"


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
def test_duplicate_content_across_sources_is_deduped_keeping_preferred() -> None:
    request = _request(
        request={"name": "dedupe", "preferred_sources": ["ncar", "cds"], "max_sources": 2},
        variables=[{"name": "clwc", "units": "x"}, {"name": "t", "units": "K"}],
    )
    common = _span("2011-01-01T00:00:00", "2012-01-01T00:00:00")
    ncar_asset = make_asset(source_id="ncar", variables=["clwc"], time_slice=common)
    cds_asset = make_asset(source_id="cds", variables=["t"], time_slice=common)
    plan = _planner().plan(
        request, [FakeAdapter("ncar", assets=[ncar_asset]), FakeAdapter("cds", assets=[cds_asset])]
    )
    assert len(plan.tasks) == 1
    assert plan.tasks[0].asset.source_id == "ncar"
    duplicates = plan.extensions["planner.duplicates"]
    assert isinstance(duplicates, list)
    assert len(duplicates) == 1
    assert duplicates[0]["source_id"] == "cds"
    assert plan.extensions["planner.assets_deduped"] == 1


# --------------------------------------------------------------------------- #
# 任务构造
# --------------------------------------------------------------------------- #
def test_tasks_start_planned_and_carry_asset_priority_attempts() -> None:
    request = _request(
        request={
            "name": "task_fields",
            "preferred_sources": ["ncar"],
            "max_sources": 1,
            "priority": "high",
        }
    )
    ncar = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc", "t"])])
    plan = _planner().plan(request, [ncar])
    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.state is TaskState.PLANNED
    assert task.priority is Priority.HIGH
    assert task.max_attempts == 4
    assert task.asset.source_id == "ncar"
    assert task.scheduled_at is None
    assert task.plan_id == plan.plan_id
    assert task.request_id == request.request_id
    # 初始 PLANNED 由真实状态机推进得到（PENDING -> PLANNED）。
    assert any("PENDING->PLANNED" in note for note in task.notes)


def test_static_file_source_is_not_split() -> None:
    request = _request(
        variables=[{"name": "clwc", "units": "x"}],
        temporal={"start": "2001-01-01", "end": "2011-12-31", "frequency": "daily"},
    )
    long_asset = make_asset(source_id="ncar", variables=["clwc"], time_slice=None)
    plan = _planner().plan(request, [FakeAdapter("ncar", assets=[long_asset])])
    assert len(plan.tasks) == 1
    assert plan.extensions["planner.split_counts"] == [1]


# --------------------------------------------------------------------------- #
# 服务端生成型数据源（CDS）：拆分与请求生命周期
# --------------------------------------------------------------------------- #
def _cds_asset(*, start: str, end: str, asset_id: str = "asset-cds-era5") -> SourceAsset:
    from researchdatahub.core.models import AccessRoute

    span = _span(start, end)
    asset = make_asset(
        source_id="cds",
        file_name="cds-result.nc",
        route=AccessRoute.CDS_API,
        size_bytes=0,
        time_slice=span,
        variables=["clwc", "t"],
        asset_id=asset_id,
    )
    return asset


def test_generated_source_splits_by_year_and_keeps_request_lifecycle() -> None:
    request = _request(
        request={"name": "cds_split", "preferred_sources": ["cds"], "max_sources": 1},
        variables=[{"name": "clwc", "units": "x"}, {"name": "t", "units": "K"}],
    )
    cds = FakeAdapter(
        "cds", assets=[_cds_asset(start="2011-01-01T00:00:00", end="2014-01-01T00:00:00")]
    )
    planner = DefaultTaskPlanner(
        PlannerConfig(preferred_split="auto"),
        retry=RetryConfig(max_attempts=3),
        clock=lambda: FIXED_NOW,
        split_hints={"cds": "year"},
    )
    plan = planner.plan(request, [cds])
    assert len(plan.tasks) == 3
    windows = [
        (task.asset.time_slice.start.year, task.asset.time_slice.end.year)
        for task in plan.tasks
        if task.asset.time_slice is not None
    ]
    assert windows == [(2011, 2012), (2012, 2013), (2013, 2014)]
    for task in plan.tasks:
        asset = task.asset
        assert asset.origin_uri.startswith("cds://")
        assert not asset.origin_uri.startswith("http")
        assert asset.size_bytes == 0
        extensions = asset.metadata.extensions
        lifecycle = extensions["cds.request_lifecycle"]
        assert isinstance(lifecycle, dict)
        assert lifecycle["mode"] == "submit_poll_result"
        assert extensions["planner.size_bytes_unknown"] is True
        assert extensions["planner.split_granularity"] == "year"
    assert len({task.asset.asset_id for task in plan.tasks}) == 3


def test_generated_source_request_granularity_for_short_span() -> None:
    request = _request(
        request={"name": "cds_single", "preferred_sources": ["cds"], "max_sources": 1},
        variables=[{"name": "clwc", "units": "x"}],
    )
    cds = FakeAdapter(
        "cds", assets=[_cds_asset(start="2011-01-01T00:00:00", end="2011-12-31T00:00:00")]
    )
    plan = _planner().plan(request, [cds])
    assert len(plan.tasks) == 1
    assert plan.extensions["planner.split_counts"] == [1]
    assert plan.tasks[0].asset.origin_uri.startswith("cds://")


def test_max_tasks_per_plan_enforced() -> None:
    request = _request(
        request={"name": "too_big", "preferred_sources": ["cds"], "max_sources": 1},
        variables=[{"name": "clwc", "units": "x"}],
    )
    cds = FakeAdapter(
        "cds", assets=[_cds_asset(start="2001-01-01T00:00:00", end="2011-01-01T00:00:00")]
    )
    planner = DefaultTaskPlanner(
        PlannerConfig(max_tasks_per_plan=2),
        retry=RetryConfig(),
        clock=lambda: FIXED_NOW,
        split_hints={"cds": "year"},
    )
    with pytest.raises(ScheduleError) as excinfo:
        planner.plan(request, [cds])
    assert "max_tasks_per_plan" in str(excinfo.value)
    assert excinfo.value.detail["max_tasks_per_plan"] == 2


def test_auto_split_rejects_span_beyond_limit() -> None:
    request = _request(
        request={"name": "auto_big", "preferred_sources": ["cds"], "max_sources": 1},
        variables=[{"name": "clwc", "units": "x"}],
    )
    cds = FakeAdapter(
        "cds", assets=[_cds_asset(start="1991-01-01T00:00:00", end="2021-01-01T00:00:00")]
    )
    planner = DefaultTaskPlanner(PlannerConfig(max_tasks_per_plan=10), clock=lambda: FIXED_NOW)
    with pytest.raises(ScheduleError):
        planner.plan(request, [cds])


# --------------------------------------------------------------------------- #
# 计划标识 / 序列化
# --------------------------------------------------------------------------- #
def test_plan_id_and_task_ids_are_deterministic_and_serializable() -> None:
    request = _request(
        request={"name": "determinism", "preferred_sources": ["ncar"], "max_sources": 1}
    )
    adapter = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc", "t"])])
    first = _planner().plan(request, [adapter])
    second = _planner().plan(
        request,
        [FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc", "t"])])],
    )
    assert first.plan_id == second.plan_id
    assert first.task_ids() == second.task_ids()
    payload = first.model_dump(mode="json")
    reloaded = AcquisitionPlan.model_validate(payload)
    assert reloaded.plan_id == first.plan_id
    assert reloaded.task_ids() == first.task_ids()
    assert reloaded.extensions["planner.version"] == "m4"


def test_planner_package_never_imports_transport_or_http() -> None:
    """证明"计划器绝不下下载"是结构性的，而不是口头承诺。"""
    forbidden = (
        "researchdatahub.transfer",
        "import httpx",
        "import requests",
        "from requests",
        "import boto3",
    )
    for path in sorted(PLANNER_SOURCE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} must not reference {needle}"
