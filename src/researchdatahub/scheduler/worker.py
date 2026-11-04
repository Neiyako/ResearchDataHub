"""任务执行器：``RUNNING -> transfer -> VERIFYING -> integrity -> storage -> COMPLETED``。

状态推进纪律（architecture.md §7.2 / ADR-0004）
----------------------------------------------
* 每一次状态写入都调用 :func:`core.state.transition`，并带
  ``save_task_and_event(expected_state=...)`` 做乐观并发校验；
* 每个写操作之前都检查"租约是否仍属于我"：心跳线程置位事件、数据库里
  ``worker_id`` 变了、或租约过期，都会立即停止，**不再写状态**（只追加审计事件）；
* 失败分类严格按 architecture §7.1：
  - 可重试传输失败 → ``RETRY_WAIT`` + ``scheduled_at=now+backoff``；
  - 永久失败/重试用尽 → ``FAILED``；
  - 校验失败/无法校验 → ``QUARANTINED``（产物移入隔离区）；
  - 文件已提交但 DB 写入失败 → **绝不标 COMPLETED**，返回 ``RECONCILE_REQUIRED``，
    由 :class:`~researchdatahub.scheduler.recovery.RecoveryService` 对账补记。

崩溃窗口（本模块显式处理）
--------------------------
1. **传输完成但进程崩溃**：staging/temp 路径按 ``task_id + file_name`` 稳定分配，重启后
   续跑同一个任务会复用同一路径 → M2 依据 ``.part`` + sidecar 续传；
2. **文件已提交但 DB 未更新**：提交日志（M5 ``recover_pending_commits``）保留中间态，
   对账服务据此补 ``FileRecord`` 并把任务推进到 ``COMPLETED``；
3. **DB 更新成功但文件提交失败**：本模块先提交文件、后写 DB，因此该顺序不可能出现；
   若 ``commit_staging`` 失败则任务按 ``FAILED`` 处理（附失败原因并把临时文件隔离）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from researchdatahub.config.models import DefaultsConfig
from researchdatahub.core.errors import (
    IntegrityError,
    ManifestError,
    RdhError,
    StateTransitionError,
    StorageError,
)
from researchdatahub.core.models import (
    AcquisitionTask,
    Checksum,
    FileRecord,
    IntegrityCheckKind,
    IntegrityReport,
    SourceAsset,
    TransferResult,
    TransferStatus,
)
from researchdatahub.core.ports import IntegrityChecker, ManifestRepository
from researchdatahub.core.state import TaskState, lease_expired, transition
from researchdatahub.scheduler.backoff import BackoffPolicy
from researchdatahub.scheduler.models import (
    ExecutionRecord,
    SchedulerStorage,
    TaskOutcome,
)
from researchdatahub.scheduler.transport_pool import TransportPool

__all__ = ["TaskExecutionError", "TaskExecutor"]

_MAX_ERROR_LENGTH = 2000


class _LeaseLost(Exception):
    """内部信号：执行权已不在本 worker 手上（不写入任何状态）。"""

    def __init__(self, task_id: str, detail: str) -> None:
        super().__init__(f"lease lost for task {task_id}: {detail}")
        self.task_id = task_id
        self.detail = detail


class TaskExecutionError(RdhError):
    """任务执行上下文的错误包装（仅用于非预期但可归因的缺陷）。"""

    default_code = "RDH_TASK_EXECUTION"


class TaskExecutor:
    """执行单个已领取（``RUNNING``）任务的完整管道。"""

    def __init__(
        self,
        *,
        storage: SchedulerStorage,
        integrity: IntegrityChecker,
        transports: TransportPool,
        backoff: BackoffPolicy,
        config: DefaultsConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._integrity = integrity
        self._transports = transports
        self._backoff = backoff
        self._config = config or DefaultsConfig()
        self._clock = clock or _utc_now

    # -- 公开入口 -------------------------------------------------------- #
    def execute(
        self,
        *,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
    ) -> ExecutionRecord:
        """执行一个任务并返回结构化结果（不抛业务异常；程序缺陷照常抛出）。"""
        task_id = task.task_id
        asset = task.asset
        try:
            current = self._ensure_lease(repo, task_id, owner, lease_lost)
        except _LeaseLost as exc:
            return self._lost_record(repo, task_id, exc.detail)

        # 幂等保护：任务已是 COMPLETED 时绝不重复传输/提交（重复执行保护）。
        if current.state is TaskState.COMPLETED:
            return ExecutionRecord(
                task_id=task_id,
                outcome=TaskOutcome.SKIPPED_COMPLETED,
                state=TaskState.COMPLETED,
                detail="task already COMPLETED; duplicate execution refused",
            )

        space_problem = self._preflight(current, asset)
        if space_problem is not None:
            return self._persist_failure(
                repo,
                current,
                owner,
                lease_lost,
                error=space_problem,
                error_code="insufficient_free_space",
                retryable=True,
            )

        try:
            destination = self._destination(task_id, asset)
        except (StorageError, ValueError) as exc:
            return self._persist_failure(
                repo,
                current,
                owner,
                lease_lost,
                error=f"cannot allocate destination: {exc}",
                error_code="storage_allocate_failed",
                retryable=False,
            )

        try:
            transport = self._transports.select(asset)
        except RdhError as exc:
            return self._persist_failure(
                repo,
                current,
                owner,
                lease_lost,
                error=exc.message,
                error_code="no_transport",
                retryable=False,
            )

        try:
            result = transport.transfer_outcome(
                asset, destination, task=current, cancel_event=lease_lost
            )
        except (StorageError, OSError) as exc:
            return self._persist_failure(
                repo,
                current,
                owner,
                lease_lost,
                error=f"transport storage failure: {exc}",
                error_code="storage_write_failed",
                retryable=False,
            )

        if result.status in (TransferStatus.SUCCEEDED, TransferStatus.SKIPPED):
            return self._on_transfer_success(
                repo=repo,
                task=current,
                owner=owner,
                lease_lost=lease_lost,
                destination=destination,
                result=result,
            )
        return self._on_transfer_failure(
            repo=repo,
            task=current,
            owner=owner,
            lease_lost=lease_lost,
            result=result,
        )

    # -- 前置检查 -------------------------------------------------------- #
    def _preflight(self, task: AcquisitionTask, asset: SourceAsset) -> str | None:
        """磁盘余量检查：不足时返回原因字符串（由调用方转成 RETRY_WAIT）。

        配额语义是"写入后仍需保留的余量"：``free - required >= floor`` 才允许开始，
        避免把卷写满导致 SQLite 与日志也写不进去。
        """
        floor_bytes = int(self._config.scheduler.free_space_floor_gb * 1024**3)
        required = asset.size_bytes if asset.size_bytes > 0 else 0
        try:
            free = self._storage.free_bytes()
        except StorageError as exc:
            return f"cannot query free space: {exc.message}"
        if free - required >= floor_bytes:
            return None
        return (
            f"insufficient free space: available={free}, required={required}, floor={floor_bytes}"
        )

    def _destination(self, task_id: str, asset: SourceAsset) -> Path:
        """稳定的临时路径：同一任务重跑复用同一路径（断点续传/崩溃续跑的前提）。"""
        return self._storage.allocate_temp(task_id, file_name=asset.file_name)

    # -- 传输成功 -------------------------------------------------------- #
    def _on_transfer_success(
        self,
        *,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
        destination: Path,
        result: TransferResult,
    ) -> ExecutionRecord:
        now = self._now()
        try:
            current = self._ensure_lease(repo, task.task_id, owner, lease_lost)
            verifying = transition(
                current,
                TaskState.VERIFYING,
                reason=f"transfer {result.status.value} via {result.transport_id}",
                now=now,
            )
            self._save(
                repo,
                verifying,
                expected_state=TaskState.RUNNING,
                event="transfer_completed",
                payload={
                    "transport_id": result.transport_id,
                    "status": result.status.value,
                    "bytes_transferred": result.bytes_transferred,
                    "resumed": result.resumed,
                    "destination": str(destination),
                },
            )
        except _LeaseLost as exc:
            return self._lost_record(repo, task.task_id, exc.detail)
        except ManifestError as exc:
            return self._lost_record(repo, task.task_id, f"state write conflict: {exc.message}")

        path = Path(result.destination_path) if result.destination_path else destination
        if not path.is_file():
            return self._finish_quarantine(
                repo=repo,
                task=verifying,
                owner=owner,
                lease_lost=lease_lost,
                path=path,
                reason="transferred file is missing before integrity check",
                report=None,
            )

        try:
            size_bytes = path.stat().st_size
            checksum = task.asset.checksum or Checksum.of_file(str(path))
        except (OSError, ValueError) as exc:
            return self._finish_quarantine(
                repo=repo,
                task=verifying,
                owner=owner,
                lease_lost=lease_lost,
                path=path,
                reason=f"cannot stat/hash transferred file: {exc}",
                report=None,
            )

        record = self._build_file_record(
            task=verifying,
            path=path,
            size_bytes=size_bytes,
            checksum=checksum,
            now=now,
        )
        kinds = self._checks_for(repo, task.request_id)
        try:
            report = self._integrity.check(
                record,
                task=verifying,
                kinds=list(kinds),
            )
        except IntegrityError as exc:
            return self._finish_quarantine(
                repo=repo,
                task=verifying,
                owner=owner,
                lease_lost=lease_lost,
                path=path,
                reason=f"integrity check cannot proceed: {exc.message}",
                report=None,
            )

        if not report.permits_completion:
            return self._finish_quarantine(
                repo=repo,
                task=verifying,
                owner=owner,
                lease_lost=lease_lost,
                path=path,
                reason="integrity failed: " + "; ".join(report.failures),
                report=report,
            )

        return self._commit_and_complete(
            repo=repo,
            task=verifying,
            owner=owner,
            lease_lost=lease_lost,
            path=path,
            record=record,
            report=report,
            transfer_result=result,
        )

    def _build_file_record(
        self,
        *,
        task: AcquisitionTask,
        path: Path,
        size_bytes: int,
        checksum: Checksum,
        now: datetime,
    ) -> FileRecord:
        asset = task.asset
        origin = "remote_trusted" if asset.checksum is not None else "local_computed"
        return FileRecord(
            file_id=f"file-{task.task_id}",
            task_id=task.task_id,
            dataset_id=asset.dataset_id,
            source_id=asset.source_id,
            access_route=asset.access_route,
            origin_uri=asset.origin_uri,
            local_path=str(path),
            size_bytes=size_bytes,
            checksum=checksum,
            content_type=asset.content_type,
            remote_etag=asset.remote_etag,
            remote_last_modified=asset.remote_last_modified,
            retrieved_at=now,
            is_read_only=True,
            extensions={"integrity.checksum_origin": origin},
        )

    # -- 提交与终态 ------------------------------------------------------ #
    def _commit_and_complete(
        self,
        *,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
        path: Path,
        record: FileRecord,
        report: IntegrityReport,
        transfer_result: TransferResult,
    ) -> ExecutionRecord:
        asset = task.asset
        try:
            publish = self._storage.commit_staging(
                path,
                dataset_id=asset.dataset_id,
                file_name=asset.file_name,
                expected_size=record.size_bytes,
                expected_checksum=record.checksum,
            )
        except StorageError as exc:
            # 文件提交失败：绝不标 COMPLETED；保留证据并说明原因。
            quarantine_path = self._quarantine(path, f"commit failed: {exc.message}")
            self._record_report(repo, report, file_id=None)
            return self._persist_failure(
                repo,
                task,
                owner,
                lease_lost,
                error=f"file commit failed: {exc.message}",
                error_code="storage_commit_failed",
                retryable=False,
                extra={"quarantine_path": quarantine_path, "report_id": report.report_id},
                from_state=TaskState.VERIFYING,
                outcome=TaskOutcome.FAILED,
            )

        final_record = record.model_copy(
            update={"local_path": str(publish.path), "size_bytes": publish.size_bytes}
        )
        now = self._now()
        try:
            self._ensure_lease(repo, task.task_id, owner, lease_lost)
            # 顺序不可交换：先写文件记录（外键前提），再写报告，最后推进状态；
            # 任何一步失败都不会出现"任务 COMPLETED 但证据缺失"。
            repo.save_file_record(final_record)
            repo.save_integrity_report(report)
            current = self._ensure_lease(repo, task.task_id, owner, lease_lost)
            completed = transition(
                current,
                TaskState.COMPLETED,
                reason="integrity PASSED and file committed to raw storage",
                now=now,
            )
            self._save(
                repo,
                completed,
                expected_state=TaskState.VERIFYING,
                event="task_completed",
                payload={
                    "file_id": final_record.file_id,
                    "report_id": report.report_id,
                    "path": final_record.local_path,
                    "size_bytes": final_record.size_bytes,
                    "publish_status": publish.status.value,
                    "resumed": transfer_result.resumed,
                },
            )
        except _LeaseLost as exc:
            return self._lost_record(repo, task.task_id, exc.detail)
        except (ManifestError, OSError) as exc:
            # 崩溃窗口：文件已提交，但 DB 尚未完整更新。绝不能标 COMPLETED。
            self._safe_event(
                repo,
                task.task_id,
                "db_write_failed_after_commit",
                {
                    "path": final_record.local_path,
                    "report_id": report.report_id,
                    "error": str(exc)[:_MAX_ERROR_LENGTH],
                    "message": "file committed but database write failed; recovery service "
                    "must reconcile this window",
                },
            )
            return ExecutionRecord(
                task_id=task.task_id,
                outcome=TaskOutcome.RECONCILE_REQUIRED,
                state=TaskState.VERIFYING,
                detail="file committed but database write failed; run reconcile",
                error_code="db_write_failed_after_commit",
                file_path=final_record.local_path,
                report_id=report.report_id,
            )

        outcome = (
            TaskOutcome.COMPLETED if publish.committed else TaskOutcome.STORAGE_SKIPPED_IDENTICAL
        )
        return ExecutionRecord(
            task_id=task.task_id,
            outcome=outcome,
            state=TaskState.COMPLETED,
            detail=(
                "file committed to raw storage"
                if publish.committed
                else "identical file already present in raw storage; not overwritten"
            ),
            file_path=final_record.local_path,
            report_id=report.report_id,
            resumed=transfer_result.resumed,
            bytes_transferred=transfer_result.bytes_transferred,
        )

    def _finish_quarantine(
        self,
        *,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
        path: Path,
        reason: str,
        report: IntegrityReport | None,
    ) -> ExecutionRecord:
        quarantine_path = self._quarantine(path, reason)
        if report is not None:
            self._record_report(repo, report, file_id=None)
        try:
            current = self._ensure_lease(repo, task.task_id, owner, lease_lost)
            quarantined = transition(
                current,
                TaskState.QUARANTINED,
                reason="integrity check failed; artifact quarantined",
                now=self._now(),
                error=reason[:_MAX_ERROR_LENGTH],
            )
            self._save(
                repo,
                quarantined,
                expected_state=TaskState.VERIFYING,
                event="quarantined",
                payload={
                    "reason": reason[:_MAX_ERROR_LENGTH],
                    "quarantine_path": quarantine_path,
                    "report_id": None if report is None else report.report_id,
                },
            )
        except _LeaseLost as exc:
            return self._lost_record(repo, task.task_id, exc.detail)
        except ManifestError as exc:
            return self._lost_record(repo, task.task_id, f"state write conflict: {exc.message}")
        return ExecutionRecord(
            task_id=task.task_id,
            outcome=TaskOutcome.QUARANTINED,
            state=TaskState.QUARANTINED,
            detail=reason[:_MAX_ERROR_LENGTH],
            error_code="integrity_failed",
            report_id=None if report is None else report.report_id,
            quarantine_path=quarantine_path,
        )

    # -- 传输失败 -------------------------------------------------------- #
    def _on_transfer_failure(
        self,
        *,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
        result: TransferResult,
    ) -> ExecutionRecord:
        error_code = result.error_code or "transfer_failed"
        error = f"{error_code}: {result.error_detail or 'transfer failed'}"
        if error_code == "cancelled":
            if lease_lost.is_set():
                return self._lost_record(repo, task.task_id, "transfer cancelled after lease loss")
            try:
                current = self._ensure_lease(repo, task.task_id, owner, lease_lost)
            except _LeaseLost as exc:
                return self._lost_record(repo, task.task_id, exc.detail)
            self._safe_event(
                repo,
                task.task_id,
                "transfer_cancelled",
                {"error_code": error_code, "state": current.state.value},
            )
            return ExecutionRecord(
                task_id=task.task_id,
                outcome=TaskOutcome.CANCELLED,
                state=current.state,
                detail="transfer cancelled by caller; task state is authoritative",
                error_code=error_code,
            )
        return self._persist_failure(
            repo,
            task,
            owner,
            lease_lost,
            error=error,
            error_code=error_code,
            retryable=result.retryable,
            from_state=TaskState.RUNNING,
            outcome=None,
        )

    # -- 通用失败推进 ---------------------------------------------------- #
    def _persist_failure(
        self,
        repo: ManifestRepository,
        task: AcquisitionTask,
        owner: str,
        lease_lost: threading.Event,
        *,
        error: str,
        error_code: str,
        retryable: bool,
        extra: dict[str, object] | None = None,
        from_state: TaskState | None = None,
        outcome: TaskOutcome | None = None,
    ) -> ExecutionRecord:
        """按 architecture §7.1 把失败映射到 ``RETRY_WAIT`` / ``FAILED``。"""
        try:
            current = self._ensure_lease(repo, task.task_id, owner, lease_lost)
        except _LeaseLost as exc:
            return self._lost_record(repo, task.task_id, exc.detail)
        expected = from_state or current.state
        now = self._now()
        payload: dict[str, object] = {
            "error_code": error_code,
            "error": error[:_MAX_ERROR_LENGTH],
            "retryable": retryable,
            "attempt": current.attempt,
            "max_attempts": current.max_attempts,
        }
        if extra:
            payload.update(extra)
        try:
            if retryable and current.attempt < current.max_attempts:
                delay = self._backoff.delay_seconds(current.attempt)
                retry_wait = transition(
                    current,
                    TaskState.RETRY_WAIT,
                    reason=f"retryable failure {error_code}",
                    now=now,
                    error=error[:_MAX_ERROR_LENGTH],
                )
                retry_wait = retry_wait.model_copy(
                    update={"scheduled_at": now + timedelta(seconds=delay)}
                )
                payload["delay_seconds"] = delay
                payload["scheduled_at"] = (
                    None if retry_wait.scheduled_at is None else retry_wait.scheduled_at.isoformat()
                )
                self._save(
                    repo,
                    retry_wait,
                    expected_state=expected,
                    event="retry_scheduled",
                    payload=payload,
                )
                return ExecutionRecord(
                    task_id=task.task_id,
                    outcome=TaskOutcome.RETRY_WAIT,
                    state=TaskState.RETRY_WAIT,
                    detail=error[:_MAX_ERROR_LENGTH],
                    error_code=error_code,
                    retry_at=retry_wait.scheduled_at,
                )
            failed = transition(
                current,
                TaskState.FAILED,
                reason=f"permanent failure {error_code}",
                now=now,
                error=error[:_MAX_ERROR_LENGTH],
            )
            self._save(
                repo,
                failed,
                expected_state=expected,
                event="task_failed",
                payload=payload,
            )
        except _LeaseLost as exc:
            return self._lost_record(repo, task.task_id, exc.detail)
        except (ManifestError, StateTransitionError) as exc:
            return self._lost_record(repo, task.task_id, f"failure persist conflict: {exc}")
        return ExecutionRecord(
            task_id=task.task_id,
            outcome=outcome or TaskOutcome.FAILED,
            state=TaskState.FAILED,
            detail=error[:_MAX_ERROR_LENGTH],
            error_code=error_code,
            file_path=None if extra is None else str(extra.get("quarantine_path") or "") or None,
        )

    # -- 工具 ------------------------------------------------------------ #
    def _checks_for(self, repo: ManifestRepository, request_id: str) -> tuple[str, ...]:
        """请求声明的校验链；请求未登记时退回 ``integrity.default_checks``。"""
        try:
            request = repo.get_request(request_id)
        except ManifestError:
            return tuple(kind.value for kind in self._config.integrity.default_checks)
        checks: Sequence[IntegrityCheckKind] = request.checks
        if not checks:
            return tuple(kind.value for kind in self._config.integrity.default_checks)
        return tuple(kind.value for kind in checks)

    def _ensure_lease(
        self,
        repo: ManifestRepository,
        task_id: str,
        owner: str,
        lease_lost: threading.Event,
    ) -> AcquisitionTask:
        """确认执行权仍属于本 worker；否则抛 :class:`_LeaseLost`。"""
        current = repo.load_task(task_id)
        now = self._now()
        if lease_lost.is_set():
            raise _LeaseLost(task_id, "heartbeat reported lease loss")
        if current.state not in (TaskState.RUNNING, TaskState.VERIFYING):
            raise _LeaseLost(task_id, f"task state is {current.state.value}")
        if current.worker_id != owner:
            raise _LeaseLost(task_id, f"lease owner is {current.worker_id!r}, not {owner!r}")
        if lease_expired(current, now=now):
            raise _LeaseLost(task_id, "lease expired")
        return current

    def _save(
        self,
        repo: ManifestRepository,
        task: AcquisitionTask,
        *,
        expected_state: TaskState,
        event: str,
        payload: dict[str, object] | None,
    ) -> None:
        repo.save_task_and_event(task, expected_state=expected_state, event=event, payload=payload)

    def _safe_event(
        self, repo: ManifestRepository, task_id: str, event: str, payload: dict[str, object]
    ) -> bool:
        try:
            repo.append_event(task_id, event, payload)
            return True
        except RdhError:
            return False

    def _record_report(
        self, repo: ManifestRepository, report: IntegrityReport, *, file_id: str | None
    ) -> None:
        """保存校验报告；隔离场景下 ``file_id`` 必须为 None（files 行不存在，外键约束）。"""
        effective = (
            report if report.file_id == file_id else report.model_copy(update={"file_id": file_id})
        )
        try:
            repo.save_integrity_report(effective)
        except ManifestError:
            self._safe_event(
                repo,
                report.task_id,
                "integrity_report_write_failed",
                {"report_id": report.report_id, "status": report.status.value},
            )

    def _quarantine(self, path: Path, reason: str) -> str | None:
        if not path.exists():
            return None
        try:
            target = self._storage.quarantine(path, reason=reason[:_MAX_ERROR_LENGTH])
        except StorageError:
            return None
        return str(target)

    def _lost_record(self, repo: ManifestRepository, task_id: str, detail: str) -> ExecutionRecord:
        """租约丢失：只记录审计事件，不写状态（状态由真正的持有者/恢复流程决定）。"""
        self._safe_event(repo, task_id, "lease_lost_detected", {"detail": detail})
        return ExecutionRecord(
            task_id=task_id,
            outcome=TaskOutcome.LEASE_LOST,
            state=TaskState.RUNNING,
            detail=f"execution aborted because lease was lost: {detail}",
            error_code="lease_lost",
        )

    def _now(self) -> datetime:
        return self._clock()


def _utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
