"""Worker 子系统：契约模型、SQLite 注册表、本地/远程执行与协议。

对外导出见 :mod:`researchdatahub.network`（顶层包是唯一稳定的公开面）。

**刻意不在这里 import ``agent``**：``python -m researchdatahub.network.workers.agent``
的启动过程会先执行本包 ``__init__``，如果它已经 import 了 ``agent``，解释器会给出
"found in sys.modules ... prior to execution" 的 RuntimeWarning。服务端是**独立进程入口**，
由 :data:`AGENT_MODULE` 显式指出，不做包级预加载。
"""

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
from researchdatahub.network.workers.store import WORKERS_SCHEMA_VERSION, WorkerStore

__all__ = [
    "AGENT_MODULE",
    "REMOTE_PROTOCOL_VERSION",
    "WORKERS_SCHEMA_VERSION",
    "FetchReport",
    "LocalWorker",
    "RemoteProtocolError",
    "RemoteTaskRequest",
    "RemoteTaskResult",
    "RemoteTaskState",
    "RemoteUnavailableError",
    "RemoteWorkerClient",
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
]

#: 服务端入口的模块名（``python -m <AGENT_MODULE> --port N``）。
AGENT_MODULE = "researchdatahub.network.workers.agent"
