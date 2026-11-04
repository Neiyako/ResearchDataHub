"""ResearchDataHub 核心数据契约（Pydantic v2）。

模型版本策略（见 ``docs/data-contract.md``）
------------------------------------------
* 本模块所有持久化模型携带 ``schema_version: Literal["1.0"]``。
* **加字段且给默认值** → 保持 1.x；**删除/重命名/收紧约束** → 升 2.0 并提供迁移函数。
* 反序列化时 ``extra="forbid"``：旧版本读到新字段会失败，因此升降级必须显式迁移，
  不能靠“忽略未知字段”蒙混过关。
* :data:`MODELS_VERSION` 记录当前契约版本，``rdh version`` 会打印它。

设计约束
--------
* 禁止把具体数据源（ERA5/CDS/NCAR）的专属字段写进核心模型；数据源特有字段只允许通过
  ``extensions: dict[str, JsonValue]`` 这个受控逃生口承载，且必须命名空间化（``era5.xxx``）。
* 时间一律 ``datetime``（要求 tz-aware，统一转 UTC）；文件大小一律 ``int`` bytes。
* 科学元数据与文件是两个层次：:class:`FileRecord` 描述字节，:class:`DatasetMetadata`
  描述科学内容。二者不可互相替代。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from researchdatahub.core.state import TaskState

__all__ = [
    "MODELS_VERSION",
    "AccessRoute",
    "AcquisitionPlan",
    "AcquisitionTask",
    "Checksum",
    "DatasetMetadata",
    "DatasetRequest",
    "DeliveryManifest",
    "FileRecord",
    "GridSpec",
    "IntegrityCheckKind",
    "IntegrityReport",
    "IntegrityStatus",
    "JsonValue",
    "Priority",
    "SourceAsset",
    "SourceMetadata",
    "SpatialExtent",
    "TemporalExtent",
    "TransferResult",
    "TransferStatus",
    "VariableSpec",
    "VerticalLevelKind",
    "VerticalLevels",
]

MODELS_VERSION: Final[str] = "1.0"
"""当前数据契约版本。所有持久化模型的 ``schema_version`` 必须等于它。"""

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

#: 受控的 JSON 值类型。仅用于 ``extensions`` 这类明确的逃生口，不允许扩散到业务字段。
type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_VARIABLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CHECKSUM_PATTERN = re.compile(r"^(sha256|sha256-16|md5|crc32):([0-9a-f]{8,64})$")

Identifier = Annotated[
    str,
    StringConstraints(pattern=_ID_PATTERN.pattern, strip_whitespace=True, max_length=63),
]
"""小写标识符：``^[a-z0-9][a-z0-9._-]{1,62}$``（数据源、数据集、任务 ID）。"""

VariableName = Annotated[
    str,
    StringConstraints(pattern=_VARIABLE_PATTERN.pattern, min_length=1, max_length=64),
]
"""科学变量名：CF 风格的 snake_case，如 ``clwc`` / ``t2m``。"""

NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, max_length=2048, strip_whitespace=True)
]


def _utc(value: datetime) -> datetime:
    """把 datetime 规整为 UTC；naive 时间被拒绝（时区不明的时间在科研数据里不可接受）。"""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (use datetime(..., tzinfo=UTC))")
    return value.astimezone(UTC)


def _check_schema_version(value: str) -> str:
    if value != MODELS_VERSION:
        raise ValueError(
            f"unsupported schema_version {value!r}; this build only reads {MODELS_VERSION!r}. "
            "Run the migration described in docs/data-contract.md."
        )
    return value


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class AccessRoute(StrEnum):
    """访问路径。一个数据源可以暴露多条访问路径，传输协议是独立的实现细节。"""

    S3 = "s3"
    HTTPS = "https"
    GLOBUS = "globus"
    ZARR = "zarr"
    CDS_API = "cds_api"
    LOCAL = "local"


class Priority(StrEnum):
    """任务优先级。调度器按该顺序出队。"""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        """数值越大越优先。"""
        return {"low": 0, "normal": 1, "high": 2, "critical": 3}[self.value]


class TransferStatus(StrEnum):
    """传输层的三态结果。半成功必须有明确语义，不能混杂。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class IntegrityStatus(StrEnum):
    """校验结论。``UNKNOWN`` 不是通过，不能被当成 COMPLETED。"""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class IntegrityCheckKind(StrEnum):
    """可执行的完整性检查种类。"""

    SIZE_MATCH = "size_match"
    CHECKSUM_MATCH = "checksum_match"
    FORMAT_SNIFF = "format_sniff"
    NETCDF_OPENABLE = "netcdf_openable"
    VARIABLE_PRESENT = "variable_present"
    TIME_COVERAGE = "time_coverage"
    METADATA_CONSISTENT = "metadata_consistent"


class VerticalLevelKind(StrEnum):
    """垂直维度的描述方式。不同产品用不同坐标，不能只存一个数字列表。"""

    PRESSURE = "pressure"  # hPa，数值从大到小
    MODEL_LEVEL = "model_level"
    HEIGHT = "height"  # m
    NONE = "none"  # 单层/地表数据


# --------------------------------------------------------------------------- #
# 基础结构
# --------------------------------------------------------------------------- #
class _Contract(BaseModel):
    """所有契约模型的公共配置。

    * ``frozen=True``：契约对象不可变，状态变化只能通过显式构造新对象完成。
    * ``extra="forbid"``：未知字段立即失败，防止上游悄悄塞数据源专属字段。
    * ``validate_assignment`` 与 ``frozen`` 互斥，因此不启用；不可变性已覆盖该需求。
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        use_enum_values=False,
    )

    schema_version: Literal["1.0"] = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _check_schema_version(value)

    def to_json_dict(self) -> dict[str, object]:
        """稳定的 JSON 友好字典（``mode="json"``），用于落库与元数据侧车文件。"""
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        """内容指纹（sha256，取前 16 位 hex），用于去重与审计，不用于科学等价判断。"""
        payload = self.model_dump_json().encode("utf-8")
        return "sha256-16:" + hashlib.sha256(payload).hexdigest()[:16]


class SpatialExtent(_Contract):
    """经纬度包围盒（WGS84）。ERA5 全球为 ``-180..180 / -90..90``。"""

    west: float = Field(ge=-180.0, le=180.0, description="西边界经度（度）")
    east: float = Field(ge=-180.0, le=180.0, description="东边界经度（度）")
    south: float = Field(ge=-90.0, le=90.0, description="南边界纬度（度）")
    north: float = Field(ge=-90.0, le=90.0, description="北边界纬度（度）")
    grid_degrees: float | None = Field(default=None, gt=0, le=10, description="水平分辨率（度）")

    @model_validator(mode="after")
    def _check_order(self) -> SpatialExtent:
        if self.west >= self.east:
            raise ValueError("west must be < east")
        if self.south >= self.north:
            raise ValueError("south must be < north")
        return self

    @property
    def is_global(self) -> bool:
        """是否近似全球覆盖（经度跨 360、纬度跨 180）。"""
        return (self.east - self.west) >= 359.9 and (self.north - self.south) >= 179.9


class TemporalExtent(_Contract):
    """闭开区间 ``[start, end)``，要求 tz-aware。科研数据的时间定义必须显式。"""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _check_order(self) -> TemporalExtent:
        if self.start >= self.end:
            raise ValueError("temporal start must be earlier than end")
        return self

    def duration_days(self) -> float:
        """区间长度（天），用于计划粒度与体积估算。"""
        return (self.end - self.start).total_seconds() / 86400.0


class VerticalLevels(_Contract):
    """垂直层定义。``kind=pressure`` 时 ``levels`` 以 hPa 表示且必须降序。"""

    kind: VerticalLevelKind
    levels: list[float] = Field(default_factory=list, max_length=512)
    units: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _check_levels(self) -> VerticalLevels:
        if self.kind is VerticalLevelKind.NONE:
            if self.levels:
                raise ValueError("kind='none' must not carry levels")
            return self
        if not self.levels:
            raise ValueError(f"kind={self.kind.value!r} requires a non-empty levels list")
        if any(level <= 0 for level in self.levels):
            raise ValueError("vertical levels must be positive")
        if self.kind is VerticalLevelKind.PRESSURE:
            if any(a <= b for a, b in zip(self.levels, self.levels[1:], strict=False)):
                raise ValueError("pressure levels must be strictly decreasing (hPa)")
            if self.units not in (None, "hPa"):
                raise ValueError("pressure levels must use units 'hPa'")
        return self


class VariableSpec(_Contract):
    """单个科学变量的描述。单位是科学等价判定的必要条件，不是装饰。"""

    name: VariableName
    units: NonEmptyStr
    long_name: str | None = Field(default=None, max_length=256)
    standard_name: str | None = Field(default=None, max_length=256)
    cell_methods: str | None = Field(default=None, max_length=256)


class GridSpec(_Contract):
    """水平网格：分辨率 + 投影/重网格化状态。``regridded=True`` 时不可与原生数据判等。"""

    resolution_degrees: float | None = Field(default=None, gt=0, le=10)
    projection: str = Field(default="regular_latlon", max_length=64)
    regridded: bool = False
    source_grid: str | None = Field(default=None, max_length=64)


class Checksum(_Contract):
    """``"<algo>:<hex>"`` 形式的校验和。算法与值绑定在一起，避免“只有一个 hex”的歧义。"""

    value: Annotated[str, StringConstraints(pattern=_CHECKSUM_PATTERN.pattern)]

    @property
    def algorithm(self) -> str:
        """校验算法名（``sha256`` / ``md5`` / ``crc32`` / ``sha256-16``）。"""
        return self.value.split(":", 1)[0]

    @property
    def hexdigest(self) -> str:
        """十六进制摘要部分（不含算法前缀）。"""
        return self.value.split(":", 1)[1]

    @classmethod
    def of_file(cls, path: str) -> Checksum:  # pragma: no cover - 需要真实文件
        """计算文件的 sha256（M1 的完整性检查会复用；M0 仅提供契约）。"""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return cls(value=f"sha256:{digest.hexdigest()}")


# --------------------------------------------------------------------------- #
# 科学元数据
# --------------------------------------------------------------------------- #
class DatasetMetadata(_Contract):
    """一个数据集的科学身份。**同名变量 ≠ 等价数据**，判定必须看这里的所有维度。"""

    product: NonEmptyStr = Field(description="产品族，如 'ERA5' / 'CMIP6/CMIP'")
    product_type: NonEmptyStr = Field(description="产品类型，如 'reanalysis' / 'model-output'")
    variables: list[VariableSpec] = Field(min_length=1)
    temporal_coverage: TemporalExtent
    spatial_extent: SpatialExtent
    vertical_levels: VerticalLevels = Field(
        default_factory=lambda: VerticalLevels(kind=VerticalLevelKind.NONE)
    )
    grid: GridSpec = Field(default_factory=GridSpec)
    time_definition: str = Field(
        default="utc",
        max_length=64,
        description="时间定义/时间步约定，如 'utc' / 'utc_hourly' / 'monthly_mean'",
    )
    data_version: str = Field(default="unknown", max_length=64, description="数据版本/发布批次")
    post_processing: list[str] = Field(default_factory=list, description="已施加的后处理步骤")
    regridded: bool = Field(default=False, description="是否为重网格化产物")
    units_normalized: bool = Field(default=False)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("variables")
    @classmethod
    def _unique_variables(cls, value: list[VariableSpec]) -> list[VariableSpec]:
        names = [spec.name for spec in value]
        if len(set(names)) != len(names):
            raise ValueError("duplicate variable names in metadata")
        return value

    @field_validator("extensions")
    @classmethod
    def _namespaced_extensions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(f"extension key {key!r} must be namespaced, e.g. 'era5.param_id'")
        return value

    def scientific_identity(self) -> tuple[object, ...]:
        """用于科学等价判定的规范化元组。两个数据集判等 ⇔ 该元组相等。"""
        return (
            self.product,
            self.product_type,
            self.data_version,
            self.time_definition,
            tuple(sorted((v.name, v.units) for v in self.variables)),
            (self.temporal_coverage.start, self.temporal_coverage.end),
            (
                self.spatial_extent.west,
                self.spatial_extent.east,
                self.spatial_extent.south,
                self.spatial_extent.north,
                self.spatial_extent.grid_degrees,
            ),
            (self.vertical_levels.kind.value, tuple(self.vertical_levels.levels)),
            (self.grid.resolution_degrees, self.grid.projection, self.grid.regridded),
            tuple(sorted(self.post_processing)),
            self.regridded,
        )


class SourceMetadata(_Contract):
    """数据源侧的元数据：谁提供、走什么路径、该源的命名/版本约定是什么。"""

    source_id: Identifier
    display_name: NonEmptyStr
    provider: NonEmptyStr = Field(description="机构/仓库名，如 'NSF NCAR RDA' / 'Copernicus CDS'")
    access_route: AccessRoute
    dataset_id: NonEmptyStr = Field(description="数据源内部的目录/数据集标识")
    origin_uri: NonEmptyStr = Field(description="发现该资产的原始 URI")
    protocol_version: str | None = Field(default=None, max_length=32)
    license: str | None = Field(default=None, max_length=128)
    citation: str | None = Field(default=None, max_length=2048)
    retrieved_at: datetime
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("retrieved_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("extensions")
    @classmethod
    def _namespaced(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(f"extension key {key!r} must be namespaced")
        return value


# --------------------------------------------------------------------------- #
# 需求 / 资产 / 计划 / 任务
# --------------------------------------------------------------------------- #
class DatasetRequest(_Contract):
    """用户的科学数据需求。这是“我要什么”，不是“哪里有文件”。"""

    request_id: Identifier
    dataset_id: Identifier = Field(description="逻辑数据集键，如 'era5-pressure-levels'")
    metadata: DatasetMetadata
    preferred_sources: list[Identifier] = Field(default_factory=list)
    max_sources: int = Field(default=1, ge=1, le=8)
    checks: list[IntegrityCheckKind] = Field(
        default_factory=lambda: [IntegrityCheckKind.SIZE_MATCH, IntegrityCheckKind.CHECKSUM_MATCH]
    )
    preserve_original: bool = Field(default=True)
    priority: Priority = Priority.NORMAL
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, value: list[IntegrityCheckKind]) -> list[IntegrityCheckKind]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate integrity checks")
        return value

    def required_variables(self) -> frozenset[str]:
        """需求中声明的变量名集合（不含单位，单位在 :class:`VariableSpec` 上）。"""
        return frozenset(v.name for v in self.metadata.variables)


class SourceAsset(_Contract):
    """远端的一个可获取文件资产。它是“某个源上的某个文件”，不是科学数据集本身。"""

    asset_id: Identifier
    source_id: Identifier
    dataset_id: Identifier
    access_route: AccessRoute
    origin_uri: NonEmptyStr
    file_name: NonEmptyStr = Field(max_length=512)
    size_bytes: int = Field(ge=0)
    checksum: Checksum | None = None
    remote_etag: str | None = Field(default=None, max_length=256)
    remote_last_modified: datetime | None = None
    content_type: str | None = Field(default=None, max_length=128)
    time_slice: TemporalExtent | None = Field(
        default=None, description="该文件覆盖的时间片段；大数据集按片段切分"
    )
    level_slice: list[float] | None = Field(default=None, description="该文件覆盖的垂直层")
    metadata: SourceMetadata
    discovered_at: datetime

    @field_validator("remote_last_modified", "discovered_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _consistent_source(self) -> SourceAsset:
        if self.metadata.source_id != self.source_id:
            raise ValueError("metadata.source_id must equal source_id")
        if self.metadata.origin_uri != self.origin_uri:
            raise ValueError("metadata.origin_uri must equal origin_uri")
        return self


class AcquisitionTask(_Contract):
    """一个可调度的采集单元。状态只能通过 :mod:`researchdatahub.core.state` 变更。"""

    task_id: Identifier
    plan_id: Identifier
    request_id: Identifier
    asset: SourceAsset
    state: TaskState = TaskState.PENDING
    priority: Priority = Priority.NORMAL
    attempt: int = Field(default=0, ge=0, le=32)
    max_attempts: int = Field(default=3, ge=1, le=32)
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_transition_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=2048)
    worker_id: str | None = Field(default=None, max_length=64)
    notes: list[str] = Field(default_factory=list)

    @field_validator("scheduled_at", "started_at", "finished_at", "last_transition_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _check_attempts(self) -> AcquisitionTask:
        if self.attempt > self.max_attempts:
            raise ValueError("attempt must be <= max_attempts")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        return self


class AcquisitionPlan(_Contract):
    """计划：把需求映射到若干任务。计划本身不可变，执行状态在任务上。"""

    plan_id: Identifier
    request_id: Identifier
    policy: str = Field(default="default", max_length=64)
    tasks: list[AcquisitionTask] = Field(default_factory=list)
    unmatched_variables: list[str] = Field(default_factory=list)
    generated_at: datetime
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("generated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _unique_tasks(self) -> AcquisitionPlan:
        ids = [task.task_id for task in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task_id in plan")
        for task in self.tasks:
            if task.plan_id != self.plan_id:
                raise ValueError(f"task {task.task_id} belongs to plan {task.plan_id}")
        return self

    def task_ids(self) -> list[str]:
        """计划内所有任务 ID（保持声明顺序）。"""
        return [task.task_id for task in self.tasks]


# --------------------------------------------------------------------------- #
# 传输结果 / 文件记录 / 校验报告 / 交付清单
# --------------------------------------------------------------------------- #
class TransferResult(_Contract):
    """一次传输尝试的结果。失败也必须留下结构化记录，不能只抛异常。"""

    task_id: Identifier
    status: TransferStatus
    transport_id: str = Field(max_length=64, description="实际使用的传输适配器 ID")
    bytes_transferred: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    destination_path: str | None = Field(default=None, max_length=4096)
    checksum: Checksum | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_detail: str | None = Field(default=None, max_length=2048)
    retries_used: int = Field(default=0, ge=0)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _status_fields(self) -> TransferResult:
        if self.status is TransferStatus.SUCCEEDED and self.destination_path is None:
            raise ValueError("succeeded transfer requires destination_path")
        if self.status is TransferStatus.FAILED and self.error_code is None:
            raise ValueError("failed transfer requires error_code")
        return self


class FileRecord(_Contract):
    """本地文件的事实记录：位置、大小、校验和、来源追踪。"""

    file_id: Identifier
    task_id: Identifier
    dataset_id: Identifier
    source_id: Identifier
    access_route: AccessRoute
    origin_uri: NonEmptyStr
    local_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0)
    checksum: Checksum
    content_type: str | None = Field(default=None, max_length=128)
    remote_etag: str | None = Field(default=None, max_length=256)
    remote_last_modified: datetime | None = None
    retrieved_at: datetime
    is_read_only: bool = True
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("retrieved_at", "remote_last_modified")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class IntegrityReport(_Contract):
    """完整性校验结果。``UNKNOWN`` 不等同通过；只有 ``PASSED`` 允许进入 COMPLETED。"""

    report_id: Identifier
    task_id: Identifier
    file_id: Identifier | None = None
    status: IntegrityStatus
    checks: list[IntegrityCheckKind] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    expected: dict[str, JsonValue] = Field(default_factory=dict)
    observed: dict[str, JsonValue] = Field(default_factory=dict)
    checked_at: datetime
    checker_id: str = Field(default="default", max_length=64)

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> IntegrityReport:
        if self.status is IntegrityStatus.PASSED and self.failures:
            raise ValueError("passed report must not contain failures")
        if self.status is IntegrityStatus.FAILED and not self.failures:
            raise ValueError("failed report must list at least one failure")
        return self

    @property
    def permits_completion(self) -> bool:
        """是否允许把任务标记为完成。语义集中在此处，避免各处重复判断。"""
        return self.status is IntegrityStatus.PASSED


class DeliveryManifest(_Contract):
    """交付清单：一次采集结束后，交付给用户的完整、自证的一批文件。

    与 SQLite ``Manifest``（运行时状态库）不同：这是**面向交付的可移植产物**，
    可以复制给别人、写进论文附录、用于复现。
    """

    delivery_id: Identifier
    request_id: Identifier
    plan_id: Identifier
    dataset_metadata: DatasetMetadata
    files: list[FileRecord] = Field(min_length=1)
    integrity: list[IntegrityReport] = Field(default_factory=list)
    sources: list[SourceMetadata] = Field(default_factory=list)
    created_at: datetime
    tool_version: str = Field(max_length=32)
    total_bytes: int = Field(default=0, ge=0)
    complete: bool = Field(default=False)
    notes: str | None = Field(default=None, max_length=4096)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _check_completeness(self) -> DeliveryManifest:
        computed = sum(record.size_bytes for record in self.files)
        if self.total_bytes not in (0, computed):
            raise ValueError(f"total_bytes {self.total_bytes} != sum of files {computed}")
        failed = [r for r in self.integrity if not r.permits_completion]
        if self.complete and failed:
            raise ValueError("delivery cannot be complete while integrity reports are not PASSED")
        if self.complete and self.total_bytes == 0:
            raise ValueError("complete delivery must have a non-zero total_bytes")
        return self

    def with_totals(self) -> DeliveryManifest:
        """返回补齐 ``total_bytes`` 的副本，避免调用方手算。"""
        return self.model_copy(update={"total_bytes": sum(f.size_bytes for f in self.files)})
