"""任务调度器：从 Manifest 原子领取任务、有限并发执行、心跳续约、汇总结果。

为什么"没有内存队列"
--------------------
任务的可执行状态**只**存在 SQLite 里（ADR-0003）。``run()`` 的每个 worker 线程循环调用
``ManifestRepository.claim_tasks()``：单条 ``BEGIN IMMEDIATE`` 事务 + 状态机推进，
数据库写锁保证同一个任务不会被两个 worker 领到。进程崩溃后重启，未完成任务仍在库里，
由 ``release_expired`` 重新排队——不需要额外的队列持久化，也没有"内存队列与库不一致"的窗口。

并发与内存
----------
* 并发由 ``scheduler.max_workers`` 决定，默认上限 :data:`DEFAULT_WORKER_LIMIT`（2）；
* 每个 worker 线程/心跳线程**各自**创建 SQLite 连接（``sqlite3`` 连接不可跨线程共享）；
* 传输是流式的（每次一个 chunk），调度器自身不缓存文件内容。

``run()`` 退出条件
------------------
没有**当前可领取**的任务时退出：``QUEUED`` 会立即执行；``RETRY_WAIT`` 只有在
``scheduled_at <= now`` 时才可领取，否则本轮结束（由下一次 ``run`` 或定时任务继续）。
退出是"本轮结束"，不是"请求完成"——汇总里的 ``unfinished`` 给出仍有未终态任务的计数。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from researchdatahub.config.models import DefaultsConfig
from researchdatahub.core.errors import RdhError, ScheduleError
from researchdatahub.core.models import (
    AcquisitionPlan,
    AcquisitionTask,
    RecoveryResult,
)
from researchdatahub.core.ports import IntegrityChecker, ManifestRepository
from researchdatahub.core.state import TERMINAL_STATES, TaskState, transition
from researchdatahub.scheduler.backoff import BackoffPolicy
from researchdatahub.scheduler.heartbeat import HeartbeatMonitor, LeaseGuard
from researchdatahub.scheduler.models import (
    DEFAULT_WORKER_LIMIT,
    ExecutionRecord,
    ReconcileReport,
    RunSummary,
    SchedulerStorage,
    TaskOutcome,
    TaskStatusView,
    TransferExecutor,
    select_worker_count,
)
from researchdatahub.scheduler.recovery import RecoveryService
from researchdatahub.scheduler.service import TaskControlService
from researchdatahub.scheduler.transport_pool import TransportPool
from researchdatahub.scheduler.worker import TaskExecutor

__all__ = ["DefaultTaskScheduler", "build_scheduler", "describe_scheduler_defaults"]


@dataclass
class _RunCounters:
    """多 worker 共享的计数器（带锁；只统计事实，不参与调度决策）。"""

    claimed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _records: list[ExecutionRecord] = field(default_factory=list)
    _errors: list[str] = field(default_factory=list)

    def claim(self) -> None:
        with self._lock:
            self.claimed += 1

    def record(self, record: ExecutionRecord) -> None:
        with self._lock:
            self._records.append(record)

    def error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)

    def snapshot(self) -> tuple[int, tuple[ExecutionRecord, ...], tuple[str, ...]]:
        with self._lock:
            return self.claimed, tuple(self._records), tuple(self._errors)


class DefaultTaskScheduler:
    """``core.ports.TaskScheduler`` 的实现（同步 + 线程池，见 architecture §4.1）。"""

    def __init__(
        self,
        *,
        repo_factory: Callable[[], ManifestRepository],
        storage: SchedulerStorage,
        integrity: IntegrityChecker,
        transports: Sequence[TransferExecutor],
        config: DefaultsConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        random_source: Callable[[], float] | None = None,
        worker_prefix: str = "rdh-worker",
        heartbeat_interval_seconds: float | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        self._repo_factory = repo_factory
        self._storage = storage
        self._config = config or DefaultsConfig()
        self._clock = clock or _utc_now
        self._pool = TransportPool(transports)
        self._backoff = BackoffPolicy(self._config.retry, random_source=random_source)
        self._executor = TaskExecutor(
            storage=storage,
            integrity=integrity,
            transports=self._pool,
            backoff=self._backoff,
            config=self._config,
            clock=self._clock,
        )
        self._recovery = RecoveryService(
            repo_factory=repo_factory,
            storage=storage,
            integrity=integrity,
            config=self._config,
            clock=self._clock,
        )
        self._heartbeat_interval = (
            self._config.scheduler.heartbeat_interval_seconds
            if heartbeat_interval_seconds is None
            else float(heartbeat_interval_seconds)
        )
        self._lease_seconds = (
            self._config.scheduler.lease_seconds if lease_seconds is None else int(lease_seconds)
        )
        if self._heartbeat_interval <= 0:
            raise ScheduleError("heartbeat_interval_seconds must be > 0")
        if self._lease_seconds < 1:
            raise ScheduleError("lease_seconds must be >= 1")
        if self._heartbeat_interval * 3 > self._lease_seconds:
            raise ScheduleError(
                "invalid lease configuration: heartbeat_interval * 3 must be <= lease_seconds",
                detail={
                    "heartbeat_interval_seconds": self._heartbeat_interval,
                    "lease_seconds": self._lease_seconds,
                },
            )
        self._worker_prefix = worker_prefix
        self._run_lock = threading.Lock()
        self._running = False

    # -- 属性 ------------------------------------------------------------ #
    @property
    def transport_ids(self) -> tuple[str, ...]:
        """已装配的传输实现 ID。"""
        return self._pool.transport_ids

    @property
    def heartbeat_interval_seconds(self) -> float:
        """心跳间隔（秒）。"""
        return self._heartbeat_interval

    @property
    def lease_seconds(self) -> int:
        """租约时长（秒）。"""
        return self._lease_seconds

    # -- TaskScheduler 协议 --------------------------------------------- #
    def submit(self, plan: AcquisitionPlan) -> int:
        """把计划中的 ``PLANNED`` 任务推进到 ``QUEUED``，返回本次真正入队的数量。

        幂等：重复调用不会重复入队（已是 ``QUEUED``/在途/终态的任务被跳过并计数）。
        计划若尚未落库，会先 ``save_plan``（``save_plan`` 对同 plan_id 幂等）。
        不使用内存队列：入队 = 数据库里的状态转换，进程崩溃也不会丢。
        """
        repo = self._repo_factory()
        try:
            try:
                repo.load_plan(plan.plan_id)
            except RdhError:
                repo.save_plan(plan)
            submitted = 0
            for task in plan.tasks:
                current = repo.load_task(task.task_id)
                if current.state is not TaskState.PLANNED:
                    continue
                queued = transition(
                    current,
                    TaskState.QUEUED,
                    reason=f"submitted by scheduler for plan {plan.plan_id}",
                    now=self._clock(),
                )
                repo.save_task_and_event(
                    queued,
                    expected_state=TaskState.PLANNED,
                    event="task_queued",
                    payload={"plan_id": plan.plan_id, "request_id": plan.request_id},
                )
                submitted += 1
            return submitted
        finally:
            repo.close()

    def run(
        self,
        *,
        max_workers: int | None = None,
        request_id: str | None = None,
        max_tasks: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> RunSummary:
        """执行当前可领取的任务直到没有可领取任务或收到停止信号。

        Parameters
        ----------
        max_workers:
            并发 worker 数；``None`` 用 ``min(scheduler.max_workers, 2)``。
        request_id:
            只执行该请求下的任务（``claim_tasks`` 下推到 SQL）。
        max_tasks:
            本轮最多领取多少个任务（测试与限流用）。
        stop_event:
            外部停止信号（例如 Ctrl-C）；置位后不再领取新任务，在途任务等其自然结束。

        Raises
        ------
        ScheduleError
            并发非法、调度器已在运行、或心跳线程崩溃。
        """
        with self._run_lock:
            if self._running:
                raise ScheduleError(
                    "scheduler is already running; run() is not reentrant",
                    detail={"worker_prefix": self._worker_prefix},
                )
            self._running = True
        try:
            return self._run_locked(
                max_workers=max_workers,
                request_id=request_id,
                max_tasks=max_tasks,
                stop_event=stop_event,
            )
        finally:
            with self._run_lock:
                self._running = False

    # -- 控制面（pause/resume/cancel/retry/status） ----------------------- #
    def cancel(self, task_id: str, reason: str) -> None:
        """取消任务（终态校验由状态机与 service 层共同保证）。"""
        repo = self._repo_factory()
        try:
            TaskControlService(repo, clock=self._clock).cancel(task_id, reason)
        finally:
            repo.close()

    def pause(self, task_id: str, reason: str = "用户暂停") -> AcquisitionTask:
        """暂停任务（``QUEUED``/``RUNNING``/``RETRY_WAIT`` → ``PAUSED``）。"""
        repo = self._repo_factory()
        try:
            return TaskControlService(repo, clock=self._clock).pause(task_id, reason)
        finally:
            repo.close()

    def resume(self, task_id: str, reason: str = "用户恢复") -> AcquisitionTask:
        """恢复任务（``PAUSED`` → ``QUEUED``）。"""
        repo = self._repo_factory()
        try:
            return TaskControlService(repo, clock=self._clock).resume(task_id, reason)
        finally:
            repo.close()

    def retry(self, task_id: str, reason: str = "用户重试") -> AcquisitionTask:
        """立即重试：``RETRY_WAIT`` → ``QUEUED``（忽略退避时间）。"""
        repo = self._repo_factory()
        try:
            return TaskControlService(repo, clock=self._clock).retry(task_id, reason)
        finally:
            repo.close()

    def status(self, task_id: str) -> TaskStatusView:
        """任务状态快照（状态、租约、文件数、最近报告与事件）。"""
        repo = self._repo_factory()
        try:
            return TaskControlService(repo, clock=self._clock).status(task_id)
        finally:
            repo.close()

    # -- 恢复 ------------------------------------------------------------ #
    def release_expired(self) -> tuple[RecoveryResult, ...]:
        """启动恢复：回收租约过期/心跳超时的在途任务。"""
        return self._recovery.release_expired()

    def reconcile(self) -> ReconcileReport:
        """崩溃窗口对账（文件已提交但 DB 未更新 / 文件缺失 / 孤儿）。"""
        return self._recovery.reconcile()

    # -- 内部实现 -------------------------------------------------------- #
    def _run_locked(
        self,
        *,
        max_workers: int | None,
        request_id: str | None,
        max_tasks: int | None,
        stop_event: threading.Event | None,
    ) -> RunSummary:
        started_at = self._clock()
        try:
            workers = select_worker_count(max_workers, self._config.scheduler.max_workers)
        except ValueError as exc:
            raise ScheduleError(str(exc)) from exc
        if max_tasks is not None and max_tasks < 1:
            raise ScheduleError(f"max_tasks must be >= 1, got {max_tasks}")

        recovered = self.release_expired()
        counters = _RunCounters()
        guard = LeaseGuard()
        run_stop = stop_event or threading.Event()
        heartbeat = HeartbeatMonitor(
            self._repo_factory,
            guard,
            interval_seconds=self._heartbeat_interval,
            lease_seconds=self._lease_seconds,
            clock=self._clock,
        )
        threads = [
            threading.Thread(
                target=self._worker_loop,
                args=(index, run_stop, guard, counters, request_id, max_tasks),
                name=f"{self._worker_prefix}-{index}",
                daemon=True,
            )
            for index in range(workers)
        ]
        heartbeat.start()
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            heartbeat.request_stop()
            heartbeat.join(timeout=max(5.0, self._heartbeat_interval * 2))
        fatal = heartbeat.fatal
        if fatal is not None:
            raise ScheduleError(
                f"heartbeat thread crashed: {fatal!r}; run aborted (in-flight tasks keep "
                "their leases and will be recovered by release_expired)"
            ) from fatal
        _, records, errors = counters.snapshot()
        heartbeat_errors = heartbeat.errors
        all_errors = (*errors, *heartbeat_errors)
        unfinished = self._count_unfinished(request_id)
        return RunSummary(
            started_at=started_at,
            finished_at=self._clock(),
            workers=workers,
            records=records,
            recovered=recovered,
            errors=all_errors,
            unfinished=unfinished,
        )

    def _worker_loop(
        self,
        index: int,
        run_stop: threading.Event,
        guard: LeaseGuard,
        counters: _RunCounters,
        request_id: str | None,
        max_tasks: int | None,
    ) -> None:
        """一个 worker 线程：claim → execute → 循环（每线程独立 SQLite 连接）。"""
        owner = f"{self._worker_prefix}-{index}"
        repo: ManifestRepository | None = None
        try:
            repo = self._repo_factory()
            while not run_stop.is_set():
                if max_tasks is not None:
                    claimed_so_far, _, _ = counters.snapshot()
                    if claimed_so_far >= max_tasks:
                        return
                try:
                    claimed = repo.claim_tasks(
                        owner=owner,
                        limit=1,
                        now=self._clock(),
                        lease_seconds=self._lease_seconds,
                        request_id=request_id,
                    )
                except RdhError as exc:
                    counters.error(f"worker {owner} claim failed: {exc}")
                    return
                if not claimed:
                    return
                task = claimed[0]
                counters.claim()
                lease_lost = guard.register(task.task_id, owner)
                try:
                    record = self._executor.execute(
                        repo=repo, task=task, owner=owner, lease_lost=lease_lost
                    )
                except RdhError as exc:
                    # 非预期业务异常：不伪造状态（任务仍 RUNNING），让租约过期后由恢复流程接管。
                    self._append_worker_error(repo, task, owner, exc)
                    record = ExecutionRecord(
                        task_id=task.task_id,
                        outcome=TaskOutcome.ERROR,
                        state=TaskState.RUNNING,
                        detail=f"worker error: {exc}",
                    )
                except BaseException:
                    raise
                finally:
                    guard.unregister(task.task_id)
                counters.record(record)
        finally:
            if repo is not None:
                repo.close()

    def _append_worker_error(
        self, repo: ManifestRepository, task: AcquisitionTask, owner: str, exc: RdhError
    ) -> None:
        try:
            repo.append_event(
                task.task_id,
                "worker_error",
                {
                    "owner": owner,
                    "error": str(exc)[:2000],
                    "message": "worker aborted without writing state; lease recovery will "
                    "requeue the task",
                },
            )
        except RdhError:
            return

    def _count_unfinished(self, request_id: str | None) -> int:
        """统计仍有未终态任务的请求（惰性遍历，不整表载入内存）。"""
        repo = self._repo_factory()
        try:
            count = 0
            for task in repo.list_tasks(request_id=request_id):
                if task.state not in TERMINAL_STATES:
                    count += 1
            return count
        finally:
            repo.close()


# --------------------------------------------------------------------------- #
# 装配工厂（CLI 与测试共用同一条装配路径）
# --------------------------------------------------------------------------- #
def build_scheduler(
    *,
    repo_factory: Callable[[], ManifestRepository],
    storage: SchedulerStorage,
    integrity: IntegrityChecker,
    transports: Sequence[TransferExecutor],
    config: DefaultsConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    random_source: Callable[[], float] | None = None,
    heartbeat_interval_seconds: float | None = None,
    lease_seconds: int | None = None,
) -> DefaultTaskScheduler:
    """按配置装配调度器（``rdh task run`` 的 service 入口）。"""
    return DefaultTaskScheduler(
        repo_factory=repo_factory,
        storage=storage,
        integrity=integrity,
        transports=transports,
        config=config,
        clock=clock,
        random_source=random_source,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        lease_seconds=lease_seconds,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def describe_scheduler_defaults() -> dict[str, object]:
    """CLI 帮助文本用的默认值说明（不读配置文件，只反映代码常量）。"""
    return {
        "default_worker_limit": DEFAULT_WORKER_LIMIT,
        "lease_seconds": 900,
        "heartbeat_interval_seconds": 60,
    }
