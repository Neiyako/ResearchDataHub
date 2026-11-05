"""多节点 / 多链路网络层（M4）：Worker 注册与健康、本地/远程执行、Route 选路与故障转移。

公开 API（MCP 层与 CLI 只应使用这里导出的名字）
-----------------------------------------------
节点
    :class:`~researchdatahub.network.workers.registry.WorkerRegistry`（注册/心跳/健康/能力匹配，
    SQLite 持久化于 ``~/.rdh/workers.db``）；模型 :class:`WorkerRecord` / ``WorkerSummary`` /
    ``WorkerCapabilities`` / ``WorkerHealth`` / ``WorkerStatus`` / ``WorkerKind``。
执行
    :class:`~researchdatahub.network.workers.local.LocalWorker`（复用既有传输通道的真实本地
    下载）、:class:`~researchdatahub.network.workers.client.RemoteWorkerClient`（HTTP 远程节点）、
    :class:`~researchdatahub.network.workers.agent.AgentServer`（真实 HTTP 服务端，可
    ``python -m researchdatahub.network.workers.agent`` 启动；为避免 ``-m`` 启动时的
    "已导入模块" 警告，这里**不**在包级 import，请用
    :func:`~researchdatahub.network.workers.agent.build_agent_server`
    或 ``from researchdatahub.network.workers.agent import AgentServer`` 显式引入）。
链路
    :class:`~researchdatahub.network.routes.manager.RouteManager`（实测选路）、
    :class:`~researchdatahub.network.routes.stats.RouteStatsStore`（``route_stats`` 真实样本）、
    :class:`~researchdatahub.network.routes.failover.FailoverCoordinator`（故障转移编排）。
错误
    :class:`~researchdatahub.network.routes.manager.NoRouteAvailable`、
    :class:`~researchdatahub.network.routes.failover.FailoverExhausted`、
    :class:`~researchdatahub.network.workers.registry.WorkerRegistrationError`、
    :class:`~researchdatahub.network.workers.client.RemoteUnavailableError`。

设计边界
--------
* 本模块**不修改** ``scheduler`` / ``transfer`` / ``manifest``：本地执行直接复用
  ``TransferExecutor`` 协议（``transfer_outcome``），远程执行把同一份字节交给
  ``GET /v1/files/{id}`` 回传后再用 ``sha256`` 复核。
* 控制面数据（节点注册、链路统计）存在**独立的** ``workers.db``，不写 ``manifest.db``。
* 没有任何"配置文件里写几个虚拟节点"的路径：健康度来自真实心跳/探活，链路性能来自真实
  传输样本；没有样本时选路结果标注 ``basis="no_measurement"``。
"""

from researchdatahub.network.routes.failover import (
    DEFAULT_MAX_ROUTES_PER_ATTEMPT,
    AttemptRecord,
    FailoverCoordinator,
    FailoverExhausted,
    TransferDelivery,
    TransferredFile,
)
from researchdatahub.network.routes.manager import (
    NoRouteAvailable,
    RouteManager,
    RouteRecord,
)
from researchdatahub.network.routes.models import (
    NO_MEASUREMENT,
    ROUTE_PROTOCOLS,
    MeasurementBasis,
    Route,
    RouteOption,
    RouteRejection,
    RouteRejectionCode,
    RouteSelection,
    RouteStats,
)
from researchdatahub.network.routes.stats import RouteStatsStore, score_route
from researchdatahub.network.workers.base import FetchReport, WorkerExecution, WorkerProtocol
from researchdatahub.network.workers.client import (
    RemoteProtocolError,
    RemoteUnavailableError,
    RemoteWorkerClient,
)
from researchdatahub.network.workers.local import LocalWorker
from researchdatahub.network.workers.models import (
    REMOTE_PROTOCOL_VERSION,
    RemoteTaskRequest,
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
    WorkerRecord,
    WorkerStatus,
    WorkerSummary,
)
from researchdatahub.network.workers.registry import (
    WorkerRegistrationError,
    WorkerRegistry,
    WorkerUnknownError,
)
from researchdatahub.network.workers.store import (
    WORKERS_SCHEMA_VERSION,
    WorkerStore,
    connect_workers_db,
)

__all__ = [
    "DEFAULT_MAX_ROUTES_PER_ATTEMPT",
    "NO_MEASUREMENT",
    "REMOTE_PROTOCOL_VERSION",
    "ROUTE_PROTOCOLS",
    "WORKERS_SCHEMA_VERSION",
    "AgentServer",
    "AttemptRecord",
    "FailoverCoordinator",
    "FailoverExhausted",
    "FetchReport",
    "LocalWorker",
    "MeasurementBasis",
    "NoRouteAvailable",
    "RemoteProtocolError",
    "RemoteTaskRequest",
    "RemoteTaskResult",
    "RemoteTaskState",
    "RemoteUnavailableError",
    "RemoteWorkerClient",
    "Route",
    "RouteManager",
    "RouteOption",
    "RouteRecord",
    "RouteRejection",
    "RouteRejectionCode",
    "RouteSelection",
    "RouteStats",
    "RouteStatsStore",
    "TransferDelivery",
    "TransferredFile",
    "WorkerCapabilities",
    "WorkerExecution",
    "WorkerHealth",
    "WorkerKind",
    "WorkerProtocol",
    "WorkerRecord",
    "WorkerRegistrationError",
    "WorkerRegistry",
    "WorkerStatus",
    "WorkerStore",
    "WorkerSummary",
    "WorkerUnknownError",
    "connect_workers_db",
    "score_route",
]
