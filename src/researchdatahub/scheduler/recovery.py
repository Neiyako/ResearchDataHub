"""崩溃窗口恢复与启动对账（ADR-0004 / ADR-0003）。

三类崩溃窗口
------------
1. **传输完成但进程崩溃**：任务卡在 ``RUNNING``/``VERIFYING``，租约过期。
   启动时 :meth:`RecoveryService.release_expired` 调 ``ManifestRepository.release_expired``，
   区分"租约过期但心跳仍新"（续租，不接管）与"心跳超时"（``recover_stale`` 重新排队或判失败）。
2. **文件已提交但 DB 未更新**：``raw`` 下有正式文件、有提交日志，但 ``files`` 表没有对应行。
   :meth:`RecoveryService.reconcile` 先跑 M5 的 ``recover_pending_commits`` 修挂起提交，
   再 ``scan_orphans`` 找出目标文件，按"任务资产 → 期望原始路径"匹配任务，**重新执行完整性
   校验**后才补记 ``FileRecord`` / ``IntegrityReport`` 并推进到 ``COMPLETED``。
   未经校验的文件绝不"因为存在就当作完成"。
3. **DB 有记录但文件不存在**（外部删除/磁盘故障）：对账报告 ``FILE_MISSING``，不静默重建、
   也不把 ``COMPLETED`` 改成别的状态（终态不可撤销，需要人工介入）。

对账只移动/记录**已由提交日志或完整性校验证明过**的文件；无法归属的正式区文件按
``ORPHAN_RAW`` 报告，不删除（科研数据默认保留，删除必须显式且留审计）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from researchdatahub.config.models import DefaultsConfig
from researchdatahub.core.errors import ManifestError, RdhError, StorageError
from researchdatahub.core.models import (
    AcquisitionTask,
    Checksum,
    FileRecord,
    IntegrityReport,
    RecoveryResult,
)
from researchdatahub.core.ports import IntegrityChecker, ManifestRepository
from researchdatahub.core.state import TaskState, transition
from researchdatahub.scheduler.models import (
    ReconcileAction,
    ReconcileEntry,
    ReconcileReport,
    SchedulerStorage,
)

__all__ = ["RecoveryService"]

_MAX_ERROR_LENGTH = 2000


class RecoveryService:
    """启动恢复 + 崩溃窗口对账（每次操作使用独立连接，满足 SQLite 线程约束）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], ManifestRepository],
        storage: SchedulerStorage,
        integrity: IntegrityChecker,
        config: DefaultsConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo_factory = repo_factory
        self._storage = storage
        self._integrity = integrity
        self._config = config or DefaultsConfig()
        self._clock = clock or _utc_now

    # -- 启动恢复 -------------------------------------------------------- #
    def release_expired(self) -> tuple[RecoveryResult, ...]:
        """回收"租约过期"的在途任务（区分续租与接管，写 ``RecoveryResult`` 审计）。"""
        repo = self._repo_factory()
        try:
            return tuple(
                repo.release_expired(
                    now=self._clock(),
                    stale_timeout_seconds=self._config.integrity.stale_timeout_seconds,
                    lease_seconds=self._config.scheduler.lease_seconds,
                )
            )
        finally:
            repo.close()

    # -- 崩溃窗口对账 ---------------------------------------------------- #
    def reconcile(self) -> ReconcileReport:
        """对账：修复挂起提交、补记"文件已提交但 DB 未更新"的任务、报告文件缺失。"""
        repo = self._repo_factory()
        try:
            return self._reconcile_with(repo)
        finally:
            repo.close()

    def _reconcile_with(self, repo: ManifestRepository) -> ReconcileReport:
        entries: list[ReconcileEntry] = []
        completed: list[str] = []
        needs_attention = 0

        # 1) 先修挂起提交（文件可能只完成了一半移动）。
        try:
            recoveries = self._storage.recover_pending_commits()
        except StorageError as exc:
            entries.append(
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=None,
                    path=None,
                    detail=f"recover_pending_commits failed: {exc.message}",
                )
            )
            recoveries = ()
        for recovery in recoveries:
            detail = f"commit journal {recovery.journal_path.name}: {recovery.action.value}"
            if recovery.detail:
                detail += f" — {recovery.detail}"
            entries.append(
                ReconcileEntry(
                    action=(
                        ReconcileAction.COMPLETED
                        if recovery.action.value in ("finalized", "retried")
                        else ReconcileAction.NEEDS_ATTENTION
                    ),
                    task_id=None,
                    path=None if recovery.target_path is None else str(recovery.target_path),
                    detail=detail,
                )
            )
            if recovery.action.value == "needs_attention":
                needs_attention += 1

        # 2) 收集 DB 已知的正式文件路径 + 检测"记录存在但文件缺失"。
        known_raw: list[Path] = []
        missing_files: list[FileRecord] = []
        for record in repo.list_file_records():
            path = Path(record.local_path)
            known_raw.append(path)
            if not path.exists():
                missing_files.append(record)
        for record in missing_files:
            needs_attention += 1
            entries.append(
                ReconcileEntry(
                    action=ReconcileAction.FILE_MISSING,
                    task_id=record.task_id,
                    path=record.local_path,
                    detail="file record exists but the raw file is missing; manual action required",
                )
            )

        # 3) 扫描正式区/staging/temp 残留。
        scan = self._storage.scan_orphans(known_raw_paths=known_raw)
        recorded_paths = {str(Path(record.local_path)) for record in repo.list_file_records()}
        candidates = _dedupe_paths((*scan.pending_commit_targets, *scan.raw_orphans))
        for path in candidates:
            try:
                matches = self._match_tasks(repo, path)
            except RdhError as exc:
                entries.append(
                    ReconcileEntry(
                        action=ReconcileAction.NEEDS_ATTENTION,
                        task_id=None,
                        path=str(path),
                        detail=f"cannot match tasks for orphan: {exc.message}",
                    )
                )
                needs_attention += 1
                continue
            if len(matches) > 1:
                needs_attention += 1
                entries.append(
                    ReconcileEntry(
                        action=ReconcileAction.NEEDS_ATTENTION,
                        task_id=None,
                        path=str(path),
                        detail=(
                            "ambiguous: multiple in-flight tasks expect this path: "
                            + ", ".join(task.task_id for task in matches)
                        ),
                    )
                )
                continue
            if not matches:
                if str(path) in recorded_paths:
                    continue
                entries.append(
                    ReconcileEntry(
                        action=ReconcileAction.ORPHAN_RAW,
                        task_id=None,
                        path=str(path),
                        detail="raw file is not referenced by any file record or in-flight task",
                    )
                )
                continue
            task = matches[0]
            entry, was_completed = self._reconcile_task_path(repo, task, path)
            entries.append(entry)
            if was_completed:
                completed.append(task.task_id)
            if entry.action is ReconcileAction.NEEDS_ATTENTION:
                needs_attention += 1

        return ReconcileReport(
            entries=tuple(entries),
            completed=tuple(completed),
            needs_attention=needs_attention,
        )

    def _reconcile_task_path(
        self, repo: ManifestRepository, task: AcquisitionTask, path: Path
    ) -> tuple[ReconcileEntry, bool]:
        """把一个"已提交但 DB 未记全"的文件补记到任务上（校验通过才 COMPLETED）。"""
        existing = next(repo.list_file_records(task_id=task.task_id), None)
        if existing is not None:
            return (
                ReconcileEntry(
                    action=ReconcileAction.ALREADY_CONSISTENT,
                    task_id=task.task_id,
                    path=existing.local_path,
                    detail="file record already exists for this task",
                ),
                False,
            )
        try:
            stat = path.stat()
        except OSError as exc:
            return (
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=task.task_id,
                    path=str(path),
                    detail=f"cannot stat committed file: {exc}",
                ),
                False,
            )
        asset = task.asset
        checksum = asset.checksum or Checksum.of_file(str(path))
        origin = "remote_trusted" if asset.checksum is not None else "local_computed"
        record = FileRecord(
            file_id=f"file-{task.task_id}",
            task_id=task.task_id,
            dataset_id=asset.dataset_id,
            source_id=asset.source_id,
            access_route=asset.access_route,
            origin_uri=asset.origin_uri,
            local_path=str(path),
            size_bytes=stat.st_size,
            checksum=checksum,
            content_type=asset.content_type,
            remote_etag=asset.remote_etag,
            remote_last_modified=asset.remote_last_modified,
            retrieved_at=self._clock(),
            is_read_only=True,
            extensions={"integrity.checksum_origin": origin},
        )
        try:
            report = self._integrity.check(
                record, task=task, kinds=self._checks_for(repo, task.request_id)
            )
        except RdhError as exc:
            return (
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=task.task_id,
                    path=str(path),
                    detail=f"integrity re-check cannot proceed: {exc.message}",
                ),
                False,
            )
        if not report.permits_completion:
            return self._quarantine_after_recheck(repo, task, path, report)
        try:
            current = repo.load_task(task.task_id)
            if current.state is TaskState.RUNNING:
                verifying = transition(
                    current,
                    TaskState.VERIFYING,
                    reason="reconcile: committed file found while RUNNING",
                    now=self._clock(),
                )
                repo.save_task_and_event(
                    verifying,
                    expected_state=TaskState.RUNNING,
                    event="reconcile_verifying",
                    payload={"path": str(path)},
                )
                current = verifying
            if current.state is not TaskState.VERIFYING:
                return (
                    ReconcileEntry(
                        action=ReconcileAction.NEEDS_ATTENTION,
                        task_id=task.task_id,
                        path=str(path),
                        detail=(
                            f"task state is {current.state.value}; refusing to complete it from "
                            "reconcile"
                        ),
                    ),
                    False,
                )
            repo.save_file_record(record)
            repo.save_integrity_report(report)
            completed = transition(
                current,
                TaskState.COMPLETED,
                reason="reconciled: file committed before crash, integrity re-checked",
                now=self._clock(),
            )
            repo.save_task_and_event(
                completed,
                expected_state=TaskState.VERIFYING,
                event="reconciled_completed",
                payload={
                    "file_id": record.file_id,
                    "report_id": report.report_id,
                    "path": record.local_path,
                },
            )
        except (ManifestError, RdhError) as exc:
            return (
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=task.task_id,
                    path=str(path),
                    detail=f"cannot complete reconciled task: {exc}",
                ),
                False,
            )
        return (
            ReconcileEntry(
                action=ReconcileAction.COMPLETED,
                task_id=task.task_id,
                path=str(path),
                detail="file record + integrity report written; task COMPLETED",
            ),
            True,
        )

    def _quarantine_after_recheck(
        self,
        repo: ManifestRepository,
        task: AcquisitionTask,
        path: Path,
        report: IntegrityReport,
    ) -> tuple[ReconcileEntry, bool]:
        """对账时发现已提交文件校验失败：隔离文件并把任务推进到 QUARANTINED。"""
        try:
            quarantined_path = self._storage.quarantine(
                path, reason=f"reconcile integrity failed: {'; '.join(report.failures)}"
            )
        except StorageError as exc:
            return (
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=task.task_id,
                    path=str(path),
                    detail=f"integrity failed and quarantine failed: {exc.message}",
                ),
                False,
            )
        current = repo.load_task(task.task_id)
        try:
            if current.state is TaskState.RUNNING:
                current = transition(
                    current,
                    TaskState.VERIFYING,
                    reason="reconcile: integrity failed while RUNNING",
                    now=self._clock(),
                )
                repo.save_task_and_event(
                    current,
                    expected_state=TaskState.RUNNING,
                    event="reconcile_verifying",
                    payload={"reason": "integrity failed"},
                )
            repo.save_integrity_report(report.model_copy(update={"file_id": None}))
            quarantined = transition(
                current,
                TaskState.QUARANTINED,
                reason="reconciled file failed integrity re-check",
                now=self._clock(),
                error="; ".join(report.failures)[:_MAX_ERROR_LENGTH],
            )
            repo.save_task_and_event(
                quarantined,
                expected_state=TaskState.VERIFYING,
                event="reconciled_quarantined",
                payload={
                    "quarantine_path": str(quarantined_path),
                    "report_id": report.report_id,
                },
            )
        except RdhError as exc:
            return (
                ReconcileEntry(
                    action=ReconcileAction.NEEDS_ATTENTION,
                    task_id=task.task_id,
                    path=str(path),
                    detail=f"quarantine recorded but state update failed: {exc}",
                ),
                False,
            )
        return (
            ReconcileEntry(
                action=ReconcileAction.QUARANTINED,
                task_id=task.task_id,
                path=str(path),
                detail=f"integrity re-check failed; file quarantined at {quarantined_path}",
            ),
            False,
        )

    # -- 匹配 ------------------------------------------------------------ #
    def _match_tasks(self, repo: ManifestRepository, path: Path) -> list[AcquisitionTask]:
        """按"任务资产 → 期望正式路径"匹配在途任务（惰性遍历，不整表载入）。"""
        resolved = path.resolve()
        matches: list[AcquisitionTask] = []
        for task in repo.list_tasks():
            if task.state not in (TaskState.RUNNING, TaskState.VERIFYING):
                continue
            try:
                expected = self._storage.expected_original_path(
                    dataset_id=task.asset.dataset_id, file_name=task.asset.file_name
                )
            except (StorageError, ValueError):
                continue
            if expected.resolve() == resolved:
                matches.append(task)
        return matches

    def _checks_for(self, repo: ManifestRepository, request_id: str) -> list[str]:
        try:
            request = repo.get_request(request_id)
        except ManifestError:
            return [kind.value for kind in self._config.integrity.default_checks]
        if not request.checks:
            return [kind.value for kind in self._config.integrity.default_checks]
        return [kind.value for kind in request.checks]


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _utc_now() -> datetime:
    return datetime.now(UTC)
