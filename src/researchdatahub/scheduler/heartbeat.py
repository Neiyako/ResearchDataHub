"""心跳与租约守卫：独立线程按 ``scheduler.heartbeat_interval_seconds`` 续约在途任务。

为什么必须是独立线程
--------------------
租约续约不能依赖 worker 的 I/O 间隙：一次大文件传输可能持续数十分钟，期间 worker 没有
任何机会写库。心跳线程用**自己的 SQLite 连接**定时把 ``heartbeat_at`` /
``lease_expires_at`` 往后推，从而把"慢任务"与"死进程"区分开（ADR-0004）。

租约丢失语义
------------
``ManifestRepository.heartbeat()`` 返回 ``False`` 表示：
* 任务已不在 ``RUNNING``/``VERIFYING``（被取消/暂停/恢复流程接管），或
* 租约持有者不是本 worker，或
* 租约已过期。

此时心跳线程置位该任务的 ``threading.Event``，worker 在下一个检查点**停止本地执行、
不再写任何状态**（只追加一条 ``lease_lost`` 审计事件），避免两个执行者互相覆盖。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from researchdatahub.core.errors import ManifestError, RdhError
from researchdatahub.core.ports import ManifestRepository

__all__ = ["HeartbeatMonitor", "InFlightTask", "LeaseGuard"]


@dataclass(frozen=True)
class InFlightTask:
    """一个正在由本进程执行、需要续约的任务。"""

    task_id: str
    owner: str


class LeaseGuard:
    """在途任务注册表 + 每任务"租约丢失"事件（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, InFlightTask] = {}
        self._events: dict[str, threading.Event] = {}
        self._lost: set[str] = set()

    def register(self, task_id: str, owner: str) -> threading.Event:
        """登记一个在途任务并返回其租约丢失事件（重复登记会替换事件对象）。"""
        with self._lock:
            event = threading.Event()
            self._inflight[task_id] = InFlightTask(task_id=task_id, owner=owner)
            self._events[task_id] = event
            self._lost.discard(task_id)
            return event

    def unregister(self, task_id: str) -> None:
        """任务执行结束，停止续约。"""
        with self._lock:
            self._inflight.pop(task_id, None)
            self._events.pop(task_id, None)
            self._lost.discard(task_id)

    def mark_lost(self, task_id: str) -> bool:
        """标记租约丢失并置位事件；返回是否确实置位（重复调用返回 False）。"""
        with self._lock:
            event = self._events.get(task_id)
            if event is None or event.is_set():
                return False
            event.set()
            self._lost.add(task_id)
            return True

    def lost(self, task_id: str) -> bool:
        """该任务是否已被判定丢失租约。"""
        with self._lock:
            return task_id in self._lost

    def snapshot(self) -> tuple[InFlightTask, ...]:
        """当前需要续约的任务（按 task_id 升序，便于确定性测试）。"""
        with self._lock:
            return tuple(self._inflight[key] for key in sorted(self._inflight))

    def active_count(self) -> int:
        """在途任务数。"""
        with self._lock:
            return len(self._inflight)


class HeartbeatMonitor:
    """后台心跳线程：周期性续约 :class:`LeaseGuard` 里登记的任务。"""

    def __init__(
        self,
        repo_factory: Callable[[], ManifestRepository],
        guard: LeaseGuard,
        *,
        interval_seconds: float,
        lease_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"heartbeat interval must be > 0, got {interval_seconds}")
        if lease_seconds < 1:
            raise ValueError(f"lease_seconds must be >= 1, got {lease_seconds}")
        self._repo_factory = repo_factory
        self._guard = guard
        self._interval = float(interval_seconds)
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._errors: list[str] = []
        self._errors_lock = threading.Lock()
        self._fatal: BaseException | None = None

    # -- 生命周期 -------------------------------------------------------- #
    def start(self) -> None:
        """启动后台线程（只能启动一次）。"""
        if self._thread is not None:
            raise RuntimeError("heartbeat monitor already started")
        self._thread = threading.Thread(target=self._run, name="rdh-heartbeat", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """请求停止（可重复调用）。"""
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        """等待线程退出。"""
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def errors(self) -> tuple[str, ...]:
        """心跳过程中记录的非致命错误（例如任务已被删除）。"""
        with self._errors_lock:
            return tuple(self._errors)

    @property
    def fatal(self) -> BaseException | None:
        """心跳线程是否因未预期异常退出（由 ``run()`` 检查并向上抛）。"""
        return self._fatal

    # -- 线程体 ---------------------------------------------------------- #
    def _run(self) -> None:
        repo: ManifestRepository | None = None
        try:
            repo = self._repo_factory()
            while not self._stop.wait(self._interval):
                for item in self._guard.snapshot():
                    self._beat(repo, item)
        except BaseException as exc:
            self._fatal = exc
        finally:
            if repo is not None:
                repo.close()

    def _beat(self, repo: ManifestRepository, item: InFlightTask) -> None:
        """续约一次；``False`` 表示租约丢失，必须让 worker 停下来。"""
        try:
            still_ours = repo.heartbeat(
                item.task_id,
                owner=item.owner,
                now=None if self._clock is None else self._clock(),
                lease_seconds=self._lease_seconds,
            )
        except ManifestError as exc:
            self._record_error(f"heartbeat failed for {item.task_id}: {exc.message}")
            return
        except RdhError as exc:
            self._record_error(f"heartbeat error for {item.task_id}: {exc.message}")
            return
        if still_ours:
            return
        observed_now = "" if self._clock is None else self._clock().isoformat()
        if self._guard.mark_lost(item.task_id):
            try:
                repo.append_event(
                    item.task_id,
                    "lease_lost",
                    {
                        "owner": item.owner,
                        "observed_at": observed_now,
                        "message": "heartbeat refused: lease lost or task no longer in flight; "
                        "worker will stop without writing state",
                    },
                )
            except RdhError as exc:
                self._record_error(f"cannot record lease_lost for {item.task_id}: {exc.message}")

    def _record_error(self, message: str) -> None:
        with self._errors_lock:
            self._errors.append(message)
