"""调度器：从 Manifest 原子领取任务、有限并发执行、心跳续约、暂停/恢复/重试/取消。

公开 API
--------
* :class:`~researchdatahub.scheduler.scheduler.DefaultTaskScheduler`
  （``core.ports.TaskScheduler`` 实现；``submit`` 只改状态，``run`` 同步阻塞执行）。
* :func:`~researchdatahub.scheduler.scheduler.build_scheduler`：CLI/测试共用的装配入口。
* :class:`~researchdatahub.scheduler.recovery.RecoveryService`：启动恢复
  （``release_expired``）与崩溃窗口对账（``reconcile``）。
* :class:`~researchdatahub.scheduler.service.TaskControlService`：``rdh task status|pause|
  resume|retry|cancel`` 的 service 层。
* :class:`~researchdatahub.scheduler.heartbeat.HeartbeatMonitor`：独立心跳线程。
"""

from researchdatahub.scheduler.backoff import BackoffPolicy
from researchdatahub.scheduler.heartbeat import HeartbeatMonitor, InFlightTask, LeaseGuard
from researchdatahub.scheduler.models import (
    DEFAULT_WORKER_LIMIT,
    ExecutionRecord,
    ReconcileAction,
    ReconcileEntry,
    ReconcileReport,
    RunSummary,
    SchedulerStorage,
    TaskOutcome,
    TaskStatusView,
    TransferExecutor,
)
from researchdatahub.scheduler.recovery import RecoveryService
from researchdatahub.scheduler.scheduler import (
    DefaultTaskScheduler,
    build_scheduler,
    describe_scheduler_defaults,
)
from researchdatahub.scheduler.service import TaskControlService
from researchdatahub.scheduler.worker import TaskExecutionError, TaskExecutor

__all__ = [
    "DEFAULT_WORKER_LIMIT",
    "BackoffPolicy",
    "DefaultTaskScheduler",
    "ExecutionRecord",
    "HeartbeatMonitor",
    "InFlightTask",
    "LeaseGuard",
    "ReconcileAction",
    "ReconcileEntry",
    "ReconcileReport",
    "RecoveryService",
    "RunSummary",
    "SchedulerStorage",
    "TaskControlService",
    "TaskExecutionError",
    "TaskExecutor",
    "TaskOutcome",
    "TaskStatusView",
    "TransferExecutor",
    "build_scheduler",
    "describe_scheduler_defaults",
]
