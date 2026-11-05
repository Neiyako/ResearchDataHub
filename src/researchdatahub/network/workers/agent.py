r"""Worker Agent 服务端：真实 HTTP 服务，接受任务并把资产下载到本机。

运行方式
--------
::

    python -m researchdatahub.network.workers.agent --port 8765 \\
        --storage-root /srv/rdh-worker --worker-id remote-a

也可在进程内直接构建（测试与嵌入式部署）::

    server = build_agent_server(worker_id="local-remote", storage_root=root, port=0)
    server.start()
    server.base_url  # http://127.0.0.1:<port>
    server.stop()

安全模型
--------
* 令牌通过 ``--token`` 或环境变量 ``RDH_WORKER_TOKEN`` 提供，只从 ``Authorization``
  头读取（查询串会进访问日志，禁止）；
* 令牌用 ``hmac.compare_digest`` 常量时间比较；日志与响应**绝不**回显令牌；
* 未配置令牌时只允许绑定回环地址，否则启动即失败（避免裸奔到内网/公网）；
* 任务只能写 ``storage_root`` 内的相对路径（``..``/绝对路径/符号链接越界全部拒绝）；
* 只下载 ``/v1/tasks`` 里声明的 ``origin_uri``，该地址的合法性与签名寿命由调用方负责。

并发与幂等
----------
* 执行线程池大小 = ``max_concurrent``；超出并发的任务停在 ``queued``；
* 幂等键是 ``task_id``：重复 ``POST /v1/tasks`` 返回原记录（``created=false``），
  **不会**启动第二次下载；同 ID 不同内容返回 409；
* 在途任务有租约：超过 ``task_lease_seconds`` 未被任何请求触达即视为孤儿并取消
  （客户端崩溃后不会永久占用并发）。

实现只用标准库（``http.server`` + ``socketserver``），不引入 Web 框架；生产环境应在
反向代理上做 TLS 终结，并只监听回环/内网地址。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from researchdatahub.core.models import (
    AcquisitionTask,
    Checksum,
    JsonValue,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.network.workers.models import (
    MAX_REMOTE_PAYLOAD_BYTES,
    RemoteTaskRequest,
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
)
from researchdatahub.network.workers.protocol import (
    AgentHealth,
    CancelResponse,
    ErrorResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    RemoteStatusResponse,
    RemoteSubmitResponse,
)
from researchdatahub.storage.naming import sha256_file
from researchdatahub.transfer.base import TransportAdapterBase
from researchdatahub.transfer.http import HttpTransportAdapter

__all__ = [
    "AGENT_TOKEN_ENV",
    "AGENT_VERSION",
    "AgentError",
    "AgentServer",
    "RemoteTaskManager",
    "build_agent_server",
    "main",
]

AGENT_VERSION: Final[str] = "0.1.0"
AGENT_TOKEN_ENV: Final[str] = "RDH_WORKER_TOKEN"
_STREAM_CHUNK: Final[int] = 1024 * 1024
_TASK_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_SUPPORTED_ROUTES: Final[tuple[str, ...]] = ("http", "https")
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


def _now() -> datetime:
    """当前 UTC 时间（唯一的时钟读取点，便于测试注入）。"""
    return datetime.now(UTC)


class AgentError(Exception):
    """协议层错误：携带 HTTP 状态码与机器可读错误码（由 handler 转成 JSON）。"""

    def __init__(
        self, status: int, code: str, message: str, *, detail: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail: dict[str, object] = detail or {}


@dataclass
class TaskRecord:
    """worker 侧的一条任务记录（内存态；进程重启即丢弃，主进程负责重下发）。"""

    request: RemoteTaskRequest
    state: RemoteTaskState = RemoteTaskState.QUEUED
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: RemoteTaskResult | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    bytes_received: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lease_deadline: float = 0.0
    target: Path | None = None


class RemoteTaskManager:
    """任务登记、幂等、执行与取消（线程安全；agent 唯一的状态持有者）。

    Parameters
    ----------
    worker_id:
        agent 节点 ID。
    storage_root:
        产物落盘根目录（自动创建）。
    max_concurrent:
        并行执行上限；能力声明里的 ``max_concurrent`` 会被它截断。
    capabilities:
        节点能力声明；能力不匹配的任务在提交时直接 403。
    task_lease_seconds:
        在途任务租约（秒）：超时未被触达即取消。
    transport:
        传输执行器；``None`` 时惰性创建直连 ``HttpTransportAdapter``。
    clock:
        可注入时钟（写状态时间戳）。
    """

    def __init__(
        self,
        *,
        worker_id: str,
        storage_root: Path,
        max_concurrent: int = 2,
        capabilities: WorkerCapabilities | None = None,
        task_lease_seconds: float = 300.0,
        transport: TransportAdapterBase | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if task_lease_seconds <= 0:
            raise ValueError("task_lease_seconds must be > 0")
        self.worker_id = worker_id
        self.storage_root = Path(storage_root).expanduser().resolve(strict=False)
        self.max_concurrent = max_concurrent
        declared = capabilities or WorkerCapabilities()
        self.capabilities = declared.model_copy(
            update={
                "protocols": list(declared.protocols) or list(_SUPPORTED_ROUTES),
                "max_concurrent": min(declared.max_concurrent, max_concurrent),
            }
        )
        self.task_lease_seconds = float(task_lease_seconds)
        self.clock = clock or _now
        self._transport = transport
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._started_at = time.monotonic()
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="rdh-agent"
        )
        self._closed = False
        self.storage_root.mkdir(parents=True, exist_ok=True)

    # -- 只读事实 ----------------------------------------------------------- #
    @property
    def uptime_s(self) -> float:
        """进程运行时长（秒，单调时钟）。"""
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def queue_depth(self) -> int:
        """排队中（``queued``）任务数。"""
        with self._lock:
            return sum(1 for task in self._tasks.values() if task.state is RemoteTaskState.QUEUED)

    @property
    def active_tasks(self) -> int:
        """在途（``queued`` + ``executing``）任务数。"""
        with self._lock:
            return sum(
                1
                for task in self._tasks.values()
                if task.state in (RemoteTaskState.QUEUED, RemoteTaskState.EXECUTING)
            )

    @property
    def known_tasks(self) -> int:
        """已登记任务总数（含终态）。"""
        with self._lock:
            return len(self._tasks)

    def free_bytes(self) -> int:
        """存储根所在卷的可用字节数（真实磁盘查询）。"""
        try:
            return int(shutil.disk_usage(self.storage_root).free)
        except OSError:  # pragma: no cover - 依赖文件系统状态
            return 0

    def status(self, task_id: str) -> TaskRecord:
        """读取任务记录（读取即续租：客户端仍在轮询说明它还活着）。"""
        record = self._require(task_id)
        self._touch(record)
        return record

    def _require(self, task_id: str) -> TaskRecord:
        if not _TASK_ID_RE.match(task_id):
            raise AgentError(400, "invalid_task_id", f"invalid task id {task_id!r}")
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise AgentError(404, "unknown_task", f"unknown task {task_id!r}")
        return record

    def _touch(self, record: TaskRecord) -> None:
        with self._lock:
            if not record.state.terminal:
                record.lease_deadline = time.monotonic() + self.task_lease_seconds

    # -- 提交 --------------------------------------------------------------- #
    def submit(self, request: RemoteTaskRequest) -> tuple[TaskRecord, bool]:
        """登记任务并异步执行，返回 ``(record, created)``。

        Raises
        ------
        AgentError
            能力不匹配（403）、任务 ID 冲突（409）、路径越界（400）。
        """
        allowed, reason = self.capabilities.allows(
            dataset_id=request.dataset_id,
            provider_id=request.provider_id,
            protocol=request.protocol,
            size_bytes=request.size_bytes,
        )
        if not allowed:
            raise AgentError(
                403,
                "capability_mismatch",
                reason or "task rejected by worker capabilities",
                detail={"worker_id": self.worker_id},
            )
        with self._lock:
            existing = self._tasks.get(request.task_id)
            if existing is not None:
                if existing.request != request:
                    raise AgentError(
                        409,
                        "task_id_conflict",
                        f"task {request.task_id!r} already exists with different content",
                    )
                return existing, False
            target = self._resolve_target(request.target_relative_path)
            record = TaskRecord(
                request=request,
                lease_deadline=time.monotonic() + self.task_lease_seconds,
                target=target,
            )
            self._tasks[request.task_id] = record
            self._executor.submit(self._execute, request.task_id)
        return record, True

    def _resolve_target(self, relative_path: str) -> Path:
        candidate = (self.storage_root / relative_path).resolve(strict=False)
        if candidate != self.storage_root and not candidate.is_relative_to(self.storage_root):
            raise AgentError(
                400,
                "path_escape",
                "target_relative_path escapes the worker storage root",
                detail={"path": relative_path},
            )
        return candidate

    # -- 执行 --------------------------------------------------------------- #
    def _execute(self, task_id: str) -> None:
        with self._lock:
            record = self._tasks[task_id]
            record.state = RemoteTaskState.EXECUTING
            record.started_at = self.clock()
            record.updated_at = record.started_at
            record.lease_deadline = time.monotonic() + self.task_lease_seconds
        request = record.request
        target = record.target
        if target is None:  # pragma: no cover - 提交时已解析，缺失即编程错误
            raise RuntimeError("task record has no resolved target")
        started = time.monotonic()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            asset, task = _models_for(request)
            outcome = self._adapter().transfer_outcome(
                asset, target, task=task, cancel_event=record.cancel_event
            )
            if record.cancel_event.is_set() or outcome.error_code == "cancelled":
                self._finish(record, RemoteTaskState.CANCELLED, error="cancelled by caller")
                return
            if outcome.status.value not in ("succeeded", "skipped"):
                self._finish(
                    record,
                    RemoteTaskState.FAILED,
                    error=(
                        f"{outcome.error_code or 'transfer_failed'}: "
                        f"{outcome.error_detail or 'transfer failed'}"
                    ),
                    error_code=outcome.error_code or "transfer_failed",
                    retryable=bool(outcome.retryable),
                )
                return
            product = Path(outcome.destination_path if outcome.destination_path else str(target))
            size = product.stat().st_size
            digest = sha256_file(product)
            if request.size_bytes and size != request.size_bytes:
                self._finish(
                    record,
                    RemoteTaskState.FAILED,
                    error=f"size mismatch: declared {request.size_bytes}, got {size}",
                    error_code="size_mismatch",
                    retryable=True,
                )
                return
            if not _checksum_agrees(request.expected_checksum, digest):
                self._finish(
                    record,
                    RemoteTaskState.FAILED,
                    error=f"sha256 mismatch: declared {request.expected_checksum}, got {digest}",
                    error_code="checksum_mismatch",
                    retryable=True,
                )
                return
            self._finish(
                record,
                RemoteTaskState.COMPLETED,
                result=RemoteTaskResult(
                    path=str(product),
                    size_bytes=size,
                    sha256=digest,
                    duration_s=max(0.0, time.monotonic() - started),
                ),
                bytes_received=size,
            )
        except Exception as exc:
            self._finish(
                record,
                RemoteTaskState.FAILED,
                error=f"worker execution error: {type(exc).__name__}: {exc}",
                error_code="worker_internal_error",
                retryable=True,
            )

    def _adapter(self) -> TransportAdapterBase:
        if self._transport is None:
            self._transport = HttpTransportAdapter(trust_env=False)
        return self._transport

    def _finish(
        self,
        record: TaskRecord,
        state: RemoteTaskState,
        *,
        result: RemoteTaskResult | None = None,
        error: str | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        bytes_received: int = 0,
    ) -> None:
        with self._lock:
            record.state = state
            record.finished_at = self.clock()
            record.updated_at = record.finished_at
            record.result = result
            record.error = None if error is None else error[:2048]
            record.error_code = error_code
            record.retryable = retryable
            record.bytes_received = bytes_received

    # -- 取消与回收 --------------------------------------------------------- #
    def cancel(self, task_id: str) -> TaskRecord:
        """取消任务。

        Raises
        ------
        AgentError
            未知任务（404）或已处于终态（409，不假装成功）。
        """
        record = self._require(task_id)
        with self._lock:
            if record.state.terminal:
                raise AgentError(
                    409,
                    "task_finished",
                    f"task {task_id!r} already {record.state.value}",
                    detail={"state": record.state.value},
                )
            record.cancel_event.set()
            if record.state is RemoteTaskState.QUEUED:
                record.state = RemoteTaskState.CANCELLED
                record.finished_at = self.clock()
                record.updated_at = record.finished_at
                record.error = "cancelled before execution"
        return record

    def reap_expired(self) -> list[str]:
        """取消租约过期的在途任务，返回被取消的 ID（客户端崩溃自愈）。"""
        now = time.monotonic()
        cancelled: list[str] = []
        with self._lock:
            for task_id, record in self._tasks.items():
                if record.state.terminal or record.lease_deadline > now:
                    continue
                record.cancel_event.set()
                if record.state is RemoteTaskState.QUEUED:
                    record.state = RemoteTaskState.CANCELLED
                    record.finished_at = self.clock()
                    record.updated_at = record.finished_at
                    record.error = "cancelled: task lease expired"
                    cancelled.append(task_id)
        return cancelled

    def product(self, task_id: str) -> tuple[Path, RemoteTaskResult]:
        """返回可下载产物 ``(path, result)``。

        Raises
        ------
        AgentError
            未知任务（404）、任务未完成（409）、产物丢失（410）。
        """
        record = self._require(task_id)
        self._touch(record)
        if record.state is not RemoteTaskState.COMPLETED or record.result is None:
            raise AgentError(
                409,
                "task_not_completed",
                f"task {task_id!r} is {record.state.value}",
                detail={"state": record.state.value},
            )
        path = Path(record.result.path)
        if not path.is_file():
            raise AgentError(410, "product_missing", f"product for {task_id!r} is gone")
        return path, record.result

    def close(self) -> None:
        """取消全部在途任务并关闭线程池（幂等）。"""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            for record in self._tasks.values():
                record.cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)


def _checksum_agrees(declared: str | None, digest: str) -> bool:
    """调用方声明的检查和在算法为 ``sha256`` 时必须与实测摘要一致；其它算法不据此判定。

    其它算法（``md5``/``crc32``）不在这里计算：传输层已经做过一次大小与 ETag 校验，
    回传阶段只对 ``sha256`` 做硬判定，避免在 worker 侧引入第二套摘要实现。
    """
    if declared is None:
        return True
    algorithm, _, value = declared.partition(":")
    return algorithm != "sha256" or value == digest


def _models_for(request: RemoteTaskRequest) -> tuple[SourceAsset, AcquisitionTask]:
    """把远程请求还原成传输层需要的 ``SourceAsset`` / ``AcquisitionTask``。

    只重建传输需要的字段；``metadata`` 里若带 ``source_metadata`` 就原样使用（主进程
    可以透传真实来源追踪信息），否则用请求字段拼一个等价的最小元数据。
    """
    payload = request.metadata.get("source_metadata")
    source_metadata = (
        SourceMetadata.model_validate(payload)
        if isinstance(payload, dict)
        else SourceMetadata(
            source_id=request.source_id,
            display_name=request.provider_id,
            provider=request.provider_id,
            access_route=request.access_route,
            dataset_id=request.dataset_id,
            origin_uri=request.origin_uri,
            retrieved_at=_now(),
        )
    )
    checksum = (
        None if request.expected_checksum is None else Checksum(value=request.expected_checksum)
    )
    asset = SourceAsset(
        asset_id=request.asset_id,
        source_id=request.source_id,
        dataset_id=request.dataset_id,
        access_route=request.access_route,
        origin_uri=request.origin_uri,
        file_name=request.file_name,
        size_bytes=request.size_bytes,
        checksum=checksum,
        metadata=source_metadata,
        discovered_at=_now(),
    )
    task = AcquisitionTask(
        task_id=request.task_id,
        plan_id="remote-agent",
        request_id="remote-agent",
        asset=asset,
    )
    return asset, task


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单段 ``Range: bytes=a-b``，返回闭区间 ``(start, end)``。

    只支持单段范围（多段范围对单文件回传没有实际用途）。``header`` 为空返回 ``None``
    表示整文件。

    Raises
    ------
    AgentError
        语法非法（400）或范围越界（416）。
    """
    if not header:
        return None
    text = header.strip()
    if not text.startswith("bytes="):
        raise AgentError(400, "invalid_range", "only bytes= ranges are supported")
    spec = text[len("bytes=") :]
    if "," in spec:
        raise AgentError(400, "invalid_range", "multiple ranges are not supported")
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text == "":
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError("suffix must be positive")
            if size == 0:
                raise AgentError(416, "range_not_satisfiable", "empty file has no range")
            return max(0, size - suffix), size - 1
        start = int(start_text)
        end = size - 1 if end_text == "" else int(end_text)
    except ValueError as exc:
        raise AgentError(400, "invalid_range", f"cannot parse range {header!r}") from exc
    if start < 0 or end < start or start >= size:
        raise AgentError(
            416,
            "range_not_satisfiable",
            f"range {header!r} is outside file size {size}",
            detail={"size_bytes": size},
        )
    return start, min(end, size - 1)


class _AgentHandler(BaseHTTPRequestHandler):
    """逐请求的路由与鉴权（协议见 :mod:`researchdatahub.network.workers.protocol`）。"""

    server_version = f"rdh-worker-agent/{AGENT_VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def agent(self) -> AgentServer:
        """所属服务器（类型收窄，避免 ``# type: ignore``）。"""
        server = self.server
        if not isinstance(server, AgentServer):  # pragma: no cover - 由构造方保证
            raise RuntimeError("handler is not attached to an AgentServer")
        return server

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """只记录方法/路径/状态码：访问日志里永远不出现请求头与令牌。"""
        self.agent.log(f"{self.command} {self.path} -> {code}")

    def do_GET(self) -> None:
        """处理 GET 路由。"""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """处理 POST 路由。"""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        try:
            self._authorize()
            if method == "GET" and path == "/v1/health":
                self._health()
                return
            if method == "GET" and path.startswith("/v1/files/"):
                self._file(path[len("/v1/files/") :])
                return
            if method == "GET" and path.startswith("/v1/tasks/"):
                self._status(path[len("/v1/tasks/") :])
                return
            if method == "POST" and path == "/v1/register":
                self._register()
                return
            if method == "POST" and path == "/v1/heartbeat":
                self._heartbeat()
                return
            if method == "POST" and path == "/v1/tasks":
                self._submit()
                return
            if method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                self._cancel(path[len("/v1/tasks/") : -len("/cancel")])
                return
            raise AgentError(404, "not_found", f"no route for {method} {path}")
        except AgentError as exc:
            self._send_error(exc)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端主动断开
            self.agent.log(f"{method} {path} client disconnected")
        except Exception as exc:
            self.agent.log(f"unhandled error: {type(exc).__name__}: {exc}")
            self._send_error(AgentError(500, "internal_error", "worker internal error"))

    def _authorize(self) -> None:
        token = self.agent.token
        if token is None:
            return
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.strip().encode("utf-8"), token.encode("utf-8")
        ):
            raise AgentError(401, "unauthorized", "missing or invalid bearer token")

    # -- 路由实现 ----------------------------------------------------------- #
    def _health(self) -> None:
        manager = self.agent.manager
        self._send_model(
            AgentHealth(
                agent_version=AGENT_VERSION,
                worker_id=manager.worker_id,
                uptime_s=manager.uptime_s,
                capabilities=manager.capabilities,
                storage_root=str(manager.storage_root),
                free_bytes=manager.free_bytes(),
                active_tasks=manager.active_tasks,
                known_tasks=manager.known_tasks,
                server_time=_now(),
            )
        )

    def _register(self) -> None:
        payload = self._read_json()
        try:
            body = RegisterRequest.model_validate(payload)
        except ValidationError as exc:
            raise AgentError(
                400,
                "invalid_register",
                "invalid register request",
                detail={"problems": _problems(exc)},
            ) from exc
        manager = self.agent.manager
        if body.worker_id != manager.worker_id:
            self._send_model(
                RegisterResponse(
                    accepted=False,
                    worker_id=body.worker_id,
                    reason=(
                        f"agent is {manager.worker_id!r}; cannot register as {body.worker_id!r}"
                    ),
                    agent_version=AGENT_VERSION,
                    effective_capabilities=manager.capabilities,
                ),
                status=403,
            )
            return
        protocols = body.capabilities.protocols or list(_SUPPORTED_ROUTES)
        unsupported = [item for item in protocols if item not in _SUPPORTED_ROUTES]
        if unsupported:
            self._send_model(
                RegisterResponse(
                    accepted=False,
                    worker_id=body.worker_id,
                    reason=f"agent does not implement protocols: {unsupported}",
                    agent_version=AGENT_VERSION,
                    effective_capabilities=manager.capabilities,
                ),
                status=403,
            )
            return
        effective = self.agent.register(
            WorkerCapabilities(
                datasets=body.capabilities.datasets,
                providers=body.capabilities.providers,
                protocols=protocols,
                max_bytes=body.capabilities.max_bytes,
                max_concurrent=min(body.capabilities.max_concurrent, manager.max_concurrent),
            ),
            caller=body.caller,
        )
        self._send_model(
            RegisterResponse(
                accepted=True,
                worker_id=manager.worker_id,
                agent_version=AGENT_VERSION,
                effective_capabilities=effective,
                registered_at=_now(),
            )
        )

    def _heartbeat(self) -> None:
        payload = self._read_json()
        try:
            body = HeartbeatRequest.model_validate(payload)
        except ValidationError as exc:
            raise AgentError(
                400,
                "invalid_heartbeat",
                "invalid heartbeat request",
                detail={"problems": _problems(exc)},
            ) from exc
        manager = self.agent.manager
        if body.worker_id != manager.worker_id:
            raise AgentError(
                403,
                "worker_mismatch",
                f"heartbeat for {body.worker_id!r} does not match agent {manager.worker_id!r}",
            )
        self.agent.record_heartbeat()
        manager.reap_expired()
        self._send_model(
            HeartbeatResponse(
                worker_id=manager.worker_id,
                uptime_s=manager.uptime_s,
                active_tasks=manager.active_tasks,
                known_tasks=manager.known_tasks,
                queue_depth=manager.queue_depth,
                server_time=_now(),
            )
        )

    def _submit(self) -> None:
        payload = self._read_json()
        try:
            request = RemoteTaskRequest.model_validate(payload)
        except ValidationError as exc:
            raise AgentError(
                400,
                "invalid_task",
                "invalid task request",
                detail={"problems": _problems(exc)},
            ) from exc
        record, created = self.agent.manager.submit(request)
        self._send_model(
            RemoteSubmitResponse(
                task_id=record.request.task_id, state=record.state, created=created
            ),
            status=201 if created else 200,
        )

    def _status(self, raw_task_id: str) -> None:
        record = self.agent.manager.status(raw_task_id)
        self._send_model(
            RemoteStatusResponse(
                task_id=record.request.task_id,
                state=record.state,
                created_at=record.created_at,
                updated_at=record.updated_at,
                started_at=record.started_at,
                finished_at=record.finished_at,
                request=record.request,
                result=record.result,
                error=record.error,
                retryable=record.retryable,
                error_code=record.error_code,
                bytes_received=record.bytes_received,
            )
        )

    def _cancel(self, raw_task_id: str) -> None:
        record = self.agent.manager.cancel(raw_task_id)
        self._send_model(
            CancelResponse(
                task_id=raw_task_id,
                state=record.state,
                cancelled=record.state is RemoteTaskState.CANCELLED,
            )
        )

    def _file(self, raw_task_id: str) -> None:
        path, result = self.agent.manager.product(raw_task_id)
        size = path.stat().st_size
        window = _parse_range(self.headers.get("Range"), size)
        if window is None:
            start, end = 0, size - 1
            status = 200
        else:
            start, end = window
            status = 206
        length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{_ascii_name(raw_task_id)}"'
        )
        self.send_header("ETag", f'"{result.sha256}"')
        self.send_header("Last-Modified", formatdate(usegmt=True))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if length == 0:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_STREAM_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # -- 工具 --------------------------------------------------------------- #
    def _read_json(self) -> object:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise AgentError(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AgentError(
                400, "invalid_length", f"invalid Content-Length {raw_length!r}"
            ) from exc
        if length < 0:
            raise AgentError(400, "invalid_length", "negative Content-Length")
        if length > MAX_REMOTE_PAYLOAD_BYTES:
            raise AgentError(
                413,
                "payload_too_large",
                f"payload exceeds {MAX_REMOTE_PAYLOAD_BYTES} bytes",
                detail={"length": length},
            )
        body = self.rfile.read(length) if length else b""
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(400, "invalid_json", f"request body is not valid JSON: {exc}") from exc

    def _send_model(self, model: object, *, status: int = 200) -> None:
        dump = getattr(model, "model_dump_json", None)
        if dump is None:  # pragma: no cover - 防御性分支
            raise AgentError(500, "internal_error", "cannot serialise response")
        body: bytes = dump().encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-RDH-Worker", self.agent.manager.worker_id)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: AgentError) -> None:
        payload = ErrorResponse.model_validate(
            {
                "error": error.code,
                "message": error.message,
                "detail": _jsonable_detail(error.detail),
            }
        ).model_dump_json()
        body = payload.encode("utf-8")
        if self.headers.get("Content-Length") not in (None, "0"):
            self.close_connection = True
        try:
            self.send_response(error.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            return


def _problems(error: ValidationError) -> list[JsonValue]:
    """把 pydantic 校验错误压成可 JSON 序列化的精简列表。"""
    return [
        {"loc": ".".join(str(part) for part in item["loc"]), "msg": item["msg"]}
        for item in error.errors(include_url=False)
    ]


def _jsonable_detail(detail: dict[str, object]) -> dict[str, JsonValue]:
    """错误详情降级为 JSON 值（不丢关键信息，也不因不可序列化而再次失败）。"""
    result: dict[str, JsonValue] = {}
    for key, value in detail.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            result[str(key)] = value
        else:
            result[str(key)] = str(value)
    return result


def _ascii_name(value: str) -> str:
    """响应头里的文件名必须 ASCII（避免头部编码错误）。"""
    cleaned = "".join(char if char.isalnum() or char in "-._" else "_" for char in value)
    return cleaned[:120]


class AgentServer(ThreadingHTTPServer):
    """Worker Agent 的 HTTP 服务器（可在后台线程运行，便于测试与嵌入式使用）。

    Parameters
    ----------
    address:
        监听地址；``port=0`` 时由系统分配空闲端口。
    manager:
        任务管理器（持有真实传输实现与存储根）。
    token:
        Bearer 令牌；``None`` 表示不鉴权（此时只允许绑定回环地址）。
    log:
        日志接收器（默认丢弃）。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        manager: RemoteTaskManager,
        *,
        token: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        host = address[0]
        if token is None and host not in _LOOPBACK_HOSTS:
            raise ValueError(
                "refusing to bind a non-loopback address without a worker token; "
                f"set {AGENT_TOKEN_ENV} or --token"
            )
        if token is not None and len(token) < 16:
            raise ValueError("worker token must be at least 16 characters")
        self.manager = manager
        self.token = token
        self._log = log
        self.heartbeats: int = 0
        self.registration: RegisterRequest | None = None
        self._thread: threading.Thread | None = None
        super().__init__(address, _AgentHandler)

    # -- 生命周期 ----------------------------------------------------------- #
    @property
    def base_url(self) -> str:
        """形如 ``http://127.0.0.1:<port>`` 的基址。"""
        host, port = str(self.server_address[0]), int(self.server_address[1])
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        """实际监听端口。"""
        return int(self.server_address[1])

    def start(self) -> None:
        """在后台守护线程里开始提供服务。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.serve_forever, name="rdh-agent-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止服务并关闭任务管理器（幂等）。"""
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=10)
            self._thread = None
        self.server_close()
        self.manager.close()

    def log(self, message: str) -> None:
        """记录一条（已脱敏的）服务端日志。"""
        if self._log is not None:
            self._log(f"[agent {self.manager.worker_id}] {message}")

    def record_heartbeat(self) -> None:
        """累计真实心跳次数（供测试断言"心跳确实到达了服务端"）。"""
        self.heartbeats += 1

    def register(self, capabilities: WorkerCapabilities, *, caller: str) -> WorkerCapabilities:
        """记录注册请求并生效新能力，返回实际生效的能力。"""
        self.registration = RegisterRequest(
            worker_id=self.manager.worker_id, capabilities=capabilities, caller=caller
        )
        self.manager.capabilities = capabilities
        return capabilities


def build_agent_server(
    *,
    worker_id: str,
    storage_root: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    capabilities: WorkerCapabilities | None = None,
    max_concurrent: int = 2,
    task_lease_seconds: float = 300.0,
    log: Callable[[str], None] | None = None,
) -> AgentServer:
    """装配一个 agent 服务器（不自动启动；``port=0`` 时由系统分配空闲端口）。"""
    manager = RemoteTaskManager(
        worker_id=worker_id,
        storage_root=storage_root,
        max_concurrent=max_concurrent,
        capabilities=capabilities,
        task_lease_seconds=task_lease_seconds,
    )
    return AgentServer((host, port), manager, token=token, log=log)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：``python -m researchdatahub.network.workers.agent``。"""
    parser = argparse.ArgumentParser(description="ResearchDataHub Worker Agent (HTTP)")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认回环）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（0=系统分配）")
    parser.add_argument("--worker-id", default="remote-worker", help="节点 ID")
    parser.add_argument("--storage-root", required=True, type=Path, help="产物落盘根目录")
    parser.add_argument(
        "--token",
        default=os.environ.get(AGENT_TOKEN_ENV),
        help=f"Bearer 令牌（或环境变量 ${AGENT_TOKEN_ENV}）",
    )
    parser.add_argument("--max-concurrent", type=int, default=2, help="并行下载上限")
    parser.add_argument("--max-bytes", type=int, default=0, help="单文件大小上限（0=不限）")
    parser.add_argument(
        "--task-lease-seconds", type=float, default=300.0, help="在途任务租约（秒）"
    )
    args = parser.parse_args(argv)
    capabilities = WorkerCapabilities(
        protocols=list(_SUPPORTED_ROUTES),
        max_bytes=args.max_bytes,
        max_concurrent=args.max_concurrent,
    )
    server = build_agent_server(
        worker_id=args.worker_id,
        storage_root=args.storage_root,
        host=args.host,
        port=args.port,
        token=args.token,
        capabilities=capabilities,
        max_concurrent=args.max_concurrent,
        task_lease_seconds=args.task_lease_seconds,
        log=lambda message: print(message, flush=True),
    )
    server.log(f"listening on {server.base_url} (storage={server.manager.storage_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        server.log("interrupted, shutting down")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
