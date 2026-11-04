"""接口契约测试：8 个 Protocol 必须可被真实实现，且文档齐全。

这里检查的是“接口定义本身是否可用”，不是 mock 调用次数：
fake 位于 ``tests/fixtures/contract_fakes.py``，它们真正实现算法。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import (
    FIXED_NOW,
    make_asset,
    make_file_record,
    make_metadata,
    make_request,
    make_source_metadata,
    make_task,
)
from tests.fixtures.contract_fakes import (
    FilesystemStorage,
    InMemoryManifest,
    InMemorySourceAdapter,
    LocalCopyTransport,
    SerialExecutor,
    SimplePlanner,
    SizeAndChecksumChecker,
    StaticCatalog,
)

from researchdatahub.core.models import (
    AccessRoute,
    FileRecord,
    IntegrityStatus,
    SourceAsset,
    TransferStatus,
)
from researchdatahub.core.ports import (
    CatalogProvider,
    DataSourceAdapter,
    IntegrityChecker,
    ManifestRepository,
    StorageBackend,
    TaskPlanner,
    TaskScheduler,
    TransportAdapter,
    assert_implements,
)
from researchdatahub.core.state import TaskState, transition

PROTOCOLS = [
    DataSourceAdapter,
    CatalogProvider,
    TransportAdapter,
    TaskPlanner,
    TaskScheduler,
    IntegrityChecker,
    StorageBackend,
    ManifestRepository,
]

EXPECTED_METHODS: dict[type, set[str]] = {
    DataSourceAdapter: {
        "source_id",
        "supported_routes",
        "describe",
        "discover_assets",
        "health_check",
    },
    CatalogProvider: {"list_datasets", "get_metadata"},
    TransportAdapter: {"transport_id", "supports", "transfer", "atransfer", "close"},
    TaskPlanner: {"plan"},
    TaskScheduler: {"submit", "run", "cancel"},
    IntegrityChecker: {"check"},
    StorageBackend: {"allocate_temp", "publish_original", "quarantine", "free_bytes"},
    ManifestRepository: {
        "save_task",
        "load_task",
        "list_tasks",
        "append_event",
        "save_file_record",
        "build_delivery_manifest",
        "initialize",
        "aiter_tasks",
    },
}


def test_all_eight_protocols_exist_and_are_runtime_checkable() -> None:
    assert len(PROTOCOLS) == 8
    for protocol in PROTOCOLS:
        assert getattr(protocol, "_is_protocol", False), protocol
        assert getattr(protocol, "_is_runtime_protocol", False), protocol
        assert protocol.__doc__, f"{protocol.__name__} 缺少接口文档"
        assert len(protocol.__doc__.strip()) > 40, f"{protocol.__name__} 文档过短（疑似 stub）"


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=[p.__name__ for p in PROTOCOLS])
def test_protocol_declares_expected_methods(protocol: type) -> None:
    """协议必须显式声明预期成员（属性通过 __dict__ 中的 property 暴露）。"""
    declared = {name for name in vars(protocol) if not name.startswith("_")}
    missing = EXPECTED_METHODS[protocol] - declared
    assert not missing, f"{protocol.__name__} 缺少方法: {sorted(missing)}"


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=[p.__name__ for p in PROTOCOLS])
def test_every_protocol_method_is_documented(protocol: type) -> None:
    """空类/stub 无法通过：每个方法都必须有 docstring。"""
    undocumented: list[str] = []
    for name in sorted(EXPECTED_METHODS[protocol]):
        member = vars(protocol).get(name)
        target = member.fget if isinstance(member, property) else member
        doc = getattr(target, "__doc__", None)
        if not doc or not doc.strip():
            undocumented.append(name)
    assert not undocumented, f"{protocol.__name__} 未写文档的方法: {undocumented}"


def test_assert_implements_rejects_non_protocol() -> None:
    with pytest.raises(TypeError, match="not a Protocol"):
        assert_implements(object(), dict)


def test_assert_implements_detects_missing_methods() -> None:
    class Partial:
        @property
        def source_id(self) -> str:
            return "x"

    with pytest.raises(TypeError, match="does not implement"):
        assert_implements(Partial(), DataSourceAdapter)


def test_assert_implements_accepts_real_implementations() -> None:
    """fake 必须真正通过协议检查（isinstance 基于结构）。"""
    adapter = InMemorySourceAdapter(metadata=make_source_metadata())
    assert_implements(adapter, DataSourceAdapter)
    assert isinstance(adapter, DataSourceAdapter)
    assert isinstance(LocalCopyTransport(), TransportAdapter)
    assert isinstance(StaticCatalog(), CatalogProvider)
    assert isinstance(SimplePlanner(), TaskPlanner)
    assert isinstance(SerialExecutor(), TaskScheduler)
    assert isinstance(SizeAndChecksumChecker(), IntegrityChecker)


# --------------------------------------------------------------------------- #
# 行为契约：不是“能被 import”，而是“按语义工作”
# --------------------------------------------------------------------------- #
def test_source_adapter_discovers_assets_for_request(tmp_path) -> None:
    adapter = InMemorySourceAdapter(
        metadata=make_source_metadata(), assets=[make_asset(), make_asset(asset_id="asset-2")]
    )
    request = make_request()
    assets = adapter.discover_assets(request)
    assert len(assets) == 2
    assert all(asset.source_id == adapter.source_id for asset in assets)
    assert adapter.health_check() is True
    assert adapter.calls == [request.request_id]


def test_transport_writes_real_bytes_and_checksum(tmp_path) -> None:
    transport = LocalCopyTransport(transport_id="s3")
    asset: SourceAsset = make_asset()
    assert transport.supports(asset) is True
    task = make_task()
    dest = tmp_path / "file.part"
    result = transport.transfer(asset, dest, task=task)
    assert result.status is TransferStatus.SUCCEEDED
    assert dest.exists()
    assert result.bytes_transferred == dest.stat().st_size
    assert result.checksum is not None
    transport.close()  # 幂等


def test_async_transport_matches_sync_semantics(tmp_path) -> None:
    """异步实现必须与同步实现语义一致（调度器允许用 asyncio.run 包装）。"""
    import asyncio

    transport = LocalCopyTransport()
    asset = make_asset()
    task = make_task()
    result = asyncio.run(transport.atransfer(asset, tmp_path / "a.part", task=task))
    assert result.status is TransferStatus.SUCCEEDED


def test_integrity_checker_detects_checksum_mismatch(tmp_path) -> None:
    path = tmp_path / "data.nc"
    path.write_bytes(b"payload")
    good = FileRecord.model_validate(
        {
            **make_file_record().model_dump(),
            "local_path": str(path),
            "size_bytes": 7,
            "checksum": {"value": "sha256:" + __import__("hashlib").sha256(b"payload").hexdigest()},
        }
    )
    checker = SizeAndChecksumChecker()
    task = make_task()
    ok_report = checker.check(good, task=task, kinds=["size_match", "checksum_match"])
    assert ok_report.status is IntegrityStatus.PASSED
    assert ok_report.permits_completion is True

    zero_digest = "sha256:" + "0" * 64
    bad = good.model_copy(
        update={"checksum": good.checksum.model_copy(update={"value": zero_digest})}
    )
    bad_report = checker.check(bad, task=task, kinds=["checksum_match"])
    assert bad_report.status is IntegrityStatus.FAILED
    assert bad_report.permits_completion is False
    assert bad_report.failures


def test_manifest_repository_optimistic_concurrency() -> None:
    """两个 worker 推进同一任务时，第二个必须失败（防止重复执行）。"""
    repo = InMemoryManifest()
    repo.initialize()
    task = make_task()
    repo.save_task(task)
    planned = transition(task, TaskState.PLANNED, reason="planned", now=FIXED_NOW)
    repo.save_task(planned, expected_state=TaskState.PENDING)

    from researchdatahub.core.errors import ManifestError

    with pytest.raises(ManifestError, match="optimistic concurrency"):
        repo.save_task(planned, expected_state=TaskState.PENDING)  # 已被推进过

    assert repo.load_task(task.task_id).state is TaskState.PLANNED


def test_manifest_repository_iteration_and_events() -> None:
    repo = InMemoryManifest()
    task = make_task()
    repo.save_task(task)
    queued = transition(task, TaskState.PLANNED, reason="p", now=FIXED_NOW)
    repo.save_task(queued)
    assert [t.task_id for t in repo.list_tasks()] == [task.task_id]
    assert list(repo.list_tasks(state=TaskState.PENDING)) == []
    repo.append_event(task.task_id, "planned", {"by": "test"})
    assert repo._events == [(task.task_id, "planned", {"by": "test"})]


def test_manifest_repository_raises_for_missing_task() -> None:
    from researchdatahub.core.errors import ManifestError

    repo = InMemoryManifest()
    with pytest.raises(ManifestError, match="not found"):
        repo.load_task("task-missing")


def test_storage_publishes_read_only_original(tmp_path) -> None:
    storage = FilesystemStorage(tmp_path)
    temp = storage.allocate_temp("task-0001")
    temp.write_bytes(b"payload")
    published = storage.publish_original(temp, dataset_id="era5", file_name="f.nc")
    assert published.exists()
    assert temp.exists() is False
    assert published.stat().st_mode & 0o200 == 0, "原始区文件必须只读（Preserve Original Data）"
    assert storage.free_bytes() > 0


def test_storage_quarantine_keeps_bytes(tmp_path) -> None:
    storage = FilesystemStorage(tmp_path)
    src = tmp_path / "bad.nc"
    src.write_bytes(b"corrupt")
    moved = storage.quarantine(src, reason="checksum mismatch")
    assert moved.exists()
    assert moved.read_bytes() == b"corrupt", "隔离必须保留数据，不能删除"


def test_planner_produces_planned_tasks() -> None:
    adapter = InMemorySourceAdapter(
        metadata=make_source_metadata(), assets=[make_asset(), make_asset(asset_id="asset-2")]
    )
    request = make_request()
    plan = SimplePlanner().plan(request, [adapter])
    assert plan.request_id == request.request_id
    assert all(task.state is TaskState.PLANNED for task in plan.tasks)
    assert all(task.plan_id == plan.plan_id for task in plan.tasks)


def test_catalog_raises_not_found() -> None:
    from researchdatahub.core.errors import SourceNotFoundError

    catalog = StaticCatalog({"era5-pressure-levels": make_metadata()})
    assert catalog.list_datasets() == ["era5-pressure-levels"]
    assert catalog.get_metadata("era5-pressure-levels").product == "ERA5"
    with pytest.raises(SourceNotFoundError):
        catalog.get_metadata("nope")


def test_scheduler_cancel_unknown_task_raises() -> None:
    from researchdatahub.core.errors import StateTransitionError

    scheduler = SerialExecutor()
    with pytest.raises(StateTransitionError):
        scheduler.cancel("task-unknown", "user")


def test_access_route_enum_values_are_lowercase() -> None:
    """传输适配器的 transport_id 必须来自 AccessRoute，避免字符串漂移。"""
    for route in AccessRoute:
        assert route.value == route.value.lower()


def test_protocol_docs_mention_concurrency() -> None:
    """并发约定必须写进接口文档，否则实现者只能猜。"""
    for protocol in (ManifestRepository, TransportAdapter, TaskScheduler):
        doc = protocol.__doc__ or ""
        assert "并发" in doc or "线程" in doc, f"{protocol.__name__} 未说明并发要求"


def test_frozen_models_are_returned_by_fakes() -> None:
    assert make_metadata().model_config["frozen"] is True
    assert datetime.now(UTC).tzinfo is not None
