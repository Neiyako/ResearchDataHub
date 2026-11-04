"""模块接口契约（``typing.Protocol``）。

为什么用 Protocol 而不是 ABC
----------------------------
* 各模块（sources / transfer / storage / manifest …）不依赖彼此的具体实现，只依赖形状。
* 便于测试用轻量 fake 替换，而不是 mock 掉核心逻辑（fake 必须真实实现算法）。
* 不会造成 ``core`` ↔ 实现模块的循环依赖。

通用约定
--------
* **线程模型**：调度器在 ``ThreadPoolExecutor`` 中执行同步传输；异步适配器必须能够
  通过 ``asyncio.run(...)`` 包装成同步调用，或在事件循环内 ``await``。
* **异常**：所有失败必须抛 :mod:`researchdatahub.core.errors` 中的具体异常；
  禁止返回 ``None`` 表示失败，禁止吞掉异常。
* **不可变性**：传入的契约对象是 frozen 的，实现不得就地修改；需要变更时返回新对象。
* **生命周期**：带 ``open()`` / ``close()`` 的实现必须可作上下文管理器使用
  （``with adapter: ...``），并在 ``close()`` 后拒绝调用其它方法（抛 ``RuntimeError``）。
* **并发安全**：见各 Protocol 的 docstring；跨线程共享的实例契约必须显式声明。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from researchdatahub.core.models import (
    AcquisitionPlan,
    AcquisitionTask,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
    FileRecord,
    IntegrityReport,
    SourceAsset,
    SourceMetadata,
    TransferResult,
)
from researchdatahub.core.state import TaskState

__all__ = [
    "CatalogProvider",
    "DataSourceAdapter",
    "IntegrityChecker",
    "ManifestRepository",
    "StorageBackend",
    "TaskPlanner",
    "TaskScheduler",
    "TransportAdapter",
    "assert_implements",
]


@runtime_checkable
class DataSourceAdapter(Protocol):
    """数据源适配器：**发现**数据，不做传输。

    职责边界
    --------
    * 负责：目录查询、变量/时间/层级参数翻译成远端请求、元数据解析、文件列表与大小/校验和、
      生成 :class:`SourceAsset`。
    * 不负责：下载字节、重试、并发、落盘。这些属于 :class:`TransportAdapter`。

    并发安全：实例必须是线程安全的（``discover_assets`` 可能被多个 worker 并发调用）；
    实现内部如需可变缓存，必须加锁。

    异步：允许实现 ``async`` 版本，但必须同时提供同步包装，或声明本协议方法为 ``async def``。
    本协议以同步签名定义，异步实现用 ``asyncio.run()`` 适配。
    """

    @property
    def source_id(self) -> str:
        """该适配器对应的数据源 ID（必须与配置 ``sources.<id>`` 一致）。"""
        ...

    @property
    def supported_routes(self) -> frozenset[str]:
        """该源可用的访问路径（取值同 :class:`AccessRoute`）；用于计划阶段的传输选择。"""
        ...

    def describe(self) -> SourceMetadata:
        """返回数据源级元数据（提供方、许可证、引用、访问路径）。

        Raises
        ------
        SourceUnavailableError
            远端元数据服务不可达。
        MetadataError
            元数据格式不符合预期。
        """
        ...

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        """把用户需求映射为该源上的若干文件资产（可以是空列表）。

        参数
        ----
        request:
            用户数据需求（含科学元数据：变量、时间、空间、垂直层）。

        返回
        ----
        Sequence[SourceAsset]
            发现的资产列表；每个资产必须自带 ``metadata`` 与来源追踪字段。

        Raises
        ------
        SourceNotFoundError
            该源不提供请求的数据集。
        SourceUnavailableError
            远端目录服务不可达或认证失败。
        MetadataError
            远端元数据与需求无法建立映射（例如变量不存在）。

        注意
        ----
        返回的资产列表与需求**逐项覆盖**：若远端一个文件包含多个变量，也要声明清楚；
        M1 实现必须保证 ``unmatched_variables`` 可被计划器识别。
        """
        ...

    def health_check(self) -> bool:
        """轻量健康检查（不下载数据）。返回 ``False`` 表示暂时不可用，不应抛异常。"""
        ...


@runtime_checkable
class TransportAdapter(Protocol):
    """传输适配器：**搬运字节**，不知道数据源的科学语义。

    关键约束（ADR-0002）
    -------------------
    * 不得包含任何特化到某个数据源的业务逻辑（例如「NCAR 的 S3 路径规则」）。
      源特定的路径/认证/前缀必须由 :class:`SourceAsset.origin_uri` 与
      ``access_route`` 完全表达。
    * 只负责：连接、超时、分块、断点续传、重试策略执行、传输统计。

    并发安全：单一实例可被多个线程并发调用 ``transfer``；实现必须无共享可变状态，
    或对共享状态加锁。

    异步：``atransfer`` 提供异步版本；调度器优先使用同步 ``transfer``（线程池内运行）。
    """

    @property
    def transport_id(self) -> str:
        """传输实现标识，如 ``s3`` / ``https`` / ``globus`` / ``cds_api``。"""
        ...

    def supports(self, asset: SourceAsset) -> bool:
        """该传输实现是否能处理给定资产（依据 ``access_route`` 与 URI scheme）。"""
        ...

    def transfer(
        self, asset: SourceAsset, destination: Path, *, task: AcquisitionTask
    ) -> TransferResult:
        """把 ``asset`` 下载到 ``destination``（实现负责断点续传与临时文件）。

        参数
        ----
        asset: 远端资产描述（含 URI、大小、可选校验和）。
        destination: 最终目标路径；实现必须**原子**落盘（先写 ``.part`` 再 rename）。
        task: 所属任务（用于超时、分块策略与错误上下文）。

        返回
        ----
        TransferResult
            无论成功与否都返回结构化结果；调用方据此推进状态机。

        Raises
        ------
        RetryableTransportError
            可重试失败（超时、连接重置、5xx、429）。
        PermanentTransportError
            不可重试失败（404、403、协议不支持、内容与声明不符）。
        StorageError
            本地磁盘写入失败。
        """
        ...

    async def atransfer(
        self, asset: SourceAsset, destination: Path, *, task: AcquisitionTask
    ) -> TransferResult:
        """``transfer`` 的异步版本。语义与异常完全一致。"""
        ...

    def close(self) -> None:
        """释放连接池/临时凭证。可重复调用（幂等）。"""
        ...


@runtime_checkable
class CatalogProvider(Protocol):
    """数据集目录：记录“有哪些数据集、它们的科学身份是什么”。

    与 Manifest 的区别：Catalog 是**声明式的产品目录**（静态、可版本化），
    Manifest 是**运行时的任务/文件状态**。二者不可混用。

    并发安全：只读接口必须线程安全；``upsert_dataset`` 必须由单线程调用。
    """

    def list_datasets(self) -> Sequence[str]:
        """返回已登记的数据集 ID 列表（升序）。"""
        ...

    def get_metadata(self, dataset_id: str) -> DatasetMetadata:
        """返回数据集的科学元数据。

        Raises
        ------
        SourceNotFoundError
            数据集未登记。
        """
        ...


@runtime_checkable
class TaskPlanner(Protocol):
    """计划器：需求 → 计划。纯函数式，不做 I/O 以外的副作用。

    并发安全：无状态，可并发调用（但依赖的 adapters 必须线程安全）。
    """

    def plan(
        self,
        request: DatasetRequest,
        adapters: Iterable[DataSourceAdapter],
    ) -> AcquisitionPlan:
        """为需求生成执行计划。

        必须做到：源匹配（能力/优先级）、文件去重、无法满足的变量记入
        ``unmatched_variables``、任务初始状态为 ``PLANNED``。

        Raises
        ------
        SourceNotFoundError
            所有候选源都不支持该请求。
        ScheduleError
            计划规模超出配置上限。
        """
        ...


@runtime_checkable
class TaskScheduler(Protocol):
    """调度器：决定何时、由谁、以何种并发执行任务。

    并发安全：``submit`` / ``cancel`` 可由多线程调用，内部队列必须线程安全；
    状态写入只能通过:class:`ManifestRepository` 的串行写入通道。
    """

    def submit(self, plan: AcquisitionPlan) -> int:
        """把计划中的任务加入队列，返回入队数量。"""
        ...

    def run(self, *, max_workers: int | None = None) -> None:
        """执行队列中的任务直到队列清空或收到取消。

        Raises
        ------
        ScheduleError
            并发配置非法或调度器已处于运行状态。
        """
        ...

    def cancel(self, task_id: str, reason: str) -> None:
        """请求取消单个任务（状态机负责校验是否允许）。

        Raises
        ------
        StateTransitionError
            任务已处于终态。
        """
        ...


@runtime_checkable
class IntegrityChecker(Protocol):
    """完整性校验：只用元数据与文件字节，不信任远端声明。

    **Integrity Before Delivery**：未通过校验的文件不得进入 ``COMPLETED``。
    并发安全：无状态实现应线程安全。
    """

    def check(
        self,
        file_record: FileRecord,
        *,
        task: AcquisitionTask,
        kinds: Sequence[str],
    ) -> IntegrityReport:
        """按 ``kinds`` 执行检查并返回报告。

        返回 ``status=FAILED`` 而不是抛异常（失败是正常结果）；只有内部不可恢复错误
        才抛 :class:`IntegrityError`。

        Raises
        ------
        IntegrityError
            检查无法进行（文件缺失但未被记录等）。
        """
        ...


@runtime_checkable
class StorageBackend(Protocol):
    """存储后端：临时区、原始区、隔离区、归档区。

    **Preserve Original Data**：原始区中的文件默认只读，``publish_original`` 之后
    不得再被覆盖式写入；清理只能通过显式 ``remove`` 并记录审计。

    并发安全：多个 worker 可并发 ``allocate_temp``（不同任务不同路径）；
    ``publish_original`` 必须是原子移动。
    """

    def allocate_temp(self, task_id: str) -> Path:
        """为该任务分配一个临时文件路径（父目录已创建）。"""
        ...

    def publish_original(self, temp_path: Path, *, dataset_id: str, file_name: str) -> Path:
        """把校验通过的文件原子移入原始区，返回最终路径（只读）。

        Raises
        ------
        StorageError
            目标已存在/磁盘不足/跨设备移动失败。
        """
        ...

    def quarantine(self, path: Path, *, reason: str) -> Path:
        """把校验失败的文件移入隔离区（不删除），返回隔离路径。"""
        ...

    def free_bytes(self) -> int:
        """返回存储根所在卷的可用字节数（调度器据此暂停任务）。"""
        ...


@runtime_checkable
class ManifestRepository(Protocol):
    """SQLite 持久化的事实来源（ADR-0003）。

    **并发规则（硬性）**：SQLite 连接不可跨线程共享。每个 worker 持有自己的连接；
    写事务必须串行化（单写者队列或 ``BEGIN IMMEDIATE`` + 重试）。

    事务边界
    --------
    ``save_task`` + ``append_event`` 必须落在同一事务内，避免“状态变了但没记事件”。
    """

    def initialize(self) -> None:
        """创建/迁移 schema。幂等。"""
        ...

    def save_task(self, task: AcquisitionTask, *, expected_state: TaskState | None = None) -> None:
        """写入任务当前状态。

        参数
        ----
        expected_state:
            乐观并发控制：若数据库中当前状态与此不符，抛 :class:`ManifestError`
            （防止两个 worker 同时推进同一任务）。

        Raises
        ------
        ManifestError
            记录不存在、版本冲突或事务失败。
        """
        ...

    def load_task(self, task_id: str) -> AcquisitionTask:
        """读取任务快照。

        Raises
        ------
        ManifestError
            任务不存在。
        """
        ...

    def list_tasks(self, *, state: TaskState | None = None) -> Iterator[AcquisitionTask]:
        """流式列出任务（可按状态过滤）。惰性迭代，避免一次载入全部。"""
        ...

    def append_event(self, task_id: str, event: str, payload: dict[str, object]) -> None:
        """追加一条不可变的事件记录（审计日志）。"""
        ...

    def save_file_record(self, record: FileRecord) -> None:
        """登记一个已落盘的文件（与任务完成同一事务内）。"""
        ...

    def build_delivery_manifest(self, request_id: str) -> DeliveryManifest:
        """从已完成的文件记录生成交付清单。

        Raises
        ------
        ManifestError
            请求下没有已完成的文件，或存在未通过的完整性报告。
        """
        ...

    async def aiter_tasks(self) -> AsyncIterator[AcquisitionTask]:
        """异步迭代任务（供异步 worker 使用）。语义与 ``list_tasks`` 相同。"""
        ...


def assert_implements(instance: object, protocol: type) -> None:
    """契约自检：断言 ``instance`` 满足 ``protocol`` 的运行时结构。

    仅在 ``@runtime_checkable`` 协议上可用；对非 Protocol 传入会抛 ``TypeError``。

    Raises
    ------
    TypeError
        对象未实现协议要求的方法/属性集合。
    """
    if not getattr(protocol, "_is_protocol", False):
        raise TypeError(f"{protocol!r} is not a Protocol")
    if not getattr(protocol, "_is_runtime_protocol", False):
        raise TypeError(f"{protocol!r} is not @runtime_checkable")
    if not isinstance(instance, protocol):
        missing = sorted(
            name
            for name in getattr(protocol, "__protocol_attrs__", ())
            if not hasattr(instance, name)
        )
        raise TypeError(
            f"{type(instance).__name__} does not implement {protocol.__name__}; missing: {missing}"
        )
