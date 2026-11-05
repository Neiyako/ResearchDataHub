"""Worker 契约与持久化注册表（含真实健康判定与进程重启恢复）。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.test_network.conftest import FakeClock

from researchdatahub.network.workers.models import (
    RemoteTaskRequest,
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
    WorkerRecord,
    WorkerStatus,
    WorkerSummary,
)
from researchdatahub.network.workers.registry import (
    WorkerRegistrationError,
    WorkerRegistry,
    WorkerUnknownError,
)
from researchdatahub.network.workers.store import WORKERS_SCHEMA_VERSION, WorkerStore


def local_record(
    worker_id: str = "local-a",
    *,
    datasets: list[str] | None = None,
    providers: list[str] | None = None,
    protocols: list[str] | None = None,
    max_bytes: int = 0,
    max_concurrent: int = 2,
    region: str | None = None,
) -> WorkerRecord:
    """构造一个本地节点声明。"""
    return WorkerRecord(
        worker_id=worker_id,
        kind=WorkerKind.LOCAL,
        region=region,
        capabilities=WorkerCapabilities(
            datasets=datasets or [],
            providers=providers or [],
            protocols=protocols or [],
            max_bytes=max_bytes,
            max_concurrent=max_concurrent,
        ),
    )


# --------------------------------------------------------------------------- #
# 契约模型
# --------------------------------------------------------------------------- #
def test_capabilities_allowlist_is_a_hard_filter() -> None:
    caps = WorkerCapabilities(
        datasets=["era5"], providers=["ncar"], protocols=["https"], max_bytes=100, max_concurrent=2
    )
    assert caps.allows(dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=100) == (
        True,
        None,
    )
    ok, reason = caps.allows(dataset_id="cmip6", provider_id="ncar", protocol="https", size_bytes=1)
    assert ok is False
    assert reason is not None
    assert "dataset" in reason
    ok, reason = caps.allows(dataset_id="era5", provider_id="cds", protocol="https", size_bytes=1)
    assert ok is False
    assert reason is not None
    assert "provider" in reason
    ok, reason = caps.allows(dataset_id="era5", provider_id="ncar", protocol="s3", size_bytes=1)
    assert ok is False
    assert reason is not None
    assert "protocol" in reason
    ok, reason = caps.allows(
        dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=101
    )
    assert ok is False
    assert reason is not None
    assert "max_bytes" in reason


def test_empty_allow_lists_mean_unrestricted() -> None:
    caps = WorkerCapabilities()
    assert caps.allows(
        dataset_id="anything", provider_id="whoever", protocol="sftp", size_bytes=10**15
    ) == (True, None)


def test_worker_record_rejects_local_with_endpoint_and_remote_without() -> None:
    with pytest.raises(ValueError, match="local worker must not declare an endpoint"):
        WorkerRecord(worker_id="local-a", kind=WorkerKind.LOCAL, endpoint="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="must declare an endpoint"):
        WorkerRecord(worker_id="remote-a", kind=WorkerKind.REMOTE)
    record = WorkerRecord(
        worker_id="remote-a", kind=WorkerKind.REMOTE, endpoint="http://127.0.0.1:8765/"
    )
    assert record.endpoint == "http://127.0.0.1:8765"


def test_offline_health_cannot_claim_uptime() -> None:
    with pytest.raises(ValueError, match="cannot report a non-zero uptime"):
        WorkerHealth(status=WorkerStatus.OFFLINE, uptime_s=12.0)
    assert WorkerHealth(status=WorkerStatus.OFFLINE).uptime_s is None


def test_remote_task_request_rejects_escaping_target() -> None:
    base = {
        "task_id": "task-0001",
        "asset_id": "asset-0001",
        "dataset_id": "era5-pressure-levels",
        "source_id": "ncar",
        "provider_id": "ncar",
        "access_route": "https",
        "origin_uri": "http://127.0.0.1:1/f.nc",
        "file_name": "f.nc",
        "size_bytes": 10,
    }
    with pytest.raises(ValueError, match="must not contain"):
        RemoteTaskRequest(**base, target_relative_path="../escape/f.nc")
    with pytest.raises(ValueError, match="must be relative"):
        RemoteTaskRequest(**base, target_relative_path="/abs/f.nc")
    # 规范化成合法相对路径的写法也要拒绝：远程下发只在"干净路径"上做审计。
    with pytest.raises(ValueError, match="must not contain"):
        RemoteTaskRequest(**base, target_relative_path="./a/../b/./f.nc")
    request = RemoteTaskRequest(**base, target_relative_path="./b/./f.nc")
    assert request.target_relative_path == "b/f.nc"


def test_worker_summary_keeps_decision_fields() -> None:
    record = local_record("local-a", datasets=["era5"], max_bytes=1024)
    summary = WorkerSummary.of(record)
    assert summary.worker_id == "local-a"
    assert summary.capabilities.max_bytes == 1024
    assert summary.capabilities.datasets == ["era5"]
    assert summary.status is WorkerStatus.HEALTHY


# --------------------------------------------------------------------------- #
# 控制面库
# --------------------------------------------------------------------------- #
def test_store_creates_schema_and_is_idempotent(workers_db: Path) -> None:
    store = WorkerStore(workers_db)
    assert store.initialize() == WORKERS_SCHEMA_VERSION
    assert store.initialize() == WORKERS_SCHEMA_VERSION
    connection = sqlite3.connect(workers_db)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {"workers", "worker_events", "route_stats", "worker_schema_version"} <= tables


def test_store_uses_wal(store: WorkerStore) -> None:
    mode = store.read(lambda connection: connection.execute("PRAGMA journal_mode").fetchone()[0])
    assert str(mode).lower() == "wal"


# --------------------------------------------------------------------------- #
# 注册 / 心跳 / 健康
# --------------------------------------------------------------------------- #
def test_register_records_real_heartbeat(registry: WorkerRegistry, clock: FakeClock) -> None:
    record = registry.register(local_record())
    assert record.health.status is WorkerStatus.HEALTHY
    assert record.health.last_heartbeat == clock.now()
    assert record.health.uptime_s == 0.0
    assert record.registered_at == clock.now()
    assert [event for event, _ in registry.events("local-a")] == ["registered"]


def test_heartbeat_refreshes_uptime(registry: WorkerRegistry, clock: FakeClock) -> None:
    registry.register(local_record())
    clock.advance(42)
    record = registry.heartbeat("local-a")
    assert record.health.last_heartbeat == clock.now()
    assert record.health.uptime_s == pytest.approx(42.0)
    assert [event for event, _ in registry.events("local-a")] == ["registered", "heartbeat"]


def test_heartbeat_of_unknown_worker_fails(registry: WorkerRegistry) -> None:
    with pytest.raises(WorkerUnknownError):
        registry.heartbeat("ghost")


def test_health_degrades_then_goes_offline_from_real_heartbeat_gap(
    registry: WorkerRegistry, clock: FakeClock
) -> None:
    registry.register(local_record())
    clock.advance(registry.heartbeat_timeout_s + 5)
    assert registry.get("local-a").health.status is WorkerStatus.DEGRADED
    clock.advance(registry.offline_after_s)
    offline = registry.get("local-a")
    assert offline.health.status is WorkerStatus.OFFLINE
    # 离线判定写回库并留痕（重启后看到的是同一个判定）
    events = [event for event, _ in registry.events("local-a")]
    assert events.count("marked_offline") == 1
    raw = registry.store.worker_row("local-a")
    assert raw is not None
    assert str(raw["status"]) == "offline"


def test_heartbeat_brings_offline_worker_back(registry: WorkerRegistry, clock: FakeClock) -> None:
    registry.register(local_record())
    clock.advance(registry.offline_after_s + 1)
    assert registry.get("local-a").health.status is WorkerStatus.OFFLINE
    revived = registry.heartbeat("local-a")
    assert revived.health.status is WorkerStatus.HEALTHY
    assert registry.get("local-a").health.status is WorkerStatus.HEALTHY


def test_consecutive_failures_degrade_worker(registry: WorkerRegistry) -> None:
    registry.register(local_record())
    for _ in range(2):
        registry.record_outcome("local-a", ok=False)
    assert registry.get("local-a").health.status is WorkerStatus.HEALTHY
    registry.record_outcome("local-a", ok=False)
    degraded = registry.get("local-a")
    assert degraded.health.status is WorkerStatus.DEGRADED
    assert degraded.health.consecutive_failures == 3
    assert registry.heartbeat("local-a").health.status is WorkerStatus.HEALTHY


def test_registry_survives_process_restart(workers_db: Path, clock: FakeClock) -> None:
    first = WorkerRegistry(WorkerStore(workers_db), clock=clock.now)
    first.register(local_record("local-a", datasets=["era5"]))
    first.register(local_record("local-b"))
    clock.advance(10)

    second = WorkerRegistry(WorkerStore(workers_db), clock=clock.now)
    records = second.list_workers()
    assert [record.worker_id for record in records] == ["local-a", "local-b"]
    assert records[0].capabilities.datasets == ["era5"]
    # uptime 是 last_heartbeat - registered_at 的实测差：注册后 10s 无心跳即为 0（诚实）。
    registered_at = records[0].registered_at
    assert registered_at is not None
    assert clock.now() - registered_at == timedelta(seconds=10)
    assert records[0].health.last_heartbeat == registered_at
    heartbeat = second.heartbeat("local-a")
    assert heartbeat.health.uptime_s == pytest.approx(10.0)
    assert records[1].health.status is WorkerStatus.HEALTHY


def test_registry_flags_offline_after_restart_and_long_silence(
    workers_db: Path, clock: FakeClock
) -> None:
    first = WorkerRegistry(WorkerStore(workers_db), clock=clock.now)
    first.register(local_record("local-a"))
    clock.advance(600)
    second = WorkerRegistry(WorkerStore(workers_db), clock=clock.now)
    assert second.get("local-a").health.status is WorkerStatus.OFFLINE
    assert clock.now() > first.get("local-a").registered_at.replace(year=2000)


def test_duplicate_register_rejected_when_replace_disabled(registry: WorkerRegistry) -> None:
    registry.register(local_record())
    with pytest.raises(WorkerRegistrationError):
        registry.register(local_record(), replace=False)
    updated = registry.register(local_record(datasets=["cmip6"]))
    assert updated.capabilities.datasets == ["cmip6"]


def test_capability_matching_filters_and_orders(registry: WorkerRegistry) -> None:
    registry.register(local_record("local-a", datasets=["era5"], max_bytes=100))
    registry.register(local_record("local-b"))
    registry.register(local_record("local-c", protocols=["s3"]))
    matched = registry.candidates(
        dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=50
    )
    assert [record.worker_id for record in matched] == ["local-a", "local-b"]
    oversized = registry.candidates(
        dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=101
    )
    assert [record.worker_id for record in oversized] == ["local-b"]


def test_candidates_exclude_offline(registry: WorkerRegistry, clock: FakeClock) -> None:
    registry.register(local_record("local-a"))
    registry.register(local_record("local-b"))
    clock.advance(registry.offline_after_s + 1)
    assert (
        registry.candidates(dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=1)
        == []
    )
    registry.heartbeat("local-a")
    matched = registry.candidates(
        dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=1
    )
    assert [record.worker_id for record in matched] == ["local-a"]


def test_degraded_ranking_puts_healthy_first(registry: WorkerRegistry) -> None:
    registry.register(local_record("local-z"))
    registry.register(local_record("local-a"))
    for _ in range(3):
        registry.record_outcome("local-a", ok=False)
    matched = registry.candidates(
        dataset_id="era5", provider_id="ncar", protocol="https", size_bytes=1
    )
    assert [record.worker_id for record in matched] == ["local-z", "local-a"]
    strict = registry.candidates(
        dataset_id="era5",
        provider_id="ncar",
        protocol="https",
        size_bytes=1,
        include_degraded=False,
    )
    assert [record.worker_id for record in strict] == ["local-z"]


def test_mark_offline_and_deregister(registry: WorkerRegistry) -> None:
    registry.register(local_record())
    flagged = registry.mark_offline("local-a", reason="operator maintenance")
    assert flagged.health.status is WorkerStatus.OFFLINE
    assert registry.deregister("local-a") is True
    assert registry.deregister("local-a") is False
    with pytest.raises(WorkerUnknownError):
        registry.get("local-a")
    events = [event for event, _ in registry.events("local-a")]
    assert events == ["registered", "marked_offline", "deregistered"]


def test_registry_rejects_invalid_thresholds(store: WorkerStore) -> None:
    with pytest.raises(ValueError, match="offline_after_s must be greater"):
        WorkerRegistry(store, heartbeat_timeout_s=100.0, offline_after_s=100.0)
    with pytest.raises(ValueError, match="degraded_after_failures must be"):
        WorkerRegistry(store, degraded_after_failures=0)
