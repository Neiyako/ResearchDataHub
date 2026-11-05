"""本地 worker：把既有 :class:`researchdatahub.scheduler.TaskExecutor` 的传输通道适配成 Worker。

设计要点
--------
* **不重写执行器**：真正的"下载 → 校验 → 提交"仍由 :class:`TaskExecutor` 与
  :class:`~researchdatahub.transfer.runner.BoundedTransferRunner` 完成；本模块只做两件事：
  1. 用一个最小的任务包装器把执行器跑在独立线程里（``prepare`` 立即返回句柄）；
  2. 让执行器在失败时**不推进任务状态**（它只做传输与校验，状态由主进程/编排层决定）。
* **失败语义对齐架构 §7.1**：执行器失败时返回 `TransferResult(status=FAILED)` 并附带
  ``retryable`` / ``error_code``，编排层据此决定故障转移还是直接失败。
* **校验两次**：执行器内部做完整性检查（Integrity Before Delivery），回传/接管阶段
  再对落盘文件重算 SHA-256；两次都对得上才算成功。

已关闭的执行器能力（显式声明，避免误解）
----------------------------------------
本包装器不负责：状态机推进、Manifest 写入、租约续约、崩溃对账。它只保证"资产被搬到了
一个本地路径"，其余交给 :mod:`researchdatahub.network.routes.failover` 与主调度器。
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from researchdatahub.core.models import (
    AcquisitionTask,
    SourceAsset,
    TransferResult,
    TransferStatus,
)
from researchdatahub.network.workers.base import (
    FetchReport,
    WorkerExecution,
    WorkerTaskError,
)
from researchdatahub.network.workers.models import (
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
    WorkerStatus,
)
from researchdatahub.scheduler.models import TransferExecutor
from researchdatahub.storage.naming import sha256_file

__all__ = ["LocalWorker"]


def _sha256_hex(path: Path) -> str:
    """对文件流式计算 SHA-256（不占内存，与存储层同一算法）。"""
    return sha256_file(path)


@dataclass
class _LocalSlot:
    """本地执行槽位：一次 ``prepare`` 的全部状态（只在锁内读写）。"""

    execution: WorkerExecution
    relative_path: str
    state: RemoteTaskState = RemoteTaskState.QUEUED
    result: RemoteTaskResult | None = None
    error: str | None = None
    retryable: bool = False
    transfer: TransferResult | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class LocalWorker:
    """进程内 worker：真实下载 + 真实 SHA-256，但只做传输这一层。

    Parameters
    ----------
    worker_id:
        节点标识（与 ``WorkerRegistry`` 中登记的一致；同时作为执行器的 ``worker_id``）。
    storage_root:
        本节点的存储根；``relative_path`` 一律相对它解析，越界立即拒绝。
    transports:
        传输执行器序列（``TransferExecutor`` 协议：``supports`` + ``transfer_outcome``）；
        按顺序选第一个支持的，与 ``TaskExecutor`` 的行为一致。
    capabilities:
        节点能力声明；``None`` 时用"不限数据集/提供方、不限制大小、并发 1"的保守默认。
    max_concurrent:
        同节点并行执行上限（超出时 ``prepare`` 抛 :class:`WorkerTaskError`，
        ``retryable=True`——被拒的任务应转投其它节点）。
    version:
        节点上报的版本号。
    """

    def __init__(
        self,
        *,
        worker_id: str,
        storage_root: Path,
        transports: Sequence[TransferExecutor],
        capabilities: WorkerCapabilities | None = None,
        max_concurrent: int = 2,
        version: str = "0.1.0",
    ) -> None:
        if not transports:
            raise ValueError("LocalWorker requires at least one transfer executor")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._worker_id = worker_id
        self._root = Path(storage_root).expanduser().resolve(strict=False)
        self._transports = tuple(transports)
        self._capabilities = capabilities or WorkerCapabilities(max_concurrent=max_concurrent)
        self._max_concurrent = min(max_concurrent, self._capabilities.max_concurrent)
        self._version = version
        self._lock = threading.Lock()
        self._slots: dict[str, _LocalSlot] = {}
        self._started_at = time.monotonic()
        self._closed = False
        self._executions = 0
        self._failures = 0

    # -- WorkerProtocol ----------------------------------------------------- #
    @property
    def worker_id(self) -> str:
        """节点 ID。"""
        return self._worker_id

    @property
    def kind(self) -> WorkerKind:
        """本地节点类型。"""
        return WorkerKind.LOCAL

    @property
    def capabilities(self) -> WorkerCapabilities:
        """能力声明。"""
        return self._capabilities

    @property
    def storage_root(self) -> Path:
        """本节点存储根。"""
        return self._root

    @property
    def artifact_root(self) -> Path:
        """产物落盘根目录（``WorkerProtocol`` 的可选结构化能力）。"""
        return self._root

    def health(self) -> WorkerHealth:
        """本地健康：进程存活即为 ``healthy``，执行失败过多记为 ``degraded``。"""
        with self._lock:
            status = WorkerStatus.DEGRADED if self._failures >= 3 else WorkerStatus.HEALTHY
            return WorkerHealth(
                status=status,
                last_heartbeat=None,
                uptime_s=max(0.0, time.monotonic() - self._started_at),
                consecutive_failures=self._failures,
            )

    def prepare(self, task: AcquisitionTask, *, relative_path: str) -> WorkerExecution:
        """登记执行并在后台线程里开始真实下载（幂等：同一 ``task_id`` 返回原句柄）。

        Raises
        ------
        WorkerTaskError
            并发已满（``retryable=True``）或存储根不可用（``retryable=False``）。
        """
        with self._lock:
            if self._closed:
                raise WorkerTaskError("local worker is closed", retryable=False)
            existing = self._slots.get(task.task_id)
            if existing is not None:
                return existing.execution
            active = sum(1 for slot in self._slots.values() if slot.state in _ACTIVE_STATES)
            if active >= self._max_concurrent:
                raise WorkerTaskError(
                    f"local worker {self._worker_id} concurrency limit {self._max_concurrent} "
                    "reached",
                    retryable=True,
                    error_code="worker_busy",
                )
            slot = _LocalSlot(
                execution=WorkerExecution(
                    worker_id=self._worker_id,
                    handle=task.task_id,
                    remote_task_id=task.task_id,
                    started_at_monotonic=time.monotonic(),
                ),
                relative_path=_normalize_relative(relative_path),
            )
            self._slots[task.task_id] = slot
        thread = threading.Thread(
            target=self._run, args=(task, slot), name=f"rdh-local-{task.task_id}", daemon=True
        )
        thread.start()
        return slot.execution

    def poll(self, execution: WorkerExecution) -> RemoteTaskState:
        """查询状态（本地执行在线程里跑，查询只读锁内状态）。"""
        with self._lock:
            slot = self._slot_for(execution)
            return slot.state

    def result(self, execution: WorkerExecution) -> RemoteTaskResult:
        """读取产物描述。

        Raises
        ------
        WorkerTaskError
            执行尚未完成（``retryable=True``）或已经失败（携带原始可重试性）。
        """
        with self._lock:
            slot = self._slot_for(execution)
            if slot.state is RemoteTaskState.COMPLETED and slot.result is not None:
                return slot.result
            if slot.state.terminal:
                raise WorkerTaskError(
                    slot.error or f"local execution ended as {slot.state.value}",
                    retryable=slot.retryable or slot.state is not RemoteTaskState.CANCELLED,
                    error_code="worker_task_failed",
                )
        raise WorkerTaskError("local execution is not finished", retryable=True)

    def fetch(self, execution: WorkerExecution, destination: Path) -> FetchReport:
        """把产物复制到 ``destination``（同机复制；原子命名 + 重算 SHA-256）。

        本地执行时"回传"就是一次文件复制：用 ``.part`` 暂存后 ``os.replace`` 原子提交，
        与远程回传的语义保持一致（读者永远看不到半截文件）。
        """
        result = self.result(execution)
        source = Path(result.path)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        resumed = False
        already_there = (
            target.is_file()
            and target.stat().st_size == result.size_bytes
            and _sha256_hex(target) == result.sha256
        )
        if already_there:
            return FetchReport(
                path=target, size_bytes=result.size_bytes, sha256=result.sha256, resumed=True
            )
        try:
            with source.open("rb") as reader, partial.open("wb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            if partial.stat().st_size != result.size_bytes:
                raise WorkerTaskError(
                    "local copy size mismatch",
                    retryable=True,
                    error_code="worker_size_mismatch",
                )
            digest = _sha256_hex(partial)
            if digest != result.sha256:
                raise WorkerTaskError(
                    "local copy sha256 mismatch",
                    retryable=True,
                    error_code="worker_checksum_mismatch",
                )
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return FetchReport(
            path=target, size_bytes=result.size_bytes, sha256=digest, resumed=resumed
        )

    def cancel(self, execution: WorkerExecution) -> bool:
        """请求取消（设置取消事件，传输层在下一个 chunk 边界停止）。"""
        with self._lock:
            slot = self._slots.get(execution.handle)
            if slot is None or slot.state.terminal:
                return False
            slot.cancel_event.set()
            return True

    def close(self) -> None:
        """取消在途执行并标记关闭（幂等）。"""
        with self._lock:
            self._closed = True
            slots = list(self._slots.values())
        for slot in slots:
            slot.cancel_event.set()

    # -- 内部执行 ----------------------------------------------------------- #
    def _slot_for(self, execution: WorkerExecution) -> _LocalSlot:
        slot = self._slots.get(execution.handle)
        if slot is None:
            raise WorkerTaskError(
                f"unknown local execution {execution.handle!r}",
                retryable=False,
                error_code="worker_unknown_handle",
            )
        return slot

    def _run(self, task: AcquisitionTask, slot: _LocalSlot) -> None:
        with self._lock:
            slot.state = RemoteTaskState.EXECUTING
        started = time.monotonic()
        try:
            destination = self._resolve(slot.relative_path)
        except WorkerTaskError as exc:
            self._finish(slot, None, str(exc), retryable=False)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        asset = task.asset
        adapter = next((item for item in self._transports if item.supports(asset)), None)
        if adapter is None:
            self._finish(
                slot,
                None,
                f"no transfer executor supports access route {asset.access_route.value}",
                retryable=False,
            )
            return
        unit_task = _sandbox_task(task)
        result = adapter.transfer_outcome(
            asset, destination, task=unit_task, cancel_event=slot.cancel_event
        )
        with self._lock:
            slot.transfer = result
        if result.status not in (TransferStatus.SUCCEEDED, TransferStatus.SKIPPED):
            if result.error_code == "cancelled" or slot.cancel_event.is_set():
                with self._lock:
                    slot.state = RemoteTaskState.CANCELLED
                    slot.error = result.error_detail or "cancelled"
                return
            self._finish(
                slot,
                None,
                f"{result.error_code or 'transfer_failed'}: {result.error_detail or 'failed'}",
                retryable=result.retryable,
            )
            return
        path = Path(result.destination_path) if result.destination_path else destination
        try:
            size = path.stat().st_size
            digest = _sha256_hex(path)
        except OSError as exc:
            self._finish(slot, None, f"cannot hash product: {exc}", retryable=True)
            return
        if asset.size_bytes and size != asset.size_bytes:
            self._finish(
                slot,
                None,
                f"size mismatch: declared {asset.size_bytes}, got {size}",
                retryable=True,
            )
            return
        expected = None if asset.checksum is None else asset.checksum.hexdigest
        if expected is not None and expected != digest:
            self._finish(
                slot,
                None,
                "sha256 mismatch: declared " + expected + ", got " + digest,
                retryable=True,
            )
            return
        product = RemoteTaskResult(
            path=str(path), size_bytes=size, sha256=digest, duration_s=time.monotonic() - started
        )
        self._finish(slot, product, None, retryable=False)

    def _finish(
        self,
        slot: _LocalSlot,
        product: RemoteTaskResult | None,
        error: str | None,
        *,
        retryable: bool,
    ) -> None:
        with self._lock:
            if slot.cancel_event.is_set() and product is None:
                slot.state = RemoteTaskState.CANCELLED
                slot.error = "cancelled"
            elif product is not None:
                slot.state = RemoteTaskState.COMPLETED
                slot.result = product
            else:
                slot.state = RemoteTaskState.FAILED
                slot.error = error or "local execution failed"
                slot.retryable = retryable
            if product is None:
                self._failures += 1
            else:
                self._failures = 0

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve(strict=False)
        if candidate != self._root and not candidate.is_relative_to(self._root):
            raise WorkerTaskError(
                f"relative path {relative_path!r} escapes worker storage root",
                retryable=False,
                error_code="worker_path_escape",
            )
        return candidate

    # -- 只读统计（选择器/报告使用） --------------------------------------- #
    @property
    def active_executions(self) -> int:
        """当前在途执行数。"""
        with self._lock:
            return sum(1 for slot in self._slots.values() if slot.state in _ACTIVE_STATES)

    @property
    def executions(self) -> int:
        """累计已登记执行数。"""
        with self._lock:
            return len(self._slots)


_ACTIVE_STATES = frozenset({RemoteTaskState.QUEUED, RemoteTaskState.EXECUTING})


def _normalize_relative(relative_path: str) -> str:
    """规范化相对路径（拒绝绝对路径与 ``..``）。"""
    text = relative_path.replace("\\", "/").strip()
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or text.startswith("/") or any(part == ".." for part in parts):
        raise WorkerTaskError(
            f"invalid relative path {relative_path!r}",
            retryable=False,
            error_code="worker_path_escape",
        )
    return "/".join(parts)


def _sandbox_task(task: AcquisitionTask) -> AcquisitionTask:
    """派生一个不带租约的只读任务副本（执行器据此判断超时与链路，不写状态）。

    传输层只读 ``task.asset`` / ``task.task_id`` 等字段；这里去掉租约字段是为了避免
    误用主进程的租约状态，而不是为了隐藏信息。
    """
    asset: SourceAsset = task.asset
    return task.model_copy(
        update={
            "asset": asset,
            "worker_id": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
        }
    )
