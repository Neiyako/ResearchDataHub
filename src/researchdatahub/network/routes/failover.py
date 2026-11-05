"""故障转移与重试编排：首选链路失败/节点失联 → 记录 → 选下一条 → 全部失败抛结构化错误。

一次 :meth:`FailoverCoordinator.run` 的完整流程
----------------------------------------------
::

    任务 + 提供方
        │
        ├─ register(worker)        若节点尚未登记（远程节点会先做真实 /v1/health）
        ├─ select()                按实测统计排序；全部被拒 → NoRouteAvailable
        ├─ 逐条尝试（带预算）:
        │     acquire → prepare → 轮询状态（心跳续命 + 总超时 + 取消）
        │     → fetch 回传 → 重算 sha256 校验 → 通过则返回
        │     → 任何失败：record_attempt(失败样本) → 节点记一次失败 → 下一条链路
        └─ 预算耗尽 / 全部失败 → FailoverExhausted（附每条链路的尝试记录）

失败分类
--------
* **可重试/可转移**：连接错误、超时、5xx、worker 报告 ``retryable=True``、回传校验不符。
* **不可转移**：调用方取消（``cancelled``）、凭据错误（401/403）、路径越界——这些换节点
  也一样失败，继续尝试只会浪费时间并放大日志。
* 远端"失联"：连续超时/连接错误达到 :class:`FailoverCoordinator` 的阈值后，把该节点登记为
  ``offline``，后续选路立即跳过它（不只是本次尝试）。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from researchdatahub.core.models import AcquisitionTask
from researchdatahub.network.routes.manager import NoRouteAvailable, RouteManager
from researchdatahub.network.routes.models import Route, RouteOption
from researchdatahub.network.workers.base import (
    FetchReport,
    WorkerExecution,
    WorkerProtocol,
    WorkerTaskError,
)
from researchdatahub.network.workers.client import (
    RemoteProtocolError,
    RemoteUnavailableError,
    RemoteWorkerClient,
)
from researchdatahub.network.workers.models import RemoteTaskState, WorkerRecord
from researchdatahub.network.workers.registry import WorkerRegistry
from researchdatahub.storage.naming import sha256_file

__all__ = [
    "DEFAULT_MAX_ROUTES_PER_ATTEMPT",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS",
    "DEFAULT_WORKER_OFFLINE_AFTER_FAILURES",
    "AttemptRecord",
    "FailoverCoordinator",
    "FailoverExhausted",
    "TransferDelivery",
    "TransferredFile",
]

DEFAULT_MAX_ROUTES_PER_ATTEMPT: Final[int] = 4
"""一次任务最多尝试的链路条数（防止链路爆炸导致无限重试）。"""

DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS: Final[float] = 1800.0
"""单条远程链路的总超时（秒）；超时即视为该链路失败并转移。"""

DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 0.05
"""远程任务轮询间隔（秒）。"""

DEFAULT_WORKER_OFFLINE_AFTER_FAILURES: Final[int] = 3
"""同一节点连续失败达到该次数即登记为 ``offline``（失联检测）。"""


@dataclass(frozen=True, slots=True)
class TransferredFile:
    """一次成功交付的产物（本机路径 + 实测字节数 + 实测 sha256）。"""

    path: Path
    size_bytes: int
    sha256: str
    relative_path: str
    resumed: bool = False


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """一次链路尝试的记录（成功与失败都保留，用于报告与故障转移决策）。"""

    route_id: str
    worker_id: str
    protocol: str
    outcome: str
    detail: str
    latency_ms: float
    duration_s: float
    bytes_transferred: int
    retryable: bool
    retried: bool = False

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示。"""
        return {
            "route_id": self.route_id,
            "worker_id": self.worker_id,
            "protocol": self.protocol,
            "outcome": self.outcome,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 3),
            "duration_s": round(self.duration_s, 6),
            "bytes_transferred": self.bytes_transferred,
            "retryable": self.retryable,
            "retried": self.retried,
        }


@dataclass(frozen=True, slots=True)
class TransferDelivery:
    """一次成功交付的完整结果（含实际使用的链路与全部尝试记录）。"""

    task_id: str
    route: Route
    file: TransferredFile
    attempts: tuple[AttemptRecord, ...] = ()

    @property
    def route_id(self) -> str:
        """最终成功的链路 ID。"""
        return self.route.route_id

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示。"""
        return {
            "task_id": self.task_id,
            "route": {
                "route_id": self.route.route_id,
                "protocol": self.route.protocol,
                "worker_id": self.route.worker_id,
                "provider_id": self.route.provider_id,
            },
            "path": str(self.file.path),
            "size_bytes": self.file.size_bytes,
            "sha256": self.file.sha256,
            "resumed": self.file.resumed,
            "attempts": [item.as_dict() for item in self.attempts],
        }


class FailoverExhausted(Exception):
    """所有候选链路都失败（结构化错误：每条链路的失败原因都在 ``attempts`` 里）。"""

    def __init__(
        self,
        *,
        task_id: str,
        provider_id: str,
        attempts: Sequence[AttemptRecord],
        message: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.provider_id = provider_id
        self.attempts = tuple(attempts)
        self.code = "RDH_FAILOVER_EXHAUSTED"
        summary = message or (
            f"all {len(self.attempts)} route attempt(s) failed for task {task_id!r} "
            f"(provider {provider_id!r})"
        )
        super().__init__(summary)

    @property
    def retryable(self) -> bool:
        """是否值得整体重试（全部失败都是不可转移原因时返回 ``False``）。"""
        return any(item.retryable for item in self.attempts) if self.attempts else True

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示。"""
        return {
            "error": "FailoverExhausted",
            "code": self.code,
            "message": str(self),
            "task_id": self.task_id,
            "provider_id": self.provider_id,
            "retryable": self.retryable,
            "attempts": [item.as_dict() for item in self.attempts],
        }


@dataclass
class _AttemptContext:
    """单条链路尝试的可变上下文（真实首字节延迟、已接收字节、重试标记）。"""

    route: Route
    started: float = 0.0
    retried: bool = False
    latency_ms: float = 0.0
    bytes_transferred: int = 0


class FailoverCoordinator:
    """在多个 worker/链路上执行同一任务，失败自动转移。

    Parameters
    ----------
    registry:
        节点注册表（健康度、失联登记与真实结果记账）。
    routes:
        链路选择器与实测统计。
    workers:
        ``worker_id -> WorkerProtocol`` 的解析器（本地/远程实现均可）。
    max_routes:
        一次任务最多尝试的链路条数。
    remote_task_timeout_s:
        单条远程链路的总超时（秒）。
    poll_interval_s:
        远程任务轮询间隔（秒）。
    worker_offline_after_failures:
        同一节点连续失败多少次后登记 ``offline``。
    clock, sleep, monotonic:
        可注入的时间函数（测试固定节奏，避免真实等待）。
    """

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        routes: RouteManager,
        workers: dict[str, WorkerProtocol],
        max_routes: int = DEFAULT_MAX_ROUTES_PER_ATTEMPT,
        remote_task_timeout_s: float = DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_SECONDS,
        worker_offline_after_failures: int = DEFAULT_WORKER_OFFLINE_AFTER_FAILURES,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if max_routes < 1:
            raise ValueError("max_routes must be >= 1")
        if remote_task_timeout_s <= 0:
            raise ValueError("remote_task_timeout_s must be > 0")
        self._registry = registry
        self._routes = routes
        self._workers = dict(workers)
        self._max_routes = max_routes
        self._remote_timeout = remote_task_timeout_s
        self._poll_interval = poll_interval_s
        self._offline_after = worker_offline_after_failures
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        # 单调时钟同时兜底"耗时测量"与"等待到点"两件事：默认是真实时钟；测试注入假时钟时，
        # 假时钟的 ``advance()`` 也必须同时驱动这两条路径，否则超时判定会失真。
        self._monotonic = monotonic or time.monotonic
        self._failures: dict[str, int] = {}
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- 装配辅助 ---------------------------------------------------------- #
    def add_worker(self, worker: WorkerProtocol) -> None:
        """注册一个可用的 worker 实现（本地或远程）。"""
        self._workers[worker.worker_id] = worker

    def close(self) -> None:
        """关闭本协调器持有的全部 worker 客户端（幂等；外部注入的引用也会被关闭）。

        关闭是"释放连接"而不是"删除节点"：注册表里的节点记录仍在，下次心跳可以复活。
        """
        for worker in self._workers.values():
            try:
                worker.close()
            except Exception:
                continue

    def ensure_remote_worker(
        self,
        *,
        worker_id: str,
        endpoint: str,
        token: str | None = None,
        region: str | None = None,
        protocol: str = "https",
        artifact_root: Path | None = None,
    ) -> WorkerRecord:
        """登记并**真实探活**一个远程节点，把它接入注册表与选路。

        参数
        ----
        artifact_root:
            该节点回传产物在本机的落盘根目录；``None`` 时用
            ``<控制面库目录>/remote-artifacts/<worker_id>``。

        Raises
        ------
        RemoteUnavailableError
            探活失败（节点不可达）。此时不会把它登记成"健康节点"。
        """
        existing = self._workers.get(worker_id)
        if isinstance(existing, RemoteWorkerClient):
            client = existing
        else:
            root = (
                artifact_root
                if artifact_root is not None
                else self._default_artifact_root(worker_id)
            )
            client = RemoteWorkerClient(
                worker_id=worker_id, base_url=endpoint, token=token, artifact_root=root
            )
        self._workers[worker_id] = client
        health = client.health_check()
        record = WorkerRecord(
            worker_id=worker_id,
            kind=client.kind,
            region=region,
            endpoint=endpoint,
            capabilities=client.capabilities,
            health=health,
        )
        self._registry.register(record)
        self._ensure_route(
            worker_id=worker_id,
            provider_id=None,
            protocol=protocol,
        )
        return self._registry.get(worker_id)

    def _default_artifact_root(self, worker_id: str) -> Path:
        """远程产物默认落盘位置：与控制面库同级的 ``remote-artifacts/<worker_id>``。"""
        db_path = Path(self._registry.store.db_path)
        return db_path.parent / "remote-artifacts" / worker_id

    def _ensure_route(
        self, *, worker_id: str, provider_id: str | None, protocol: str
    ) -> Route | None:
        """为节点补一条链路（已存在则跳过）。``provider_id=None`` 表示等待首次任务确定。"""
        if provider_id is None:
            return None
        route_id = f"{worker_id}:{provider_id}:{protocol}"
        existing = [route for route in self._routes.list_routes() if route.route_id == route_id]
        if existing:
            return existing[0]
        record = self._registry.get(worker_id)
        return self._routes.add(
            Route(
                route_id=route_id,
                protocol=protocol,
                worker_id=worker_id,
                provider_id=provider_id,
                supports_resume=True,
                max_concurrent=record.capabilities.max_concurrent,
            )
        )

    # -- 取消 -------------------------------------------------------------- #
    def cancel(self) -> None:
        """请求取消当前任务（在途远程任务会被下发取消请求）。"""
        self._cancel.set()

    def reset_cancel(self) -> None:
        """清除取消标记（同一协调器复用前必须调用）。"""
        self._cancel.clear()

    # -- 主流程 ------------------------------------------------------------ #
    def run(
        self,
        task: AcquisitionTask,
        *,
        provider_id: str | None = None,
        relative_path: str | None = None,
    ) -> TransferDelivery:
        """执行任务直到某条链路成功，或全部失败。

        Parameters
        ----------
        task:
            待执行任务（只读取 ``asset`` 与标识字段）。
        provider_id:
            目标提供方；默认取 ``task.asset.source_id``。
        relative_path:
            产物相对路径；默认 ``<dataset_id>/<file_name>``。

        Raises
        ------
        NoRouteAvailable
            一条可用链路都没有（结构化错误，含全部被拒原因）。
        FailoverExhausted
            所有尝试都失败（结构化错误，含每次尝试原因）。
        """
        provider = provider_id or task.asset.source_id
        relative = relative_path or f"{task.asset.dataset_id}/{task.asset.file_name}"
        for worker_id in self._workers:
            self._ensure_route(worker_id=worker_id, provider_id=provider, protocol="https")
        selection = self._routes.select(
            provider_id=provider,
            dataset_id=task.asset.dataset_id,
            size_bytes=task.asset.size_bytes,
            target=relative,
        )
        attempts: list[AttemptRecord] = []
        excluded: set[str] = set()
        for option in selection.options[: self._max_routes]:
            if self._cancel.is_set():
                attempts.append(
                    AttemptRecord(
                        route_id=option.route.route_id,
                        worker_id=option.route.worker_id,
                        protocol=option.route.protocol,
                        outcome="cancelled",
                        detail="cancelled by caller before dispatch",
                        latency_ms=0.0,
                        duration_s=0.0,
                        bytes_transferred=0,
                        retryable=False,
                    )
                )
                break
            delivery, failure = self._attempt(task, option, relative)
            attempts.append(failure)
            if delivery is not None:
                return TransferDelivery(
                    task_id=task.task_id,
                    route=option.route,
                    file=delivery,
                    attempts=tuple(attempts),
                )
            excluded.add(option.route.route_id)
            if not failure.retryable:
                break
        if not attempts:
            raise NoRouteAvailable(
                provider_id=provider,
                dataset_id=task.asset.dataset_id,
                size_bytes=task.asset.size_bytes,
                rejected=tuple(selection.rejected),
            )
        raise FailoverExhausted(task_id=task.task_id, provider_id=provider, attempts=attempts)

    # -- 单条链路 ---------------------------------------------------------- #
    def _attempt(
        self, task: AcquisitionTask, option: RouteOption, relative_path: str
    ) -> tuple[TransferredFile | None, AttemptRecord]:
        """尝试一条链路；返回 ``(产物, 尝试记录)``（失败时产物为 ``None``）。

        无论成败都会写一条实测样本：失败样本同样计入 ``samples`` 与 ``failure_rate``。
        """
        route = option.route
        worker = self._workers.get(route.worker_id)
        context = _AttemptContext(route=route, started=self._monotonic())
        root = self._artifact_root(worker)
        if worker is None or root is None:
            failure = _failure_record(
                context,
                "worker_not_resolved",
                None
                if worker is None
                else f"worker {route.worker_id!r} exposes no artifact_root; cannot fetch",
                self._monotonic,
                retryable=False,
            )
            self._record_failure(context, failure)
            return None, failure
        self._routes.acquire(route.route_id)
        try:
            execution = worker.prepare(task, relative_path=relative_path)
            # 首字节延迟：从 dispatch 到 worker 明确受理（POST /v1/tasks 返回）的实测间隔。
            context.latency_ms = max(0.0, (self._monotonic() - context.started) * 1000.0)
            state = self._await_terminal(worker, execution)
            if state is not RemoteTaskState.COMPLETED:
                detail = f"worker reported {state.value}"
                retryable = state is not RemoteTaskState.CANCELLED
                try:
                    worker.result(execution)
                except (WorkerTaskError, RemoteUnavailableError) as exc:
                    detail = str(exc)
                    retryable = bool(getattr(exc, "retryable", retryable))
                failure = _failure_record(
                    context, state.value, detail, self._monotonic, retryable=retryable
                )
                self._record_failure(context, failure)
                return None, failure
            destination = root / relative_path
            fetched = worker.fetch(execution, destination)
            if context.latency_ms == 0.0:
                # 本地同步 worker 没有"受理"阶段：用产物落盘时刻作为首字节延迟。
                context.latency_ms = max(0.0, (self._monotonic() - context.started) * 1000.0)
            size, digest = self._verify(fetched, task, option)
            delivery = TransferredFile(
                path=fetched.path,
                size_bytes=size,
                sha256=digest,
                relative_path=relative_path,
                resumed=fetched.resumed,
            )
            self._record_success(option, delivery, context)
            return delivery, AttemptRecord(
                route_id=route.route_id,
                worker_id=route.worker_id,
                protocol=route.protocol,
                outcome="succeeded",
                detail="delivered",
                latency_ms=context.latency_ms,
                duration_s=max(0.0, self._monotonic() - context.started),
                bytes_transferred=size,
                retryable=False,
                retried=context.retried,
            )
        except (WorkerTaskError, RemoteUnavailableError, RemoteProtocolError, OSError) as exc:
            retryable = not isinstance(exc, RemoteProtocolError) and bool(
                getattr(exc, "retryable", True)
            )
            failure = _failure_record(
                context, type(exc).__name__, str(exc), self._monotonic, retryable=retryable
            )
            self._record_failure(context, failure)
            return None, failure
        finally:
            self._routes.release(route.route_id)

    @staticmethod
    def _artifact_root(worker: WorkerProtocol | None) -> Path | None:
        """读取 worker 的本机落盘根（未实现该可选能力时返回 ``None``）。"""
        if worker is None:
            return None
        root = getattr(worker, "artifact_root", None)
        return root if isinstance(root, Path) else None

    def _await_terminal(
        self, worker: WorkerProtocol, execution: WorkerExecution
    ) -> RemoteTaskState:
        """轮询直到终态、超时或取消；每轮都给远程节点发真实心跳/状态查询。"""
        deadline = self._monotonic() + self._remote_timeout
        last: RemoteTaskState = RemoteTaskState.QUEUED
        while True:
            if self._cancel.is_set():
                worker.cancel(execution)
                return RemoteTaskState.CANCELLED
            last = worker.poll(execution)
            if last.terminal:
                return last
            if isinstance(worker, RemoteWorkerClient):
                # 心跳失败不改写任务状态：状态查询才是事实来源，下一轮会再试。
                with contextlib.suppress(RemoteUnavailableError, RemoteProtocolError):
                    worker.heartbeat()
            if self._monotonic() >= deadline:
                worker.cancel(execution)
                raise WorkerTaskError(
                    f"worker task exceeded {self._remote_timeout:.0f}s timeout",
                    retryable=True,
                    error_code="worker_task_timeout",
                )
            self._sleep(self._poll_interval)

    def _verify(
        self, fetched: FetchReport, task: AcquisitionTask, option: RouteOption
    ) -> tuple[int, str]:
        """对**落盘文件**重新计算 SHA-256 并核对声明的期望值。

        Raises
        ------
        WorkerTaskError
            大小或摘要不符（可重试：换链路重新拉一次是合理动作）。
        """
        path = fetched.path
        size = path.stat().st_size
        digest = sha256_file(path)
        if size != fetched.size_bytes or digest != fetched.sha256:
            raise WorkerTaskError(
                "artifact changed between fetch and verification",
                retryable=True,
                error_code="verify_mismatch",
            )
        expected = task.asset.checksum
        if expected is not None and expected.algorithm == "sha256" and expected.hexdigest != digest:
            with contextlib.suppress(OSError):  # 删除失败不影响判定结果
                path.unlink()
            raise WorkerTaskError(
                f"sha256 mismatch after failover via {option.route.route_id}: "
                f"expected {expected.hexdigest}, got {digest}",
                retryable=True,
                error_code="checksum_mismatch",
            )
        if task.asset.size_bytes and size != task.asset.size_bytes:
            raise WorkerTaskError(
                f"size mismatch: declared {task.asset.size_bytes}, got {size}",
                retryable=True,
                error_code="size_mismatch",
            )
        return size, digest

    # -- 记账 -------------------------------------------------------------- #
    def _record_success(
        self, option: RouteOption, delivery: TransferredFile, context: _AttemptContext
    ) -> None:
        """写入成功样本（``latency_ms`` 用本次实测的首字节延迟，不是历史均值）。"""
        elapsed = max(0.0, self._monotonic() - context.started)
        self._routes.record_attempt(
            option.route,
            success=True,
            latency_ms=context.latency_ms,
            duration_s=elapsed,
            bytes_transferred=delivery.size_bytes,
            retried=context.retried,
            outcome="succeeded",
        )
        with self._lock:
            self._failures[option.route.worker_id] = 0
        self._safe_registry(self._registry.record_outcome, option.route.worker_id, ok=True)

    def _record_failure(self, context: _AttemptContext, failure: AttemptRecord) -> None:
        """写入失败样本（失败**必须**计入样本，否则成功率会被高估）。"""
        self._routes.record_attempt(
            context.route,
            success=False,
            latency_ms=context.latency_ms,
            duration_s=failure.duration_s,
            bytes_transferred=failure.bytes_transferred,
            retried=context.retried,
            outcome=failure.outcome,
        )
        with self._lock:
            count = self._failures.get(context.route.worker_id, 0) + 1
            self._failures[context.route.worker_id] = count
        self._safe_registry(self._registry.record_outcome, context.route.worker_id, ok=False)
        if count >= self._offline_after:
            self._safe_registry(
                self._registry.mark_offline,
                context.route.worker_id,
                reason=f"{count} consecutive transport failures",
            )

    @staticmethod
    def _safe_registry(operation: Callable[..., object], *args: object, **kwargs: object) -> None:
        """注册表记账失败不应改变"传输已经失败"这一事实（只影响健康度展示）。"""
        try:
            operation(*args, **kwargs)
        except Exception:
            return


def _failure_record(
    context: _AttemptContext,
    outcome: str,
    detail: str | None,
    monotonic: Callable[[], float],
    *,
    retryable: bool = True,
) -> AttemptRecord:
    """构造失败尝试记录（耗时与延迟都取真实单调时钟差）。"""
    now = monotonic()
    elapsed = max(0.0, now - context.started)
    return AttemptRecord(
        route_id=context.route.route_id,
        worker_id=context.route.worker_id,
        protocol=context.route.protocol,
        outcome=outcome,
        detail=(detail or outcome)[:2000],
        latency_ms=context.latency_ms,
        duration_s=elapsed,
        bytes_transferred=context.bytes_transferred,
        retryable=retryable,
        retried=context.retried,
    )
