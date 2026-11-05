"""Provider 层自有的元数据构造工具（**不 import ``sources/``**）。

为什么不用 ``sources.base`` 的 ``build_spatial_extent`` / ``build_temporal_extent``
--------------------------------------------------------------------------------
Provider 层与资产发现层是**两个独立的层**（见 :mod:`researchdatahub.providers.base`
的模块文档）：Provider 只产出"数据集候选 / 详情 / 变量"，不产出 ``SourceAsset``。
若为了两个小构造函数 import 整个 ``sources.base``（它会连带 import
``config.models`` 与 ``SourceAdapter``），两层就产生了实际耦合，后续桥接时
更难判断依赖方向。这里只依赖 ``core.models``，保持单向：``providers → core``。

"未知"的处理
------------
* 时间/空间任一端缺失 → 返回 ``None``，**不**用"当前时间"或"全球范围"补齐。
* 文本字段空白 → ``None``。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from researchdatahub.core.errors import MetadataError
from researchdatahub.core.models import JsonValue, SpatialExtent, TemporalExtent

__all__ = [
    "as_utc",
    "build_spatial_extent",
    "build_temporal_extent",
    "flat_json",
    "parse_timestamp",
]

_TIMESTAMP_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
)


def as_utc(value: datetime, *, field: str) -> datetime:
    """把带时区的时间规整为 UTC。

    Raises
    ------
    MetadataError
        传入 naive 时间（时区不明的时间在科研数据里不可接受）。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetadataError(f"{field} must be timezone-aware", detail={"field": field})
    return value.astimezone(UTC)


def parse_timestamp(
    value: object,
    *,
    field: str,
    date_only_assume_utc: bool = True,
) -> datetime | None:
    """解析远端 ISO-8601 时间戳；无法解析返回 ``None``（调用方按"未知"处理）。

    支持的实测格式（CMR / STAC / OData）
    -----------------------------------
    * ``2014-03-08T22:09:50.674Z``（CMR granule，毫秒 + ``Z``）
    * ``2026-11-05T14:22:58.655654Z``（CDSE OData，微秒 + ``Z``）
    * ``2015-06-27T10:25:31Z``（STAC extent）
    * 纯日期 ``2024-06-01``（``date_only_assume_utc=True`` 时按 UTC 零点解释——这是
      **显式声明的解释规则**，并把原值留在 extensions 里，不是猜测）

    Raises
    ------
    MetadataError
        文本带时区以外的解析失败且不是日期型，或解析出 naive 时间。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value, field=field)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if len(text) == 10 and date_only_assume_utc:
                return parsed.replace(tzinfo=UTC)
            raise MetadataError(
                f"{field} has no timezone offset; refusing to guess its timezone",
                detail={"field": field, "observed": text},
            )
        return parsed.astimezone(UTC)
    for fmt in _TIMESTAMP_FORMATS:
        try:
            fallback = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fallback.tzinfo is None or fallback.utcoffset() is None:
            raise MetadataError(
                f"{field} has no timezone offset; refusing to guess its timezone",
                detail={"field": field, "observed": text},
            )
        return fallback.astimezone(UTC)
    return None


def build_temporal_extent(start: datetime | None, end: datetime | None) -> TemporalExtent | None:
    """构造时间范围；任一端未知返回 ``None``（不推断）。"""
    if start is None or end is None:
        return None
    return TemporalExtent(
        start=as_utc(start, field="temporal start"), end=as_utc(end, field="temporal end")
    )


def build_spatial_extent(
    *,
    north: float,
    south: float,
    west: float,
    east: float,
) -> SpatialExtent | None:
    """构造经纬度包围盒；顺序不合法时返回 ``None``（不"修正"远端数据）。

    ``east < west`` 表示跨越 180° 经线，核心模型 :class:`SpatialExtent` 允许该情形
    （仅校验各自取值范围），因此这里不额外拒绝。
    """
    if south > north:
        return None
    try:
        return SpatialExtent(west=west, east=east, south=south, north=north)
    except ValueError:
        return None


def flat_json(value: Any) -> JsonValue:
    """把任意远端值收窄为可序列化的 :data:`JsonValue`（datetime → ISO 字符串）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return as_utc(value, field="extension timestamp").isoformat()
    if isinstance(value, Mapping):
        return {str(key): flat_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [flat_json(item) for item in value]
    return str(value)
