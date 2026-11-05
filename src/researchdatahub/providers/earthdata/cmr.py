"""CMR（Common Metadata Repository）响应解析：纯函数，输入是**真实响应样本**。

依据的真实响应（2026-11-05 用 ``curl`` 抓取，样例存于
``tests/unit/test_providers/fixtures/``）
---------------------------------------------------------------------
* ``GET /search/collections.json`` —— 顶层为 ``{"feed": {...}}``，``feed.entry`` 是列表；
  条目字段实测含 ``id``（concept-id，如 ``C2179081499-GES_DISC``）、``short_name``、``title``、
  ``summary``、``data_center``、``time_start``、``time_end``、``boxes``（``"S W N E"`` 字符串）、
  ``version_id``、``processing_level_id``、``original_format``、``has_variables``、``links``。
  **注意**：``entry`` 里**没有** ``concept-id`` 字段（实测为 ``null``），concept-id 就是 ``id``。
* ``GET /search/collections.umm_json`` —— 顶层 ``{"hits", "took", "items": [{"meta", "umm"}]}``；
  ``umm`` 含 ``ShortName`` / ``EntryTitle`` / ``Abstract`` / ``Version`` / ``TemporalExtents``
  （``RangeDateTimes[].BeginningDateTime/EndingDateTime``）/ ``SpatialExtent.HorizontalSpatialDomain
  .Geometry.BoundingRectangles[].{West,South,East,North}BoundingCoordinate`` /
  ``ArchiveAndDistributionInformation.FileArchiveInformation[].Format`` /
  ``RelatedUrls[].{URL,Type,Subtype}`` / ``DataCenters[].ShortName`` / ``DOI.DOI`` /
  ``ProcessingLevel.Id`` / ``UseConstraints.Description`` / ``CollectionCitations[]``。
  实测 ``SpatialExtents``（复数）为 ``null``，**空间范围在 ``SpatialExtent``（单数）**里。
* ``GET /search/granules.json`` —— ``feed.entry[]`` 含 ``id``（granule concept-id）、
  ``title``、``granule_size``（**字符串形式的 MB**，如 ``"823.5269718170166"``）、
  ``time_start`` / ``time_end``（ISO8601 带毫秒与 ``Z``）、``links``（``rel`` 形如
  ``http://esipfed.org/ns/fedsearch/1.1/data#``，另有 ``s3#`` / ``service#`` / ``metadata#``）、
  ``collection_concept_id``、``producer_granule_id``、``original_format``（``ECHO10``）。
  下载链接的判据：``rel`` 以 ``data#`` 结尾且 ``href`` 以 ``https://`` 开头；``inherited=True``
  的条目是集合级继承链接（``virtual-directory`` / ``search.earthdata.nasa.gov``），**不是文件**。
* ``GET /search/variables.umm_json`` —— ``items[].umm`` 含 ``Name`` / ``LongName`` /
  ``Definition`` / ``Units`` / ``DataType`` / ``FillValues``。
* 错误形状：参数非法时 ``400 {"errors": ["page_size must be a number between 0 and 2000"]}``；
  不存在的 concept-id **不报错**，返回空 ``feed.entry``（``200``，实测 2026-11-05）。

本模块**只解析，不发请求**：便于离线单测覆盖真实形状。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from researchdatahub.core.errors import MetadataError
from researchdatahub.core.models import SpatialExtent, TemporalExtent
from researchdatahub.providers._metadata import (
    build_spatial_extent,
    build_temporal_extent,
    parse_timestamp,
)
from researchdatahub.providers.base import DatasetCandidate, DatasetVariable

__all__ = [
    "CMR_DATA_REL_SUFFIX",
    "CMR_GRANULE_SIZE_UNIT",
    "CMR_S3_REL_SUFFIX",
    "CmrCollection",
    "CmrFileLink",
    "CmrGranule",
    "UmmCollectionDetail",
    "candidate_from_collection",
    "granule_size_bytes",
    "parse_collection_entries",
    "parse_granule_entries",
    "parse_umm_collection",
    "parse_umm_variables",
    "pick_download_link",
]

CMR_DATA_REL_SUFFIX: Final[str] = "data#"
"""文件下载链接的 ``rel`` 后缀（实测 ``http://esipfed.org/ns/fedsearch/1.1/data#``）。"""

CMR_S3_REL_SUFFIX: Final[str] = "s3#"
"""S3 直连链接的 ``rel`` 后缀；本 Provider **只声明、不解析为可下载资产**（unsupported）。"""

CMR_GRANULE_SIZE_UNIT: Final[str] = "MB"
"""``granule_size`` 的单位（实测字符串 ``"823.5269718170166"``，与 HEAD 的 MB 量级一致）。"""

_MB_TO_BYTES: Final[int] = 1024 * 1024


# --------------------------------------------------------------------------- #
# 小工具（不抛错，缺字段一律视为"未知"）
# --------------------------------------------------------------------------- #
def _text(value: object) -> str | None:
    """把远端值规整为去空白的字符串；非字符串/空白返回 ``None``。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_mapping(value: object) -> Mapping[str, Any]:
    """把远端值收窄为映射（非映射返回空映射，缺字段按"未知"处理）。"""
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> Sequence[Any]:
    """把远端值收窄为序列（字符串不算序列，避免逐字符遍历）。"""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def granule_size_bytes(raw: object) -> int | None:
    """把 CMR ``granule_size``（MB 的字符串/数字）换算为字节；无法解析返回 ``None``。

    实测 ``"823.5269718170166"`` → ``863_533_236`` 字节（1024² 进位）。
    这是**远端声明值**，未与真实字节核验；调用方必须标为"catalog 声明值"。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        megabytes = float(raw)
    elif isinstance(raw, str):
        try:
            megabytes = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if megabytes < 0:
        return None
    return round(megabytes * _MB_TO_BYTES)


def _parse_boxes(raw: object) -> SpatialExtent | None:
    """解析 ``boxes``（列表，每项 ``"S W N E"``，单位度）；多框取**外包框**。

    ``boxes`` 实测出现在搜索结果语境，因此 ``inspect`` 优先使用 UMM_JSON 的
    ``SpatialExtent``；本函数只给 ``search`` 阶段提供尽力而为的范围。
    """
    norths: list[float] = []
    souths: list[float] = []
    wests: list[float] = []
    easts: list[float] = []
    for item in _as_sequence(raw):
        if not isinstance(item, str):
            continue
        parts = item.split()
        if len(parts) != 4:
            continue
        try:
            south, west, north, east = (float(part) for part in parts)
        except ValueError:
            continue
        souths.append(south)
        wests.append(west)
        norths.append(north)
        easts.append(east)
    if not norths:
        return None
    return build_spatial_extent(
        north=max(norths), south=min(souths), west=min(wests), east=max(easts)
    )


# --------------------------------------------------------------------------- #
# 集合（collections.json）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CmrCollection:
    """``collections.json`` 的一条集合条目（字段均来自真实响应）。"""

    concept_id: str
    short_name: str | None
    title: str
    abstract: str | None
    provider_id: str | None
    time_start: datetime | None
    time_end: datetime | None
    spatial: SpatialExtent | None
    version_id: str | None
    processing_level: str | None
    original_format: str | None
    has_variables: bool
    cloud_hosted: bool
    landing_url: str | None
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)

    @property
    def dataset_id(self) -> str:
        """Provider 内稳定的数据集 ID：CMR concept-id（如 ``C2179081499-GES_DISC``）。"""
        return self.concept_id

    def temporal_extent(self) -> TemporalExtent | None:
        """时间覆盖；任一端缺失返回 ``None``（不推断）。"""
        return build_temporal_extent(self.time_start, self.time_end)


def _landing_url(links: object) -> str | None:
    """从 ``links`` 中挑落地页 URL（``metadata#``、非 inherited、非文档服务器）。"""
    for item in _as_sequence(links):
        entry = _as_mapping(item)
        rel = _text(entry.get("rel")) or ""
        href = _text(entry.get("href"))
        if href is None or entry.get("inherited") is True:
            continue
        if rel.endswith("metadata#") and href.startswith("https://") and "docserver" not in href:
            return href
    return None


def _parse_entry(entry: Mapping[str, Any]) -> CmrCollection:
    """解析单条集合条目；``id`` 缺失视为形状错误（concept-id 是必需事实）。

    Raises
    ------
    MetadataError
        条目没有 ``id``。
    """
    concept_id = _text(entry.get("id"))
    if concept_id is None:
        raise MetadataError(
            "CMR collection entry has no 'id' (concept-id)",
            detail={"observed_keys": sorted(entry)[:20]},
        )
    title = _text(entry.get("title")) or _text(entry.get("dataset_id")) or concept_id
    return CmrCollection(
        concept_id=concept_id,
        short_name=_text(entry.get("short_name")),
        title=title,
        abstract=_text(entry.get("summary")),
        provider_id=_text(entry.get("data_center")),
        time_start=parse_timestamp(entry.get("time_start"), field="time_start"),
        time_end=parse_timestamp(entry.get("time_end"), field="time_end"),
        spatial=_parse_boxes(entry.get("boxes")),
        version_id=_text(entry.get("version_id")),
        processing_level=_text(entry.get("processing_level_id")),
        original_format=_text(entry.get("original_format")),
        has_variables=bool(entry.get("has_variables")),
        cloud_hosted=bool(entry.get("cloud_hosted")),
        landing_url=_landing_url(entry.get("links")),
        raw=entry,
    )


def parse_collection_entries(payload: Mapping[str, Any]) -> tuple[CmrCollection, ...]:
    """解析 ``collections.json`` 的 ``feed.entry``。

    Raises
    ------
    MetadataError
        顶层没有 ``feed``，或 ``feed.entry`` 不是列表。
    """
    feed = payload.get("feed")
    if not isinstance(feed, Mapping):
        raise MetadataError(
            "CMR collections response has no 'feed' object",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    entries = feed.get("entry", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise MetadataError(
            "CMR collections feed.entry is not a list",
            detail={"observed_type": type(feed.get("entry")).__name__},
        )
    return tuple(_parse_entry(_as_mapping(item)) for item in entries)


# --------------------------------------------------------------------------- #
# 集合详情（collections.umm_json）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UmmCollectionDetail:
    """``collections.umm_json`` 解析出的集合详情（只取已实测存在的字段）。"""

    concept_id: str
    short_name: str | None
    entry_title: str
    abstract: str | None
    version: str | None
    temporal: TemporalExtent | None
    spatial: SpatialExtent | None
    formats: tuple[str, ...]
    data_centers: tuple[str, ...]
    doi: str | None
    processing_level: str | None
    license_text: str | None
    citation: str | None
    landing_url: str | None
    variable_concept_ids: tuple[str, ...]


def _bounding_rectangles(umm: Mapping[str, Any]) -> SpatialExtent | None:
    """解析 ``SpatialExtent.HorizontalSpatialDomain.Geometry.BoundingRectangles``（多框取外包）。"""
    horizontal = _as_mapping(_as_mapping(umm.get("SpatialExtent")).get("HorizontalSpatialDomain"))
    geometry = _as_mapping(horizontal.get("Geometry"))
    norths: list[float] = []
    souths: list[float] = []
    wests: list[float] = []
    easts: list[float] = []
    for rect in geometry.get("BoundingRectangles") or []:
        body = _as_mapping(rect)
        try:
            south = float(body["SouthBoundingCoordinate"])
            west = float(body["WestBoundingCoordinate"])
            north = float(body["NorthBoundingCoordinate"])
            east = float(body["EastBoundingCoordinate"])
        except (KeyError, TypeError, ValueError):
            continue
        souths.append(south)
        wests.append(west)
        norths.append(north)
        easts.append(east)
    if not norths:
        return None
    return build_spatial_extent(
        north=max(norths), south=min(souths), west=min(wests), east=max(easts)
    )


def _temporal_from_umm(umm: Mapping[str, Any]) -> TemporalExtent | None:
    """解析 ``TemporalExtents[].RangeDateTimes[].{Beginning,Ending}DateTime``（多区间取外包）。"""
    starts: list[datetime] = []
    ends: list[datetime] = []
    for extent in _as_sequence(umm.get("TemporalExtents")):
        for window in _as_sequence(_as_mapping(extent).get("RangeDateTimes")):
            body = _as_mapping(window)
            start = parse_timestamp(body.get("BeginningDateTime"), field="BeginningDateTime")
            end = parse_timestamp(body.get("EndingDateTime"), field="EndingDateTime")
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
    if not starts or not ends:
        return None
    return build_temporal_extent(min(starts), max(ends))


def _formats_from_umm(umm: Mapping[str, Any]) -> tuple[str, ...]:
    """解析 ``ArchiveAndDistributionInformation.FileArchiveInformation[].Format``。"""
    formats: list[str] = []
    info = _as_mapping(umm.get("ArchiveAndDistributionInformation"))
    for item in _as_sequence(info.get("FileArchiveInformation")):
        fmt = _text(_as_mapping(item).get("Format"))
        if fmt is not None and fmt not in formats:
            formats.append(fmt)
    return tuple(formats)


def _landing_from_related_urls(umm: Mapping[str, Any]) -> str | None:
    """从 ``RelatedUrls`` 中挑落地页（``Type == 'DATA SET LANDING PAGE'``）。"""
    for item in _as_sequence(umm.get("RelatedUrls")):
        body = _as_mapping(item)
        kind = _text(body.get("Type")) or ""
        href = _text(body.get("URL"))
        if href is not None and kind == "DATA SET LANDING PAGE" and href.startswith("https://"):
            return href
    return None


def _citation_from_umm(umm: Mapping[str, Any]) -> str | None:
    """解析 ``CollectionCitations``：拼出 ``Creator. Title. Publisher. ReleaseDate``。

    只拼接**远端真实存在**的片段；全部缺失返回 ``None``（不编造引用）。
    """
    for item in _as_sequence(umm.get("CollectionCitations")):
        body = _as_mapping(item)
        pieces = [
            piece
            for piece in (
                _text(body.get("Creator")),
                _text(body.get("Title")),
                _text(body.get("Publisher")),
            )
            if piece
        ]
        if not pieces:
            continue
        text = ". ".join(pieces)
        release = _text(body.get("ReleaseDate"))
        return f"{text}. {release}" if release else text
    return None


def parse_umm_collection(payload: Mapping[str, Any]) -> UmmCollectionDetail:
    """解析 ``collections.umm_json`` 的 ``items[0]``。

    Raises
    ------
    MetadataError
        ``items`` 为空（concept-id 不存在时 CMR 返回 ``hits=0``）或 ``umm`` 缺失。
    """
    items = _as_sequence(payload.get("items"))
    if not items:
        raise MetadataError(
            "CMR returned no collection for the requested concept-id",
            detail={"hits": payload.get("hits")},
        )
    item = _as_mapping(items[0])
    umm = _as_mapping(item.get("umm"))
    if not umm:
        raise MetadataError("CMR UMM collection item has no 'umm' object", detail={})
    meta = _as_mapping(item.get("meta"))
    concept_id = _text(item.get("id")) or _text(meta.get("concept-id"))
    if concept_id is None:
        raise MetadataError("CMR UMM collection item has no concept-id", detail={})
    associations = _as_mapping(meta.get("associations"))
    variable_ids = tuple(
        text
        for text in (_text(value) for value in _as_sequence(associations.get("variables")))
        if text is not None
    )
    return UmmCollectionDetail(
        concept_id=concept_id,
        short_name=_text(umm.get("ShortName")),
        entry_title=_text(umm.get("EntryTitle")) or concept_id,
        abstract=_text(umm.get("Abstract")),
        version=_text(umm.get("Version")),
        temporal=_temporal_from_umm(umm),
        spatial=_bounding_rectangles(umm),
        formats=_formats_from_umm(umm),
        data_centers=tuple(
            text
            for text in (
                _text(_as_mapping(center).get("ShortName"))
                for center in _as_sequence(umm.get("DataCenters"))
            )
            if text is not None
        ),
        doi=_text(_as_mapping(umm.get("DOI")).get("DOI")),
        processing_level=_text(_as_mapping(umm.get("ProcessingLevel")).get("Id")),
        license_text=_text(_as_mapping(umm.get("UseConstraints")).get("Description")),
        citation=_citation_from_umm(umm),
        landing_url=_landing_from_related_urls(umm),
        variable_concept_ids=variable_ids,
    )


# --------------------------------------------------------------------------- #
# 变量（variables.umm_json）
# --------------------------------------------------------------------------- #
def parse_umm_variables(payload: Mapping[str, Any]) -> tuple[DatasetVariable, ...] | None:
    """解析 ``variables.umm_json`` 的 ``items[].umm``；``items`` 为空返回 ``None``（未知）。

    ``None`` 与空元组的区别是刻意的：``None`` = "远端没有声明变量清单"。CMR 的
    ``variables`` 关联为空时，我们无法区分"确实没有变量"与"未登记"，故按未知处理。
    """
    items = _as_sequence(payload.get("items"))
    if not items:
        return None
    variables: list[DatasetVariable] = []
    for item in items:
        umm = _as_mapping(_as_mapping(item).get("umm"))
        name = _text(umm.get("Name"))
        if name is None:
            continue
        variables.append(
            DatasetVariable(
                name=name,
                long_name=_text(umm.get("LongName")),
                units=_text(umm.get("Units")),
                data_type=_text(umm.get("DataType")),
                description=_text(umm.get("Definition")),
                source="remote",
            )
        )
    return tuple(variables) if variables else None


# --------------------------------------------------------------------------- #
# granule（granules.json）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CmrFileLink:
    """granule 的一条 HTTPS 数据链接（``rel`` 以 ``data#`` 结尾，非继承）。"""

    href: str
    title: str | None
    rel: str


@dataclass(frozen=True)
class CmrGranule:
    """``granules.json`` 的一条 granule（字段来自真实响应）。"""

    concept_id: str
    collection_concept_id: str | None
    producer_granule_id: str | None
    title: str | None
    time_start: datetime | None
    time_end: datetime | None
    size_bytes: int | None
    size_declared_mb: str | None
    data_links: tuple[CmrFileLink, ...]
    s3_links: tuple[str, ...]
    original_format: str | None

    def temporal_extent(self) -> TemporalExtent | None:
        """该 granule 的时间区间；任一端缺失返回 ``None``。"""
        return build_temporal_extent(self.time_start, self.time_end)


def _is_file_link(entry: Mapping[str, Any]) -> bool:
    """判断链接是否为 granule 自己的 HTTPS 数据文件（排除集合级继承链接）。

    ``rel`` 必须以 ``/data#`` 结尾（按**路径段**匹配，不能用 ``endswith("data#")``：
    ``.../metadata#`` 同样以 ``data#`` 结尾——这是实测踩到的坑）。
    """
    if entry.get("inherited") is True:
        return False
    rel = _text(entry.get("rel")) or ""
    href = _text(entry.get("href")) or ""
    return rel.endswith(f"/{CMR_DATA_REL_SUFFIX}") and href.startswith("https://")


def pick_download_link(links: Sequence[Mapping[str, Any]]) -> str | None:
    """从一组 CMR ``links`` 中挑**唯一**的 HTTPS 数据下载链接；不唯一或没有返回 ``None``。

    为什么要求唯一：多文件 granule 时任意取一个会把"部分数据"当成"全部数据"。
    调用方必须显式处理 ``None``（报告不唯一），而不是沉默地挑第一个。
    """
    candidates = [entry for entry in links if _is_file_link(entry)]
    if len(candidates) != 1:
        return None
    return _text(candidates[0].get("href"))


def parse_granule_entries(payload: Mapping[str, Any]) -> tuple[CmrGranule, ...]:
    """解析 ``granules.json`` 的 ``feed.entry``。

    Raises
    ------
    MetadataError
        顶层没有 ``feed``、``feed.entry`` 不是列表，或某条目缺 concept-id。
    """
    feed = payload.get("feed")
    if not isinstance(feed, Mapping):
        raise MetadataError(
            "CMR granules response has no 'feed' object",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    entries = feed.get("entry", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise MetadataError(
            "CMR granules feed.entry is not a list",
            detail={"observed_type": type(feed.get("entry")).__name__},
        )
    granules: list[CmrGranule] = []
    for raw in entries:
        entry = _as_mapping(raw)
        concept_id = _text(entry.get("id"))
        if concept_id is None:
            raise MetadataError("CMR granule entry has no concept-id", detail={})
        links = [_as_mapping(item) for item in _as_sequence(entry.get("links"))]
        size_raw = entry.get("granule_size")
        granules.append(
            CmrGranule(
                concept_id=concept_id,
                collection_concept_id=_text(entry.get("collection_concept_id")),
                producer_granule_id=_text(entry.get("producer_granule_id")),
                title=_text(entry.get("title")),
                time_start=parse_timestamp(entry.get("time_start"), field="granule time_start"),
                time_end=parse_timestamp(entry.get("time_end"), field="granule time_end"),
                size_bytes=granule_size_bytes(size_raw),
                size_declared_mb=size_raw if isinstance(size_raw, str) else None,
                data_links=tuple(
                    CmrFileLink(
                        href=_text(item.get("href")) or "",
                        title=_text(item.get("title")),
                        rel=_text(item.get("rel")) or "",
                    )
                    for item in links
                    if _is_file_link(item)
                ),
                s3_links=tuple(
                    href
                    for href in (_text(item.get("href")) for item in links)
                    if href is not None and href.startswith("s3://")
                ),
                original_format=_text(entry.get("original_format")),
            )
        )
    return tuple(granules)


def candidate_from_collection(
    collection: CmrCollection,
    *,
    provider: str,
    short_name: str,
    login_host: str,
) -> DatasetCandidate:
    """把 :class:`CmrCollection` 转成契约对象 :class:`DatasetCandidate`。

    ``variables`` 留空且 ``variables_known=False``：``collections.json`` 只标记
    ``has_variables``，不含变量名（变量需另查 ``variables.umm_json``）。
    """
    access_modes = ["https"]
    if collection.cloud_hosted:
        access_modes.append("s3")
    return DatasetCandidate(
        provider=provider,
        dataset_id=collection.dataset_id,
        title=collection.title,
        abstract=collection.abstract,
        variables=(),
        variables_known=False,
        temporal_coverage=collection.temporal_extent(),
        spatial_extent=collection.spatial,
        formats=(collection.original_format,) if collection.original_format else (),
        access_modes=tuple(access_modes),
        # CMR 目录元数据匿名可读（实测 2026-11-05）；下载受保护数据另需 EDL 凭证。
        auth_required=False,
        license=None,
        citation=None,
        landing_url=collection.landing_url,
        native_id=collection.concept_id,
        extra={
            "cmr.short_name": collection.short_name or short_name,
            "cmr.provider_id": collection.provider_id,
            "cmr.version_id": collection.version_id,
            "cmr.processing_level": collection.processing_level,
            "cmr.has_variables": collection.has_variables,
            "cmr.cloud_hosted": collection.cloud_hosted,
            "cmr.granule_size_unit": CMR_GRANULE_SIZE_UNIT,
            "cmr.login_host": login_host,
        },
    )
