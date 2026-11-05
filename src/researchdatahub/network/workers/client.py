"""``RemoteWorkerClient``：主进程侧的 HTTP 客户端（真实 ``httpx``，带超时与重试预算）。

要点
----
* **不在客户端做"无限重试"**：控制面请求的重试由 :class:`httpx.HTTPTransport`
  的 ``retries`` 与显式的一次补偿重试共同覆盖；业务级重试（换节点/换链路）属于
  :mod:`researchdatahub.network.routes.failover` 的职责，客户端只负责一次诚实的尝试。
* **错误分类**：连接失败/超时/5xx → :class:`RemoteUnavailableError`（可重试，编排层据此
  故障转移）；4xx → :class:`RemoteProtocolError`（不可重试，说明请求本身错了）；
  401 → 直接失败，避免"换节点掩盖凭据错误"。
* **回传续传**：``fetch`` 支持从已存在的 ``.part`` 继续（``Range``）；服务端不支持
  Range（返回 200）时丢弃前缀整文件重下，绝不拼接来源不明的字节。
* 令牌只放在 ``Authorization: Bearer`` 头里，不出现在 URL、日志或异常详情中。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from researchdatahub.core.models import AcquisitionTask, JsonValue
from researchdatahub.network.workers.base import FetchReport, WorkerExecution
from researchdatahub.network.workers.models import (
    REMOTE_PROTOCOL_VERSION,
    RemoteTaskRequest,
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
    WorkerStatus,
)
from researchdatahub.network.workers.protocol import (
    AgentHealth,
    CancelResponse,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    RemoteStatusResponse,
    RemoteSubmitResponse,
)
from researchdatahub.storage.naming import sha256_file

__all__ = [
    "RemoteProtocolError",
    "RemoteUnavailableError",
    "RemoteWorkerClient",
]

_DEFAULT_TIMEOUT: Final[float] = 10.0
_DOWNLOAD_TIMEOUT: Final[float] = 300.0
_STREAM_CHUNK: Final[int] = 1024 * 1024


class RemoteUnavailableError(Exception):
    """节点不可达或暂时失败（连接错误、超时、5xx、429）——可重试/可故障转移。"""

    def __init__(
        self, message: str, *, status: int | None = None, code: str = "unavailable"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = True


class RemoteProtocolError(Exception):
    """请求被节点明确拒绝（4xx / 协议不匹配）——重试同样输入不会成功。"""

    def __init__(self, message: str, *, status: int, code: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.retryable = False


class RemoteWorkerClient:
    """一个远程 worker 节点的客户端（对应一个 ``WorkerRecord.endpoint``）。

    Parameters
    ----------
    worker_id:
        节点 ID（与服务端 ``--worker-id`` 一致）。
    base_url:
        形如 ``http://host:port`` 的基址。
    token:
        Bearer 令牌；``None`` 表示服务端未启用鉴权。
    timeout_seconds:
        控制面请求超时（健康检查/注册/心跳/状态查询）。
    connect_timeout_seconds:
        建连超时。
    fetch_timeout_seconds:
        文件回传的读超时（长下载用较大的读空闲上限）。
    """

    def __init__(
        self,
        *,
        worker_id: str,
        base_url: str,
        token: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        connect_timeout_seconds: float = 5.0,
        fetch_timeout_seconds: float = _DOWNLOAD_TIMEOUT,
        artifact_root: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._connect_timeout = connect_timeout_seconds
        self._fetch_timeout = fetch_timeout_seconds
        self._clock = clock or time.monotonic
        self._closed = False
        self._artifact_root = (
            None
            if artifact_root is None
            else Path(artifact_root).expanduser().resolve(strict=False)
        )
        self._capabilities = WorkerCapabilities()
        self._handles: dict[str, WorkerExecution] = {}
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout, read=self._timeout),
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            headers=self._headers(),
        )

    # -- 属性 -------------------------------------------------------------- #
    @property
    def worker_id(self) -> str:
        """节点 ID。"""
        return self._worker_id

    @property
    def kind(self) -> WorkerKind:
        """远程节点类型。"""
        return WorkerKind.REMOTE

    @property
    def base_url(self) -> str:
        """节点基址。"""
        return self._base_url

    @property
    def artifact_root(self) -> Path:
        """回传产物的本机落盘根目录（必须由装配方显式设置）。

        Raises
        ------
        RuntimeError
            装配时没有提供 ``artifact_root``——编排层无法决定产物放哪里。
        """
        if self._artifact_root is None:
            raise RuntimeError(
                f"remote worker {self._worker_id!r} has no artifact_root; "
                "pass artifact_root=... when constructing RemoteWorkerClient"
            )
        return self._artifact_root

    @artifact_root.setter
    def artifact_root(self, value: Path) -> None:
        """设置回传产物的本机落盘根目录。"""
        self._artifact_root = Path(value).expanduser().resolve(strict=False)

    @property
    def capabilities(self) -> WorkerCapabilities:
        """能力声明（以最近一次 ``/v1/health`` 或注册结果为准；未知时为空=不限）。"""
        return self._capabilities

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "researchdatahub/0.1 (worker-client)",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # -- 控制面 ------------------------------------------------------------ #
    def health_check(self) -> WorkerHealth:
        """真实探活：``GET /v1/health``（探活本身就是一次心跳，故 ``last_heartbeat`` 有值）。

        Raises
        ------
        RemoteUnavailableError
            节点不可达（连接失败/超时/5xx）。
        RemoteProtocolError
            令牌错误（401）或协议不匹配。
        """
        payload = self._get_json("/v1/health")
        try:
            health = AgentHealth.model_validate(payload)
        except Exception as exc:
            raise RemoteProtocolError(
                f"invalid /v1/health payload: {exc}", status=200, code="invalid_payload"
            ) from exc
        if health.worker_id != self._worker_id:
            raise RemoteProtocolError(
                f"endpoint {self._base_url} serves worker {health.worker_id!r}, "
                f"expected {self._worker_id!r}",
                status=200,
                code="worker_mismatch",
            )
        self._capabilities = health.capabilities
        return WorkerHealth(
            status=WorkerStatus.HEALTHY,
            last_heartbeat=health.server_time,
            uptime_s=health.uptime_s,
            consecutive_failures=0,
        )

    def register(
        self,
        capabilities: WorkerCapabilities,
        *,
        caller: str = "rdh-main",
    ) -> RegisterResponse:
        """``POST /v1/register``；被拒绝时返回 ``accepted=False``（不抛异常，便于展示原因）。"""
        payload = self._post_json(
            "/v1/register",
            RegisterRequest(worker_id=self._worker_id, capabilities=capabilities, caller=caller),
            allow_status=(200, 403),
        )
        response = RegisterResponse.model_validate(payload)
        if response.accepted:
            self._capabilities = response.effective_capabilities
        return response

    def heartbeat(self) -> HeartbeatResponse:
        """``POST /v1/heartbeat``：续命并取回服务端事实（运行时长、在途任务数）。"""
        payload = self._post_json(
            "/v1/heartbeat",
            {"worker_id": self._worker_id, "protocol_version": REMOTE_PROTOCOL_VERSION},
        )
        return HeartbeatResponse.model_validate(payload)

    def status(self, task_id: str) -> RemoteStatusResponse:
        """``GET /v1/tasks/{id}``。"""
        return RemoteStatusResponse.model_validate(self._get_json(f"/v1/tasks/{task_id}"))

    def submit(self, request: RemoteTaskRequest) -> RemoteSubmitResponse:
        """``POST /v1/tasks``：幂等下发（重复提交返回 ``created=False`` 与原状态）。"""
        raw = self._post_json("/v1/tasks", request)
        return RemoteSubmitResponse.model_validate(raw)

    def cancel_remote(self, task_id: str) -> CancelResponse:
        """``POST /v1/tasks/{id}/cancel``。"""
        return CancelResponse.model_validate(self._post_json(f"/v1/tasks/{task_id}/cancel", {}))

    # -- 文件回传 ---------------------------------------------------------- #
    def download(
        self, task_id: str, destination: Path, *, expected_size: int | None = None
    ) -> FetchReport:
        """``GET /v1/files/{id}`` 回传产物，返回**实测**字节数与 SHA-256。

        续传：若 ``destination`` 旁存在 ``<name>.part`` 且服务端支持 Range，则从已有长度
        继续；服务端返回 200 而非 206 时丢弃前缀整文件重下。
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        resumed = partial.is_file() and partial.stat().st_size > 0
        offset = partial.stat().st_size if resumed else 0
        headers = dict(self._headers())
        if offset:
            headers["Range"] = f"bytes={offset}-"
        timeout = httpx.Timeout(self._fetch_timeout, connect=self._connect_timeout)
        try:
            with self._client.stream(
                "GET", f"/v1/files/{task_id}", headers=headers, timeout=timeout
            ) as response:
                if offset and response.status_code == 200:
                    # 服务端不支持 Range：丢弃前缀，整文件重下（绝不拼接）。
                    resumed = False
                    offset = 0
                    partial.unlink(missing_ok=True)
                elif offset and response.status_code == 416:
                    partial.unlink(missing_ok=True)
                    raise RemoteUnavailableError(
                        "server rejected resume range (416); partial file discarded",
                        status=416,
                        code="range_not_satisfiable",
                    )
                elif response.status_code not in (200, 206):
                    raise self._error_for(response.status_code, "file download failed")
                mode = "ab" if offset else "wb"
                with partial.open(mode) as handle:
                    for chunk in response.iter_bytes(_STREAM_CHUNK):
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(
                f"file download failed: {type(exc).__name__}: {exc}", code="download_failed"
            ) from exc
        size = partial.stat().st_size
        if expected_size is not None and size != expected_size:
            partial.unlink(missing_ok=True)
            raise RemoteUnavailableError(
                f"downloaded size {size} does not match declared {expected_size}",
                code="size_mismatch",
            )
        digest = sha256_file(partial)
        partial.replace(target)
        return FetchReport(path=target, size_bytes=size, sha256=digest, resumed=resumed)

    # -- WorkerProtocol（编排层使用） -------------------------------------- #
    def health(self) -> WorkerHealth:
        """真实探活（与 :meth:`health_check` 同一请求；失败抛 ``RemoteUnavailableError``）。"""
        return self.health_check()

    def prepare(self, task: AcquisitionTask, *, relative_path: str) -> WorkerExecution:
        """登记远程任务并返回句柄（同一 ``task_id`` 重复调用返回原句柄）。"""
        cached = self._handles.get(task.task_id)
        if cached is not None:
            return cached
        request = RemoteTaskRequest.from_task(
            task,
            provider_id=task.asset.source_id,
            protocol="https",
            target_relative_path=relative_path,
        )
        self.submit(request)
        execution = WorkerExecution(
            worker_id=self._worker_id,
            handle=task.task_id,
            remote_task_id=task.task_id,
            started_at_monotonic=self._clock(),
        )
        self._handles[task.task_id] = execution
        return execution

    def poll(self, execution: WorkerExecution) -> RemoteTaskState:
        """查询远程任务状态。"""
        return self.status(execution.remote_task_id).state

    def result(self, execution: WorkerExecution) -> RemoteTaskResult:
        """读取远程产物描述。

        Raises
        ------
        RemoteUnavailableError
            任务尚未完成（可稍后重试）。
        """
        status = self.status(execution.remote_task_id)
        if status.state is not RemoteTaskState.COMPLETED or status.result is None:
            raise RemoteUnavailableError(
                f"task {execution.remote_task_id!r} is {status.state.value}",
                code="task_not_completed",
            )
        return status.result

    def fetch(self, execution: WorkerExecution, destination: Path) -> FetchReport:
        """取回远程产物到 ``destination``。"""
        result = self.result(execution)
        return self.download(execution.remote_task_id, destination, expected_size=result.size_bytes)

    def cancel(self, execution: WorkerExecution) -> bool:
        """取消远程任务（终态任务返回 ``False``）。"""
        try:
            response = self.cancel_remote(execution.remote_task_id)
        except RemoteProtocolError:
            return False
        return response.cancelled

    def close(self) -> None:
        """关闭 HTTP 客户端（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._client.close()

    # -- 内部 -------------------------------------------------------------- #
    def _get_json(self, path: str) -> dict[str, JsonValue]:
        try:
            response = self._client.get(path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(
                f"GET {path} failed: {type(exc).__name__}: {exc}", code="transport_error"
            ) from exc
        if response.status_code != 200:
            raise self._error_for(response.status_code, f"GET {path} failed")
        return _json_object(response)

    def _post_json(
        self,
        path: str,
        model: object,
        *,
        allow_status: tuple[int, ...] = (200, 201),
    ) -> dict[str, JsonValue]:
        body = _payload(model)
        try:
            response = self._client.post(path, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(
                f"POST {path} failed: {type(exc).__name__}: {exc}", code="transport_error"
            ) from exc
        if response.status_code not in allow_status:
            raise self._error_for(response.status_code, f"POST {path} failed")
        return _json_object(response)

    def _error_for(self, status: int, message: str) -> Exception:
        """把 HTTP 状态码映射成客户端异常（可重试 vs 不可重试）。"""
        if status in (401, 403):
            return RemoteProtocolError(
                f"{message}: {status} (authentication/authorisation failed)",
                status=status,
                code="unauthorized",
            )
        if status in (429, 500, 502, 503, 504):
            return RemoteUnavailableError(
                f"{message}: HTTP {status}", status=status, code="server_busy"
            )
        return RemoteProtocolError(
            f"{message}: HTTP {status}", status=status, code="request_rejected"
        )


def _payload(model: object) -> Mapping[str, Any] | None:
    """把请求体模型转成可 JSON 序列化的映射（``dict`` 直接透传）。

    返回 ``None`` 的语义是"空请求体"（``POST /v1/tasks/{id}/cancel`` 不需要正文）。
    """
    if isinstance(model, Mapping):
        if not model:
            return None
        return {str(key): value for key, value in model.items()}
    dump = getattr(model, "model_dump", None)
    if dump is None:  # pragma: no cover - 防御性分支
        raise TypeError(f"cannot serialise {type(model).__name__} as a JSON body")
    payload: Mapping[str, Any] = dump(mode="json")
    return payload


def _json_object(response: httpx.Response) -> dict[str, JsonValue]:
    """解析响应体为 JSON 对象；失败时抛 :class:`RemoteProtocolError`。"""
    try:
        raw = response.json()
    except ValueError as exc:
        raise RemoteProtocolError(
            f"response is not JSON: {exc}", status=response.status_code, code="invalid_payload"
        ) from exc
    if not isinstance(raw, dict):
        raise RemoteProtocolError(
            "response JSON is not an object",
            status=response.status_code,
            code="invalid_payload",
        )
    result: dict[str, JsonValue] = {}
    for key, value in raw.items():
        result[str(key)] = _json_value(value)
    return result


def _json_value(value: object) -> JsonValue:
    """把任意 Python 值降级成 :data:`JsonValue`（嵌套容器递归处理）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)
