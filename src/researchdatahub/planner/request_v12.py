"""版本化数据请求契约 v1.2（见 ADR-0012）。

与既有加载器 ``planner.request`` 的分工
----------------------------------------
* :mod:`researchdatahub.planner.request` 加载**既有 YAML 格式**，并做 ``文件:行`` 级错误定位；
  它服务 CLI 与既有 979 个测试，保持向后兼容，**本模块不替换它**。
* 本模块定义**面向 Agent 的版本化契约**（``schema_version: "1.2"``），把三条最容易静默出错的
  语义**显式化、机读化**：

  1. **坐标顺序**：``spatial.bounds`` 固定为 ``[west, south, east, north]``。
     这是最危险的坑——CDS 用 ``[north, west, south, east]``，两者顺序完全不同，
     翻译时必须显式转序（:func:`to_cds_area` 提供唯一正确实现）。
  2. **日期边界**：闭开区间 ``[start, end)``；``end`` 为纯日期时解释为次日 UTC 00:00，
     因此 ``2015-01-01``..``2024-12-31`` 包含 2024-12-31 全天。
  3. **时区**：只接受 UTC；带偏移的时间规整为 UTC；显式写非 UTC 时区即拒绝。

  另外新增**语义变量名 → Provider 原生变量 ID** 的显式映射槽（``provider_native``）：
  统一变量名只是语义标识，绝不等于平台变量 ID（指令 §10）。

两者最终产出同一个执行契约对象 :class:`~researchdatahub.core.models.DatasetRequest`，
转换入口是 :meth:`AgentDataRequestV12.to_dataset_request`（有 round-trip 测试）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import (
    DatasetMetadata,
    DatasetRequest,
    GridSpec,
    IntegrityCheckKind,
    JsonValue,
    Priority,
    SpatialExtent,
    TemporalExtent,
    VariableSpec,
    VerticalLevelKind,
    VerticalLevels,
)

__all__ = [
    "REQUEST_SCHEMA_VERSION",
    "AgentDataRequestV12",
    "DatasetRef",
    "DatasetRefError",
    "ExecutionSpec",
    "Frequency",
    "OutputSpec",
    "SelectionSpec",
    "SpatialSpec",
    "SpatialSpecError",
    "TemporalSpec",
    "TemporalSpecError",
    "VariableSelector",
    "VerticalSpec",
    "describe_contract",
    "parse_agent_request",
    "to_cds_area",
]

REQUEST_SCHEMA_VERSION: Final[str] = "1.2"
"""当前请求契约版本。``AgentDataRequestV12.schema_version`` 必须等于它。"""

COORDINATE_ORDER: Final[tuple[str, ...]] = ("west", "south", "east", "north")
"""``spatial.bounds`` 的固定顺序：西、南、东、北。机器可读，便于测试与文档交叉引用。"""

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_VARIABLE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RequestContractError(RdhError):
    """请求契约 v1.2 的校验失败（结构化，定位到字段路径）。"""

    default_code = "RDH_REQUEST_CONTRACT"


class SpatialSpecError(RequestContractError):
    """空间范围不合契约。"""

    default_code = "RDH_SPATIAL"


class TemporalSpecError(RequestContractError):
    """时间范围/频率不合契约。"""

    default_code = "RDH_TEMPORAL"


class DatasetRefError(RequestContractError):
    """数据集引用不合契约。"""

    default_code = "RDH_DATASET_REF"


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class Frequency(StrEnum):
    """时间分辨率语义。枚举而非自由字符串，避免 ``hour``/``hourly``/``1h`` 三种写法混用。"""

    HOURLY = "hourly"
    THREE_HOURLY = "3_hourly"
    SIX_HOURLY = "6_hourly"
    DAILY = "daily"
    MONTHLY = "monthly"

    @property
    def step(self) -> timedelta:
        """一个时间步的长度。"""
        return {
            Frequency.HOURLY: timedelta(hours=1),
            Frequency.THREE_HOURLY: timedelta(hours=3),
            Frequency.SIX_HOURLY: timedelta(hours=6),
            Frequency.DAILY: timedelta(days=1),
            Frequency.MONTHLY: timedelta(days=28),  # 最短月，用于"区间是否至少一个步长"的下界判断
        }[self]

    @property
    def time_definition(self) -> str:
        """映射到 ``DatasetMetadata.time_definition`` 的规范化取值。"""
        return f"utc_{self.value}" if self is not Frequency.MONTHLY else "utc_monthly_mean"


class EndSemantics(StrEnum):
    """``temporal.end`` 的解释规则。只有一种被支持，显式写明以免歧义。"""

    EXCLUSIVE_NEXT_DAY = "exclusive_next_day"
    """纯日期 ``end`` 表示次日 00:00 UTC（闭开区间），即 end 当天**包含**在内。"""


class DestinationKind(StrEnum):
    """数据落地位置语义。"""

    LOCAL = "local"
    REMOTE_WORKER = "remote_worker"
    OBJECT_STORE = "object_store"


class ExecutionMode(StrEnum):
    """获取方式语义。"""

    AUTOMATIC = "automatic"
    PREVIEW_ONLY = "preview_only"
    """只生成计划、不做任何外部提交（不消耗配额、不产生副作用）。"""


# --------------------------------------------------------------------------- #
# 子结构
# --------------------------------------------------------------------------- #
class _Strict(BaseModel):
    """契约模型公共配置：不可变、拒绝未知字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class DatasetRef(_Strict):
    """要获取哪个数据集。``auto`` / ``auto_discover`` 表示由发现阶段决定。"""

    provider: str = Field(default="auto", max_length=64)
    dataset_id: str = Field(default="auto_discover", max_length=64)
    product_family: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        text = value.strip().lower()
        if text in {"auto", "auto_discover"}:
            return "auto"
        if not _IDENTIFIER.match(text):
            raise DatasetRefError(
                f"provider {value!r} must be an identifier or 'auto'",
                detail={"field": "dataset.provider"},
            )
        return text

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id(cls, value: str) -> str:
        text = value.strip().lower()
        if text in {"auto", "auto_discover"}:
            return "auto_discover"
        if not _IDENTIFIER.match(text):
            raise DatasetRefError(
                f"dataset_id {value!r} must be an identifier or 'auto_discover'",
                detail={"field": "dataset.dataset_id"},
            )
        return text

    @property
    def needs_discovery(self) -> bool:
        """是否需要在发现阶段解析出具体 Provider/数据集。"""
        return self.provider == "auto" or self.dataset_id == "auto_discover"


class VariableSelector(_Strict):
    """一个变量的**语义**选择。

    ``name`` 是 CF 风格语义名（``cloud_liquid_water``），**不是**平台变量 ID（``clwc``）。
    ``provider_native`` 给出显式映射槽：键是 provider_id，值是平台原生变量 ID；
    缺省时由 Provider 内置映射表负责，映射失败必须报错而不是静默丢弃。
    """

    name: str = Field(min_length=1, max_length=64)
    units: str | None = Field(default=None, max_length=32)
    provider_native: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = value.strip().lower()
        if not _VARIABLE.match(text):
            raise RequestContractError(
                f"variable name {value!r} must be snake_case identifier "
                "(semantic name, not a platform variable id)",
                detail={"field": "selection.variables[].name"},
            )
        return text

    @field_validator("provider_native")
    @classmethod
    def _native(cls, value: dict[str, str]) -> dict[str, str]:
        for key, mapped in value.items():
            if not key.strip() or not mapped.strip():
                raise RequestContractError(
                    "provider_native entries must have non-empty provider and native id",
                    detail={"entry": key},
                )
        return value


class TemporalSpec(_Strict):
    """时间范围与分辨率（ADR-0012：闭开区间、UTC、显式 end 语义）。

    ``end_was_date_only`` 是**由输入形态推导**的私有字段：Pydantic 在字段校验时会把
    ``"2024-12-31"`` 强制转换成 ``datetime``，之后就无法区分"日期"与"当天零点"。
    闭开区间的展开规则依赖这个区别，因此必须在输入阶段就记录下来。
    """

    start: datetime | date | str
    end: datetime | date | str
    end_was_date_only: bool = False
    """``end`` 的输入形态是否为纯日期。Round-trip 时必须保留（否则展开规则会静默改变）。"""
    frequency: Frequency = Frequency.HOURLY
    end_semantics: EndSemantics = EndSemantics.EXCLUSIVE_NEXT_DAY
    time_zone: str = Field(default="UTC", max_length=16)

    @model_validator(mode="before")
    @classmethod
    def _capture_end_form(cls, data: object) -> object:
        """记录 ``end`` 的输入形态（纯日期 / 带时刻），供闭开区间展开使用。"""
        if isinstance(data, Mapping) and "end" in data:
            raw = data["end"]
            merged = dict(data)
            # 显式携带的标记优先（round-trip：序列化后 end 已变成 datetime，不能再靠形态推断）。
            if "end_was_date_only" not in data:
                merged["end_was_date_only"] = _is_date_only(raw)
            return merged
        return data

    @field_validator("start", "end")
    @classmethod
    def _parse(cls, value: datetime | date | str) -> datetime:
        return _parse_utc(value, field="temporal.start/end")

    @field_validator("time_zone")
    @classmethod
    def _utc_only(cls, value: str) -> str:
        text = value.strip()
        if text.upper() not in {"UTC", "Z", "+00:00", "UTC+0"}:
            raise TemporalSpecError(
                f"time_zone {value!r} is not supported: only UTC is accepted "
                "(local time zones silently shift cross-dataset joins)",
                detail={"field": "selection.temporal.time_zone"},
            )
        return "UTC"

    @model_validator(mode="after")
    def _order_and_frequency(self) -> TemporalSpec:
        start = _as_datetime(self.start)
        end = _as_datetime(self.end)
        if start >= end:
            raise TemporalSpecError(
                "temporal.start must be earlier than temporal.end "
                "(the interval is half-open [start, end))",
                detail={"start": start.isoformat(), "end": end.isoformat()},
            )
        if end - start < self.frequency.step:
            raise TemporalSpecError(
                f"requested interval {start.date()}..{end.date()} is shorter than one "
                f"{self.frequency.value} step",
                detail={"frequency": self.frequency.value},
            )
        return self

    @property
    def start_at(self) -> datetime:
        """``start`` 的 UTC ``datetime``。"""
        return _as_datetime(self.start)

    @property
    def end_at(self) -> datetime:
        """``end`` 的 UTC ``datetime``（闭开区间右端）。

        ``end_semantics=exclusive_next_day`` 且 ``end`` 是**纯日期**时展开为**次日 00:00 UTC**，
        因此 ``start=2015-01-01, end=2024-12-31`` 覆盖到 2024-12-31 全天 23:59:59。
        若 ``end`` 自带时刻（例如 ``2024-12-31T06:00Z``），则按给定的瞬间解释，不再展开。
        """
        resolved = _as_datetime(self.end)
        if self.end_semantics is EndSemantics.EXCLUSIVE_NEXT_DAY and self.end_was_date_only:
            return resolved + timedelta(days=1)
        return resolved


class SpatialSpec(_Strict):
    """空间范围：``bounds`` 固定顺序 ``[west, south, east, north]``。"""

    bounds: tuple[float, float, float, float]
    type: str = Field(default="bounding_box", max_length=32)
    grid_degrees: float | None = Field(default=None, gt=0, le=10)

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in {"bounding_box", "bbox", "global"}:
            raise SpatialSpecError(
                f"spatial.type {value!r} is not supported (bounding_box | global)",
                detail={"field": "selection.spatial.type"},
            )
        return "bounding_box" if text in {"bounding_box", "bbox"} else "global"

    @field_validator("bounds")
    @classmethod
    def _bounds(cls, value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        west, south, east, north = value
        if not (-180.0 <= west < east <= 180.0):
            raise SpatialSpecError(
                "bounds must be [west, south, east, north] with -180 <= west < east <= 180",
                detail={"bounds": list(value)},
            )
        if not (-90.0 <= south < north <= 90.0):
            raise SpatialSpecError(
                "bounds must be [west, south, east, north] with -90 <= south < north <= 90",
                detail={"bounds": list(value)},
            )
        return value

    @field_validator("type")
    @classmethod
    def _global_bounds(cls, value: str, info: ValidationInfo) -> str:
        """``type=global`` 必须配全球包围盒（在 type 字段上校验，保证校验一定执行）。"""
        if value != "global":
            return value
        bounds = info.data.get("bounds")
        expected = (-180.0, -90.0, 180.0, 90.0)
        if bounds is not None and tuple(bounds) != expected:
            raise SpatialSpecError(
                "spatial.type=global requires bounds [-180, -90, 180, 90]",
                detail={"bounds": list(bounds)},
            )
        return value

    @property
    def extent(self) -> SpatialExtent:
        """转换为执行契约对象。"""
        west, south, east, north = self.bounds
        return SpatialExtent(
            west=west, east=east, south=south, north=north, grid_degrees=self.grid_degrees
        )


class VerticalSpec(_Strict):
    """垂直维度；``kind=none`` 表示单层/地表。"""

    kind: VerticalLevelKind = VerticalLevelKind.NONE
    levels: tuple[float, ...] = ()
    units: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _validate(self) -> VerticalSpec:
        try:
            if self.kind is VerticalLevelKind.NONE:
                if self.levels:
                    raise SpatialSpecError("vertical.kind=none must not carry levels")
                return self
            # 复用执行契约的校验（气压层必须降序、必须为正）
            VerticalLevels(kind=self.kind, levels=list(self.levels), units=self.units)
        except ValidationError as exc:
            raise RequestContractError(
                "invalid vertical spec",
                detail={"errors": exc.errors(include_url=False)},
            ) from exc
        return self

    def to_levels(self) -> VerticalLevels:
        """转换为执行契约对象。"""
        return VerticalLevels(kind=self.kind, levels=list(self.levels), units=self.units)


class SelectionSpec(_Strict):
    """变量 / 时间 / 空间 / 垂直选择。"""

    variables: tuple[VariableSelector, ...]
    temporal: TemporalSpec
    spatial: SpatialSpec
    vertical: VerticalSpec = Field(default_factory=VerticalSpec)

    @field_validator("variables")
    @classmethod
    def _unique(cls, value: tuple[VariableSelector, ...]) -> tuple[VariableSelector, ...]:
        if not value:
            raise RequestContractError(
                "selection.variables must not be empty",
                detail={"field": "selection.variables"},
            )
        names = [item.name for item in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise RequestContractError(
                "duplicate variables in selection",
                detail={"duplicates": sorted(duplicates)},
            )
        return value

    @property
    def variable_names(self) -> tuple[str, ...]:
        """语义变量名列表（保持声明顺序）。"""
        return tuple(item.name for item in self.variables)


class OutputSpec(_Strict):
    """交付格式与落地位置。``format`` 只声明期望，**不承诺转换**。"""

    format: str = Field(default="netcdf", max_length=32)
    destination: DestinationKind = DestinationKind.LOCAL
    path: str = Field(default="./data/raw", min_length=1, max_length=4096)

    @field_validator("format")
    @classmethod
    def _format(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in {"netcdf", "netcdf4", "grib", "zarr", "csv", "geotiff", "tiff"}:
            raise RequestContractError(
                f"output.format {value!r} is not supported in schema {REQUEST_SCHEMA_VERSION}",
                detail={"supported": ["netcdf", "grib", "zarr", "csv", "geotiff"]},
            )
        return "netcdf" if text == "netcdf4" else text

    @field_validator("path")
    @classmethod
    def _no_parent_escape(cls, value: str) -> str:
        text = value.strip()
        if "\x00" in text:
            raise RequestContractError("output.path must not contain NUL bytes")
        return text


class ExecutionSpec(_Strict):
    """执行方式与重试预算。"""

    mode: ExecutionMode = ExecutionMode.AUTOMATIC
    max_retries: int = Field(default=3, ge=0, le=32)


class ValidationSpec(_Strict):
    """交付前校验项。默认全开；关掉必须显式写。"""

    checksum: bool = True
    metadata: bool = True
    coverage: bool = True

    def to_checks(self) -> list[IntegrityCheckKind]:
        """映射为执行契约的完整性检查列表（保持固定顺序，便于比对）。"""
        checks: list[IntegrityCheckKind] = [IntegrityCheckKind.SIZE_MATCH]
        if self.checksum:
            checks.append(IntegrityCheckKind.CHECKSUM_MATCH)
        if self.metadata:
            checks.extend([IntegrityCheckKind.FORMAT_SNIFF, IntegrityCheckKind.METADATA_CONSISTENT])
        if self.coverage:
            checks.extend([IntegrityCheckKind.VARIABLE_PRESENT, IntegrityCheckKind.TIME_COVERAGE])
        return checks


# --------------------------------------------------------------------------- #
# 顶层
# --------------------------------------------------------------------------- #
class AgentDataRequestV12(_Strict):
    """面向 Agent 的版本化数据请求（``schema_version: "1.2"``）。"""

    schema_version: str = Field(default=REQUEST_SCHEMA_VERSION)
    request_id: str | None = Field(default=None, max_length=64)
    dataset: DatasetRef = Field(default_factory=DatasetRef)
    selection: SelectionSpec
    output: OutputSpec = Field(default_factory=OutputSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != REQUEST_SCHEMA_VERSION:
            raise RequestContractError(
                f"unsupported request schema_version {value!r}; this build reads "
                f"{REQUEST_SCHEMA_VERSION!r}",
                detail={"supported": REQUEST_SCHEMA_VERSION},
            )
        return value

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if not _IDENTIFIER.match(text):
            raise RequestContractError(
                f"request_id {value!r} must match ^[a-z0-9][a-z0-9._-]{{1,62}}$",
                detail={"field": "request_id"},
            )
        return text

    # -- 转换到执行契约 --------------------------------------------------- #
    def to_dataset_request(
        self,
        *,
        resolved_provider: str | None = None,
        resolved_dataset_id: str | None = None,
        product: str | None = None,
        product_type: str = "reanalysis",
        data_version: str = "unknown",
        path: str | None = None,
        priority: str | None = None,
    ) -> DatasetRequest:
        """转换为执行契约 :class:`~researchdatahub.core.models.DatasetRequest`。

        Parameters
        ----------
        resolved_provider, resolved_dataset_id:
            发现阶段的解析结果。契约里是 ``auto``/``auto_discover`` 时必须提供，
            否则抛 :class:`DatasetRefError`（**不允许**带着 ``auto`` 进入执行层）。
        product, product_type, data_version:
            数据集身份，缺省取契约里的 ``dataset.product_family``；
            ``product`` 仍未确定时抛错（执行契约要求非空）。
        path:
            落地路径覆盖（缺省取 ``output.path``）。
        priority:
            优先级字符串（``critical``/``high``/``normal``/``low``）。

        Returns
        -------
        DatasetRequest
            可直接交给 planner 的执行契约对象。

        Raises
        ------
        DatasetRefError
            数据集身份未解析，或 ``auto`` 未被替换。
        """
        provider = resolved_provider or (
            None if self.dataset.provider == "auto" else self.dataset.provider
        )
        dataset_id = resolved_dataset_id or (
            None if self.dataset.dataset_id == "auto_discover" else self.dataset.dataset_id
        )
        if provider is None or dataset_id is None:
            raise DatasetRefError(
                "dataset identity is unresolved: run discovery first and pass "
                "resolved_provider / resolved_dataset_id",
                detail={
                    "provider": self.dataset.provider,
                    "dataset_id": self.dataset.dataset_id,
                },
            )
        resolved_product = product or self.dataset.product_family or provider
        if not resolved_product:
            raise DatasetRefError(
                "dataset.product_family (or product=) is required to build the execution contract",
                detail={"field": "dataset.product_family"},
            )
        specs = [
            VariableSpec(
                name=item.name,
                units=item.units or "unknown",
                standard_name=None,
            )
            for item in self.selection.variables
        ]
        metadata = DatasetMetadata(
            product=resolved_product,
            product_type=product_type,
            variables=specs,
            temporal_coverage=TemporalExtent(
                start=self.selection.temporal.start_at,
                end=self.selection.temporal.end_at,
            ),
            spatial_extent=self.selection.spatial.extent,
            vertical_levels=self.selection.vertical.to_levels(),
            grid=GridSpec(
                resolution_degrees=self.selection.spatial.grid_degrees,
                projection="regular_latlon",
            ),
            time_definition=self.selection.temporal.frequency.time_definition,
            data_version=self.dataset.version or data_version,
            extensions={
                "rdh.contract_version": REQUEST_SCHEMA_VERSION,
                "rdh.frequency": self.selection.temporal.frequency.value,
                "rdh.end_semantics": self.selection.temporal.end_semantics.value,
                "rdh.coordinate_order": ",".join(COORDINATE_ORDER),
                "rdh.output_format": self.output.format,
                "rdh.destination": self.output.destination.value,
                "rdh.output_path": path or self.output.path,
                "rdh.provider_native": _native_mapping(self.selection.variables),
            },
        )
        request_id = self.request_id or _synthesise_request_id(provider, dataset_id)
        return DatasetRequest(
            request_id=request_id,
            dataset_id=dataset_id,
            metadata=metadata,
            preferred_sources=[provider] if provider else [],
            checks=self.validation.to_checks(),
            priority=_priority(priority),
        )


def _native_mapping(variables: Sequence[VariableSelector]) -> JsonValue:
    """把 ``provider_native`` 槽位压平成扩展字段（JSON 友好的嵌套 dict）。"""
    out: dict[str, JsonValue] = {}
    for item in variables:
        out[item.name] = dict(item.provider_native.items())
    return out


def _synthesise_request_id(provider: str, dataset_id: str) -> str:
    """由 provider + dataset 派生稳定的 request_id（无需外部随机源，可复现）。"""
    raw = f"{provider}-{dataset_id}".replace("_", "-").lower()
    cleaned = re.sub(r"[^a-z0-9.-]+", "-", raw).strip("-")
    if len(cleaned) < 2:
        cleaned = "req-auto"
    return cleaned[:63]


def _priority(value: str | None) -> Priority:
    """把优先级字符串交给执行契约做校验（非法值由 Pydantic 报错）。"""
    return Priority(value) if value is not None else Priority.NORMAL


def _is_date_only(value: datetime | date | str) -> bool:
    """判断原始输入是否只是"日期"（无时刻）。``datetime``/``date`` 对象直接按类型判断。"""
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    text = value.strip()
    return len(text) == 10 and text.count("-") == 2


def _as_datetime(value: datetime | date | str) -> datetime:
    """把已解析的时间取回 UTC ``datetime``（Pydantic 校验后会存 datetime）。"""
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return _parse_utc(value, field="temporal")


def _parse_utc(value: datetime | date | str, *, field: str) -> datetime:
    """把日期/时间/字符串解析为 UTC ``datetime``。

    规则（ADR-0012）：

    * 纯日期 → 当日 **00:00 UTC**；对于 ``end``，由 :class:`TemporalSpec` 调用方在语义上
      解释为"次日 00:00"，这里统一返回当日 00:00 再在构造执行契约时展开——
      展开逻辑见 :meth:`TemporalSpec.end_at`。
    * 带 ``Z``/偏移的 ISO 时间 → 规整为 UTC。
    * 无时区的日期时间（``2024-01-01T12:00``）→ **拒绝**，不猜时区。

    Raises
    ------
    TemporalSpecError
        无法解析或缺少时区。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TemporalSpecError(
                f"{field}: naive datetime is ambiguous; attach an offset or use a date",
                detail={"value": value.isoformat()},
            )
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = value.strip()
    if not text:
        raise TemporalSpecError(f"{field}: empty value", detail={"value": value})
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise TemporalSpecError(
            f"{field}: cannot parse {value!r} as ISO 8601 date or datetime",
            detail={"value": value},
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 只有"纯日期"允许无时区（按 UTC 零点解释）；带时刻的必须显式给时区。
        is_date_only = len(text) == 10 and text.count("-") == 2
        if not is_date_only:
            raise TemporalSpecError(
                f"{field}: naive datetime {value!r} is ambiguous; attach an offset",
                detail={"value": value},
            )
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_cds_area(spatial: SpatialSpec) -> tuple[float, float, float, float]:
    """把契约的 ``[west, south, east, north]`` 转成 CDS 的 ``[north, west, south, east]``。

    **这是唯一允许做该转序的地方**：CDS ``area`` 参数顺序是
    ``[North, West, South, East]``，与本契约的 ``[W, S, E, N]`` 完全不同，
    写错会**静默**拿到另一个区域的数据（不会报错）。因此集中在此并强制测试。

    Parameters
    ----------
    spatial:
        契约空间范围。

    Returns
    -------
    tuple[float, float, float, float]
        ``(north, west, south, east)``。
    """
    west, south, east, north = spatial.bounds
    return (north, west, south, east)


def parse_agent_request(payload: Mapping[str, object]) -> AgentDataRequestV12:
    """从 mapping（例如 MCP 入参或 YAML dict）解析契约 v1.2。

    Parameters
    ----------
    payload:
        请求字典。

    Returns
    -------
    AgentDataRequestV12
        校验通过的契约对象。

    Raises
    ------
    RequestContractError
        字段缺失、类型错误或语义违规；``detail["errors"]`` 逐条给出定位。
    """
    try:
        return AgentDataRequestV12.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        raise RequestContractError(
            f"request does not match schema {REQUEST_SCHEMA_VERSION}",
            detail={
                "errors": [
                    {"path": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
                    for err in errors
                ]
            },
        ) from exc


def describe_contract() -> dict[str, object]:
    """返回契约语义摘要（供 MCP / 文档 / 测试交叉引用，避免文档漂移）。"""
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "coordinate_order": list(COORDINATE_ORDER),
        "temporal_interval": "half-open [start, end)",
        "end_semantics": [item.value for item in EndSemantics],
        "time_zone": "UTC",
        "frequency": [item.value for item in Frequency],
        "formats": ["netcdf", "grib", "zarr", "csv", "geotiff"],
        "destinations": [item.value for item in DestinationKind],
        "execution_modes": [item.value for item in ExecutionMode],
    }
