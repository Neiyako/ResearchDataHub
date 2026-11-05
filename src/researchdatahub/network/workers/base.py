"""Worker 能力协议与两种实现：进程内本地 worker、HTTP 远程 worker。

职责边界
--------
Worker 只做一件事：**把一个远端资产搬到本机的一个相对路径，并报告字节数与 sha256**。
它不写 Manifest、不改任务状态、不做状态机推进——那些是主进程
:class:`~researchdatahub.scheduler.TaskExecutor` 的职责。这条边界让"本地执行"与
"远程执行"在调度侧可以互换。

协议形状
--------
::

    worker_id / kind / capabilities / health()
    prepare(task, *, relative_path) -> WorkerExecution   # 非阻塞：登记并开始执行
    poll(handle) -> RemoteTaskState | None
    fetch(handle, destination) -> FetchReport            # 把产物取回本机
    cancel(handle) -> bool
    close() -> None

为什么 ``prepare`` 与 ``poll`` 分开
-----------------------------------
本地 worker 的真实执行是**同步**的（直接复用 ``TaskExecutor`` 的传输通道），远程 worker
则是异步的（``POST /v1/tasks`` 只是登记）。统一成"登记 → 轮询状态 → 取回"之后，
故障转移编排只需要处理一种形状；本地 worker 的同步执行放在线程里跑，``poll`` 立即
返回终态。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from researchdatahub.core.models import AcquisitionTask
from researchdatahub.network.workers.models import (
    RemoteTaskResult,
    RemoteTaskState,
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
)

__all__ = [
    "FetchReport",
    "WorkerExecution",
    "WorkerProtocol",
    "WorkerTaskError",
]


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    """一次已登记的 worker 侧执行（句柄 + 幂等键）。

    Attributes
    ----------
    worker_id:
        承接执行的节点 ID。
    handle:
        worker 侧的执行标识（本地 worker 用任务 ID，远程 worker 用 ``task_id``）。
    remote_task_id:
        远程协议中的 ``task_id``；本地 worker 与主任务 ID 相同。
    """

    worker_id: str
    handle: str
    remote_task_id: str
    started_at_monotonic: float

    def __post_init__(self) -> None:
        """校验句柄非空（空句柄代表调用方装配错误，必须立刻失败）。"""
        if not self.worker_id or not self.handle:
            raise ValueError("WorkerExecution requires non-empty worker_id and handle")


@dataclass(frozen=True, slots=True)
class FetchReport:
    """产物回传 / 本地接管的结果（**以实际写出的字节为准，不采信声明**）。

    Attributes
    ----------
    path:
        本机最终落盘路径。
    size_bytes:
        实际写出的字节数。
    sha256:
        对落盘文件重新计算的 SHA-256（十六进制小写）。
    resumed:
        回传是否用了 HTTP Range 续传/跳过已存在前缀。
    """

    path: Path
    size_bytes: int
    sha256: str
    resumed: bool = False

    @property
    def checksum_value(self) -> str:
        """``sha256:<hex>`` 形式，可直接构造核心契约的 :class:`Checksum`。"""
        return f"sha256:{self.sha256}"


@runtime_checkable
class WorkerProtocol(Protocol):
    """Worker 能力契约（本地/远程实现共享）。

    并发安全：不同 ``task_id`` 的调用可并发；同一 ``task_id`` 重复 ``prepare`` 必须
    返回**已有**的执行句柄（幂等），不得重复执行。
    """

    @property
    def worker_id(self) -> str:
        """节点 ID。"""
        ...

    @property
    def kind(self) -> WorkerKind:
        """节点类型。"""
        ...

    @property
    def capabilities(self) -> WorkerCapabilities:
        """能力声明。"""
        ...

    def health(self) -> WorkerHealth:
        """真实健康检查（不发请求时返回本地事实，远程实现会真实探活）。"""
        ...

    def prepare(self, task: AcquisitionTask, *, relative_path: str) -> WorkerExecution:
        """登记执行并返回句柄（幂等：同一任务重复登记返回原句柄）。"""
        ...

    def poll(self, execution: WorkerExecution) -> RemoteTaskState:
        """查询执行状态；终态之后的重复调用返回同一终态。"""
        ...

    def result(self, execution: WorkerExecution) -> RemoteTaskResult:
        """读取成功的产物描述（非 ``completed`` 时抛 :class:`WorkerTaskError`）。"""
        ...

    def fetch(self, execution: WorkerExecution, destination: Path) -> FetchReport:
        """把产物取回本机 ``destination``（原子落盘，返回实测字节数与 SHA-256）。"""
        ...

    def cancel(self, execution: WorkerExecution) -> bool:
        """取消执行（返回服务端是否接受了取消）。"""
        ...

    def close(self) -> None:
        """释放资源（幂等）。"""
        ...

    # -- 可选能力（编排层用 ``getattr`` 探测，不强制实现） ------------------ #
    #: ``artifact_root``：产物在本机的落盘根目录（``Path``）。编排层用它计算回传目标；
    #: 未实现该属性的 worker 无法参与故障转移（会被跳过并记录原因）。


class WorkerTaskError(Exception):
    """worker 侧执行失败（内部信号；由编排层转成 ``TransferResult``）。"""

    def __init__(self, message: str, *, retryable: bool, error_code: str = "worker_failed") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code
