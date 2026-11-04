"""Copernicus CDS 数据源适配器。

远端形态（**2026-11-04 实测**，不是记忆）
----------------------------------------
CDS 不是静态文件目录，而是"提交请求 → 服务端排队生成 → 轮询状态 → 拿临时结果 URL"。
本模块把它建模成两个独立步骤（ADR-0002 第 5 节）：

1. 发现阶段（本模块）：翻译科学需求 → CDS 请求 → 提交 → 轮询 → 取得**临时结果 URL**，
   产出 :class:`~researchdatahub.core.models.SourceAsset`（``access_route=cds_api``）。
2. 传输阶段（M2）：只从 ``SourceAsset.origin_uri`` 取字节；URL 过期抛可重试错误，
   重试前重新走一次发现获取新 URL。

实测的远端端点与参数（本模块常量/解析逻辑的依据）
--------------------------------------------------
* API 根：``https://cds.climate.copernicus.eu/api``（官方文档 how-to-api + 实测可达）
* 进程 schema：``GET {root}/retrieve/v1/processes/{collection}``（实测 200，无需凭证）
  返回 ``inputs``：``product_type`` / ``variable`` / ``year`` / ``month`` / ``day`` /
  ``time`` / ``pressure_level`` / ``area`` / ``data_format`` / ``download_format``，
  其中 ``variable`` 与各离散参数带有真实 enum（变量名为 ``specific_humidity`` 这类长名）。
* 目录元数据：``GET {root}/catalogue/v1/collections/{collection}``（实测 200，无需凭证）
* 异步客户端：``ecmwf-datastores-client``（``cdsapi>=0.7.7`` 的依赖，官方文档
  https://ecmwf.github.io/ecmwf-datastores-client/ 实测列出 ``submit`` /
  ``submit_and_wait_on_results`` / ``get_results`` / ``download_results``）。
  ``Remote.status`` 取值实测源码：``accepted`` / ``running`` / ``successful`` /
  ``failed`` / ``rejected`` / ``dismissed`` / ``deleted``。
* 结果对象：``Results.location``、``Results.content_length``、``Results.content_type``、
  ``Results.asset["file:checksum"]``。

**未验证项**（不得当成事实）
----------------------------
* ``area`` 数组顺序：本实现采用 ``[north, west, south, east]``。**2026-11-04 已用真实
  产物验证**：提交非对称 bbox ``area=[45, 100, 30, 120]`` 的 t2m 请求，返回文件经 xarray
  校验 ``latitude ∈ [30, 45]``、``longitude ∈ [100, 120]``（61×81 格），与 [N,W,S,E] 一致。
  证据：[`docs/M6-efficiency-benchmark.md`](../../../docs/M6-efficiency-benchmark.md) §3.1。
* 结果校验和 ``file:checksum`` 的具体格式：无凭证时看不到结果对象，不能假设它是
  ``sha256:<hex>``；因此仅当它符合核心模型 ``<algo>:<hex>`` 形式时才填
  ``SourceAsset.checksum``，否则原样保留在 extensions 并标记未知。
* 结果下载 URL 的过期时长、以及多文件结果的分包规则：未验证。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Final, Protocol, cast

import httpx

from researchdatahub.config.models import SourceConfig
from researchdatahub.core.errors import (
    MetadataError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import (
    AccessRoute,
    DatasetRequest,
    JsonValue,
    SourceAsset,
    SourceMetadata,
    SpatialExtent,
    TemporalExtent,
    VariableSpec,
    VerticalLevelKind,
    VerticalLevels,
)
from researchdatahub.sources.base import (
    DEFAULT_TIMEOUT_SECONDS,
    AdapterCapabilities,
    SourceAdapter,
    as_utc,
    asset_id_for,
    build_asset,
    build_source_metadata,
    json_safe,
    parse_checksum_or_none,
    require_text,
    utcnow,
)

__all__ = [
    "ADS_API_ROOT",
    "CDS_API_ROOT",
    "DEFAULT_COLLECTIONS",
    "CDSAdapter",
    "CdsJobSnapshot",
    "CdsJobState",
    "CdsProcessSchema",
    "CdsRemoteRef",
    "CdsRequestLifecycle",
    "CdsRequestTranslator",
    "CdsSubmission",
    "CdsTranslation",
    "EcmwfDatastoresBridge",
]

logger = logging.getLogger(__name__)

CDS_API_ROOT: Final[str] = "https://cds.climate.copernicus.eu/api"
"""CDS API 根（官方 how-to-api 文档给出，实测 ``/catalogue/v1`` 与 ``/retrieve/v1`` 可访问）。"""

ADS_API_ROOT: Final[str] = "https://ads.atmosphere.copernicus.eu/api"
"""ADS（Atmosphere Data Store）API 根；实测可达，但 ADS 数据集未在本适配器中验证。"""

DEFAULT_COLLECTIONS: Final[dict[str, str]] = {
    # 实测：GET {CDS_API_ROOT}/catalogue/v1/collections/<id> 均返回 200。
    "era5-pressure-levels": "reanalysis-era5-pressure-levels",
    "era5-single-levels": "reanalysis-era5-single-levels",
}
"""逻辑数据集 ID → CDS collection ID 的**已实测**默认映射（2026-11-04）。"""

_API_VERSION: Final[str] = "retrieve/v1"
_CATALOGUE_VERSION: Final[str] = "catalogue/v1"
_TEMPORAL_GRANULARITY: Final[dict[str, str]] = {
    "utc_hourly": "hourly",
    "hourly": "hourly",
    "utc_daily": "daily",
    "daily": "daily",
    "monthly_mean": "monthly",
    "monthly": "monthly",
}
_AREA_ORDER: Final[tuple[str, ...]] = ("north", "west", "south", "east")
# 2026-11-04 由真实产物反证（非对称 bbox [45,100,30,120] → lat 30..45 / lon 100..120），
# 证据见 docs/M6-efficiency-benchmark.md §3.1。
_AREA_ORDER_VERIFIED: Final[bool] = True
_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_UNPROBED: Final[object] = object()
"""依赖探测尚未执行的哨兵（``None`` 表示已探测且缺失）。"""


# --------------------------------------------------------------------------- #
# 依赖探测（懒加载：cdsapi 缺失时明确 unavailable，不伪造）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CdsDependencies:
    """已安装的 CDS 客户端模块集合；只保存模块对象，不含任何凭证值。"""

    cdsapi: object
    datastores: object


def probe_cds_dependencies() -> CdsDependencies | None:
    """探测 ``cdsapi`` 与 ``ecmwf.datastores`` 是否可导入；缺失返回 ``None``。

    二者是同一个官方客户端栈：``cdsapi>=0.7.7`` 依赖 ``ecmwf-datastores-client``。
    缺失时适配器必须报告 unavailable，而不是回退成"没有文件"。
    """
    try:
        cdsapi_module = importlib.import_module("cdsapi")
        datastores_module = importlib.import_module("ecmwf.datastores")
    except ImportError:
        return None
    return CdsDependencies(cdsapi=cdsapi_module, datastores=datastores_module)


# --------------------------------------------------------------------------- #
# 远端进程 schema
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CdsInput:
    """CDS 进程输入参数（来自 ``retrieve/v1/processes/<collection>`` 的 ``inputs``）。

    ``allowed`` 是远端给出的 enum 白名单；为空表示远端没有枚举，**不能**用它验证取值，
    此时翻译逻辑必须拒绝猜测（见 :meth:`CdsProcessSchema.variable_names`）。
    """

    name: str
    title: str | None
    param_type: str | None
    allowed: tuple[str, ...]

    def allows(self, value: str) -> bool:
        """远端枚举非空时判断取值是否合法；枚举为空时返回 ``False``（不可验证）。"""
        return bool(self.allowed) and value in self.allowed


@dataclass(frozen=True)
class CdsProcessSchema:
    """CDS collection 的进程 schema（真实响应解析结果，保留原始 payload 摘要）。"""

    collection_id: str
    title: str | None
    version: str | None
    inputs: Mapping[str, CdsInput]
    source_url: str
    fetched_at: datetime

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        collection_id: str,
        source_url: str,
        fetched_at: datetime,
    ) -> CdsProcessSchema:
        """解析 ``processes/<collection>`` 响应；形状不符时抛 :class:`MetadataError`。

        Raises
        ------
        MetadataError
            ``inputs`` 缺失或不是对象，或单个参数 schema 不是对象。
        """
        raw_inputs = payload.get("inputs")
        if not isinstance(raw_inputs, Mapping) or not raw_inputs:
            raise MetadataError(
                "CDS process schema has no 'inputs' mapping; refusing to guess request parameters",
                detail={"collection": collection_id, "source_url": source_url},
            )
        inputs: dict[str, CdsInput] = {}
        for name, raw in raw_inputs.items():
            if not isinstance(raw, Mapping):
                raise MetadataError(
                    "CDS process input description is not an object",
                    detail={"collection": collection_id, "input": str(name)},
                )
            schema = raw.get("schema")
            if not isinstance(schema, Mapping):
                schema = {}
            allowed: list[str] = []
            direct_enum = schema.get("enum")
            if isinstance(direct_enum, Sequence) and not isinstance(direct_enum, (str, bytes)):
                allowed.extend(str(item) for item in direct_enum)
            items = schema.get("items")
            if isinstance(items, Mapping):
                item_enum = items.get("enum")
                if isinstance(item_enum, Sequence) and not isinstance(item_enum, (str, bytes)):
                    allowed.extend(str(item) for item in item_enum)
            title = raw.get("title")
            param_type = schema.get("type")
            inputs[str(name)] = CdsInput(
                name=str(name),
                title=str(title) if isinstance(title, str) else None,
                param_type=str(param_type) if isinstance(param_type, str) else None,
                allowed=tuple(allowed),
            )
        return cls(
            collection_id=collection_id,
            title=payload.get("title") if isinstance(payload.get("title"), str) else None,
            version=payload.get("version") if isinstance(payload.get("version"), str) else None,
            inputs=inputs,
            source_url=source_url,
            fetched_at=as_utc(fetched_at, field="fetched_at"),
        )

    def input(self, name: str) -> CdsInput | None:
        """按名字取输入参数描述；不存在返回 ``None``。"""
        return self.inputs.get(name)

    def require(self, name: str) -> CdsInput:
        """按名字取输入参数描述；不存在抛 :class:`MetadataError`。

        Raises
        ------
        MetadataError
            远端 schema 没有该参数，说明请求参数约定与预期不符。
        """
        found = self.inputs.get(name)
        if found is None:
            raise MetadataError(
                f"CDS process schema for {self.collection_id!r} has no input {name!r}",
                detail={"collection": self.collection_id, "available_inputs": sorted(self.inputs)},
            )
        return found

    @property
    def variable_names(self) -> tuple[str, ...]:
        """远端支持的变量长名白名单（可能为空，空表示无法验证）。"""
        return self.require("variable").allowed


# --------------------------------------------------------------------------- #
# 请求翻译
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CdsTranslation:
    """一次"科学需求 → CDS 请求"的完整翻译结果（可审计）。"""

    collection_id: str
    request: Mapping[str, JsonValue]
    schema: CdsProcessSchema
    translated_at: datetime


class CdsRequestTranslator:
    """把 :class:`DatasetRequest` 翻译成 CDS 请求字典。

    翻译原则
    --------
    * 变量名以**远端 schema 的 enum 白名单**为准：用 ``long_name`` / ``standard_name`` /
      ``name`` 规范化后匹配，匹配不上直接报错并列出远端可用变量，绝不猜测。
    * 时间范围按 CDS 的 ``year/month/day/time`` 笛卡尔积语义展开；这是 CDS 请求模型的
      固有近似（区间可能被扩展到一个完整的月/年边界），在本类 docstring 中显式声明。
    * 垂直层只支持 ``kind=pressure`` 的 hPa 值；其它坐标类型没有已验证的 CDS 参数。
    * 输出格式来自 ``request.extensions["cds.data_format"]``、
      ``request.extensions["cds.download_format"]``，默认 ``netcdf`` / ``unarchived``；
      取值必须命中远端 enum。
    """

    area_order: tuple[str, ...] = _AREA_ORDER
    area_order_verified: bool = _AREA_ORDER_VERIFIED

    def __init__(self, schema: CdsProcessSchema, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._schema = schema
        self._clock = clock

    @property
    def schema(self) -> CdsProcessSchema:
        """本次翻译使用的远端 schema。"""
        return self._schema

    def translate(self, request: DatasetRequest) -> CdsTranslation:
        """执行翻译；任何无法用远端 schema 验证的映射都会抛 :class:`MetadataError`。

        Raises
        ------
        MetadataError
            变量无法映射、时间粒度无法表达、垂直层类型不支持或格式取值不在白名单。
        """
        metadata = request.metadata
        translated: dict[str, JsonValue] = {}
        product_type = self._product_type(metadata.product_type)
        if product_type:
            translated["product_type"] = json_safe(list(product_type))
        translated["variable"] = list(self._variables(metadata.variables))
        temporal = self._temporal(metadata.temporal_coverage, metadata.time_definition)
        for name, values in temporal.items():
            translated[name] = json_safe(list(values))
        if metadata.vertical_levels.kind is VerticalLevelKind.PRESSURE:
            translated["pressure_level"] = json_safe(
                list(self._pressure_levels(metadata.vertical_levels))
            )
        elif metadata.vertical_levels.kind is not VerticalLevelKind.NONE:
            raise MetadataError(
                "CDS request translation only supports pressure levels; "
                "other vertical coordinates have no verified CDS parameter",
                detail={"kind": metadata.vertical_levels.kind.value},
            )
        translated["area"] = json_safe(list(self._area(metadata.spatial_extent)))
        translated["data_format"] = self._choice(
            request.extensions, "cds.data_format", "netcdf", "data_format"
        )
        translated["download_format"] = self._choice(
            request.extensions, "cds.download_format", "unarchived", "download_format"
        )
        self._validate_against_schema(translated)
        return CdsTranslation(
            collection_id=self._schema.collection_id,
            request=translated,
            schema=self._schema,
            translated_at=as_utc(self._clock(), field="translated_at"),
        )

    # -- 各维度翻译 -------------------------------------------------------- #
    def _product_type(self, product_type: str) -> list[str]:
        spec = self._schema.input("product_type")
        if spec is None:
            # 远端没有 product_type 参数（例如某些派生数据集）：不发送该键。
            return []
        candidate = product_type.strip().lower()
        if not spec.allows(candidate):
            raise MetadataError(
                "product_type is not supported by the remote CDS dataset",
                detail={
                    "collection": self._schema.collection_id,
                    "product_type": product_type,
                    "available": list(spec.allowed),
                },
            )
        return [candidate]

    def _variables(self, variables: Sequence[VariableSpec]) -> tuple[str, ...]:
        spec = self._schema.require("variable")
        available = spec.allowed
        if not available:
            raise MetadataError(
                "remote CDS dataset does not enumerate variables; refusing to guess long names",
                detail={"collection": self._schema.collection_id},
            )
        resolved: list[str] = []
        for variable in variables:
            candidates = [
                normalize_candidate(value)
                for value in (variable.long_name, variable.standard_name, variable.name)
                if value
            ]
            match = next((candidate for candidate in candidates if candidate in available), None)
            if match is None:
                raise MetadataError(
                    "variable cannot be mapped to a CDS variable name",
                    detail={
                        "collection": self._schema.collection_id,
                        "variable": variable.name,
                        "candidates": candidates,
                        "available": list(available),
                    },
                )
            if match not in resolved:
                resolved.append(match)
        return tuple(resolved)

    def _temporal(self, extent: TemporalExtent, time_definition: str) -> dict[str, list[str]]:
        self._schema.require("year")
        granularity = _TEMPORAL_GRANULARITY.get(time_definition.strip().lower())
        if granularity is None:
            raise MetadataError(
                "cannot determine CDS temporal granularity from time_definition "
                "(no inference is allowed)",
                detail={"time_definition": time_definition},
            )
        if granularity == "hourly":
            self._schema.require("day")
            self._schema.require("time")
        elif granularity == "daily":
            self._schema.require("day")
            if self._schema.input("time") is not None:
                raise MetadataError(
                    "remote CDS dataset requires an explicit 'time' input; "
                    "daily granularity cannot be expressed without guessing hours",
                    detail={"collection": self._schema.collection_id},
                )
        else:  # monthly
            self._schema.require("month")
            if self._schema.input("day") is not None or self._schema.input("time") is not None:
                raise MetadataError(
                    "remote CDS dataset requires day/time inputs; "
                    "monthly granularity cannot be expressed",
                    detail={"collection": self._schema.collection_id},
                )

        start = extent.start
        last = extent.end - timedelta(microseconds=1)
        if last < start:
            raise MetadataError(
                "temporal coverage is empty after applying the [start, end) convention",
                detail={"start": start.isoformat(), "end": extent.end.isoformat()},
            )
        translated: dict[str, list[str]] = {
            "year": [f"{year:04d}" for year in range(start.year, last.year + 1)],
            "month": [f"{month:02d}" for month in _covered_months(start, last)],
        }
        if granularity in ("hourly", "daily"):
            translated["day"] = [f"{day:02d}" for day in _covered_days(start, last)]
        if granularity == "hourly":
            translated["time"] = [f"{hour:02d}:00" for hour in _covered_hours(start, last)]
        # 远端 schema 没有该参数时不要多发（例如某些月度数据集无 day/time）。
        return {name: values for name, values in translated.items() if self._schema.input(name)}

    def _pressure_levels(self, levels: VerticalLevels) -> tuple[str, ...]:
        spec = self._schema.require("pressure_level")
        if not spec.allowed:
            raise MetadataError(
                "remote CDS dataset does not enumerate pressure_level; refusing to guess",
                detail={"collection": self._schema.collection_id},
            )
        translated: list[str] = []
        for level in levels.levels:
            candidate = _format_level(level)
            if candidate not in spec.allowed:
                raise MetadataError(
                    "pressure level is not listed by the remote CDS dataset",
                    detail={
                        "collection": self._schema.collection_id,
                        "level": level,
                        "available": list(spec.allowed),
                    },
                )
            translated.append(candidate)
        return tuple(translated)

    def _area(self, extent: SpatialExtent) -> tuple[float, float, float, float]:
        """按类属性 ``area_order`` 输出 bbox；默认 ``[north, west, south, east]``。"""
        values = {
            "north": extent.north,
            "west": extent.west,
            "south": extent.south,
            "east": extent.east,
        }
        return tuple(values[key] for key in self.area_order)  # type: ignore[return-value]

    def _choice(
        self,
        extensions: Mapping[str, JsonValue],
        key: str,
        default: str,
        input_name: str,
    ) -> str:
        spec = self._schema.require(input_name)
        raw = extensions.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise MetadataError(
                f"request extension {key!r} must be a non-empty string",
                detail={"key": key, "observed": json_safe(raw)},
            )
        candidate = raw.strip()
        if not spec.allows(candidate):
            raise MetadataError(
                f"{key!r} value is not supported by the remote CDS dataset",
                detail={
                    "collection": self._schema.collection_id,
                    "value": candidate,
                    "available": list(spec.allowed),
                },
            )
        return candidate

    def _validate_against_schema(self, translated: Mapping[str, JsonValue]) -> None:
        for name, value in translated.items():
            spec = self._schema.input(name)
            if spec is None:
                raise MetadataError(
                    f"translated parameter {name!r} is not accepted by the remote CDS process",
                    detail={"collection": self._schema.collection_id},
                )
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, (int, float)):
                    continue
                if not spec.allows(str(item)):
                    raise MetadataError(
                        f"translated value {item!r} is not in the remote enum for {name!r}",
                        detail={
                            "collection": self._schema.collection_id,
                            "parameter": name,
                            "available": list(spec.allowed)[:50],
                        },
                    )


def normalize_candidate(value: str) -> str:
    """把变量长名规范化为 CDS enum 风格（小写下划线）。"""
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _format_level(level: float) -> str:
    if float(level).is_integer():
        return str(int(level))
    return f"{level:g}"


def _covered_months(start: datetime, last: datetime) -> list[int]:
    months: list[int] = []
    year, month = start.year, start.month
    while (year, month) <= (last.year, last.month):
        months.append(month)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return sorted(set(months))


def _covered_days(start: datetime, last: datetime) -> list[int]:
    max_day = 0
    for year, month in _year_months(start, last):
        max_day = max(max_day, _days_in_month(year, month))
    return list(range(1, max_day + 1))


def _covered_hours(start: datetime, last: datetime) -> list[int]:
    if start.hour == 0 and (last - start).total_seconds() >= 86_400:
        return list(range(24))
    return list(range(start.hour, last.hour + 1))


def _year_months(start: datetime, last: datetime) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (last.year, last.month):
        pairs.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return pairs


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(days=1)).day


# --------------------------------------------------------------------------- #
# 异步请求生命周期（submit → poll → results）
# --------------------------------------------------------------------------- #
class CdsJobState(StrEnum):
    """CDS 任务状态（取值来自 ``ecmwf.datastores.processing.Remote`` 的实测识别逻辑）。"""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    REJECTED = "rejected"
    DISMISSED = "dismissed"
    DELETED = "deleted"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        """是否为终态（成功或失败）。"""
        return self in {
            CdsJobState.SUCCESSFUL,
            CdsJobState.FAILED,
            CdsJobState.REJECTED,
            CdsJobState.DISMISSED,
            CdsJobState.DELETED,
        }

    @property
    def failed(self) -> bool:
        """是否为失败终态。"""
        return self.terminal and self is not CdsJobState.SUCCESSFUL


def _parse_state(remote_status: str) -> CdsJobState:
    try:
        return CdsJobState(remote_status.strip().lower())
    except ValueError:
        return CdsJobState.UNKNOWN


@dataclass(frozen=True)
class CdsRemoteRef:
    """一个已提交远端任务的客户端句柄（``handle`` 由具体桥接实现解释）。"""

    request_id: str
    handle: object


@dataclass(frozen=True)
class CdsJobSnapshot:
    """某一时刻的任务状态快照；结果 URL/大小只在 ``results`` 阶段出现。"""

    request_id: str
    state: CdsJobState
    remote_status: str
    observed_at: datetime
    location: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    checksum_raw: JsonValue = None
    raw: Mapping[str, JsonValue] = field(default_factory=dict)


class CdsClientBridge(Protocol):
    """CDS 客户端桥接协议：把官方客户端对象收敛成 submit/poll/results 三步。

    实现可以是真实 ``ecmwf-datastores-client`` 桥接，也可以是本地 HTTP 服务测试用的
    同一实现（测试用**真实 HTTP**，不 mock socket 语义）。
    """

    def submit(self, collection_id: str, request: Mapping[str, Any]) -> CdsRemoteRef:
        """提交请求并返回远端句柄。"""
        ...

    def poll(self, remote: CdsRemoteRef) -> CdsJobSnapshot:
        """查询当前状态（不触发结果下载）。"""
        ...

    def results(self, remote: CdsRemoteRef) -> CdsJobSnapshot:
        """取得结果元数据（临时 URL、大小、类型、可用时的校验和）。"""
        ...


class EcmwfDatastoresBridge:
    """用官方 ``ecmwf-datastores-client`` 实现 :class:`CdsClientBridge`。

    ``cdsapi>=0.7.7`` 内部对新 CDS 接口即委托给该包（实测源码），因此这里直接使用它
    以获得明确的 submit/poll/results 生命周期，而不是依赖 ``retrieve()`` 的阻塞行为。
    依赖缺失时抛 :class:`SourceUnavailableError`（懒加载，不在 import 期失败）。
    """

    def __init__(
        self,
        *,
        api_root: str,
        key: str,
        dependencies: CdsDependencies | None = None,
        timeout: float = 60.0,
        maximum_tries: int = 3,
        retry_after: float = 2.0,
        client: object | None = None,
    ) -> None:
        self._api_root = api_root
        self._key = key
        self._dependencies = dependencies
        self._timeout = timeout
        self._maximum_tries = maximum_tries
        self._retry_after = retry_after
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        deps = self._dependencies or probe_cds_dependencies()
        if deps is None:
            raise SourceUnavailableError(
                "CDS client dependencies are not installed "
                "(need cdsapi>=0.7.7 and ecmwf-datastores-client)",
                detail={"adapter": "cds", "missing": ["cdsapi", "ecmwf.datastores"]},
            )
        datastores_module = cast(Any, deps.datastores)
        try:
            self._client = datastores_module.Client(
                url=self._api_root,
                key=self._key,
                timeout=self._timeout,
                progress=False,
                cleanup=False,
                maximum_tries=self._maximum_tries,
                retry_after=self._retry_after,
                sleep_max=min(self._retry_after, 30.0),
            )
        except Exception as exc:  # pragma: no cover - 依赖内部错误形态不稳定
            raise SourceUnavailableError(
                "failed to construct the CDS API client",
                detail={"adapter": "cds", "error_type": type(exc).__name__},
            ) from exc
        return self._client

    def submit(self, collection_id: str, request: Mapping[str, Any]) -> CdsRemoteRef:
        """提交 CDS 请求；失败折算为 :class:`SourceUnavailableError`。"""
        client = self._ensure_client()
        try:
            remote = client.submit(collection_id, dict(request))
            request_id = require_text(str(getattr(remote, "request_id", "")), field="request_id")
        except SourceError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                "CDS request submission failed",
                detail={"adapter": "cds", "error_type": type(exc).__name__},
            ) from exc
        return CdsRemoteRef(request_id=request_id, handle=remote)

    def poll(self, remote: CdsRemoteRef) -> CdsJobSnapshot:
        """查询任务状态；连接/服务端错误折算为 :class:`SourceUnavailableError`。"""
        handle = remote.handle
        try:
            status = str(handle.status)  # type: ignore[attr-defined]
            raw_payload = _safe_mapping(getattr(handle, "json", None))
        except SourceError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                "CDS request status query failed",
                detail={"adapter": "cds", "error_type": type(exc).__name__},
            ) from exc
        return CdsJobSnapshot(
            request_id=remote.request_id,
            state=_parse_state(status),
            remote_status=status,
            observed_at=utcnow(),
            raw={"status": status, **_truncate_payload(raw_payload)},
        )

    def results(self, remote: CdsRemoteRef) -> CdsJobSnapshot:
        """取结果元数据（不下载字节）；失败折算为 :class:`SourceUnavailableError`。"""
        handle = remote.handle
        try:
            results = handle.get_results()  # type: ignore[attr-defined]
            location = str(results.location)
            size = int(results.content_length)
            content_type = str(results.content_type)
            asset = _safe_mapping(getattr(results, "asset", None))
        except SourceError:
            raise
        except Exception as exc:
            raise SourceUnavailableError(
                "CDS results query failed",
                detail={"adapter": "cds", "error_type": type(exc).__name__},
            ) from exc
        checksum = asset.get("file:checksum") if isinstance(asset, Mapping) else None
        return CdsJobSnapshot(
            request_id=remote.request_id,
            state=CdsJobState.SUCCESSFUL,
            remote_status="successful",
            observed_at=utcnow(),
            location=location,
            size_bytes=size,
            content_type=content_type,
            checksum_raw=json_safe(checksum),
            raw=_truncate_payload(asset),
        )


def _safe_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _truncate_payload(payload: Mapping[str, Any], *, max_items: int = 24) -> dict[str, JsonValue]:
    """只保留有诊断价值的字段，避免把整份远端 JSON 塞进资产扩展。"""
    keep = ("request_id", "status", "created", "updated", "links", "asset", "contentType")
    selected = {key: json_safe(payload[key]) for key in keep if key in payload}
    for index, (key, value) in enumerate(payload.items()):
        if index >= max_items:
            break
        selected.setdefault(key, json_safe(value))
    return selected


@dataclass(frozen=True)
class CdsSubmission:
    """一次已提交请求（生命周期起点）。"""

    collection_id: str
    request: Mapping[str, JsonValue]
    remote: CdsRemoteRef
    submitted_at: datetime


class CdsRequestLifecycle:
    """CDS 异步请求生命周期：submit → poll → results。

    该类**只做请求编排**，不碰传输：结果阶段交给 M2 从 ``origin_uri`` 取字节。
    """

    default_timeout_seconds: float = 3600.0

    def __init__(
        self,
        bridge: CdsClientBridge,
        *,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bridge = bridge
        self._clock = clock
        self._sleep = sleep

    def submit(self, collection_id: str, request: Mapping[str, JsonValue]) -> CdsSubmission:
        """提交请求并记录提交时间。"""
        remote = self._bridge.submit(collection_id, dict(request))
        return CdsSubmission(
            collection_id=collection_id,
            request=dict(request),
            remote=remote,
            submitted_at=as_utc(self._clock(), field="submitted_at"),
        )

    def poll(self, submission: CdsSubmission) -> CdsJobSnapshot:
        """查询一次状态（不做等待）。"""
        return self._bridge.poll(submission.remote)

    def wait(
        self,
        submission: CdsSubmission,
        *,
        timeout_seconds: float | None = None,
        initial_interval: float = _POLL_INTERVAL_SECONDS,
        max_interval: float = 60.0,
        max_polls: int = 1000,
    ) -> CdsJobSnapshot:
        """轮询直到成功/失败终态或超时。

        Raises
        ------
        SourceUnavailableError
            超时、未知状态或失败终态；错误信息包含远端状态与错误说明（不含凭证）。
        """
        budget = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        started = self._clock()
        interval = initial_interval
        polls = 0
        while True:
            snapshot = self.poll(submission)
            if snapshot.state is CdsJobState.SUCCESSFUL:
                return snapshot
            if snapshot.state.failed or snapshot.state is CdsJobState.UNKNOWN:
                detail: dict[str, Any] = {
                    "adapter": "cds",
                    "request_id": submission.remote.request_id,
                    "remote_status": snapshot.remote_status,
                }
                if snapshot.raw:
                    detail["remote"] = dict(snapshot.raw)
                raise SourceUnavailableError(
                    f"CDS request ended in state {snapshot.remote_status!r}",
                    detail=detail,
                )
            polls += 1
            elapsed = (as_utc(self._clock(), field="clock") - started).total_seconds()
            if polls >= max_polls or elapsed + interval > budget:
                raise SourceUnavailableError(
                    "CDS request did not finish before the polling deadline",
                    detail={
                        "adapter": "cds",
                        "request_id": submission.remote.request_id,
                        "polls": polls,
                        "elapsed_seconds": round(elapsed, 3),
                        "timeout_seconds": budget,
                        "last_status": snapshot.remote_status,
                    },
                )
            self._sleep(interval)
            interval = min(interval * 1.5, max_interval)

    def results(self, submission: CdsSubmission) -> CdsJobSnapshot:
        """取得结果元数据（临时 URL / 大小 / 类型 / 校验和原文）。

        Raises
        ------
        SourceUnavailableError
            结果未成功或远端查询失败。
        """
        snapshot = self._bridge.results(submission.remote)
        if snapshot.state is not CdsJobState.SUCCESSFUL:
            raise SourceUnavailableError(
                "CDS results requested before the request succeeded",
                detail={"adapter": "cds", "state": snapshot.state.value},
            )
        return snapshot

    def resolve(
        self,
        submission: CdsSubmission,
        *,
        timeout_seconds: float | None = None,
        initial_interval: float = _POLL_INTERVAL_SECONDS,
        max_interval: float = 60.0,
        max_polls: int = 1000,
    ) -> CdsJobSnapshot:
        """``wait`` + ``results`` 的组合快捷方式（成功后返回带 URL 的快照）。"""
        self.wait(
            submission,
            timeout_seconds=timeout_seconds,
            initial_interval=initial_interval,
            max_interval=max_interval,
            max_polls=max_polls,
        )
        return self.results(submission)


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class CDSAdapter(SourceAdapter):
    """Copernicus CDS 数据源适配器（发现阶段，不下载字节）。"""

    adapter_id: ClassVar[str] = "cds"

    def __init__(
        self,
        *,
        source_id: str = "cds",
        config: SourceConfig | None = None,
        api_root: str | None = None,
        credentials: Mapping[str, str] | None = None,
        dataset_names: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = CdsRequestLifecycle.default_timeout_seconds,
        dependency_probe: Callable[[], CdsDependencies | None] = probe_cds_dependencies,
        bridge: CdsClientBridge | None = None,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            source_id=source_id,
            config=config,
            timeout=timeout,
            http_client=http_client,
            clock=clock,
        )
        self._credentials = dict(credentials) if credentials is not None else None
        self._dataset_names = dict(dataset_names) if dataset_names is not None else None
        self._api_root = api_root
        self._request_timeout_seconds = request_timeout_seconds
        self._dependency_probe = dependency_probe
        self._dependencies: CdsDependencies | object | None = _UNPROBED
        self._bridge = bridge
        self._sleep = sleep
        self._lock = threading.Lock()
        self._schema_cache: dict[str, CdsProcessSchema] = {}
        self._catalogue_cache: dict[str, Mapping[str, JsonValue]] = {}
        self._lifecycle_instance: CdsRequestLifecycle | None = None

    @property
    def supported_routes(self) -> frozenset[str]:
        """CDS 适配器只产出 ``cds_api`` 路径的资产（与配置无关）。"""
        return frozenset({AccessRoute.CDS_API.value})

    # -- 配置解析 ---------------------------------------------------------- #
    @property
    def api_root(self) -> str:
        """CDS API 根：显式参数 > ``credentials.url`` > 配置 endpoint > 官方默认。"""
        if self._api_root:
            return self._api_root.rstrip("/")
        credentials = self.credentials
        url = credentials.get("url") or (self._config.endpoint if self._config else None)
        return (url or CDS_API_ROOT).rstrip("/")

    @property
    def credentials(self) -> Mapping[str, str]:
        """凭证映射（值来自配置 loader 展开后的结果；**日志/异常里只允许出现键名**）。"""
        if self._credentials is not None:
            return dict(self._credentials)
        if self._config is not None:
            return dict(self._config.credentials)
        return {}

    @property
    def credentials_configured(self) -> bool:
        """是否配置了 key（只判断键存在与非空，不读取值的语义）。"""
        return bool(self.credentials.get("key", "").strip())

    @property
    def dataset_names(self) -> Mapping[str, str]:
        """逻辑数据集 → CDS collection ID 的映射。"""
        if self._dataset_names is not None:
            return dict(self._dataset_names)
        mapping: dict[str, str] = {}
        if self._config is not None:
            extension_map = self._config.extensions.get("cds.dataset_names")
            if isinstance(extension_map, Mapping):
                mapping.update(
                    {str(key): str(value) for key, value in extension_map.items() if value}
                )
            single = self._config.extensions.get("cds.dataset_name")
            declared = list(self._config.datasets)
            if isinstance(single, str) and single and len(declared) == 1:
                mapping.setdefault(declared[0], single)
        mapping.update(
            {key: value for key, value in DEFAULT_COLLECTIONS.items() if key not in mapping}
        )
        return mapping

    def resolve_collection(self, dataset_id: str) -> str:
        """把请求里的逻辑数据集 ID 映射为 CDS collection ID。

        Raises
        ------
        SourceNotFoundError
            逻辑数据集没有已配置/已实测的 collection 映射（不猜测）。
        """
        collection = self.dataset_names.get(dataset_id)
        if not collection:
            raise SourceNotFoundError(
                f"no verified CDS collection mapping for dataset {dataset_id!r}; "
                "configure sources.<id>.extensions['cds.dataset_names']",
                source_id=self.source_id,
                detail={"dataset_id": dataset_id, "configured": sorted(self.dataset_names)},
            )
        return collection

    # -- 配置校验 ---------------------------------------------------------- #
    def validate_configuration(self) -> tuple[str, ...]:
        """检查静态配置（不发网络请求）；返回问题列表，空元组表示通过。

        校验的是"这份配置是否具备可执行的最低条件"，不是远端可用性
        （后者由 :meth:`capabilities` / :meth:`health_check` 负责）。
        """
        issues: list[str] = []
        try:
            parsed = httpx.URL(self.api_root)
        except (httpx.InvalidURL, ValueError):
            issues.append(f"invalid CDS api_root URL: {self.api_root!r}")
        else:
            if parsed.scheme not in ("http", "https") or not parsed.host:
                issues.append("CDS api_root must be an absolute http(s) URL")
        if not self.credentials_configured:
            issues.append(
                "sources.<id>.credentials.key is not configured "
                "(the value must never be hard-coded in source code)"
            )
        names = self.dataset_names
        if not names:
            issues.append(
                "no dataset mapping: configure sources.<id>.datasets plus "
                "extensions['cds.dataset_name'] / ['cds.dataset_names']"
            )
        for dataset_id, collection in names.items():
            if not collection.strip() or "/" in collection or " " in collection:
                issues.append(
                    f"invalid CDS collection id for dataset {dataset_id!r}: {collection!r}"
                )
        return tuple(issues)

    # -- 依赖与能力 -------------------------------------------------------- #
    def _probe_dependencies(self) -> CdsDependencies | None:
        if self._dependencies is _UNPROBED:
            with self._lock:
                if self._dependencies is _UNPROBED:
                    self._dependencies = self._dependency_probe()
        return cast(CdsDependencies | None, self._dependencies)

    def _is_available(self) -> bool:
        return self._probe_dependencies() is not None and self.credentials_configured

    def capabilities(self) -> AdapterCapabilities:
        """如实列出已验证/未验证的能力边界（不含任何凭证值）。"""
        deps = self._probe_dependencies()
        missing_dependencies: list[str] = []
        if deps is None:
            missing_dependencies = ["cdsapi>=0.7.7", "ecmwf-datastores-client"]
        missing_credentials: list[str] = []
        if not self.credentials_configured:
            missing_credentials = ["key"]
        reason: str | None = None
        if deps is None:
            reason = "CDS client dependencies are not installed"
        elif not self.credentials_configured:
            reason = "CDS credentials are not configured (sources.<id>.credentials.key)"
        unverified = [
            "result checksum format ('file:checksum') not observed without credentials",
            "signed result URL expiry window and multi-file result splitting not verified",
        ]
        if deps is None:
            unverified.append(
                "remote request parameters cannot be fetched without cdsapi installed"
            )
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            source_id=self.source_id,
            available=deps is not None and self.credentials_configured,
            routes=("cds_api",),
            verified=(
                "retrieve/v1 process schema fetched from the public CDS API (2026-11-04)",
                "catalogue/v1 collection metadata fetched from the public CDS API (2026-11-04)",
                "request translation for variable/year/month/day/time/levels/area/data_format",
                "async lifecycle submit/status/results through ecmwf-datastores-client",
            ),
            unverified=tuple(unverified),
            missing_dependencies=tuple(missing_dependencies),
            missing_credentials=tuple(missing_credentials),
            reason=reason,
        )

    # -- schema / catalogue HTTP ------------------------------------------ #
    def _process_url(self, collection_id: str) -> str:
        return f"{self.api_root}/{_API_VERSION}/processes/{collection_id}"

    def _catalogue_url(self, collection_id: str) -> str:
        return f"{self.api_root}/{_CATALOGUE_VERSION}/collections/{collection_id}"

    def fetch_process_schema(self, collection_id: str | None = None) -> CdsProcessSchema:
        """从公开 CDS API 拉取并解析 process schema（按 collection 缓存）。

        Raises
        ------
        SourceUnavailableError
            连接失败、非 200 或响应无法解析。
        """
        self._ensure_open()
        resolved = collection_id or self._default_collection()
        with self._lock:
            cached = self._schema_cache.get(resolved)
        if cached is not None:
            return cached
        url = self._process_url(resolved)
        try:
            response = self.http.get(url)
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"CDS process schema request failed for {resolved!r}",
                source_id=self.source_id,
                detail={"url": url, "error_type": type(exc).__name__},
            ) from exc
        if response.status_code != httpx.codes.OK:
            raise SourceUnavailableError(
                f"CDS process schema endpoint returned HTTP {response.status_code}",
                source_id=self.source_id,
                detail={"url": url, "status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetadataError(
                "CDS process schema response is not valid JSON",
                source_id=self.source_id,
                detail={"url": url},
            ) from exc
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CDS process schema response is not a JSON object",
                source_id=self.source_id,
                detail={"url": url},
            )
        schema = CdsProcessSchema.from_payload(
            payload,
            collection_id=resolved,
            source_url=url,
            fetched_at=self.clock(),
        )
        with self._lock:
            self._schema_cache[resolved] = schema
        return schema

    def _default_collection(self) -> str:
        names = self.dataset_names
        if not names:
            raise SourceNotFoundError(
                "CDS adapter has no dataset mapping configured",
                source_id=self.source_id,
            )
        declared = self._config.datasets if self._config is not None else []
        for dataset_id in declared:
            if dataset_id in names:
                return names[dataset_id]
        return next(iter(names.values()))

    def fetch_catalogue_metadata(self, collection_id: str | None = None) -> Mapping[str, JsonValue]:
        """拉取 collection 元数据（license/title/时间范围），失败时返回已缓存的空映射。"""
        resolved = collection_id or self._default_collection()
        with self._lock:
            cached = self._catalogue_cache.get(resolved)
        if cached is not None:
            return cached
        url = self._catalogue_url(resolved)
        try:
            response = self.http.get(url)
            payload = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "CDS catalogue metadata unavailable: source_id=%s collection=%s",
                self.source_id,
                resolved,
            )
            return {}
        if not isinstance(payload, Mapping):
            return {}
        safe_payload = {str(key): json_safe(value) for key, value in payload.items()}
        with self._lock:
            self._catalogue_cache[resolved] = safe_payload
        return safe_payload

    # -- SourceAdapter 钩子 ------------------------------------------------ #
    def _describe(self) -> SourceMetadata:
        collection = self._default_collection()
        catalogue = self.fetch_catalogue_metadata(collection)
        license_value = catalogue.get("license")
        license_text = license_value if isinstance(license_value, str) else None
        title = catalogue.get("title")
        if isinstance(title, str) and title.strip():
            display_name = title.strip()
        elif self._config is not None:
            display_name = self._config.display_name
        else:
            display_name = f"CDS {collection}"
        provider = self._config.provider if self._config is not None else "Copernicus CDS"
        extensions: dict[str, JsonValue] = {
            "cds.api_root": self.api_root,
            "cds.catalogue_api": _CATALOGUE_VERSION,
            "cds.collection_id": collection,
            "cds.area_order": list(self.area_order),
            "cds.area_order_verified": self.area_order_verified,
        }
        updated = catalogue.get("updated")
        if updated is not None:
            extensions["cds.catalogue_updated"] = updated
        return build_source_metadata(
            source_id=self.source_id,
            display_name=display_name,
            provider=provider,
            access_route=AccessRoute.CDS_API,
            dataset_id=collection,
            origin_uri=self._catalogue_url(collection),
            retrieved_at=self.clock(),
            protocol_version=_API_VERSION,
            license=license_text,
            citation=None,
            extensions=extensions,
        )

    def _health_check(self) -> bool:
        collection = self._default_collection()
        response = self.http.get(self._process_url(collection))
        return response.status_code == httpx.codes.OK

    @property
    def area_order(self) -> tuple[str, ...]:
        """Bbox 输出顺序（默认 ``[north, west, south, east]``，待官方文档复核）。"""
        return CdsRequestTranslator.area_order

    @property
    def area_order_verified(self) -> bool:
        """``area`` 顺序是否已由官方文档/有凭证请求确认（当前为 ``False``）。"""
        return CdsRequestTranslator.area_order_verified

    # -- 发现流程 ---------------------------------------------------------- #
    def translate_request(self, request: DatasetRequest) -> CdsTranslation:
        """翻译请求（公网 schema 查询 + 纯函数翻译），不提交、不下载。"""
        collection = self.resolve_collection(request.dataset_id)
        schema = self.fetch_process_schema(collection)
        return CdsRequestTranslator(schema, clock=self.clock).translate(request)

    def _ensure_discoverable(self) -> None:
        if self._probe_dependencies() is None:
            raise SourceUnavailableError(
                "CDS client dependencies are not installed; discovery is unavailable",
                source_id=self.source_id,
                detail={"missing": ["cdsapi>=0.7.7", "ecmwf-datastores-client"]},
            )
        if not self.credentials_configured:
            raise SourceUnavailableError(
                "CDS credentials are not configured; cannot submit a discovery request",
                source_id=self.source_id,
                detail={"missing": ["key"], "source": "sources.<id>.credentials.key"},
            )

    def _make_lifecycle(self) -> CdsRequestLifecycle:
        """惰性构造（并缓存）请求生命周期；线程安全。"""
        with self._lock:
            if self._lifecycle_instance is None:
                lifecycle_bridge: CdsClientBridge
                if self._bridge is not None:
                    lifecycle_bridge = self._bridge
                else:
                    lifecycle_bridge = EcmwfDatastoresBridge(
                        api_root=self.api_root,
                        key=self.credentials.get("key", ""),
                        dependencies=self._probe_dependencies(),
                        timeout=min(self._request_timeout_seconds, 300.0),
                    )
                self._lifecycle_instance = CdsRequestLifecycle(
                    lifecycle_bridge, clock=self.clock, sleep=self._sleep
                )
            return self._lifecycle_instance

    def _discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        translation = self.translate_request(request)
        self._ensure_discoverable()
        lifecycle = self._make_lifecycle()
        submission = lifecycle.submit(translation.collection_id, translation.request)
        snapshot = lifecycle.wait(
            submission,
            timeout_seconds=self._request_timeout_seconds,
        )
        result = lifecycle.results(submission)
        return (self._asset_from_result(request, translation, submission, snapshot, result),)

    def _asset_from_result(
        self,
        request: DatasetRequest,
        translation: CdsTranslation,
        submission: CdsSubmission,
        snapshot: CdsJobSnapshot,
        result: CdsJobSnapshot,
    ) -> SourceAsset:
        location = require_text(result.location, field="CDS result location")
        file_name = location.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        checksum, checksum_raw = parse_checksum_or_none(result.checksum_raw)
        metadata_extensions: dict[str, JsonValue] = {
            "cds.api_root": self.api_root,
            "cds.collection_id": translation.collection_id,
            "cds.request_id": submission.remote.request_id,
            "cds.response_state": snapshot.remote_status,
            "cds.request": json_safe(translation.request),
            "cds.area_order": list(self.area_order),
            "cds.area_order_verified": self.area_order_verified,
            "cds.result_location_is_temporary": True,
        }
        if checksum is None and checksum_raw is not None:
            metadata_extensions["cds.result_checksum_raw"] = checksum_raw
            metadata_extensions["cds.result_checksum_recognized"] = False
        metadata = build_source_metadata(
            source_id=self.source_id,
            display_name=(
                self._config.display_name if self._config is not None else "Copernicus CDS"
            ),
            provider=self._config.provider if self._config is not None else "Copernicus CDS",
            access_route=AccessRoute.CDS_API,
            dataset_id=translation.collection_id,
            origin_uri=location,
            retrieved_at=self.clock(),
            protocol_version=_API_VERSION,
            license=None,
            citation=None,
            extensions=metadata_extensions,
        )
        return build_asset(
            source_id=self.source_id,
            dataset_id=request.dataset_id,
            access_route=AccessRoute.CDS_API,
            origin_uri=location,
            file_name=file_name,
            size_bytes=result.size_bytes,
            metadata=metadata,
            discovered_at=self.clock(),
            asset_id=asset_id_for(
                self.source_id, f"{translation.collection_id}:{submission.remote.request_id}"
            ),
            checksum=checksum,
            content_type=result.content_type,
            time_slice=request.metadata.temporal_coverage,
            level_slice=(
                list(request.metadata.vertical_levels.levels)
                if request.metadata.vertical_levels.kind is VerticalLevelKind.PRESSURE
                else None
            ),
        )

    # -- 测试/诊断辅助 ----------------------------------------------------- #
    @property
    def request_timeout_seconds(self) -> float:
        """轮询等待上限（秒），来自配置 ``cds_api.request_timeout_seconds`` 或构造参数。"""
        return self._request_timeout_seconds


def cds_adapter_from_config(source_id: str, config: SourceConfig) -> CDSAdapter:
    """从 :class:`SourceConfig` 构造 :class:`CDSAdapter`（供 SourceRegistry 工厂使用）。"""
    binding = next((item for item in config.transports if item.route is AccessRoute.CDS_API), None)
    timeout_extension = (
        binding.options.get("cds_api.request_timeout_seconds") if binding is not None else None
    )
    request_timeout = (
        float(timeout_extension) if isinstance(timeout_extension, (int, float)) else 3600.0
    )
    return CDSAdapter(
        source_id=source_id,
        config=config,
        api_root=config.endpoint or config.credentials.get("url"),
        request_timeout_seconds=request_timeout,
    )
