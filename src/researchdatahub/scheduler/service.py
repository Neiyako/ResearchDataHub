"""调度器控制面 service：``rdh task status|pause|resume|retry|cancel`` 的实现。

设计原则
--------
* **状态机是唯一权威**：每个动作都通过 :func:`core.state.transition`；非法动作抛
  ``StateTransitionError`` 并给中文原因，不做"看起来像成功"的静默降级。
* **写操作带乐观并发**：``save_task_and_event(expected_state=current.state)``；
  并发的另一个执行者（worker/恢复流程）已改过状态时，本次动作失败而不是覆盖。
* **不启动 worker**：这些命令只改状态，真正执行由 ``rdh task run`` 负责。

命令面对应关系（供 Lead 在 ``cli/main.py`` 注册）
-------------------------------------------------
``rdh task status <task_id>``  → :meth:`TaskControlService.status`
``rdh task pause <task_id>``   → :meth:`TaskControlService.pause`
``rdh task resume <task_id>``  → :meth:`TaskControlService.resume`
``rdh task retry <task_id>``   → :meth:`TaskControlService.retry`
``rdh task cancel <task_id>``  → :meth:`TaskControlService.cancel`
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from researchdatahub.core.errors import StateTransitionError
from researchdatahub.core.models import AcquisitionTask
from researchdatahub.core.ports import ManifestRepository
from researchdatahub.core.state import TERMINAL_STATES, TaskState, transition
from researchdatahub.scheduler.models import TaskStatusView

__all__ = ["TaskControlService"]

_PAUSABLE: frozenset[TaskState] = frozenset(
    {TaskState.QUEUED, TaskState.RUNNING, TaskState.RETRY_WAIT}
)
"""状态机允许且业务上可暂停的状态（``PLANNED`` 尚未入队，无需暂停）。"""


class TaskControlService:
    """面向 CLI 的任务控制服务（一个仓库连接；调用方负责关闭）。"""

    def __init__(
        self, repo: ManifestRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._repo = repo
        self._clock = clock or _utc_now

    # -- 查询 ------------------------------------------------------------ #
    def status(self, task_id: str) -> TaskStatusView:
        """读取任务状态快照；不修改任何状态。

        Raises
        ------
        ManifestError
            任务不存在。
        """
        task = self._repo.load_task(task_id)
        files = tuple(self._repo.list_file_records(task_id=task_id))
        reports = self._repo.list_integrity_reports(task_id=task_id)
        events = self._repo.list_events(task_id, limit=20)
        latest_status = reports[-1].status.value if reports else None
        return TaskStatusView(
            task=task,
            file_count=len(files),
            latest_report_status=latest_status,
            events=tuple(f"{event.event_type}: {event.message}" for event in events),
        )

    # -- 暂停 / 恢复 ------------------------------------------------------ #
    def pause(self, task_id: str, reason: str = "用户暂停") -> AcquisitionTask:
        """暂停任务（``QUEUED``/``RUNNING``/``RETRY_WAIT`` → ``PAUSED``）。

        对 ``RUNNING`` 任务：状态立即变为 ``PAUSED``，正在执行的 worker 会在下一次心跳/写库
        检查时发现租约丢失并停止（不会再写状态）。已经下载的临时文件按可续传语义保留。

        Raises
        ------
        StateTransitionError
            任务已终态或当前状态不允许暂停。
        """
        task = self._repo.load_task(task_id)
        if task.state in TERMINAL_STATES:
            raise StateTransitionError(
                f"task {task_id!r} is already terminal ({task.state.value}); cannot pause",
                from_state=task.state.value,
                to_state=TaskState.PAUSED.value,
                task_id=task_id,
            )
        if task.state not in _PAUSABLE:
            raise StateTransitionError(
                f"task {task_id!r} in state {task.state.value} cannot be paused; "
                f"pausable states: {sorted(state.value for state in _PAUSABLE)}",
                from_state=task.state.value,
                to_state=TaskState.PAUSED.value,
                task_id=task_id,
            )
        return self._apply(
            task,
            TaskState.PAUSED,
            reason=reason,
            event="task_paused",
            payload={"previous_state": task.state.value, "reason": reason},
        )

    def resume(self, task_id: str, reason: str = "用户恢复") -> AcquisitionTask:
        """恢复被暂停的任务（``PAUSED`` → ``QUEUED``）。

        Raises
        ------
        StateTransitionError
            任务不是 ``PAUSED``。
        """
        task = self._repo.load_task(task_id)
        if task.state is not TaskState.PAUSED:
            raise StateTransitionError(
                f"task {task_id!r} is in state {task.state.value}, not PAUSED; nothing to resume",
                from_state=task.state.value,
                to_state=TaskState.QUEUED.value,
                task_id=task_id,
            )
        resumed = self._apply(
            task,
            TaskState.QUEUED,
            reason=reason,
            event="task_resumed",
            payload={"reason": reason},
        )
        # 恢复后立即可领取：清掉历史退避时间（否则要等 scheduled_at 到期）。
        if resumed.scheduled_at is not None:
            resumed = resumed.model_copy(update={"scheduled_at": None})
            self._repo.save_task(resumed, expected_state=TaskState.QUEUED)
        return resumed

    # -- 取消 ------------------------------------------------------------ #
    def cancel(self, task_id: str, reason: str = "用户取消") -> AcquisitionTask:
        """取消任务（任意非终态 → ``CANCELLED``，终态不可取消）。

        Raises
        ------
        StateTransitionError
            任务已是终态。
        """
        task = self._repo.load_task(task_id)
        if task.state in TERMINAL_STATES:
            raise StateTransitionError(
                f"task {task_id!r} is already terminal ({task.state.value}); cannot cancel",
                from_state=task.state.value,
                to_state=TaskState.CANCELLED.value,
                task_id=task_id,
            )
        return self._apply(
            task,
            TaskState.CANCELLED,
            reason=reason,
            event="task_cancelled",
            payload={"previous_state": task.state.value, "reason": reason},
        )

    # -- 重试 ------------------------------------------------------------ #
    def retry(self, task_id: str, reason: str = "用户重试") -> AcquisitionTask:
        """立即重试：把 ``RETRY_WAIT`` 的任务提前拉回 ``QUEUED``（忽略退避时间）。

        说明（与 ADR-0004 一致）：``FAILED``/``QUARANTINED`` 是**终态**，不能原地重试。
        对它们必须重新规划（``rdh plan create`` 会产生新任务），这样"失败原因/证据"不会被
        一次重试抹掉。这里给出明确错误而不是偷偷改状态。

        Raises
        ------
        StateTransitionError
            任务不是 ``RETRY_WAIT``（例如仍在执行、已终态、或被暂停需先 ``resume``）。
        """
        task = self._repo.load_task(task_id)
        if task.state is TaskState.RETRY_WAIT:
            retried = self._apply(
                task,
                TaskState.QUEUED,
                reason=reason,
                event="task_retried",
                payload={"reason": reason, "attempt": task.attempt},
            )
            if retried.scheduled_at is not None:
                retried = retried.model_copy(update={"scheduled_at": None})
                self._repo.save_task(retried, expected_state=TaskState.QUEUED)
            return retried
        if task.state in TERMINAL_STATES:
            raise StateTransitionError(
                f"task {task_id!r} is terminal ({task.state.value}); retry is not allowed. "
                "Re-plan the request to create a new task so the failure evidence is preserved",
                from_state=task.state.value,
                to_state=TaskState.QUEUED.value,
                task_id=task_id,
            )
        raise StateTransitionError(
            f"task {task_id!r} in state {task.state.value} cannot be retried; "
            "only RETRY_WAIT tasks can be retried immediately",
            from_state=task.state.value,
            to_state=TaskState.QUEUED.value,
            task_id=task_id,
        )

    # -- 内部 ------------------------------------------------------------ #
    def _apply(
        self,
        task: AcquisitionTask,
        target: TaskState,
        *,
        reason: str,
        event: str,
        payload: dict[str, object],
    ) -> AcquisitionTask:
        """经状态机推进并同事务写审计事件；状态冲突时把底层错误原样抛出。"""
        updated = transition(task, target, reason=reason, now=self._clock())
        self._repo.save_task_and_event(
            updated,
            expected_state=task.state,
            event=event,
            payload=dict(payload),
        )
        return updated


def _utc_now() -> datetime:
    return datetime.now(UTC)
