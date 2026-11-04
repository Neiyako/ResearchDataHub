"""YAML 采集需求加载器：把 ``.yaml`` 需求文件变成 :class:`DatasetRequest`。

规范格式（master prompt §6.2，分段）::

    request:
      name: era5_cloud_water_2011
      priority: normal
      preferred_sources: [cds, ncar]
      max_sources: 1
    dataset:
      provider: cds
      name: reanalysis-era5-pressure-levels
      product: reanalysis
      product_family: ERA5
      version: unknown
    variables:
      - clwc
      - {name: t, units: K, long_name: Temperature}
    temporal: {start: "2011-01-01", end: "2011-12-31", frequency: hourly}
    spatial: {type: global}
    vertical: {levels: [1000, 925, 850], kind: pressure}
    storage: {preserve_original: true}
    validation: {verify_size: true, calculate_sha256: true, netcdf_openable: true}

兼容格式（仓库既有 ``examples/era5-request.yaml``，等同直接序列化 ``DatasetRequest``）::

    request_id: ...
    metadata: {product: ..., variables: [...], temporal_coverage: ...}
    preferred_sources: [ncar, cds]

语义约定（重要，不可推断）
--------------------------
* **时间**：日期型 ``start`` 解释为当日 UTC 00:00；日期型 ``end`` 解释为**次日** UTC 00:00
  （闭开区间 ``[start, end)``，否则最后一天的数据会丢）；带时区的时间按原样使用。
* **units**：字符串变量没有单位时**不得猜测**。若调用方从真实数据源快照提供了
  ``units_catalog`` 就用它（并在 ``rdh.units_source`` 标注 ``caller_supplied``），
  否则写 ``"unknown"`` 并标注 ``unspecified_in_request``，同时产出 warning。
* **vertical.levels**：气压层按 hPa 记录；loader 统一排序为**降序**（模型要求严格递减），
  重复层直接报错——排序是有损归一化，重复属于需求错误。
* 未知段/字段一律拒绝（``extra="forbid"``），错误定位到 ``文件:行`` + YAML 字段路径。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, cast

import yaml
from pydantic import ValidationError

from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import (
    DatasetRequest,
    IntegrityCheckKind,
    VerticalLevelKind,
)

__all__ = [
    "LoadedRequest",
    "RequestLoadError",
    "load_request",
    "load_request_detailed",
    "load_request_text",
    "load_request_text_detailed",
    "parse_request",
]

_SECTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "request",
        "dataset",
        "variables",
        "temporal",
        "spatial",
        "vertical",
        "storage",
        "validation",
        "extensions",
    }
)
_STRUCTURAL_SECTION_KEYS: Final[frozenset[str]] = _SECTION_KEYS - {"extensions"}
"""判定"是否是分段格式"的结构段；``extensions`` 不是结构标记（扁平格式也可以有）。"""

_REQUEST_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "priority", "preferred_sources", "max_sources", "extensions"}
)
_DATASET_KEYS: Final[frozenset[str]] = frozenset(
    {"provider", "name", "product", "product_family", "version"}
)
_VARIABLE_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "units", "long_name", "standard_name", "cell_methods"}
)
_TEMPORAL_KEYS: Final[frozenset[str]] = frozenset({"start", "end", "frequency"})
_SPATIAL_KEYS: Final[frozenset[str]] = frozenset({"type", "bbox"})
_BBOX_KEYS: Final[frozenset[str]] = frozenset({"west", "east", "south", "north", "grid_degrees"})
_VERTICAL_KEYS: Final[frozenset[str]] = frozenset({"levels", "kind", "units"})
_STORAGE_KEYS: Final[frozenset[str]] = frozenset({"preserve_original"})
_VALIDATION_KEYS: Final[frozenset[str]] = frozenset(
    {"verify_size", "calculate_sha256", "netcdf_openable"}
)
_FLAT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "request_id",
        "dataset_id",
        "metadata",
        "preferred_sources",
        "max_sources",
        "checks",
        "preserve_original",
        "priority",
        "extensions",
    }
)

#: ``temporal.frequency`` → 一个时间步的秒数（用于"frequency 与区间不匹配"校验）。
_FREQUENCY_STEP_SECONDS: Final[dict[str, float]] = {
    "hourly": 3600.0,
    "utc_hourly": 3600.0,
    "3_hourly": 3 * 3600.0,
    "6_hourly": 6 * 3600.0,
    "daily": 86400.0,
    "utc_daily": 86400.0,
    "monthly": 28 * 86400.0,
    "monthly_mean": 28 * 86400.0,
}

_EXT_PROVIDER = "rdh.provider"
_EXT_DATASET_NAME = "rdh.dataset_name"
_EXT_UNITS_SOURCE = "rdh.units_source"
_EXT_FREQUENCY = "rdh.frequency"

_MSG_MISSING = "缺少必填字段"
_MSG_INVALID = "取值无效"

type _Path = tuple[object, ...]
type _LineMap = dict[_Path, int]
"""YAML 路径 → 行号（1 起）。"""


class RequestLoadError(RdhError):
    """需求文件无法加载或不符合契约。``detail["errors"]`` 是逐条定位信息。"""

    default_code = "RDH_REQUEST"


@dataclass(frozen=True)
class LoadedRequest:
    """一次需求加载的结果：契约对象 + 非致命 warning（用于 CLI 展示与审计）。"""

    request: DatasetRequest
    source: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示（``rdh request validate`` 的输出数据源）。"""
        return {
            "source": self.source,
            "request_id": self.request.request_id,
            "dataset_id": self.request.dataset_id,
            "variables": [spec.name for spec in self.request.metadata.variables],
            "temporal": {
                "start": self.request.metadata.temporal_coverage.start.isoformat(),
                "end": self.request.metadata.temporal_coverage.end.isoformat(),
            },
            "time_definition": self.request.metadata.time_definition,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _render_path(path: _Path) -> str:
    """把路径元组渲染成 ``a.b[0].c`` 形式（用 YAML 侧的名字，不是 pydantic loc）。"""
    if not path:
        return "<root>"
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _walk_node(node: object, path: _Path, lines: _LineMap) -> None:
    """递归遍历 YAML 节点树，记录每个映射键/序列元素的行号。"""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = getattr(key_node, "value", None)
            if not isinstance(key, str):
                continue
            child = (*path, key)
            lines[child] = int(key_node.start_mark.line) + 1
            _walk_node(value_node, child, lines)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            child = (*path, index)
            lines[child] = int(item.start_mark.line) + 1
            _walk_node(item, child, lines)


def _compose_line_map(text: str) -> _LineMap:
    """用 ``yaml.compose`` 建路径→行号映射（不重复解析文档语义）。"""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        # 语法错误由 safe_load 那条路径负责报（带 mark），这里退化为空映射。
        return {}
    lines: _LineMap = {}
    _walk_node(root, (), lines)
    return lines


def _line_for(path: _Path, line_map: _LineMap) -> int | None:
    """取路径自身或其最近祖先的行号。"""
    candidate = path
    while candidate:
        line = line_map.get(candidate)
        if line is not None:
            return line
        candidate = candidate[:-1]
    return None


def _identity_origins(node: object, path: _Path = ()) -> dict[_Path, _Path]:
    """扁平格式：payload 路径与 YAML 路径一致。"""
    origins: dict[_Path, _Path] = {path: path} if path else {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str):
                origins.update(_identity_origins(value, (*path, key)))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            origins.update(_identity_origins(item, (*path, index)))
    return origins


def _mapping(value: object, *, source: str, path: _Path) -> Mapping[str, object]:
    """断言 ``value`` 是键为字符串的映射，否则抛带定位的需求错误。"""
    if not isinstance(value, Mapping):
        raise RequestLoadError(
            f"{source}: 段 {_render_path(path)} 必须是映射（key: value）",
            detail={"source": source, "field": _render_path(path)},
        )
    bad_keys = [key for key in value if not isinstance(key, str)]
    if bad_keys:
        raise RequestLoadError(
            f"{source}: 段 {_render_path(path)} 的键必须是字符串，发现 {bad_keys!r}",
            detail={"source": source, "field": _render_path(path)},
        )
    return cast("Mapping[str, object]", value)


def _sequence(value: object, *, source: str, path: _Path) -> Sequence[object]:
    """断言 ``value`` 是序列（且不是字符串），否则抛带定位的需求错误。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RequestLoadError(
            f"{source}: 字段 {_render_path(path)} 必须是列表",
            detail={"source": source, "field": _render_path(path)},
        )
    return value


def _check_unknown_keys(
    section: Mapping[str, object], allowed: frozenset[str], *, source: str, path: _Path
) -> None:
    """拒绝未知字段：拼写错误必须暴露，不能被静默忽略。"""
    unknown = sorted(str(key) for key in section if key not in allowed)
    if unknown:
        raise RequestLoadError(
            f"{source}: 段 {_render_path(path)} 含未知字段 {unknown}；"
            f"允许的字段：{sorted(allowed)}",
            detail={
                "source": source,
                "section": _render_path(path),
                "unknown": unknown,
                "allowed": sorted(allowed),
            },
        )


def _to_datetime(value: object, *, source: str, path: _Path, end_date_expands: bool) -> datetime:
    """把 YAML 时间值规整为 tz-aware UTC datetime。

    * 日期（``2011-12-31``）→ 当日 00:00 UTC；``end`` 再加一天（闭开区间）。
    * 带时区的 datetime 原样转 UTC；naive datetime 直接拒绝（时间必须可审计）。
    """
    moment: datetime
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise RequestLoadError(
                f"{source}: 字段 {_render_path(path)} 缺少时区；"
                "请写 2011-01-01T00:00:00Z 或纯日期 2011-01-01",
                detail={"source": source, "field": _render_path(path)},
            )
        moment = value
    elif isinstance(value, date):
        # 纯日期按 UTC 00:00 解释（不猜时区）；end 日期再加一天凑成闭开区间。
        moment = datetime(value.year, value.month, value.day, tzinfo=UTC)
        if end_date_expands:
            moment += timedelta(days=1)
        return moment
    elif isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                parsed_date = date.fromisoformat(text)
                moment = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
                if end_date_expands:
                    moment += timedelta(days=1)
                return moment
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RequestLoadError(
                f"{source}: 字段 {_render_path(path)} 不是合法时间"
                f"（期望 ISO 8601，如 2011-12-31 或 2011-12-31T00:00:00Z）：{value!r}",
                detail={"source": source, "field": _render_path(path), "value": value},
            ) from exc
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise RequestLoadError(
                f"{source}: 字段 {_render_path(path)} 缺少时区；"
                "请写 2011-01-01T00:00:00Z 或纯日期 2011-01-01",
                detail={"source": source, "field": _render_path(path)},
            )
    else:
        raise RequestLoadError(
            f"{source}: 字段 {_render_path(path)} 必须是日期/时间，实际是 {type(value).__name__}",
            detail={"source": source, "field": _render_path(path)},
        )
    return moment.astimezone(UTC)


def _normalize_variable(
    item: object,
    *,
    source: str,
    index: int,
    units_catalog: Mapping[str, str] | None,
    warnings: list[str],
) -> tuple[dict[str, object], str]:
    """归一化一个变量声明；返回 ``(payload, units_source)``。"""
    path: _Path = ("variables", index)
    if isinstance(item, str):
        name = item.strip()
        if not name:
            raise RequestLoadError(
                f"{source}: variables[{index}] 是空字符串", detail={"source": source}
            )
        declared_units: str | None = None
        extra: dict[str, object] = {}
    else:
        item_map = _mapping(item, source=source, path=path)
        _check_unknown_keys(item_map, _VARIABLE_KEYS, source=source, path=path)
        name = str(item_map.get("name", "")).strip()
        if not name:
            raise RequestLoadError(
                f"{source}: variables[{index}] 缺少 name；"
                "要么写成 name: xxx 映射，要么直接写变量名字符串",
                detail={"source": source, "field": _render_path(path)},
            )
        raw_units = item_map.get("units")
        declared_units = None if raw_units is None else str(raw_units).strip()
        extra = {str(key): value for key, value in item_map.items() if key != "units"}

    if declared_units is None or declared_units == "":
        catalog_units = None if units_catalog is None else units_catalog.get(name)
        if catalog_units is not None and catalog_units.strip():
            declared_units = catalog_units.strip()
            units_source = "caller_supplied"
        else:
            declared_units = "unknown"
            units_source = "unspecified_in_request"
            warnings.append(
                f"变量 {name} 未声明 units；科学等价性判定将按 'unknown' 处理"
                "（请求有责任声明单位，loader 不做猜测）"
            )
    else:
        units_source = "declared_in_request"
    payload: dict[str, object] = {**extra, "name": name, "units": declared_units}
    return payload, units_source


def _normalize_spatial(raw: object, *, source: str) -> dict[str, object]:
    """归一化 spatial 段：``type: global`` 或 ``bbox: {...}``。"""
    spatial = _mapping(raw, source=source, path=("spatial",))
    _check_unknown_keys(spatial, _SPATIAL_KEYS, source=source, path=("spatial",))
    spatial_type = spatial.get("type")
    bbox = spatial.get("bbox")
    if bbox is not None and spatial_type not in (None, "bbox"):
        raise RequestLoadError(
            f"{source}: spatial 同时给了 type={spatial_type!r} 与 bbox，二者只能取一个",
            detail={"source": source, "section": "spatial"},
        )
    if bbox is not None or spatial_type == "bbox":
        bbox_map = _mapping(bbox, source=source, path=("spatial", "bbox"))
        _check_unknown_keys(bbox_map, _BBOX_KEYS, source=source, path=("spatial", "bbox"))
        missing = sorted({"west", "east", "south", "north"} - set(bbox_map))
        if missing:
            raise RequestLoadError(
                f"{source}: spatial.bbox 缺少字段 {missing}",
                detail={"source": source, "missing": missing},
            )
        return dict(bbox_map)
    if spatial_type != "global":
        raise RequestLoadError(
            f"{source}: spatial.type 只支持 'global'（或提供 bbox）；实际 {spatial_type!r}",
            detail={"source": source, "observed": spatial_type},
        )
    if any(key in spatial for key in _BBOX_KEYS):
        raise RequestLoadError(
            f"{source}: spatial.type=global 时不要再给经纬度字段；需要局部区域请用 bbox",
            detail={"source": source, "section": "spatial"},
        )
    return {"west": -180.0, "east": 180.0, "south": -90.0, "north": 90.0}


def _normalize_vertical(raw: object, *, source: str) -> dict[str, object]:
    """归一化 vertical 段：levels 统一降序，重复层报错。"""
    vertical = _mapping(raw, source=source, path=("vertical",))
    _check_unknown_keys(vertical, _VERTICAL_KEYS, source=source, path=("vertical",))
    kind_text = str(vertical.get("kind", "pressure")).strip().lower()
    try:
        kind = VerticalLevelKind(kind_text)
    except ValueError as exc:
        allowed = [item.value for item in VerticalLevelKind]
        raise RequestLoadError(
            f"{source}: vertical.kind 取值无效 {kind_text!r}；允许：{allowed}",
            detail={"source": source, "observed": kind_text, "allowed": allowed},
        ) from exc
    raw_levels = vertical.get("levels")
    units = vertical.get("units")
    if kind is VerticalLevelKind.NONE:
        if raw_levels not in (None, []):
            raise RequestLoadError(
                f"{source}: vertical.kind=none 时不能给 levels",
                detail={"source": source, "section": "vertical"},
            )
        return {"kind": kind.value, "levels": []}
    if raw_levels is None:
        raise RequestLoadError(
            f"{source}: vertical.kind={kind.value} 时必须提供 levels 列表",
            detail={"source": source, "section": "vertical"},
        )
    levels_seq = _sequence(raw_levels, source=source, path=("vertical", "levels"))
    if not levels_seq:
        raise RequestLoadError(
            f"{source}: vertical.levels 不能为空；单层数据请写 kind: none",
            detail={"source": source, "section": "vertical.levels"},
        )
    numeric: list[float] = []
    for index, level in enumerate(levels_seq):
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise RequestLoadError(
                f"{source}: vertical.levels[{index}] 必须是数字，实际是 {type(level).__name__}",
                detail={"source": source, "index": index},
            )
        numeric.append(float(level))
    if len(set(numeric)) != len(numeric):
        raise RequestLoadError(
            f"{source}: vertical.levels 含重复层 {sorted(numeric)}；重复层会让下游无法判断语义",
            detail={"source": source, "levels": numeric},
        )
    normalized = sorted(numeric, reverse=True)
    if kind is VerticalLevelKind.PRESSURE:
        if units not in (None, "hPa"):
            raise RequestLoadError(
                f"{source}: vertical.kind=pressure 时 units 只能是 hPa（实际 {units!r}）",
                detail={"source": source, "units": units},
            )
        return {"kind": kind.value, "levels": normalized, "units": "hPa"}
    payload: dict[str, object] = {"kind": kind.value, "levels": normalized}
    if units is not None:
        payload["units"] = str(units)
    return payload


def _check_frequency(*, frequency: str, start: datetime, end: datetime, source: str) -> None:
    """识别 frequency 与时间区间不匹配（含最小区间、月对齐）。"""
    step = _FREQUENCY_STEP_SECONDS.get(frequency)
    span = (end - start).total_seconds()
    if span <= 0:
        raise RequestLoadError(
            f"{source}: 字段 temporal.end 早于或等于 temporal.start"
            f"（start={start.isoformat()}，end={end.isoformat()}）；要求 start < end",
            detail={
                "source": source,
                "field": "temporal.end",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "errors": [
                    f"{source}: 字段 temporal.end 早于或等于 temporal.start"
                    f"（start={start.isoformat()}，end={end.isoformat()}）"
                ],
            },
        )
    if step is None:
        return
    if frequency in {"monthly", "monthly_mean"}:
        if start.day != 1 or start.hour != 0:
            raise RequestLoadError(
                f"{source}: frequency={frequency} 要求 start 是月初 00:00 UTC"
                f"（实际 {start.isoformat()}）",
                detail={"source": source, "start": start.isoformat()},
            )
        if end.day != 1:
            raise RequestLoadError(
                f"{source}: frequency={frequency} 要求 end 落在月初（闭开区间），"
                f"实际 {end.isoformat()}",
                detail={"source": source, "end": end.isoformat()},
            )
    if span < step:
        raise RequestLoadError(
            f"{source}: frequency={frequency} 与时间区间不匹配：跨度 {span:.0f} 秒"
            f" < 一个时间步 {step:.0f} 秒",
            detail={
                "source": source,
                "frequency": frequency,
                "span_seconds": span,
                "step_seconds": step,
            },
        )
    remainder = span % step
    if remainder != 0:
        raise RequestLoadError(
            f"{source}: 时间区间长度 {span:.0f} 秒不是 frequency={frequency} 的整数倍"
            f"（余 {remainder:.0f} 秒）；请调整 end 或 frequency",
            detail={
                "source": source,
                "frequency": frequency,
                "span_seconds": span,
                "step_seconds": step,
                "remainder_seconds": remainder,
            },
        )


# --------------------------------------------------------------------------- #
# 两种格式的归一化
# --------------------------------------------------------------------------- #
def _normalize_flat(
    document: Mapping[str, object], *, source: str
) -> tuple[dict[str, object], dict[_Path, _Path], list[str]]:
    """扁平格式：几乎原样交给 pydantic，只补 ``schema_version``。"""
    payload: dict[str, object] = {}
    for key, value in document.items():
        payload[key] = value
    _check_unknown_keys(document, _FLAT_KEYS, source=source, path=())
    payload.setdefault("schema_version", "1.0")
    return payload, _identity_origins(payload), []


def _normalize_sectioned(
    document: Mapping[str, object],
    *,
    source: str,
    units_catalog: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[_Path, _Path], list[str]]:
    """分段格式：把 §6.2 各段组装成 ``DatasetRequest`` payload。"""
    unknown = sorted(key for key in document if key not in _SECTION_KEYS)
    if unknown:
        raise RequestLoadError(
            f"{source}: 未知的顶层段 {unknown}；允许的段：{sorted(_SECTION_KEYS)}",
            detail={"source": source, "unknown": unknown, "allowed": sorted(_SECTION_KEYS)},
        )

    warnings: list[str] = []
    payload: dict[str, object] = {"schema_version": "1.0"}
    origins: dict[_Path, _Path] = {}

    # ---- request 段 --------------------------------------------------- #
    request_section = _mapping(document.get("request"), source=source, path=("request",))
    _check_unknown_keys(request_section, _REQUEST_KEYS, source=source, path=("request",))
    if "name" in request_section:
        payload["request_id"] = request_section["name"]
    if "priority" in request_section:
        payload["priority"] = request_section["priority"]
    if "preferred_sources" in request_section:
        payload["preferred_sources"] = request_section["preferred_sources"]
    if "max_sources" in request_section:
        payload["max_sources"] = request_section["max_sources"]
    origins[("request_id",)] = ("request", "name")
    origins[("priority",)] = ("request", "priority")
    origins[("preferred_sources",)] = ("request", "preferred_sources")
    origins[("max_sources",)] = ("request", "max_sources")
    origins[("metadata",)] = ("dataset",)
    request_extensions: dict[str, object] = dict(
        _mapping(
            request_section.get("extensions", {}),
            source=source,
            path=("request", "extensions"),
        )
    )

    # ---- dataset 段 ---------------------------------------------------- #
    dataset_section = _mapping(document.get("dataset"), source=source, path=("dataset",))
    _check_unknown_keys(dataset_section, _DATASET_KEYS, source=source, path=("dataset",))
    dataset_name = dataset_section.get("name")
    if not isinstance(dataset_name, str) or not dataset_name.strip():
        raise RequestLoadError(
            f"{source}: dataset.name 必填（数据集的逻辑标识，如 reanalysis-era5-pressure-levels）",
            detail={"source": source, "section": "dataset"},
        )
    payload["dataset_id"] = dataset_name.strip()
    origins[("dataset_id",)] = ("dataset", "name")
    dataset_extensions: dict[str, object] = {_EXT_DATASET_NAME: dataset_name.strip()}
    provider = dataset_section.get("provider")
    if provider is not None:
        dataset_extensions[_EXT_PROVIDER] = str(provider)
    product_family = dataset_section.get("product_family") or dataset_name
    metadata: dict[str, object] = {
        "product": str(product_family).strip(),
        "product_type": str(dataset_section.get("product", "unknown")).strip(),
        "data_version": str(dataset_section.get("version", "unknown")).strip(),
    }
    origins[("metadata", "product")] = ("dataset", "product_family")
    origins[("metadata", "product_type")] = ("dataset", "product")
    origins[("metadata", "data_version")] = ("dataset", "version")

    # ---- variables 段 -------------------------------------------------- #
    raw_variables = _sequence(document.get("variables"), source=source, path=("variables",))
    if not raw_variables:
        raise RequestLoadError(
            f"{source}: 段 variables 不能为空；至少声明一个科学变量",
            detail={"source": source, "section": "variables"},
        )
    variables: list[dict[str, object]] = []
    units_sources: set[str] = set()
    origins[("metadata", "variables")] = ("variables",)
    for index, item in enumerate(raw_variables):
        normalized, units_source = _normalize_variable(
            item,
            source=source,
            index=index,
            units_catalog=units_catalog,
            warnings=warnings,
        )
        variables.append(normalized)
        units_sources.add(units_source)
        origins[("metadata", "variables", index)] = ("variables", index)
    metadata["variables"] = variables
    if "unspecified_in_request" in units_sources:
        dataset_extensions[_EXT_UNITS_SOURCE] = "unspecified_in_request"
    elif "caller_supplied" in units_sources:
        dataset_extensions[_EXT_UNITS_SOURCE] = "caller_supplied"

    # ---- temporal 段 --------------------------------------------------- #
    temporal = _mapping(document.get("temporal"), source=source, path=("temporal",))
    _check_unknown_keys(temporal, _TEMPORAL_KEYS, source=source, path=("temporal",))
    if "start" not in temporal or "end" not in temporal:
        raise RequestLoadError(
            f"{source}: temporal 段必须同时提供 start 与 end",
            detail={"source": source, "section": "temporal"},
        )
    start = _to_datetime(
        temporal["start"], source=source, path=("temporal", "start"), end_date_expands=False
    )
    end = _to_datetime(
        temporal["end"], source=source, path=("temporal", "end"), end_date_expands=True
    )
    frequency = temporal.get("frequency")
    if not isinstance(frequency, str) or not frequency.strip():
        raise RequestLoadError(
            f"{source}: temporal.frequency 必填（hourly / daily / monthly / ...）",
            detail={"source": source, "section": "temporal"},
        )
    frequency = frequency.strip().lower()
    _check_frequency(frequency=frequency, start=start, end=end, source=source)
    metadata["temporal_coverage"] = {"start": start, "end": end}
    metadata["time_definition"] = frequency
    dataset_extensions[_EXT_FREQUENCY] = frequency
    origins[("metadata", "temporal_coverage")] = ("temporal",)
    origins[("metadata", "temporal_coverage", "start")] = ("temporal", "start")
    origins[("metadata", "temporal_coverage", "end")] = ("temporal", "end")
    origins[("metadata", "time_definition")] = ("temporal", "frequency")

    # ---- spatial 段 ---------------------------------------------------- #
    spatial = _normalize_spatial(document.get("spatial"), source=source)
    metadata["spatial_extent"] = spatial
    origins[("metadata", "spatial_extent")] = ("spatial",)
    spatial_raw = document.get("spatial")
    nested_in_bbox = isinstance(spatial_raw, Mapping) and "bbox" in spatial_raw
    for key in spatial:
        origins[("metadata", "spatial_extent", key)] = (
            ("spatial", "bbox", key) if nested_in_bbox else ("spatial", key)
        )

    # ---- vertical 段（可选；缺省 = 单层/地表） -------------------------- #
    if document.get("vertical") is not None:
        vertical = _normalize_vertical(document.get("vertical"), source=source)
        metadata["vertical_levels"] = vertical
        origins[("metadata", "vertical_levels")] = ("vertical",)
        origins[("metadata", "vertical_levels", "kind")] = ("vertical", "kind")
        origins[("metadata", "vertical_levels", "levels")] = ("vertical", "levels")
        origins[("metadata", "vertical_levels", "units")] = ("vertical", "units")

    metadata["extensions"] = dataset_extensions
    payload["metadata"] = metadata

    # ---- storage / validation / 顶层 extensions ------------------------ #
    if document.get("storage") is not None:
        storage = _mapping(document.get("storage"), source=source, path=("storage",))
        _check_unknown_keys(storage, _STORAGE_KEYS, source=source, path=("storage",))
        if "preserve_original" in storage:
            payload["preserve_original"] = storage["preserve_original"]
            origins[("preserve_original",)] = ("storage", "preserve_original")

    if document.get("validation") is not None:
        validation = _mapping(document.get("validation"), source=source, path=("validation",))
        _check_unknown_keys(validation, _VALIDATION_KEYS, source=source, path=("validation",))
        for key in _VALIDATION_KEYS:
            if key in validation and not isinstance(validation[key], bool):
                raise RequestLoadError(
                    f"{source}: validation.{key} 必须是 true/false",
                    detail={"source": source, "section": f"validation.{key}"},
                )
        checks: list[IntegrityCheckKind] = []
        if validation.get("verify_size") is True:
            checks.append(IntegrityCheckKind.SIZE_MATCH)
        if validation.get("calculate_sha256") is True:
            checks.append(IntegrityCheckKind.CHECKSUM_MATCH)
        if validation.get("netcdf_openable") is True:
            checks.append(IntegrityCheckKind.NETCDF_OPENABLE)
        if not checks:
            raise RequestLoadError(
                f"{source}: validation 段至少要开启一项检查"
                "（verify_size / calculate_sha256 / netcdf_openable）；"
                "全为 false 会让校验状态成为 UNKNOWN，任务永远无法 COMPLETED",
                detail={"source": source, "section": "validation"},
            )
        payload["checks"] = checks
        origins[("checks",)] = ("validation",)

    top_extensions = document.get("extensions")
    if top_extensions is not None:
        merged = dict(request_extensions)
        merged.update(_mapping(top_extensions, source=source, path=("extensions",)))
        request_extensions = merged
    if request_extensions:
        payload["extensions"] = request_extensions
        origins[("extensions",)] = ("request", "extensions")

    return payload, origins, warnings


def _normalize(
    document: Mapping[str, object],
    *,
    source: str,
    units_catalog: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[_Path, _Path], list[str]]:
    """识别格式并归一化为 ``DatasetRequest`` payload + 路径映射 + warnings。"""
    has_metadata = "metadata" in document
    has_sections = any(key in document for key in _STRUCTURAL_SECTION_KEYS)
    if has_metadata and has_sections:
        raise RequestLoadError(
            f"{source}: 不能混用扁平格式（metadata）与分段格式"
            "（request/dataset/variables/temporal/spatial/vertical/storage/validation）",
            detail={"source": source, "conflicting": ["metadata", "request/dataset/..."]},
        )
    if has_metadata:
        return _normalize_flat(document, source=source)
    if has_sections:
        return _normalize_sectioned(document, source=source, units_catalog=units_catalog)
    raise RequestLoadError(
        f"{source}: 无法识别需求格式；需要 metadata（扁平格式）或 "
        "request/dataset/variables/temporal/spatial 段（分段格式）",
        detail={"source": source, "allowed_sections": sorted(_SECTION_KEYS)},
    )


# --------------------------------------------------------------------------- #
# Pydantic 错误 → 文件:行 + 字段路径
# --------------------------------------------------------------------------- #
def _resolve_yaml_path(loc: _Path, origins: Mapping[_Path, _Path]) -> _Path:
    """把 pydantic 的 payload 路径翻译回 YAML 路径（取最长已登记的祖先）。"""
    candidate = loc
    while candidate:
        mapped = origins.get(candidate)
        if mapped is not None:
            return (*mapped, *loc[len(candidate) :])
        candidate = candidate[:-1]
    return loc


def _chinese_prefix(error_type: str, message: str) -> str:
    """把常见 pydantic 错误类型翻译成中文前缀（细节保留英文便于工程检索）。"""
    if error_type == "missing" or message.startswith("Field required"):
        return _MSG_MISSING
    return _MSG_INVALID


def _format_errors(
    exc: ValidationError, *, source: str, line_map: _LineMap, origins: Mapping[_Path, _Path]
) -> list[str]:
    """把 pydantic 错误逐条格式化成 ``文件:行: 字段 …`` 的中文说明。"""
    messages: list[str] = []
    for error in exc.errors():
        raw_loc = error.get("loc", ())
        loc: _Path = tuple(raw_loc) if isinstance(raw_loc, (tuple, list)) else ()
        yaml_path = _resolve_yaml_path(loc, origins)
        line = _line_for(yaml_path, line_map)
        prefix = _chinese_prefix(str(error.get("type", "")), str(error.get("msg", "")))
        location = f"{source}:{line}" if line is not None else source
        messages.append(
            f"{location}: 字段 {_render_path(yaml_path)} {prefix}（{error.get('msg', '')}）"
        )
    return messages


def _load_detailed(
    document: Mapping[str, object],
    *,
    source: str,
    line_map: _LineMap,
    units_catalog: Mapping[str, str] | None,
) -> LoadedRequest:
    """内部入口：归一化 + 校验 + warning 汇总。"""
    payload, origins, warnings = _normalize(document, source=source, units_catalog=units_catalog)
    try:
        request = DatasetRequest.model_validate(payload)
    except ValidationError as exc:
        errors = _format_errors(exc, source=source, line_map=line_map, origins=origins)
        summary = errors[0] if errors else f"{source}: 需求校验失败"
        more = f"（共 {len(errors)} 条错误）" if len(errors) > 1 else ""
        raise RequestLoadError(
            f"{summary}{more}", detail={"source": source, "errors": errors}
        ) from exc
    return LoadedRequest(request=request, source=source, warnings=tuple(warnings))


def load_request(
    path: str | Path, *, units_catalog: Mapping[str, str] | None = None
) -> DatasetRequest:
    """读取 YAML 需求文件并校验为 :class:`DatasetRequest`（不返回 warnings）。

    Raises
    ------
    RequestLoadError
        文件不存在、不可读、YAML 语法错误或契约校验失败。

    Notes
    -----
    需要 warning（如变量未声明 units）时用 :func:`load_request_detailed`。
    """
    return load_request_detailed(path, units_catalog=units_catalog).request


def load_request_text(
    text: str,
    *,
    source: str = "<yaml>",
    units_catalog: Mapping[str, str] | None = None,
) -> DatasetRequest:
    """从 YAML 文本加载需求（不返回 warnings）。"""
    return load_request_text_detailed(text, source=source, units_catalog=units_catalog).request


def load_request_text_detailed(
    text: str,
    *,
    source: str = "<yaml>",
    units_catalog: Mapping[str, str] | None = None,
) -> LoadedRequest:
    """从 YAML 文本加载需求，返回附带 warning 的 :class:`LoadedRequest`。"""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = int(mark.line) + 1 if mark is not None else None
        location = f"{source}:{line}" if line is not None else source
        raise RequestLoadError(
            f"{location}: YAML 语法错误：{exc}",
            detail={"source": source, "line": line, "problem": str(exc)},
        ) from exc
    if loaded is None:
        raise RequestLoadError(f"{source}: 需求文件为空", detail={"source": source})
    if not isinstance(loaded, Mapping):
        raise RequestLoadError(
            f"{source}: 需求文件顶层必须是映射，实际是 {type(loaded).__name__}",
            detail={"source": source},
        )
    line_map = _compose_line_map(text)
    return _load_detailed(
        cast("Mapping[str, object]", loaded),
        source=source,
        line_map=line_map,
        units_catalog=units_catalog,
    )


def load_request_detailed(
    path: str | Path, *, units_catalog: Mapping[str, str] | None = None
) -> LoadedRequest:
    """读取 YAML 需求文件，返回 :class:`LoadedRequest`（含 warnings 与源路径）。"""
    request_path = Path(path).expanduser()
    if not request_path.is_file():
        raise RequestLoadError(
            f"需求文件不存在或不是普通文件：{request_path}",
            detail={"path": str(request_path)},
        )
    try:
        text = request_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RequestLoadError(
            f"无法读取需求文件 {request_path}：{exc}",
            detail={"path": str(request_path), "reason": str(exc)},
        ) from exc
    return load_request_text_detailed(text, source=str(request_path), units_catalog=units_catalog)


def parse_request(
    document: Mapping[str, object],
    *,
    source: str = "<memory>",
    units_catalog: Mapping[str, str] | None = None,
) -> DatasetRequest:
    """从已解析的映射构造需求（程序化调用/测试用，无行号但有字段路径）。"""
    return _load_detailed(document, source=source, line_map={}, units_catalog=units_catalog).request
