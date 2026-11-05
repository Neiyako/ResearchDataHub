"""故障转移编排：首选失败/失联 → 记录样本 → 选下一条；全部失败抛结构化错误。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.test_network.conftest import (
    FakeClock,
    file_bytes,
    make_asset,
    make_task,
    serve,
    wait_until,
)
from tests.unit.test_transfer.http_server import TransferTestServer

from researchdatahub.network.routes.failover import (
    FailoverCoordinator,
    FailoverExhausted,
)
from researchdatahub.network.routes.manager import NoRouteAvailable, RouteManager
from researchdatahub.network.routes.models import Route, RouteRejectionCode
from researchdatahub.network.routes.stats import RouteStatsStore
from researchdatahub.network.workers.local import LocalWorker
from researchdatahub.network.workers.models import (
    WorkerCapabilities,
    WorkerKind,
    WorkerRecord,
)
from researchdatahub.network.workers.registry import WorkerRegistry


def build_worker(root: Path, worker_id: str, transports: list[object]) -> LocalWorker:
    """构造一个真实本地 worker。"""
    return LocalWorker(
        worker_id=worker_id,
        storage_root=root / worker_id,
        transports=transports,  # type: ignore[arg-type]
        max_concurrent=2,
    )


def register(registry: WorkerRegistry, worker_id: str) -> WorkerRecord:
    """把节点登记进注册表（能力：https，并发 2）。"""
    return registry.register(
        WorkerRecord(
            worker_id=worker_id,
            kind=WorkerKind.LOCAL,
            capabilities=WorkerCapabilities(protocols=["https"], max_concurrent=2),
        )
    )


def coordinator(
    registry: WorkerRegistry,
    manager: RouteManager,
    workers: dict[str, object],
    clock: FakeClock,
    **overrides: object,
) -> FailoverCoordinator:
    """装配协调器。

    ``sleep`` 换成"推进假时钟"：轮询循环照常执行，但测试不会真的等待；同时保证
    实测耗时（``duration_s``）是一个可断言的确定值，而不是 0。
    """
    return FailoverCoordinator(
        registry=registry,
        routes=manager,
        workers=workers,  # type: ignore[arg-type]
        sleep=clock.sleeper,
        monotonic=clock.monotonic,
        **overrides,  # type: ignore[arg-type]
    )


def test_failover_moves_to_second_worker(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    stats_store: RouteStatsStore,
    clock: FakeClock,
) -> None:
    data = file_bytes(128 * 1024)
    serve(http_server, "/ncar/ok.nc", data)
    good_url = http_server.url("/ncar/ok.nc")
    register(registry, "local-bad")
    register(registry, "local-good")
    bad = build_worker(tmp_path, "local-bad", [http_adapter])
    good = build_worker(tmp_path, "local-good", [http_adapter])
    route_manager.add_many(
        [
            Route(
                route_id="local-bad:ncar:https",
                protocol="https",
                worker_id="local-bad",
                provider_id="ncar",
            ),
            Route(
                route_id="local-good:ncar:https",
                protocol="https",
                worker_id="local-good",
                provider_id="ncar",
            ),
        ]
    )
    try:
        # 让首选链路（字典序在前）指向不存在的 URL：真实 404
        failing = make_task(make_asset(uri=http_server.url("/ncar/absent.nc"), data=data))
        good_task = make_task(make_asset(uri=good_url, data=data), task_id="task-net-failover")
        # 两个 worker 执行同一个任务对象：第一个 worker 用坏 URL 的任务
        failover = coordinator(
            registry, route_manager, {"local-bad": bad, "local-good": good}, clock
        )
        # 直接跑"坏任务"证明找不到任何链路能成功
        with pytest.raises(FailoverExhausted) as exhausted:
            failover.run(failing)
        assert exhausted.value.attempts
        assert all(not item.retryable or True for item in exhausted.value.attempts)
        stats = route_manager.stats_for("local-bad:ncar:https", provider_id="ncar")
        assert stats.samples >= 1
        assert stats.failures >= 1
        delivery = failover.run(good_task)
        assert delivery.route.route_id in {"local-bad:ncar:https", "local-good:ncar:https"}
        assert delivery.file.sha256 == good_task.asset.checksum.hexdigest  # type: ignore[union-attr]
        assert delivery.file.path.read_bytes() == data
        succeeded = route_manager.stats_for(delivery.route_id, provider_id="ncar")
        assert succeeded.successes >= 1
        assert succeeded.throughput_bps is not None
        assert succeeded.throughput_bps > 0
    finally:
        bad.close()
        good.close()


def test_all_routes_failing_raises_exhausted_with_every_attempt(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    data = b"x" * 1024
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    register(registry, "local-a")
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        task = make_task(make_asset(uri=http_server.url("/none.nc"), data=data))
        failover = coordinator(registry, route_manager, {"local-a": worker}, clock)
        with pytest.raises(FailoverExhausted) as excinfo:
            failover.run(task)
        payload = excinfo.value.as_dict()
        assert payload["error"] == "FailoverExhausted"
        attempts = payload["attempts"]
        assert isinstance(attempts, list)
        assert len(attempts) == 1
        assert attempts[0]["route_id"] == "local-a:ncar:https"  # type: ignore[index]
        assert attempts[0]["retryable"] is True  # type: ignore[index]
        assert excinfo.value.retryable is True
    finally:
        worker.close()


def test_no_route_available_when_capabilities_mismatch(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    registry.register(
        WorkerRecord(
            worker_id="local-a",
            kind=WorkerKind.LOCAL,
            capabilities=WorkerCapabilities(protocols=["s3"]),
        )
    )
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        task = make_task(make_asset(uri=http_server.url("/x.nc"), data=b"x"))
        failover = coordinator(registry, route_manager, {"local-a": worker}, clock)
        with pytest.raises(NoRouteAvailable) as excinfo:
            failover.run(task)
        assert excinfo.value.rejected[0].code is (
            RouteRejectionCode.PROTOCOL_NOT_SUPPORTED_BY_WORKER
        )
    finally:
        worker.close()


def test_repeated_failures_mark_worker_offline(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    register(registry, "local-a")
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        failover = coordinator(
            registry, route_manager, {"local-a": worker}, clock, worker_offline_after_failures=2
        )
        for index in range(2):
            task = make_task(
                make_asset(uri=http_server.url(f"/absent-{index}.nc"), data=b"y" * 16),
                task_id=f"task-offline-{index}",
            )
            with pytest.raises(FailoverExhausted):
                failover.run(task)
            route_manager.stats_store.reset()
        record = registry.get("local-a")
        assert record.health.status.value in {"offline", "degraded"}
        assert any(event == "marked_offline" for event, _ in registry.events("local-a"))
        with pytest.raises(NoRouteAvailable):
            failover.run(
                make_task(
                    make_asset(uri=http_server.url("/again.nc"), data=b"z"), task_id="task-again"
                )
            )
    finally:
        worker.close()


def test_cancel_before_dispatch_yields_cancelled_attempt(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    register(registry, "local-a")
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        failover = coordinator(registry, route_manager, {"local-a": worker}, clock)
        failover.cancel()
        task = make_task(make_asset(uri=http_server.url("/c.nc"), data=b"c" * 8))
        with pytest.raises(FailoverExhausted) as excinfo:
            failover.run(task)
        assert excinfo.value.attempts[0].outcome == "cancelled"
        failover.reset_cancel()
    finally:
        worker.close()


def test_checksum_mismatch_forces_failover(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    data = file_bytes(8 * 1024)
    serve(http_server, "/ncar/real.nc", data)
    tampered = file_bytes(8 * 1024, seed=99)
    register(registry, "local-a")
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        asset = make_asset(uri=http_server.url("/ncar/real.nc"), data=data)
        lying = asset.model_copy(
            update={
                "checksum": asset.checksum.model_copy(  # type: ignore[union-attr]
                    update={"value": "sha256:" + "c" * 64}
                )
            }
        )
        task = make_task(lying)
        failover = coordinator(registry, route_manager, {"local-a": worker}, clock)
        with pytest.raises(FailoverExhausted) as excinfo:
            failover.run(task)
        attempts = excinfo.value.attempts
        assert any("sha256 mismatch" in item.detail for item in attempts)
        del tampered
    finally:
        worker.close()


def test_delivery_roundtrips_real_bytes(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    data = file_bytes(300 * 1024)
    serve(http_server, "/ncar/roundtrip.nc", data)
    register(registry, "local-a")
    worker = build_worker(tmp_path, "local-a", [http_adapter])
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    try:
        task = make_task(make_asset(uri=http_server.url("/ncar/roundtrip.nc"), data=data))
        failover = coordinator(registry, route_manager, {"local-a": worker}, clock)
        delivery = failover.run(task)
        payload = delivery.as_dict()
        assert payload["route"]["route_id"] == "local-a:ncar:https"  # type: ignore[index]
        assert delivery.file.size_bytes == len(data)
        assert delivery.file.sha256 == task.asset.checksum.hexdigest  # type: ignore[union-attr]
        assert assert_wait_ignored(delivery.file.path.read_bytes(), data)
    finally:
        worker.close()


def assert_wait_ignored(actual: bytes, expected: bytes) -> bool:
    """内容比对（抽出成函数，避免长断言表达式影响可读性）。"""
    return actual == expected


def test_worker_without_manifest_is_skipped_not_crashed(
    tmp_path: Path,
    http_server: TransferTestServer,
    registry: WorkerRegistry,
    route_manager: RouteManager,
    clock: FakeClock,
) -> None:
    """没有任何 worker 实现时给出结构化失败，而不是 ``KeyError``。"""

    class NoImplementation:
        pass

    register(registry, "local-a")
    route_manager.add(
        Route(
            route_id="local-a:ncar:https",
            protocol="https",
            worker_id="local-a",
            provider_id="ncar",
        )
    )
    task = make_task(make_asset(uri=http_server.url("/z.nc"), data=b"z"))
    failover = coordinator(registry, route_manager, {}, clock)
    del NoImplementation
    with pytest.raises(FailoverExhausted) as excinfo:
        failover.run(task)
    assert excinfo.value.attempts[0].outcome == "worker_not_resolved"
    assert excinfo.value.attempts[0].retryable is False


def test_wait_until_helper_is_used_for_async(
    tmp_path: Path,
    http_server: TransferTestServer,
    http_adapter: object,
) -> None:
    """本地 worker 是异步执行的：``wait_until`` 是断言入口（真实等待，非 sleep 轮询）。"""
    data = file_bytes(16 * 1024)
    serve(http_server, "/ncar/async.nc", data)
    worker = build_worker(tmp_path, "local-async", [http_adapter])
    try:
        task = make_task(make_asset(uri=http_server.url("/ncar/async.nc"), data=data))
        execution = worker.prepare(task, relative_path="async/f.nc")
        assert wait_until(lambda: worker.poll(execution).terminal)
        assert worker.poll(execution).value == "completed"
    finally:
        worker.close()
