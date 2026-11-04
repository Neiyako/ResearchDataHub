"""数据源适配器基类与元数据标准化工具。

设计边界（ADR-0002）
-------------------
* 本模块只负责"数据在哪、元数据长什么样"：目录查询、远端参数翻译、元数据解析、
  产出 :class:`~researchdatahub.core.models.SourceAsset`。
* **绝不下载字节**：没有 ``.download()``；文件大小/校验和来自远端元数据声明，
  是否与真实字节一致由 M2/M4 的传输与完整性模块负责。
* 原始远端字段一律保留在 ``extensions`` 中并使用命名空间（``rda.*`` / ``cds.*``），
  不允许把数据源专属字段塞进核心模型字段。

"未知"的处理原则
----------------
* 远端没有给的值 **不得推断**：可选字段写 ``None``，版本写 ``"unknown"``。
* 必填科学字段缺失时 **不得构造** :class:`DatasetMetadata`，抛
  :class:`~researchdatahub.core.errors.MetadataError` 并列出缺失字段。
* 拿不到文件大小就不能产出 :class:`SourceAsset`（``size_bytes`` 是传输与完整性
  校验的输入，填 0 会把"未知"伪装成"空文件"）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, ClassVar, Final, Self, cast

import httpx
from pydantic import ValidationError

from researchdatahub.config.models import SourceConfig
from researchdatahub.core.errors import MetadataError, SourceError, SourceNotFoundError
from researchdatahub.core.models import (
    AccessRoute,
    Checksum,
    DatasetMetadata,
    DatasetRequest,
    GridSpec,
    JsonValue,
    SourceAsset,
    SourceMetadata,
    SpatialExtent,
    TemporalExtent,
    VariableSpec,
    VerticalLevelKind,
    VerticalLevels,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "AdapterCapabilities",
    "SourceAdapter",
    "as_utc",
    "asset_id_for",
    "build_asset",
    "build_dataset_metadata",
    "build_source_metadata",
    "build_spatial_extent",
    "build_temporal_extent",
    "build_variable_spec",
    "build_vertical_levels",
    "json_safe",
    "namespaced_key",
    "normalize_source_metadata",
    "normalize_token",
    "normalize_variable_specs",
    "parse_size_bytes",
    "parse_timestamp",
    "require_text",
    "utcnow",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
"""单次元数据请求的超时（秒）。传输超时由 ``transfer`` 配置决定，二者不混用。"""

_IDENTIFIER_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
_CHECKSUM_PATTERN: Final = re.compile(r"^(sha256|sha256-16|md5|crc32):([0-9a-f]{8,64})$")
_SPACE_PATTERN: Final = re.compile(r"[\s\-/]+")
_TOKEN_STRIP_PATTERN: Final = re.compile(r"[^a-z0-9_]+")

_TIMESTAMP_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d %H:%M %z",  # NCAR RDA：``1940-01-01 00:00 +0000``（真实响应）
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区）。测试通过注入 ``clock`` 覆盖。"""
    return datetime.now(UTC)


def as_utc(value: datetime, *, field: str = "datetime") -> datetime:
    """把 ``datetime`` 规整为 UTC；naive 时间被拒绝（时区不明的时间不可接受）。

    Raises
    ------
    MetadataError
        ``value`` 没有时区信息。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetadataError(
            f"{field} must be timezone-aware; naive datetime is ambiguous in science data",
            detail={"field": field},
        )
    return value.astimezone(UTC)


def json_safe(value: Any) -> JsonValue:
    """把任意远端值转换为可 JSON 序列化的 :data:`JsonValue`（datetime → ISO 字符串）。

    远端元数据来自 JSON，绝大多数值本来就是可序列化的；本函数只处理
    datetime/date 以及未知对象的字符串化，保证 ``extensions`` 永远能落库。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return as_utc(value, field="extension timestamp").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)


def namespaced_key(prefix: str, key: str) -> str:
    """构造 ``extensions`` 的命名空间键（``"rda"`` + ``"gindex"`` → ``"rda.gindex"``）。

    Raises
    ------
    MetadataError
        ``prefix`` 为空或 ``key`` 为空。
    """
    clean_prefix = prefix.strip().strip(".")
    clean_key = key.strip().strip(".")
    if not clean_prefix or not clean_key:
        raise MetadataError(
            "extension keys must be non-empty and namespaced",
            detail={"prefix": prefix, "key": key},
        )
    return f"{clean_prefix}.{clean_key}"


def normalize_token(value: str) -> str:
    """把长名称规范化为 snake_case 令牌（``"Specific humidity"`` → ``"specific_humidity"``）。

    只做大写转小写、空白/连字符/斜杠转下划线、去掉其余非字母数字字符；不做同义词映射。
    """
    lowered = value.strip().lower()
    lowered = _SPACE_PATTERN.sub("_", lowered)
    lowered = _TOKEN_STRIP_PATTERN.sub("", lowered)
    return lowered.strip("_")


def require_text(value: object, *, field: str) -> str:
    """断言字符串字段非空，否则抛 :class:`MetadataError`（不返回空串哄骗调用方）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise MetadataError(
        f"{field} is required but the remote metadata did not provide it",
        detail={"field": field, "observed": json_safe(value)},
    )


def parse_timestamp(
    value: object,
    *,
    field: str,
    allow_date_only: bool = True,
    date_only_assume_utc: bool = True,
) -> datetime | None:
    """解析远端时间戳；无法解析时返回 ``None``（调用方按"未知"处理）。

    支持的真实格式（来自实测响应）
    ------------------------------
    * ISO 8601（带 ``Z``/偏移）：``2025-10-30T00:00:00Z``、``2026-11-04T09:00:00+00:00``
    * NCAR RDA：``1940-01-01 00:00 +0000``
    * 仅日期：``2025-10-30``（``allow_date_only=True`` 时按 UTC 零点解释）

    ``date_only_assume_utc`` 是**显式声明的解释规则**：日期型元数据没有时区，
    这里固定按 UTC 零点读取，并把原值保留在 extensions 中；不是"猜"。

    Raises
    ------
    MetadataError
        文本存在时区偏移以外的解析失败，或 naive 时间戳。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value, field=field)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    iso_candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            if len(text) == 10 and allow_date_only and date_only_assume_utc:
                return parsed.replace(tzinfo=UTC)
            raise MetadataError(
                f"{field} has no timezone offset; refusing to guess its timezone",
                detail={"field": field, "observed": text},
            )
        return parsed.astimezone(UTC)

    for fmt in _TIMESTAMP_FORMATS:
        try:
            candidate = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if candidate.tzinfo is None:
            if date_only_assume_utc:
                return candidate.replace(tzinfo=UTC)
            raise MetadataError(
                f"{field} has no timezone offset; refusing to guess its timezone",
                detail={"field": field, "observed": text},
            )
        return candidate.astimezone(UTC)

    if len(text) == 10 and allow_date_only and date_only_assume_utc:
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def parse_size_bytes(value: object, *, field: str = "size") -> int | None:
    """解析字节大小；支持整数与纯数字字符串。负数/非法值返回 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if text.isdigit():
            return int(text)
        return None
    return None


def _identifier(value: str, *, field: str) -> str:
    """校验并返回 :data:`Identifier` 形状的标识符。"""
    text = value.strip()
    if not _IDENTIFIER_PATTERN.match(text):
        raise MetadataError(
            f"{field} must match ^[a-z0-9][a-z0-9._-]{{1,62}}$",
            detail={"field": field, "observed": text},
        )
    return text


def asset_id_for(source_id: str, seed: str) -> str:
    """生成稳定、合规的 ``asset_id``（``<source>-<sha256 前 16 位>``）。

    ``seed`` 应选择**在一次发现内稳定**的标识（源内 ID / 请求 ID），
    不要用会过期的临时下载 URL，否则重试后资产 ID 会漂移。
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    prefix = _TOKEN_STRIP_PATTERN.sub("", source_id.replace(".", "_").lower())[:40] or "src"
    candidate = f"{prefix}-{digest}"
    if not _IDENTIFIER_PATTERN.match(candidate):
        candidate = f"asset-{digest}"
    return candidate


def _parse_checksum(value: object) -> Checksum | None:
    """仅接受核心模型定义过的 ``<algo>:<hex>`` 形式；其它形式保留原文不猜测。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _CHECKSUM_PATTERN.match(text):
        return None
    return Checksum(value=text)


def build_variable_spec(
    *,
    name: str,
    units: str | None,
    long_name: str | None = None,
    standard_name: str | None = None,
    cell_methods: str | None = None,
) -> VariableSpec:
    """构造 :class:`VariableSpec`；单位缺失时抛 :class:`MetadataError`（单位不是装饰）。"""
    clean_name = name.strip().lower()
    if not clean_name:
        raise MetadataError("variable name is empty", detail={"name": name})
    clean_units = (units or "").strip()
    if not clean_units:
        raise MetadataError(
            "variable units are required; remote metadata did not provide them",
            detail={"variable": clean_name},
        )
    try:
        return VariableSpec(
            name=clean_name,
            units=clean_units,
            long_name=long_name,
            standard_name=standard_name,
            cell_methods=cell_methods,
        )
    except ValidationError as exc:
        raise MetadataError(
            f"invalid variable spec for {clean_name!r}",
            detail={"variable": clean_name, "errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_temporal_extent(
    start: datetime | None, end: datetime | None, *, field: str = "temporal_coverage"
) -> TemporalExtent | None:
    """构造闭开区间时间范围；任一端未知则返回 ``None``（不推断）。"""
    if start is None or end is None:
        return None
    try:
        return TemporalExtent(start=start, end=end)
    except ValidationError as exc:
        raise MetadataError(
            f"invalid {field}: remote start/end are inconsistent",
            detail={"field": field, "errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_spatial_extent(
    *,
    north: float | None,
    south: float | None,
    west: float | None,
    east: float | None,
    grid_degrees: float | None = None,
) -> SpatialExtent | None:
    """构造经纬度包围盒；任一边缺失则返回 ``None``（不推断）。"""
    if north is None or south is None or west is None or east is None:
        return None
    try:
        return SpatialExtent(
            west=west, east=east, south=south, north=north, grid_degrees=grid_degrees
        )
    except ValidationError as exc:
        raise MetadataError(
            "invalid spatial extent: remote bounds are inconsistent",
            detail={"errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_vertical_levels(
    *,
    kind: VerticalLevelKind | str,
    levels: Sequence[float] | None = None,
    units: str | None = None,
) -> VerticalLevels:
    """构造垂直层定义；``kind='none'`` 时不得携带层（模型会拒绝）。"""
    try:
        resolved_kind = VerticalLevelKind(kind)
    except ValueError as exc:
        raise MetadataError(
            f"unknown vertical level kind {kind!r}",
            detail={"kind": str(kind)},
        ) from exc
    if resolved_kind is VerticalLevelKind.NONE:
        return VerticalLevels(kind=resolved_kind)
    try:
        return VerticalLevels(kind=resolved_kind, levels=list(levels or []), units=units or None)
    except ValidationError as exc:
        raise MetadataError(
            "invalid vertical levels: remote levels are missing or not ordered",
            detail={"errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_dataset_metadata(
    *,
    product: str | None,
    product_type: str | None,
    variables: Sequence[VariableSpec],
    temporal_coverage: TemporalExtent | None,
    spatial_extent: SpatialExtent | None,
    vertical_levels: VerticalLevels | None = None,
    grid: GridSpec | None = None,
    time_definition: str | None = None,
    data_version: str | None = None,
    post_processing: Sequence[str] = (),
    regridded: bool = False,
    units_normalized: bool = False,
    extensions: Mapping[str, JsonValue] | None = None,
) -> DatasetMetadata:
    """用**明确的远端值**构造 :class:`DatasetMetadata`；缺失必填项则拒绝构造。

    这里不补任何默认科学事实：``product``/``product_type``/变量/时间/空间任一缺失
    都会抛 :class:`MetadataError`，并在 ``detail["missing"]`` 中逐项列出。

    Raises
    ------
    MetadataError
        必填科学元数据缺失，或字段形状不合法（如变量名不合规）。
    """
    missing = [
        field
        for field, value in (
            ("product", product),
            ("product_type", product_type),
            ("variables", variables),
            ("temporal_coverage", temporal_coverage),
            ("spatial_extent", spatial_extent),
        )
        if value is None or (isinstance(value, Sequence) and not value)
    ]
    if missing:
        raise MetadataError(
            "cannot build DatasetMetadata: required remote metadata is missing "
            "(no inference is allowed)",
            detail={"missing": missing},
        )
    namespace: dict[str, JsonValue] = {}
    for key, value in (extensions or {}).items():
        if "." not in key:
            raise MetadataError(
                f"dataset extension key {key!r} must be namespaced, e.g. 'cds.collection'",
                detail={"key": key},
            )
        namespace[key] = json_safe(value)
    try:
        return DatasetMetadata(
            product=require_text(product, field="product"),
            product_type=require_text(product_type, field="product_type"),
            variables=list(variables),
            temporal_coverage=cast(TemporalExtent, temporal_coverage),
            spatial_extent=cast(SpatialExtent, spatial_extent),
            vertical_levels=vertical_levels or VerticalLevels(kind=VerticalLevelKind.NONE),
            grid=grid or GridSpec(),
            time_definition=time_definition or "unknown",
            data_version=data_version or "unknown",
            post_processing=list(post_processing),
            regridded=regridded,
            units_normalized=units_normalized,
            extensions=namespace,
        )
    except ValidationError as exc:
        raise MetadataError(
            "invalid DatasetMetadata built from remote metadata",
            detail={"errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_source_metadata(
    *,
    source_id: str,
    display_name: str,
    provider: str,
    access_route: AccessRoute,
    dataset_id: str,
    origin_uri: str,
    retrieved_at: datetime,
    protocol_version: str | None = None,
    license: str | None = None,
    citation: str | None = None,
    extensions: Mapping[str, JsonValue] | None = None,
) -> SourceMetadata:
    """构造 :class:`SourceMetadata`；未知的可选字段保持 ``None``（不补默认值）。

    Raises
    ------
    MetadataError
        必填字段为空、ID 形状不合法或 extensions 未命名空间化。
    """
    namespace: dict[str, JsonValue] = {}
    for key, value in (extensions or {}).items():
        if "." not in key:
            raise MetadataError(
                f"source extension key {key!r} must be namespaced, e.g. 'rda.dsid'",
                detail={"key": key},
            )
        namespace[key] = json_safe(value)
    try:
        return SourceMetadata(
            source_id=_identifier(source_id, field="source_id"),
            display_name=require_text(display_name, field="display_name"),
            provider=require_text(provider, field="provider"),
            access_route=access_route,
            dataset_id=require_text(dataset_id, field="dataset_id"),
            origin_uri=require_text(origin_uri, field="origin_uri"),
            protocol_version=protocol_version,
            license=license,
            citation=citation,
            retrieved_at=as_utc(retrieved_at, field="retrieved_at"),
            extensions=namespace,
        )
    except ValidationError as exc:
        raise MetadataError(
            "invalid SourceMetadata built from configuration/remote metadata",
            detail={"errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def build_asset(
    *,
    source_id: str,
    dataset_id: str,
    access_route: AccessRoute,
    origin_uri: str,
    file_name: str | None,
    size_bytes: int | None,
    metadata: SourceMetadata,
    discovered_at: datetime,
    asset_id: str | None = None,
    checksum: Checksum | None = None,
    remote_etag: str | None = None,
    remote_last_modified: datetime | None = None,
    content_type: str | None = None,
    time_slice: TemporalExtent | None = None,
    level_slice: Sequence[float] | None = None,
    extensions: Mapping[str, JsonValue] | None = None,
) -> SourceAsset:
    """构造 :class:`SourceAsset`；大小未知时**拒绝构造**（而不是写 0）。

    Parameters
    ----------
    file_name:
        远端文件名；``None`` 时从 ``origin_uri`` 的 path 推导，推导不出则失败。
    size_bytes:
        远端声明的字节数；``None`` 表示未知——此时不允许生成资产，
        因为传输与完整性校验依赖这个数字做自证。

    Raises
    ------
    MetadataError
        缺少文件名/大小，或 ``metadata`` 与资产字段不一致。
    """
    if size_bytes is None:
        raise MetadataError(
            "cannot build SourceAsset: remote size is unknown "
            "(use the catalog API or mark the source unavailable instead of assuming 0)",
            detail={"origin_uri": origin_uri},
        )
    resolved_name = (file_name or "").strip()
    if not resolved_name:
        try:
            path = httpx.URL(origin_uri).path
        except (httpx.InvalidURL, ValueError) as exc:
            raise MetadataError(
                "cannot derive file_name from an invalid origin_uri",
                detail={"origin_uri": origin_uri},
            ) from exc
        resolved_name = path.rstrip("/").rsplit("/", 1)[-1]
    resolved_name = require_text(resolved_name, field="file_name")
    resolved_asset_id = asset_id or asset_id_for(source_id, origin_uri)
    try:
        return SourceAsset(
            asset_id=_identifier(resolved_asset_id, field="asset_id"),
            source_id=_identifier(source_id, field="source_id"),
            dataset_id=_identifier(dataset_id, field="dataset_id"),
            access_route=access_route,
            origin_uri=require_text(origin_uri, field="origin_uri"),
            file_name=resolved_name,
            size_bytes=size_bytes,
            checksum=checksum,
            remote_etag=remote_etag,
            remote_last_modified=(
                None
                if remote_last_modified is None
                else as_utc(remote_last_modified, field="remote_last_modified")
            ),
            content_type=content_type,
            time_slice=time_slice,
            level_slice=None if level_slice is None else list(level_slice),
            metadata=metadata,
            discovered_at=as_utc(discovered_at, field="discovered_at"),
        )
    except ValidationError as exc:
        raise MetadataError(
            "invalid SourceAsset built from remote metadata",
            detail={"errors": json_safe(exc.errors(include_url=False))},
        ) from exc


def parse_checksum_or_none(value: object) -> tuple[Checksum | None, JsonValue]:
    """解析远端校验和：仅识别核心模型支持的形式，其余原样返回供 extensions 保存。

    Returns
    -------
    tuple[Checksum | None, JsonValue]
        ``(解析到的校验和 或 None, 原始值)``。原始值永远返回，便于审计。
    """
    return _parse_checksum(value), json_safe(value)


@dataclass(frozen=True)
class AdapterCapabilities:
    """适配器能力边界：**已实现/已实测** 与 **未验证/受限** 必须分开陈述。

    这是"不得虚构远端事实"的机器可读出口：``available=False`` 时调用方必须
    拒绝该源，而不是退化成空结果。
    """

    adapter_id: str
    source_id: str
    available: bool
    routes: tuple[str, ...]
    verified: tuple[str, ...]
    unverified: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
    missing_credentials: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        """稳定字典（供 CLI/报告输出，不含任何密钥值）。"""
        return {
            "adapter_id": self.adapter_id,
            "source_id": self.source_id,
            "available": self.available,
            "routes": list(self.routes),
            "verified": list(self.verified),
            "unverified": list(self.unverified),
            "missing_dependencies": list(self.missing_dependencies),
            "missing_credentials": list(self.missing_credentials),
            "reason": self.reason,
        }


class SourceAdapter(ABC):
    """所有数据源适配器的基类（实现 :class:`~researchdatahub.core.ports.DataSourceAdapter`）。

    子类只需实现 :meth:`_describe`、:meth:`_discover_assets`、:meth:`_health_check`
    与 :attr:`adapter_id`；基类负责协议一致性校验（source_id / dataset_id / 重复资产）、
    生命周期（关闭后拒绝调用）与健康检查"不抛异常"的语义。
    """

    adapter_id: ClassVar[str] = "unknown"

    def __init__(
        self,
        *,
        source_id: str,
        config: SourceConfig | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._source_id = _identifier(source_id, field="source_id")
        self._config = config
        self._timeout = timeout
        self._http_client = http_client
        self._owns_http = http_client is None
        self._clock = clock
        self._closed = False
        self._source_metadata: SourceMetadata | None = None
        self._describe_lock = threading.Lock()

    # -- 协议属性 ---------------------------------------------------------- #
    @property
    def source_id(self) -> str:
        """数据源 ID（与配置 ``sources.<id>`` 一致）。"""
        return self._source_id

    @property
    def config(self) -> SourceConfig | None:
        """该适配器对应的配置（可能为 ``None``，例如单元测试直接构造）。"""
        return self._config

    @property
    def supported_routes(self) -> frozenset[str]:
        """该适配器**能产出**的访问路径；默认取配置声明的 routes。"""
        if self._config is None:
            return frozenset()
        return frozenset(binding.route.value for binding in self._config.transports)

    @property
    def clock(self) -> Callable[[], datetime]:
        """注入的时钟（测试固定 ``now``，避免依赖真实时间）。"""
        return self._clock

    # -- 生命周期 ---------------------------------------------------------- #
    def close(self) -> None:
        """释放 HTTP 客户端。幂等。"""
        if self._http_client is not None and self._owns_http:
            self._http_client.close()
        self._closed = True

    def __enter__(self) -> Self:
        """进入上下文（返回自身；退出时关闭 HTTP 客户端）。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文并调用 :meth:`close`。"""
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("adapter is closed; create a new instance")

    @property
    def http(self) -> httpx.Client:
        """惰性创建的 HTTP 客户端（跟随重定向；超时只用于元数据查询）。"""
        self._ensure_open()
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                headers={"User-Agent": "researchdatahub-sources/0.1"},
            )
            self._owns_http = True
        return self._http_client

    # -- DataSourceAdapter 协议 ------------------------------------------- #
    def describe(self) -> SourceMetadata:
        """返回数据源级元数据（首次成功调用后缓存）。"""
        self._ensure_open()
        with self._describe_lock:
            if self._source_metadata is None:
                self._source_metadata = self._describe()
            return self._source_metadata

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        """把需求映射为远端资产，并做协议级一致性校验（返回新元组，不缓存）。"""
        self._ensure_open()
        if not self._supports_dataset(request.dataset_id):
            raise SourceNotFoundError(
                f"source {self.source_id!r} does not provide dataset {request.dataset_id!r}",
                source_id=self.source_id,
                detail={"requested": request.dataset_id, "supported": self._dataset_ids()},
            )
        assets = tuple(self._discover_assets(request))
        seen: set[str] = set()
        for asset in assets:
            if not isinstance(asset, SourceAsset):
                raise MetadataError(
                    "adapter returned a non-SourceAsset value",
                    source_id=self.source_id,
                    detail={"type": type(asset).__name__},
                )
            if asset.source_id != self.source_id:
                raise MetadataError(
                    "adapter produced an asset for a different source",
                    source_id=self.source_id,
                    detail={"asset_source_id": asset.source_id, "asset_id": asset.asset_id},
                )
            if asset.dataset_id != request.dataset_id:
                raise MetadataError(
                    "adapter produced an asset for a different dataset",
                    source_id=self.source_id,
                    detail={"asset_dataset_id": asset.dataset_id, "requested": request.dataset_id},
                )
            if asset.asset_id in seen:
                raise MetadataError(
                    "adapter produced duplicate asset ids",
                    source_id=self.source_id,
                    detail={"asset_id": asset.asset_id},
                )
            seen.add(asset.asset_id)
        return assets

    def health_check(self) -> bool:
        """轻量健康检查；**任何**失败都返回 ``False``，绝不抛异常（协议要求）。"""
        if self._closed:
            return False
        try:
            return bool(self._health_check())
        except (SourceError, httpx.HTTPError, OSError, RuntimeError, MetadataError) as exc:
            logger.warning(
                "health check failed: source_id=%s adapter=%s error=%s",
                self.source_id,
                self.adapter_id,
                type(exc).__name__,
            )
            return False

    # -- 子类钩子 ---------------------------------------------------------- #
    @abstractmethod
    def _describe(self) -> SourceMetadata:
        """实现 :meth:`describe` 的数据源级元数据查询。"""

    @abstractmethod
    def _discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        """实现 :meth:`discover_assets` 的发现逻辑（不得下载字节）。"""

    @abstractmethod
    def _health_check(self) -> bool:
        """实现 :meth:`health_check` 的轻量探测；可抛异常，由基类折算为 ``False``。"""

    def capabilities(self) -> AdapterCapabilities:
        """默认能力描述；子类应覆盖以如实列出 verified/unverified 边界。"""
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            source_id=self.source_id,
            available=self._is_available(),
            routes=tuple(sorted(self.supported_routes)),
            verified=(),
            unverified=(),
        )

    def _is_available(self) -> bool:
        """依赖/凭证是否就绪；基类默认视为可用。"""
        return True

    # -- 数据集支持判断 ---------------------------------------------------- #
    def _dataset_ids(self) -> tuple[str, ...]:
        if self._config is None:
            return ()
        return tuple(self._config.datasets)

    def _supports_dataset(self, dataset_id: str) -> bool:
        """配置声明了 datasets 时按声明判断；未声明时交给子类解析（不猜）。"""
        declared = self._dataset_ids()
        return not declared or dataset_id in declared


def normalize_variable_specs(
    raw_variables: Sequence[Mapping[str, Any]],
    *,
    name_key: str = "name",
    units_key: str = "units",
    long_name_key: str = "long_name",
    standard_name_key: str = "standard_name",
    cell_methods_key: str = "cell_methods",
) -> tuple[VariableSpec, ...]:
    """把远端原始变量数组映射为 :class:`VariableSpec` 元组（字段名由调用方显式给出）。

    只映射调用方**明确声明**的键，不做同义词匹配；单位缺失直接失败（不猜单位）。
    原始条目由调用方在需要时另行放入 extensions。

    Raises
    ------
    MetadataError
        列表为空、条目不是对象、变量名/单位缺失。
    """
    if not raw_variables:
        raise MetadataError("remote variable list is empty; cannot build DatasetMetadata")
    specs: list[VariableSpec] = []
    for index, raw in enumerate(raw_variables):
        if not isinstance(raw, Mapping):
            raise MetadataError(
                "remote variable entry is not an object",
                detail={"index": index, "observed_type": type(raw).__name__},
            )
        name = raw.get(name_key)
        units = raw.get(units_key)
        long_name = raw.get(long_name_key)
        standard_name = raw.get(standard_name_key)
        cell_methods = raw.get(cell_methods_key)
        if not isinstance(name, str) or not name.strip():
            raise MetadataError(
                "remote variable entry has no usable name",
                detail={"index": index, "observed": json_safe(raw.get(name_key))},
            )
        specs.append(
            build_variable_spec(
                name=name,
                units=units if isinstance(units, str) else None,
                long_name=long_name if isinstance(long_name, str) else None,
                standard_name=standard_name if isinstance(standard_name, str) else None,
                cell_methods=cell_methods if isinstance(cell_methods, str) else None,
            )
        )
    return tuple(specs)


def normalize_source_metadata(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    display_name: str,
    provider: str,
    access_route: AccessRoute,
    dataset_id: str,
    origin_uri: str,
    retrieved_at: datetime,
    prefix: str,
    field_map: Mapping[str, str] | None = None,
    include_raw: bool = True,
) -> SourceMetadata:
    """把远端原始 dict 映射为 :class:`SourceMetadata`，未映射字段进命名空间扩展。

    ``field_map`` 的键是契约字段名（``license`` / ``citation`` / ``protocol_version``），
    值是远端原始键名——只有调用方显式声明的映射才会被使用，**不做同义词猜测**。
    未映射的原始键（``include_raw=True`` 时）原样保存在
    ``extensions["<prefix>.<key>"]`` 中，保证"原始元数据必须保留"。

    Raises
    ------
    MetadataError
        必填参数为空、``prefix`` 为空或映射到的值类型不符。
    """
    if not prefix.strip():
        raise MetadataError("extension prefix must be non-empty")
    mapped: dict[str, str | None] = {"license": None, "citation": None, "protocol_version": None}
    consumed: set[str] = set()
    for contract_key, raw_key in (field_map or {}).items():
        if contract_key not in mapped:
            raise MetadataError(
                f"unsupported contract field {contract_key!r} in field_map",
                detail={"allowed": sorted(mapped)},
            )
        if raw_key not in raw:
            continue
        value = raw[raw_key]
        if not isinstance(value, str) or not value.strip():
            raise MetadataError(
                f"remote field {raw_key!r} mapped to {contract_key!r} is not a non-empty string",
                detail={"field": raw_key, "observed": json_safe(value)},
            )
        mapped[contract_key] = value.strip()
        consumed.add(raw_key)
    extensions: dict[str, JsonValue] = {}
    if include_raw:
        for key, value in raw.items():
            if key in consumed:
                continue
            extensions[f"{prefix}.{key}"] = json_safe(value)
    return build_source_metadata(
        source_id=source_id,
        display_name=display_name,
        provider=provider,
        access_route=access_route,
        dataset_id=dataset_id,
        origin_uri=origin_uri,
        retrieved_at=retrieved_at,
        protocol_version=mapped["protocol_version"],
        license=mapped["license"],
        citation=mapped["citation"],
        extensions=extensions,
    )
