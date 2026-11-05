"""Worker 契约：节点身份、能力、健康、远程任务与状态模型。

设计取舍
--------
* **节点能力必须如实声明**：一个 worker 能跑哪些数据集、哪些提供方、哪些链路协议、
  单文件上限与并发上限，全部显式建模。调度侧据此做能力匹配，而不是"假设所有节点
  都一样"——不同区域/云上节点的出网能力差异极大。
* **健康是事实而不是配置**：``status`` 只有 ``healthy`` / ``degraded`` / ``offline``，
  且由 :class:`~researchdatahub.network.workers.registry.WorkerRegistry` 依据**最近一次
  真实心跳/探测时间**判定；``last_heartbeat`` 为 ``None`` 表示从未收到心跳，此时
  ``uptime_s`` 必须是 ``None``（从未上线过，不能编造在线时长）。
* **不记录任何密钥**：``WorkerCapabilities.protocols`` 只描述能做什么，认证材料留在
  调用方的 ``auth_ref`` 指针里（见 :class:`~researchdatahub.network.routes.models.Route`）。
* 远程任务的 ``result`` 描述的是 **worker 侧** 已完成的产物（路径、字节数、sha256），
  回传由主进程通过 ``GET /v1/files/{id}`` 完成并**重新校验**；不信任任何声明。

所有模型复用 ``core.models`` 的不可变契约基类（``frozen`` + ``extra="forbid"``），
与既有模块的序列化约定保持一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from researchdatahub.core.models import AccessRoute, Checksum, JsonValue

#: 与 ``core.models`` 完全相同的标识符与非空字符串约束；这里重新声明而不是 import
#: 核心模块的私有名字（``Identifier``/``NonEmptyStr`` 是 ``core.models.__all__`` 之外的
#: 派生类型，跨模块 import 私有名会锁死核心模块的重构自由）。
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$", strip_whitespace=True, max_length=63),
]
NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, max_length=2048, strip_whitespace=True)
]


class _Contract(BaseModel):
    """契约模型公共配置：不可变 + 拒绝未知字段（与 ``core.models._Contract`` 同义）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


if TYPE_CHECKING:  # pragma: no cover - 仅类型期
    from researchdatahub.core.models import AcquisitionTask

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS",
    "MAX_REMOTE_PAYLOAD_BYTES",
    "REMOTE_PROTOCOL_VERSION",
    "RemoteTaskRequest",
    "RemoteTaskResult",
    "RemoteTaskState",
    "WorkerCapabilities",
    "WorkerHealth",
    "WorkerKind",
    "WorkerRecord",
    "WorkerStatus",
    "WorkerSummary",
]

REMOTE_PROTOCOL_VERSION = "1.0"
"""远程 Worker HTTP 协议版本（请求体内 ``protocol_version`` 字段）。"""

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
"""默认心跳间隔（秒）。主进程按该间隔给远程 worker 续命，worker 侧据此判定租约。"""

DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS = 1800.0
"""默认单个远程下载任务的总超时（秒）。超时由主进程强制执行并记为可重试失败。"""

MAX_REMOTE_PAYLOAD_BYTES = 8 * 1024 * 1024
"""控制面 JSON 请求体的硬上限（字节）。超出直接 413，先拒绝再解析。"""

_WORKER_ID_PATTERN_HINT = "小写标识符（字母/数字/._-，长度 2..63）"


class WorkerKind(StrEnum):
    """节点类型。它决定"这个节点凭什么有资格工作"，不影响协议形状。"""

    LOCAL = "local"
    REMOTE = "remote"
    REGIONAL = "regional"
    CLOUD = "cloud"


class WorkerStatus(StrEnum):
    """节点健康三态。``offline`` 由心跳超时**推导**，不允许直接注册为 offline。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class RemoteTaskState(StrEnum):
    """远程任务的服务端状态（与本地 12 态状态机是不同层次，不要混淆）。

    * ``queued``：已接受但尚未开始（worker 并发已满或排队）；
    * ``executing``：正在下载；
    * ``completed``：下载 + worker 侧 sha256 校验完成，产物可通过 ``/v1/files/{id}`` 取回；
    * ``failed``：失败（``error`` 给出可重试性）；
    * ``cancelled``：被显式取消。
    """

    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """是否终态（终态不可再被 ``/v1/tasks`` 改写，只能原样返回）。"""
        return self in (
            RemoteTaskState.COMPLETED,
            RemoteTaskState.FAILED,
            RemoteTaskState.CANCELLED,
        )


class WorkerCapabilities(_Contract):
    """节点能力声明：能跑哪些数据、走哪些链路、最多搬多大、最多并行几个。

    ``datasets`` / ``providers`` / ``protocols`` 为空表示**不限制**（"我什么都能跑"），
    因此"没写"与"不支持"是两件事：要表达不支持就必须显式列一个非空白名单。
    """

    datasets: list[str] = Field(
        default_factory=list, max_length=256, description="支持的数据集 ID 白名单（空=不限制）"
    )
    providers: list[str] = Field(
        default_factory=list, max_length=256, description="支持的提供方 ID 白名单（空=不限制）"
    )
    protocols: list[str] = Field(
        default_factory=list, max_length=16, description="支持的链路协议白名单（空=不限制）"
    )
    max_bytes: int = Field(default=0, ge=0, description="单文件大小上限（字节）；0 表示不限制")
    max_concurrent: int = Field(default=1, ge=1, le=64, description="节点内并行下载上限")

    @field_validator("datasets", "providers", "protocols")
    @classmethod
    def _unique_entries(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("capability entries must not be blank")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    def allows(
        self,
        *,
        dataset_id: str,
        provider_id: str,
        protocol: str,
        size_bytes: int,
    ) -> tuple[bool, str | None]:
        """判断本节点能否承接该请求。

        Returns
        -------
        tuple[bool, str | None]
            ``(True, None)`` 表示可承接；否则 ``(False, 原因)``，原因字符串可直接展示。
        """
        if self.datasets and dataset_id not in self.datasets:
            return False, f"dataset {dataset_id!r} not in worker dataset allowlist"
        if self.providers and provider_id not in self.providers:
            return False, f"provider {provider_id!r} not in worker provider allowlist"
        if self.protocols and protocol not in self.protocols:
            return False, f"protocol {protocol!r} not supported by worker"
        if self.max_bytes and size_bytes > self.max_bytes:
            return False, f"asset size {size_bytes} exceeds worker max_bytes {self.max_bytes}"
        return True, None


class WorkerHealth(_Contract):
    """节点健康快照（事实 + 推导结果，二者分字段存放）。"""

    status: WorkerStatus = WorkerStatus.HEALTHY
    last_heartbeat: datetime | None = Field(
        default=None,
        description="最近一次真实心跳时间（UTC）；``None`` 表示注册表从未观测到心跳",
    )
    uptime_s: float | None = Field(
        default=None, ge=0.0, description="节点自注册以来的运行秒数；从未心跳时为 ``None``"
    )
    consecutive_failures: int = Field(
        default=0, ge=0, description="连续探测/传输失败次数（用于判定 degraded）"
    )

    @field_validator("last_heartbeat")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_heartbeat must be timezone aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _offline_implies_no_uptime(self) -> WorkerHealth:
        """``offline`` 节点不能声称在线时长（没有任何新鲜证据支撑该数字）。"""
        if self.status is WorkerStatus.OFFLINE and self.uptime_s not in (None, 0.0):
            raise ValueError("an offline worker cannot report a non-zero uptime")
        return self


class WorkerRecord(_Contract):
    """一个已注册节点的完整事实记录（SQLite ``workers`` 表的行投影）。"""

    worker_id: Identifier
    kind: WorkerKind
    region: str | None = Field(default=None, max_length=64, description="部署区域标签（仅展示用）")
    capabilities: WorkerCapabilities = Field(default_factory=WorkerCapabilities)
    health: WorkerHealth = Field(default_factory=WorkerHealth)
    version: str = Field(default="0.1.0", min_length=1, max_length=64)
    endpoint: str | None = Field(
        default=None,
        max_length=512,
        description="远程节点的基址（``kind=local`` 时必须为 ``None``）",
    )
    registered_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("kind")
    @classmethod
    def _kind_known(cls, value: WorkerKind) -> WorkerKind:
        return value

    @field_validator("region")
    @classmethod
    def _region_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not text.replace("-", "").replace("_", "").isalnum():
            raise ValueError("region must be a short alphanumeric label")
        return text

    @field_validator("endpoint")
    @classmethod
    def _endpoint_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().rstrip("/")
        if not text.startswith(("http://", "https://")):
            raise ValueError("endpoint must start with http:// or https://")
        return text

    @model_validator(mode="after")
    def _local_has_no_endpoint(self) -> WorkerRecord:
        if self.kind is WorkerKind.LOCAL and self.endpoint is not None:
            raise ValueError("local worker must not declare an endpoint")
        if self.kind is not WorkerKind.LOCAL and self.endpoint is None:
            raise ValueError(f"{self.kind.value} worker must declare an endpoint")
        return self

    @property
    def is_available(self) -> bool:
        """``offline`` 之外都算"可以派活"（``degraded`` 仍可用，只是排在后面）。"""
        return self.health.status is not WorkerStatus.OFFLINE

    def matches(
        self,
        *,
        dataset_id: str,
        provider_id: str,
        protocol: str,
        size_bytes: int,
        require_available: bool = True,
    ) -> tuple[bool, str | None]:
        """能力 + 健康度联合匹配（按能力维度筛选的单一入口）。"""
        if require_available and not self.is_available:
            return False, f"worker {self.worker_id} is offline"
        return self.capabilities.allows(
            dataset_id=dataset_id,
            provider_id=provider_id,
            protocol=protocol,
            size_bytes=size_bytes,
        )


class WorkerSummary(_Contract):
    """面向 MCP ``list_workers`` 的精简视图：只给决策需要的字段。"""

    worker_id: str
    kind: WorkerKind
    region: str | None = None
    status: WorkerStatus
    last_heartbeat: datetime | None = None
    uptime_s: float | None = None
    version: str
    endpoint: str | None = None
    capabilities: WorkerCapabilities = Field(default_factory=WorkerCapabilities)

    @classmethod
    def of(cls, record: WorkerRecord) -> WorkerSummary:
        """从完整记录派生摘要（不丢关键能力字段）。"""
        return cls(
            worker_id=record.worker_id,
            kind=record.kind,
            region=record.region,
            status=record.health.status,
            last_heartbeat=record.health.last_heartbeat,
            uptime_s=record.health.uptime_s,
            version=record.version,
            endpoint=record.endpoint,
            capabilities=record.capabilities,
        )


class RemoteTaskResult(_Contract):
    """worker 侧产物的结构化描述（主进程收到后**重新校验**才算数）。"""

    path: str = Field(min_length=1, max_length=4096, description="worker 本机绝对路径")
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    duration_s: float = Field(ge=0.0)

    @property
    def checksum(self) -> Checksum:
        """转成核心契约的 :class:`Checksum`（``sha256:<hex>``）。"""
        return Checksum(value=f"sha256:{self.sha256}")


class RemoteTaskRequest(_Contract):
    """``POST /v1/tasks`` 的请求体：asset 描述 + 目标相对路径 + 可选的期望校验和。"""

    protocol_version: str = Field(default=REMOTE_PROTOCOL_VERSION, max_length=16)
    task_id: Identifier
    asset_id: Identifier
    dataset_id: Identifier
    source_id: Identifier
    provider_id: NonEmptyStr = Field(max_length=128)
    access_route: AccessRoute
    protocol: str = Field(default="https", min_length=1, max_length=32)
    origin_uri: NonEmptyStr = Field(max_length=4096, description="远端原始地址（含签名串）")
    file_name: NonEmptyStr = Field(max_length=512)
    size_bytes: int = Field(ge=0)
    expected_checksum: str | None = Field(
        default=None,
        max_length=72,
        pattern=r"^(sha256|sha256-16|md5|crc32):[0-9a-f]{8,64}$",
        description="调用方声明的期望校验和；worker 只在算法为 sha256 时据此自判",
    )
    target_relative_path: NonEmptyStr = Field(
        max_length=1024, description="相对 worker 存储根的落盘路径（禁止绝对路径/..）"
    )
    timeout_seconds: float = Field(default=DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS, gt=0.0, le=86_400.0)
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict, description="透传的只读上下文（不含任何密钥）"
    )

    @field_validator("target_relative_path")
    @classmethod
    def _no_escape(cls, value: str) -> str:
        """只做**拒绝**，不做规范化：``a/../b`` 这类"规范化后合法"的写法同样拒绝。

        规范化会把越界写成合法相对路径，从而在审计记录里留下一个"看起来没问题"的路径；
        远程下发场景宁可要求调用方自己给出干净路径。
        """
        text = value.strip().replace("\\", "/")
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if not parts:
            raise ValueError("target_relative_path must name at least one component")
        if any(part == ".." for part in parts):
            raise ValueError("target_relative_path must not contain '..'")
        if text.startswith("/"):
            raise ValueError("target_relative_path must be relative")
        return "/".join(parts)

    @classmethod
    def from_task(
        cls,
        task: AcquisitionTask,
        *,
        provider_id: str,
        protocol: str,
        target_relative_path: str,
        timeout_seconds: float = DEFAULT_REMOTE_TASK_TIMEOUT_SECONDS,
    ) -> RemoteTaskRequest:
        """从采集任务派生远程请求（``provider_id``/``protocol`` 来自选中的 Route）。"""
        asset = task.asset
        return cls(
            task_id=task.task_id,
            asset_id=asset.asset_id,
            dataset_id=asset.dataset_id,
            source_id=asset.source_id,
            provider_id=provider_id,
            access_route=asset.access_route,
            protocol=protocol,
            origin_uri=asset.origin_uri,
            file_name=asset.file_name,
            size_bytes=asset.size_bytes,
            expected_checksum=None if asset.checksum is None else asset.checksum.value,
            target_relative_path=target_relative_path,
            timeout_seconds=timeout_seconds,
        )
