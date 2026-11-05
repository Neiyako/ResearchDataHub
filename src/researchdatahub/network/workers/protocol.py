"""远程 Worker 的 HTTP 协议契约（JSON over HTTP，生产环境应置于 HTTPS/TLS 之后）。

协议总览（``protocol_version = "1.0"``）
-------------------------------------
===================== ====== ======================================================
端点                   方法   语义
===================== ====== ======================================================
``/v1/health``         GET    真实健康检查：返回版本、能力、运行时长、可接受的能力校验
``/v1/register``       POST   登记调用方声明的能力（拒绝则返回 ``accepted=false`` + 原因）
``/v1/heartbeat``      POST   心跳续命；返回节点运行时长与在途任务数
``/v1/tasks``          POST   **幂等**下发任务：同一 ``task_id`` 重复提交返回原状态，不重复执行
``/v1/tasks/{id}``     GET    查询任务状态（``queued|executing|completed|failed|cancelled``）
``/v1/tasks/{id}/cancel`` POST 取消（终态后返回 409，不假装成功）
``/v1/files/{id}``     GET    取回产物字节流（支持 ``Range``，返回 ``Accept-Ranges: bytes``）
===================== ====== ======================================================

状态码约定
----------
* ``200``：成功；``201``：首次创建任务；``206``：范围响应；
* ``400``：请求体不是合法 JSON 或违反 schema（附 ``problems``）；
* ``401``：需要令牌但缺失/错误；``403``：令牌有效但该节点拒绝（能力不匹配）；
* ``404``：未知任务/未知产物；``409``：状态冲突（对终态任务取消、或重复任务内容不一致）；
* ``413``：请求体超过 :data:`~researchdatahub.network.workers.models.MAX_REMOTE_PAYLOAD_BYTES`；
* ``416``：``Range`` 越界；``429``：并发已满；``500``：worker 内部错误。

失败响应体统一为 ``{"error": "<code>", "message": "...", "detail": {...}}``。
**任何响应都不回显令牌**；``/v1/health`` 会校验令牌（非法即 401），因为"能探活"本身
就说明持有有效凭据。

幂等与失联
----------
* 任务幂等键是 ``task_id``：首次提交后无论重试多少次，worker **只执行一次**；
* 主进程侧靠心跳续命 + 请求超时判定失联；失联节点由
  :class:`~researchdatahub.network.workers.registry.WorkerRegistry` 降级/下线；
* worker 侧靠"任务租约"自愈：超过 ``lease_seconds`` 没有再被心跳或状态查询触达的
  在途任务视为孤儿并取消（孤儿回收由 :meth:`RemoteTaskManager.reap_expired` 触发）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from researchdatahub.core.models import JsonValue
from researchdatahub.network.workers.models import (
    REMOTE_PROTOCOL_VERSION,
    RemoteTaskRequest,
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
    _Contract,
)

__all__ = [
    "AgentHealth",
    "CancelResponse",
    "ErrorResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "RegisterRequest",
    "RegisterResponse",
    "RemoteStatusResponse",
    "RemoteSubmitResponse",
]

WorkerIdStr = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$")]


class _WireModel(_Contract):
    """线上模型的公共基类别名（与契约模型同一套冻结/拒绝未知字段配置）。"""


def _check_version(value: str) -> str:
    """协议版本必须精确匹配；不匹配时显式失败，而不是"尽力兼容"。

    版本漂移时的正确做法是让双方都看见失败并升级，而不是静默降级——控制面协议一旦
    开始"尽力兼容"，幂等与状态语义就会出现无法审计的分叉。
    """
    if value != REMOTE_PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol_version {value!r}; this worker speaks {REMOTE_PROTOCOL_VERSION}"
        )
    return value


class RegisterRequest(_WireModel):
    """``POST /v1/register`` 请求体：调用方为某节点声明能力。"""

    protocol_version: str = Field(default=REMOTE_PROTOCOL_VERSION, max_length=16)
    worker_id: WorkerIdStr
    capabilities: WorkerCapabilities = Field(default_factory=WorkerCapabilities)
    caller: str = Field(default="rdh-main", min_length=1, max_length=64, description="调用方标识")

    _verify_version = field_validator("protocol_version")(_check_version)


class RegisterResponse(_WireModel):
    """注册结果。被拒绝时 ``accepted=False`` 且 ``reason`` 非空。"""

    accepted: bool
    worker_id: str
    reason: str | None = Field(default=None, max_length=512)
    agent_version: str
    effective_capabilities: WorkerCapabilities
    registered_at: datetime | None = None

    @field_validator("registered_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)


class HeartbeatRequest(_WireModel):
    """``POST /v1/heartbeat`` 请求体。"""

    protocol_version: str = Field(default=REMOTE_PROTOCOL_VERSION, max_length=16)
    worker_id: WorkerIdStr

    _verify_version = field_validator("protocol_version")(_check_version)


class HeartbeatResponse(_WireModel):
    """心跳结果：节点在线时长与在途任务数（事实，不编造）。"""

    worker_id: str
    ok: bool = True
    uptime_s: float = Field(ge=0.0)
    active_tasks: int = Field(ge=0)
    known_tasks: int = Field(ge=0)
    queue_depth: int = Field(ge=0)
    server_time: datetime

    @field_validator("server_time")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class AgentHealth(_WireModel):
    """``GET /v1/health`` 响应体。"""

    ok: bool = True
    agent_version: str
    protocol_version: str = REMOTE_PROTOCOL_VERSION
    worker_id: str
    uptime_s: float = Field(ge=0.0)
    capabilities: WorkerCapabilities
    storage_root: str
    free_bytes: int = Field(ge=0)
    active_tasks: int = Field(ge=0)
    known_tasks: int = Field(ge=0)
    server_time: datetime

    @field_validator("server_time")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class RemoteSubmitResponse(_WireModel):
    """``POST /v1/tasks`` 的响应：``created=False`` 表示幂等命中（返回的是原状态）。"""

    task_id: str
    state: RemoteTaskState
    created: bool


class RemoteStatusResponse(_WireModel):
    """``GET /v1/tasks/{id}`` 的响应。"""

    task_id: str
    state: RemoteTaskState
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request: RemoteTaskRequest
    result: RemoteTaskResult | None = None
    error: str | None = Field(default=None, max_length=2048)
    retryable: bool = False
    error_code: str | None = Field(default=None, max_length=64)
    bytes_received: int = Field(default=0, ge=0, description="worker 侧实际收到的字节数")

    @field_validator("created_at", "updated_at", "started_at", "finished_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)


class CancelResponse(_WireModel):
    """``POST /v1/tasks/{id}/cancel`` 的响应。"""

    task_id: str
    state: RemoteTaskState
    cancelled: bool


class ErrorResponse(_WireModel):
    """统一失败响应体（``error`` 是机器可读码，``message`` 是人类可读说明）。"""

    error: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2048)
    detail: dict[str, JsonValue] = Field(default_factory=dict)
