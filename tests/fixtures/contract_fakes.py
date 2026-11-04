"""可运行的 fake 实现，用于证明 Protocol 契约可被满足。

这些不是 mock：它们**真实实现**算法（内存字典存储、真实的 sha256 计算、
真实的文件移动），只是把外部 I/O 换成进程内存储。这样契约测试检查的是
“接口能否被正确实现”，而不是“mock 被调用了几次”。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from pathlib import Path

from researchdatahub.core.models import (
    AcquisitionPlan,
    AcquisitionTask,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
    FileRecord,
    IntegrityReport,
    IntegrityStatus,
    SourceAsset,
    SourceMetadata,
    TransferResult,
    TransferStatus,
)
from researchdatahub.core.state import TaskState


class InMemorySourceAdapter:
    """数据源适配器 fake：按需求返回预置资产列表。"""

    def __init__(
        self,
        *,
        source_id: str = "ncar",
        routes: frozenset[str] = frozenset({"s3", "https"}),
        assets: Sequence[SourceAsset] | None = None,
        metadata: SourceMetadata | None = None,
    ) -> None:
        self._source_id = source_id
        self._routes = routes
        self._assets = list(assets or [])
        self._metadata = metadata
        self.calls: list[str] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return self._routes

    def describe(self) -> SourceMetadata:
        if self._metadata is None:
            raise RuntimeError("fake adapter has no metadata configured")
        return self._metadata

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        """返回匹配该数据集的前 N 个预置资产（N = 需求变量数），模拟按变量切分文件。"""
        self.calls.append(request.request_id)
        matching = [asset for asset in self._assets if asset.dataset_id == request.dataset_id]
        return matching[: len(request.required_variables())]

    def health_check(self) -> bool:
        return True


class LocalCopyTransport:
    """传输 fake：用本地文件复制模拟下载，真实计算 sha256。"""

    def __init__(self, *, transport_id: str = "s3", home: Path | None = None) -> None:
        self._transport_id = transport_id
        self._home = home

    @property
    def transport_id(self) -> str:
        return self._transport_id

    def supports(self, asset: SourceAsset) -> bool:
        return asset.access_route.value == self._transport_id

    def transfer(
        self, asset: SourceAsset, destination: Path, *, task: AcquisitionTask
    ) -> TransferResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{asset.asset_id}:{asset.size_bytes}".encode()
        destination.write_bytes(payload)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return TransferResult(
            task_id=task.task_id,
            status=TransferStatus.SUCCEEDED,
            transport_id=self._transport_id,
            bytes_transferred=len(payload),
            duration_seconds=0.001,
            destination_path=str(destination),
            checksum={"value": digest},
            completed_at=task.last_transition_at or task.finished_at or task.asset.discovered_at,
        )

    async def atransfer(
        self, asset: SourceAsset, destination: Path, *, task: AcquisitionTask
    ) -> TransferResult:
        return self.transfer(asset, destination, task=task)

    def close(self) -> None:
        return None


class StaticCatalog:
    """目录 fake：内存字典，只读查询 + 一次 upsert。"""

    def __init__(self, datasets: dict[str, DatasetMetadata] | None = None) -> None:
        self._datasets = dict(datasets or {})

    def list_datasets(self) -> Sequence[str]:
        return sorted(self._datasets)

    def get_metadata(self, dataset_id: str) -> DatasetMetadata:
        from researchdatahub.core.errors import SourceNotFoundError

        try:
            return self._datasets[dataset_id]
        except KeyError:
            raise SourceNotFoundError(f"dataset {dataset_id!r} not in catalog") from None

    def upsert_dataset(self, dataset_id: str, metadata: DatasetMetadata) -> None:
        self._datasets[dataset_id] = metadata


class SimplePlanner:
    """计划器 fake：为每个资产生成一个 PLANNED 任务。"""

    def __init__(self, *, plan_id: str = "plan-fake") -> None:
        self._plan_id = plan_id

    def plan(self, request: DatasetRequest, adapters: Iterable[object]) -> AcquisitionPlan:
        from datetime import UTC, datetime

        tasks: list[AcquisitionTask] = []
        covered: set[str] = set()
        for adapter in adapters:
            for index, asset in enumerate(adapter.discover_assets(request)):  # type: ignore[attr-defined]
                tasks.append(
                    AcquisitionTask(
                        task_id=f"task-{index:04d}",
                        plan_id=self._plan_id,
                        request_id=request.request_id,
                        asset=asset,
                        state=TaskState.PLANNED,
                        priority=request.priority,
                    )
                )
                covered.add(asset.dataset_id)
        unmatched = sorted(request.required_variables() - covered) if not covered else []
        return AcquisitionPlan(
            plan_id=self._plan_id,
            request_id=request.request_id,
            tasks=tasks,
            unmatched_variables=unmatched,
            generated_at=datetime.now(UTC),
        )


class SerialExecutor:
    """调度器 fake：串行执行，不并发（同时满足接口形状）。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    def submit(self, plan: AcquisitionPlan) -> int:
        self.submitted.extend(plan.task_ids())
        return len(plan.tasks)

    def run(self, *, max_workers: int | None = None) -> None:
        return None

    def cancel(self, task_id: str, reason: str) -> None:
        if task_id not in self.submitted:
            from researchdatahub.core.errors import StateTransitionError

            raise StateTransitionError(
                "unknown task", from_state="UNKNOWN", to_state="CANCELLED", task_id=task_id
            )
        self.cancelled.append(task_id)


class SizeAndChecksumChecker:
    """完整性检查 fake：真实比较大小与 sha256。"""

    def check(
        self,
        file_record: FileRecord,
        *,
        task: AcquisitionTask,
        kinds: Sequence[str],
    ) -> IntegrityReport:
        path = Path(file_record.local_path)
        if not path.exists():
            from researchdatahub.core.errors import IntegrityError

            raise IntegrityError(f"file missing: {path}", task_id=task.task_id)
        size = path.stat().st_size
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        failures: list[str] = []
        if "size_match" in kinds and size != file_record.size_bytes:
            failures.append(f"size mismatch: expected {file_record.size_bytes}, got {size}")
        if "checksum_match" in kinds and digest != file_record.checksum.value:
            failures.append("checksum mismatch")
        return IntegrityReport(
            report_id=f"report-{task.task_id}",
            task_id=task.task_id,
            file_id=file_record.file_id,
            status=IntegrityStatus.FAILED if failures else IntegrityStatus.PASSED,
            checks=list(kinds),
            failures=failures,
            expected={"size_bytes": file_record.size_bytes, "checksum": file_record.checksum.value},
            observed={"size_bytes": size, "checksum": digest},
            checked_at=task.last_transition_at or file_record.retrieved_at,
        )


class FilesystemStorage:
    """存储 fake：真实移动文件，维护 temp/original/quarantine 三个目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in ("tmp", "raw", "quarantine"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def allocate_temp(self, task_id: str) -> Path:
        path = self.root / "tmp" / f"{task_id}.part"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def publish_original(self, temp_path: Path, *, dataset_id: str, file_name: str) -> Path:
        target = self.root / "raw" / dataset_id / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_path), str(target))
        target.chmod(0o444)
        return target

    def quarantine(self, path: Path, *, reason: str) -> Path:
        target = self.root / "quarantine" / f"{path.name}.{hash(reason) % 10_000}"
        shutil.move(str(path), str(target))
        return target

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free


class InMemoryManifest:
    """Manifest fake：字典存储 + 乐观并发检查（不是 mock，是真实现）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, AcquisitionTask] = {}
        self._files: dict[str, FileRecord] = {}
        self._events: list[tuple[str, str, dict[str, object]]] = []

    def initialize(self) -> None:
        return None

    def save_task(self, task: AcquisitionTask, *, expected_state: TaskState | None = None) -> None:
        from researchdatahub.core.errors import ManifestError

        current = self._tasks.get(task.task_id)
        if expected_state is not None:
            actual = current.state if current else None
            if actual is not expected_state:
                raise ManifestError(
                    f"optimistic concurrency conflict: expected {expected_state}, found {actual}",
                    task_id=task.task_id,
                )
        self._tasks[task.task_id] = task

    def load_task(self, task_id: str) -> AcquisitionTask:
        from researchdatahub.core.errors import ManifestError

        try:
            return self._tasks[task_id]
        except KeyError:
            raise ManifestError(f"task {task_id!r} not found", task_id=task_id) from None

    def list_tasks(self, *, state: TaskState | None = None) -> Iterator[AcquisitionTask]:
        for task in sorted(self._tasks.values(), key=lambda t: t.task_id):
            if state is None or task.state is state:
                yield task

    def append_event(self, task_id: str, event: str, payload: dict[str, object]) -> None:
        self._events.append((task_id, event, payload))

    def save_file_record(self, record: FileRecord) -> None:
        self._files[record.file_id] = record

    def build_delivery_manifest(
        self, request_id: str, *, metadata: DatasetMetadata | None = None
    ) -> DeliveryManifest:
        """由已登记文件生成交付清单；metadata 必须显式提供（fake 不持有科学元数据）。"""
        from researchdatahub.core.errors import ManifestError

        files = [record for record in self._files.values() if record.task_id in self._tasks]
        if not files:
            raise ManifestError(f"no completed files for request {request_id!r}")
        if metadata is None:
            raise ManifestError("build_delivery_manifest requires dataset metadata")
        return DeliveryManifest(
            delivery_id=f"delivery-{request_id}",
            request_id=request_id,
            plan_id="plan-fake",
            dataset_metadata=metadata,
            files=files,
            created_at=files[0].retrieved_at,
            tool_version="0.1.0",
        )

    async def aiter_tasks(self) -> AsyncIterator[AcquisitionTask]:
        for task in self.list_tasks():
            yield task
