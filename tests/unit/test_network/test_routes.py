"""Route 契约、实测统计与选路（含冷启动 ``no_measurement`` 与"不按地区猜速度"）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.unit.test_network.conftest import FakeClock

from researchdatahub.network.routes.manager import NoRouteAvailable, RouteManager
from researchdatahub.network.routes.models import (
    NO_MEASUREMENT,
    ROUTE_PROTOCOLS,
    MeasurementBasis,
    Route,
    RouteRejectionCode,
    RouteStats,
)
from researchdatahub.network.routes.stats import (
    P50_BYTES_PER_SECOND,
    RouteStatsStore,
    score_route,
)
from researchdatahub.network.workers.models import (
    WorkerCapabilities,
    WorkerKind,
    WorkerRecord,
)
from researchdatahub.network.workers.registry import WorkerRegistry


def route(
    route_id: str = "local-a:ncar:https",
    *,
    worker_id: str = "local-a",
    provider_id: str = "ncar",
    protocol: str = "https",
    supports_resume: bool = True,
    max_concurrent: int = 2,
    enabled: bool = True,
    dataset_ids: list[str] | None = None,
) -> Route:
    """构造一条链路。"""
    return Route(
        route_id=route_id,
        protocol=protocol,
        worker_id=worker_id,
        provider_id=provider_id,
        supports_resume=supports_resume,
        max_concurrent=max_concurrent,
        enabled=enabled,
        dataset_ids=dataset_ids or [],
        auth_ref="env:RDH_NCAR_TOKEN",
    )


def register_worker(
    registry: WorkerRegistry,
    worker_id: str,
    *,
    protocols: list[str] | None = None,
    max_bytes: int = 0,
    region: str | None = None,
) -> WorkerRecord:
    """登记一个真实可用的本地节点。"""
    return registry.register(
        WorkerRecord(
            worker_id=worker_id,
            kind=WorkerKind.LOCAL,
            region=region,
            capabilities=WorkerCapabilities(
                protocols=protocols or [], max_bytes=max_bytes, max_concurrent=4
            ),
        )
    )


# --------------------------------------------------------------------------- #
# Route 契约
# --------------------------------------------------------------------------- #
def test_route_protocol_enum_covers_requested_set() -> None:
    assert set(ROUTE_PROTOCOLS) == {
        "https",
        "rest",
        "http_range",
        "s3",
        "ftp",
        "sftp",
        "provider_api",
        "provider_async",
        "cloud_export",
    }


def test_route_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError, match="unknown route protocol"):
        route(protocol="gopher")


def test_route_rejects_inline_secret() -> None:
    with pytest.raises(ValueError, match="never inline a secret value"):
        Route(
            route_id="r1",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
            auth_ref="sk-live-abcdef123456",
        )


def test_route_auth_ref_must_be_pointer() -> None:
    assert route().auth_ref == "env:RDH_NCAR_TOKEN"


# --------------------------------------------------------------------------- #
# 统计与打分
# --------------------------------------------------------------------------- #
def test_stats_derived_metrics_are_none_without_samples() -> None:
    stats = RouteStats(route_id="r1")
    assert stats.success_rate is None
    assert stats.failure_rate is None
    assert stats.throughput_bps is None
    assert stats.latency_ms is None


def test_score_is_none_for_cold_start() -> None:
    score, rationale = score_route(RouteStats(route_id="r1"), supports_resume=True, size_bytes=1024)
    assert score is None
    assert "no measured samples" in rationale


def test_score_rewards_success_and_throughput() -> None:
    fast = RouteStats(
        route_id="r1",
        samples=10,
        successes=10,
        transferred_bytes=10 * P50_BYTES_PER_SECOND.__int__(),
        transfer_duration_s=10.0,
        latency_ms_total=10 * 50.0,
    )
    slow = RouteStats(
        route_id="r2",
        samples=10,
        successes=10,
        transferred_bytes=1024 * 1024,
        transfer_duration_s=100.0,
        latency_ms_total=10 * 1500.0,
    )
    fast_score, _ = score_route(fast, supports_resume=True, size_bytes=100)
    slow_score, _ = score_route(slow, supports_resume=True, size_bytes=100)
    assert fast_score is not None
    assert slow_score is not None
    assert fast_score > slow_score
    flaky = RouteStats(
        route_id="r3",
        samples=10,
        successes=5,
        failures=5,
        transferred_bytes=1024 * 1024,
        transfer_duration_s=100.0,
        latency_ms_total=10 * 1500.0,
    )
    flaky_score, _ = score_route(flaky, supports_resume=True, size_bytes=100)
    assert flaky_score is not None
    assert flaky_score < slow_score


def test_score_penalises_no_resume_for_huge_files() -> None:
    stats = RouteStats(
        route_id="r1",
        samples=5,
        successes=5,
        transferred_bytes=5 * 1024**2,
        transfer_duration_s=5.0,
        latency_ms_total=5 * 100.0,
    )
    small_resumable, _ = score_route(stats, supports_resume=True, size_bytes=1024)
    small_rigid, _ = score_route(stats, supports_resume=False, size_bytes=1024)
    huge_rigid, _ = score_route(stats, supports_resume=False, size_bytes=8 * 1024**3)
    assert small_resumable > small_rigid > huge_rigid


def test_stats_store_records_real_samples(stats_store: RouteStatsStore) -> None:
    target = route()
    stats_store.record(
        target,
        success=True,
        latency_ms=120.0,
        duration_s=2.0,
        bytes_transferred=1024,
        retried=False,
        outcome="succeeded",
        provider_id="ncar",
    )
    stats_store.record(
        target,
        success=False,
        latency_ms=300.0,
        duration_s=1.0,
        bytes_transferred=0,
        retried=True,
        outcome="failed",
        provider_id="ncar",
    )
    stats = stats_store.get(target.route_id, provider_id="ncar")
    assert stats is not None
    assert (stats.samples, stats.successes, stats.failures, stats.retry_count) == (2, 1, 1, 1)
    assert stats.success_rate == 0.5
    assert stats.failure_rate == 0.5
    assert stats.transferred_bytes == 1024
    assert stats.transfer_duration_s == pytest.approx(3.0)
    assert stats.latency_ms == pytest.approx(210.0)
    assert stats.throughput_bps == pytest.approx(1024 / 3.0)


def test_stats_store_aggregates_across_providers(stats_store: RouteStatsStore) -> None:
    target = route()
    stats_store.record(
        target,
        success=True,
        latency_ms=10.0,
        duration_s=1.0,
        bytes_transferred=100,
        retried=False,
        outcome="succeeded",
        provider_id="ncar",
    )
    stats_store.record(
        target,
        success=False,
        latency_ms=20.0,
        duration_s=1.0,
        bytes_transferred=0,
        retried=False,
        outcome="failed",
        provider_id="cds",
    )
    aggregated = stats_store.get(target.route_id)
    assert aggregated is not None
    assert (aggregated.samples, aggregated.successes, aggregated.failures) == (2, 1, 1)
    assert len(stats_store.list_stats()) == 2
    assert stats_store.reset() == 2
    assert stats_store.list_stats() == []


# --------------------------------------------------------------------------- #
# 选路
# --------------------------------------------------------------------------- #
def test_select_cold_start_is_marked_no_measurement(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    register_worker(registry, "local-a")
    route_manager.add(route("local-a:ncar:https"))
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=100)
    assert selection.basis is MeasurementBasis.NO_MEASUREMENT
    chosen = selection.chosen
    assert chosen is not None
    assert chosen.score is None
    assert chosen.basis is MeasurementBasis.NO_MEASUREMENT
    assert selection.rejected == []


def test_select_prefers_measured_route_with_better_history(
    registry: WorkerRegistry, route_manager: RouteManager, stats_store: RouteStatsStore
) -> None:
    register_worker(registry, "local-a")
    register_worker(registry, "local-b")
    fast = route("local-a:ncar:https", worker_id="local-a")
    slow = route("local-b:ncar:https", worker_id="local-b")
    route_manager.add_many([slow, fast])
    for _ in range(6):
        stats_store.record(
            fast,
            success=True,
            latency_ms=20.0,
            duration_s=1.0,
            bytes_transferred=10 * 1024**2,
            retried=False,
            outcome="succeeded",
        )
        stats_store.record(
            slow,
            success=True,
            latency_ms=900.0,
            duration_s=30.0,
            bytes_transferred=1024,
            retried=False,
            outcome="succeeded",
        )
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=10**7)
    assert selection.basis is MeasurementBasis.MEASURED
    assert selection.chosen is not None
    assert selection.chosen.route.route_id == "local-a:ncar:https"
    assert selection.chosen.score is not None


def test_select_does_not_guess_by_region(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    """两条链路完全等价（无样本）时，地区标签不得影响排序——只按稳定字典序。"""
    register_worker(registry, "local-cn", region="cn-east")
    register_worker(registry, "local-us", region="us-west")
    route_manager.add_many(
        [
            route("local-us:ncar:https", worker_id="local-us"),
            route("local-cn:ncar:https", worker_id="local-cn"),
        ]
    )
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=1)
    assert [option.route.route_id for option in selection.options] == [
        "local-cn:ncar:https",
        "local-us:ncar:https",
    ]
    assert all(option.score is None for option in selection.options)


def test_select_reports_structured_rejections(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    register_worker(registry, "local-a", protocols=["https"])
    register_worker(registry, "local-b", protocols=["s3"], max_bytes=10)
    route_manager.add_many(
        [
            route("local-a:ncar:https", worker_id="local-a"),
            route("local-a:ncar:s3", worker_id="local-a", protocol="s3"),
            route("local-b:ncar:https", worker_id="local-b"),
            route("local-b:ncar:s3", worker_id="local-b", protocol="s3"),
            route("local-a:cds:https", worker_id="local-a", provider_id="cds"),
            route("off:ncar:https", worker_id="off"),
            route("a:era5-only", worker_id="local-a", dataset_ids=["cmip6"]),
            route("a:disabled", worker_id="local-a", enabled=False),
        ]
    )
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=100)
    assert [option.route.route_id for option in selection.options] == ["local-a:ncar:https"]
    codes = {item.route_id: item.code for item in selection.rejected}
    assert codes["local-a:ncar:s3"] is RouteRejectionCode.PROTOCOL_NOT_SUPPORTED_BY_WORKER
    assert codes["local-b:ncar:https"] is RouteRejectionCode.PROTOCOL_NOT_SUPPORTED_BY_WORKER
    assert codes["local-b:ncar:s3"] is RouteRejectionCode.SIZE_EXCEEDS_LIMIT
    assert codes["local-a:cds:https"] is RouteRejectionCode.PROVIDER_MISMATCH
    assert codes["off:ncar:https"] is RouteRejectionCode.WORKER_OFFLINE
    assert codes["a:era5-only"] is RouteRejectionCode.DATASET_MISMATCH
    assert codes["a:disabled"] is RouteRejectionCode.DISABLED


def test_select_raises_no_route_available_with_reasons(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    register_worker(registry, "local-a", protocols=["s3"])
    route_manager.add(route("local-a:ncar:https"))
    with pytest.raises(NoRouteAvailable) as excinfo:
        route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=10)
    error = excinfo.value
    assert error.code == "RDH_NO_ROUTE_AVAILABLE"
    payload = error.as_dict()
    assert payload["error"] == "NoRouteAvailable"
    assert error.rejected
    assert error.rejected[0].code is (RouteRejectionCode.PROTOCOL_NOT_SUPPORTED_BY_WORKER)


def test_select_skips_excluded_routes(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    register_worker(registry, "local-a")
    register_worker(registry, "local-b")
    route_manager.add_many(
        [
            route("local-a:ncar:https", worker_id="local-a"),
            route("local-b:ncar:https", worker_id="local-b"),
        ]
    )
    selection = route_manager.select(
        provider_id="ncar",
        dataset_id="era5",
        size_bytes=1,
        exclude=frozenset({"local-a:ncar:https"}),
    )
    assert [option.route.route_id for option in selection.options] == ["local-b:ncar:https"]


def test_degraded_worker_ranks_below_healthy(
    registry: WorkerRegistry, route_manager: RouteManager, stats_store: RouteStatsStore
) -> None:
    register_worker(registry, "local-a")
    register_worker(registry, "local-b")
    good = route("local-b:ncar:https", worker_id="local-b")
    degraded = route("local-a:ncar:https", worker_id="local-a")
    route_manager.add_many([degraded, good])
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    for _ in range(4):
        stats_store.record(
            good,
            success=True,
            latency_ms=100.0,
            duration_s=1.0,
            bytes_transferred=1024**2,
            retried=False,
            outcome="succeeded",
            at=now,
        )
    # degraded 节点反而"历史更快"，但健康度要压过速度
    for _ in range(4):
        stats_store.record(
            degraded,
            success=True,
            latency_ms=10.0,
            duration_s=0.5,
            bytes_transferred=1024**2,
            retried=False,
            outcome="succeeded",
            at=now,
        )
    for _ in range(3):
        registry.record_outcome("local-a", ok=False)
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=1024)
    assert selection.chosen is not None
    assert selection.chosen.route.route_id == "local-b:ncar:https"
    scaled = next(item for item in selection.options if item.route.route_id == "local-a:ncar:https")
    assert scaled.worker_status == "degraded"
    assert scaled.rationale.endswith("(degraded worker: score scaled)")
    strict = route_manager.select(
        provider_id="ncar", dataset_id="era5", size_bytes=1024, include_degraded=False
    )
    assert [option.route.route_id for option in strict.options] == ["local-b:ncar:https"]


def test_concurrency_budget_is_enforced(
    registry: WorkerRegistry, route_manager: RouteManager
) -> None:
    register_worker(registry, "local-a")
    route_manager.add(route("local-a:ncar:https", max_concurrent=1))
    route_manager.acquire("local-a:ncar:https")
    with pytest.raises(NoRouteAvailable) as excinfo:
        route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=1)
    assert excinfo.value.rejected[0].code is RouteRejectionCode.CONCURRENCY_EXHAUSTED
    route_manager.release("local-a:ncar:https")
    route_manager.release("local-a:ncar:https")
    selection = route_manager.select(provider_id="ncar", dataset_id="era5", size_bytes=1)
    assert selection.chosen is not None


def test_record_attempt_writes_real_sample_and_audit(
    registry: WorkerRegistry,
    route_manager: RouteManager,
    stats_store: RouteStatsStore,
) -> None:
    register_worker(registry, "local-a")
    target = route_manager.add(route("local-a:ncar:https"))
    record = route_manager.record_attempt(
        target,
        success=True,
        latency_ms=42.0,
        duration_s=1.5,
        bytes_transferred=4096,
        retried=False,
        outcome="succeeded",
    )
    assert record.route_id == "local-a:ncar:https"
    assert record.stats.samples == 1
    refreshed = route_manager.stats_for("local-a:ncar:https", provider_id="ncar")
    assert refreshed.success_rate == 1.0
    assert refreshed.throughput_bps == pytest.approx(4096 / 1.5)
    events = registry.events("local-a")
    assert ("route_sample", events[-1][1]) in events


def test_route_status_reports_workers_and_measurements(
    registry: WorkerRegistry, route_manager: RouteManager, clock: FakeClock
) -> None:
    register_worker(registry, "local-a")
    route_manager.add(route("local-a:ncar:https"))
    status = route_manager.status()
    entries = status["routes"]
    assert isinstance(entries, list)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, dict)
    assert entry["measurement"] == NO_MEASUREMENT
    assert entry["worker_status"] == "healthy"
    assert entry["stats"]["samples"] == 0  # type: ignore[index]
    clock.advance(600)
    assert route_manager.status()["routes"][0]["worker_status"] == "offline"  # type: ignore[index]
