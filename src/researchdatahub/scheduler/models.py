"""调度器的共享数据结构与能力协议。

为什么需要 ``SchedulerStorage`` / ``TransferExecutor`` 扩展协议
---------------------------------------------------------------
``core.ports`` 的 ``StorageBackend`` / ``TransportAdapter`` 只覆盖契约必需的最小面：
* 崩溃恢复要求"staging 路径跨进程稳定"，因此需要 ``allocate_temp(file_name=...)`` 与
  ``scan_orphans`` / ``recover_pending_commits``（M5 的提交日志能力）；
* M2 明确把 ``transfer_outcome()`` 作为"调度器集成"入口（失败也返回结构化
  ``TransferResult``，不靠捕获异常猜分类）。

这里用 ``Protocol`` 声明这些**已存在**的能力，而不是 import 具体实现类：
调度器仍然只依赖形状，测试可以注入真实实现（``LocalStorageBackend`` / ``HttpTransportAdapter``）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from researchdatahub.core.models import (
    AcquisitionTask,
    Checksum,
    JsonValue,
    RecoveryResult,
    SourceAsset,
    TransferResult,
)
from researchdatahub.core.ports import StorageBackend, TransportAdapter
from researchdatahub.core.state import TaskState
from researchdatahub.storage.local import CommitRecovery, OrphanScan, PublishResult

__all__ = [
    "DEFAULT_WORKER_LIMIT",
    "ExecutionRecord",
    "ReconcileAction",
    "ReconcileEntry",
    "ReconcileReport",
    "RunSummary",
    "SchedulerStorage",
    "TaskOutcome",
    "TaskStatusView",
    "TransferExecutor",
]

DEFAULT_WORKER_LIMIT: int = 2
"""并发默认值。

8GB 内存约束下，传输线程默认 2：每个线程只驻留一个 chunk（约 1 MiB），真正吃内存的是
SQLite 连接与操作系统页缓存，因此并发翻倍带来的 RSS 增长是可控的；但仍不建议在个人
工作站上盲目开到 8+。配置可显式覆盖（``scheduler.max_workers``）。
"""


@runtime_checkable
class SchedulerStorage(StorageBackend, Protocol):
    """``StorageBackend`` + 崩溃恢复所需的 M5 能力。"""

    def allocate_temp(self, task_id: str, *, file_name: str | None = None) -> Path:
        """分配进程级临时路径（同 task 同 file_name 稳定，支撑断点续传/崩溃续跑）。"""
        ...

    def allocate_staging(self, task_id: str, file_name: str) -> Path:
        """分配 staging 路径（跨进程可恢复）。"""
        ...

    def expected_original_path(self, *, dataset_id: str, file_name: str) -> Path:
        """计算正式区目标路径（不移动文件，用于对账）。"""
        ...

    def scan_orphans(
        self, *, known_raw_paths: Iterable[Path] = (), max_temp_age_seconds: float = 86_400.0
    ) -> OrphanScan:
        """扫描正式区/staging/temp 的孤儿与残留（只读）。"""
        ...

    def commit_staging(
        self,
        temp_path: Path,
        *,
        dataset_id: str,
        file_name: str,
        expected_size: int | None = None,
        expected_checksum: Checksum | None = None,
    ) -> PublishResult:
        """以提交日志保护的方式把文件提交到正式区。"""
        ...

    def recover_pending_commits(
        self, *, verify_checksum: bool = True
    ) -> tuple[CommitRecovery, ...]:
        """处理挂起提交日志（文件已移动但 DB 未更新）。"""
        ...


@runtime_checkable
class TransferExecutor(TransportAdapter, Protocol):
    """``TransportAdapter`` + M2 的"结果优先"入口。"""

    def transfer_outcome(
        self,
        asset: SourceAsset,
        destination: Path,
        *,
        task: AcquisitionTask,
        cancel_event: threading.Event | None = None,
    ) -> TransferResult:
        """执行传输；传输级失败返回 ``TransferResult(status=FAILED)`` 而不是抛异常。"""
        ...


class TaskOutcome(StrEnum):
    """一次任务执行的结果分类（写进 ``RunSummary``，供 CLI/报告直接展示）。"""

    COMPLETED = "completed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    RETRY_WAIT = "retry_wait"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    SKIPPED_COMPLETED = "skipped_completed"
    STORAGE_SKIPPED_IDENTICAL = "skipped_identical"
    RECONCILE_REQUIRED = "reconcile_required"
    ERROR = "error"


@dataclass(frozen=True)
class ExecutionRecord:
    """单个任务的一次执行结果（不可变，便于汇总与测试断言）。"""

    task_id: str
    outcome: TaskOutcome
    state: TaskState
    detail: str
    error_code: str | None = None
    file_path: str | None = None
    report_id: str | None = None
    quarantine_path: str | None = None
    retry_at: datetime | None = None
    resumed: bool = False
    bytes_transferred: int = 0

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        payload: dict[str, JsonValue] = {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "state": self.state.value,
            "detail": self.detail,
            "resumed": self.resumed,
            "bytes_transferred": self.bytes_transferred,
        }
        optional: dict[str, str | None] = {
            "error_code": self.error_code,
            "file_path": self.file_path,
            "report_id": self.report_id,
            "quarantine_path": self.quarantine_path,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
        if self.retry_at is not None:
            payload["retry_at"] = self.retry_at.isoformat()
        return payload


@dataclass(frozen=True)
class RunSummary:
    """一次 ``run()`` 的汇总：谁跑了、成功几个、为什么失败、有哪些恢复动作。"""

    started_at: datetime
    finished_at: datetime
    workers: int
    records: tuple[ExecutionRecord, ...] = ()
    recovered: tuple[RecoveryResult, ...] = ()
    errors: tuple[str, ...] = ()
    unfinished: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """按 ``TaskOutcome`` 计数的执行结果。"""
        result: dict[str, int] = {}
        for record in self.records:
            key = record.outcome.value
            result[key] = result.get(key, 0) + 1
        return result

    @property
    def completed(self) -> int:
        """``COMPLETED``（含内容相同跳过）的任务数。"""
        return sum(
            1
            for record in self.records
            if record.outcome in (TaskOutcome.COMPLETED, TaskOutcome.STORAGE_SKIPPED_IDENTICAL)
        )

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示（CLI/报告复用）。"""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "workers": self.workers,
            "counts": dict(self.counts),
            "completed": self.completed,
            "unfinished": self.unfinished,
            "recovered": [
                {
                    "task_id": item.task_id,
                    "previous_state": item.previous_state,
                    "new_state": item.new_state,
                    "reason": item.reason,
                }
                for item in self.recovered
            ],
            "errors": list(self.errors),
            "records": [record.as_dict() for record in self.records],
        }


@dataclass(frozen=True)
class TaskStatusView:
    """``rdh task status`` 的结构化输出（只展示事实，不做健康判定）。"""

    task: AcquisitionTask
    file_count: int = 0
    latest_report_status: str | None = None
    events: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        task = self.task
        return {
            "task_id": task.task_id,
            "plan_id": task.plan_id,
            "request_id": task.request_id,
            "state": task.state.value,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "priority": task.priority.value,
            "scheduled_at": None if task.scheduled_at is None else task.scheduled_at.isoformat(),
            "worker_id": task.worker_id,
            "heartbeat_at": None if task.heartbeat_at is None else task.heartbeat_at.isoformat(),
            "lease_expires_at": (
                None if task.lease_expires_at is None else task.lease_expires_at.isoformat()
            ),
            "last_error": task.last_error,
            "source_id": task.asset.source_id,
            "file_name": task.asset.file_name,
            "origin_uri": task.asset.origin_uri,
            "access_route": task.asset.access_route.value,
            "file_count": self.file_count,
            "latest_report_status": self.latest_report_status,
            "events": list(self.events),
        }


class ReconcileAction(StrEnum):
    """一次对账动作的分类。"""

    COMPLETED = "completed"
    FILE_RECORDED = "file_recorded"
    ALREADY_CONSISTENT = "already_consistent"
    ORPHAN_RAW = "orphan_raw"
    FILE_MISSING = "file_missing"
    QUARANTINED = "quarantined"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class ReconcileEntry:
    """对账中的一条发现（可审计；``detail`` 说明为什么这样处理）。"""

    action: ReconcileAction
    task_id: str | None
    path: str | None
    detail: str

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        payload: dict[str, JsonValue] = {
            "action": self.action.value,
            "detail": self.detail,
        }
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class ReconcileReport:
    """一次崩溃窗口对账的结果。"""

    entries: tuple[ReconcileEntry, ...] = ()
    completed: tuple[str, ...] = ()
    needs_attention: int = 0

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "completed": list(self.completed),
            "needs_attention": self.needs_attention,
        }


def select_worker_count(requested: int | None, configured: int) -> int:
    """计算实际并发：显式请求优先；否则 ``min(配置值, DEFAULT_WORKER_LIMIT)``。"""
    if requested is not None:
        if requested < 1:
            raise ValueError(f"max_workers must be >= 1, got {requested}")
        return requested
    if configured < 1:
        raise ValueError(f"configured scheduler.max_workers must be >= 1, got {configured}")
    return min(configured, DEFAULT_WORKER_LIMIT)


def dedupe_sequential(items: Sequence[str]) -> tuple[str, ...]:
    """保持顺序去重（事件/来源 ID 列表展示用）。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)
