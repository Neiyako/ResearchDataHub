"""CDSE（Copernicus Data Space Ecosystem）STAC / OData 响应解析：纯函数。

依据的真实响应（2026-11-05 ``curl``，样例见 ``tests/unit/test_providers/fixtures/``）
-----------------------------------------------------------------------------------
* ``GET /stac/`` 落地文档：``{"type": "Catalog", "id": "cdse-stac", "stac_version": "1.0.0"}``。
* ``GET /stac/collections`` —— ``{"collections": [...], "links": [...], "numberMatched": 428,
  "numberReturned": 10}``；未带 ``limit`` 时**只返回 10 条**，分页链接是
  ``https://stac.dataspace.copernicus.eu/v1/collections?offset=10``（注意主机是
  ``stac.dataspace.copernicus.eu``，与 ``catalogue.dataspace.copernicus.eu`` 不同）。
* ``GET /stac/collections/{id}`` —— 集合详情：``extent.spatial.bbox``、
  ``extent.temporal.interval``（结束时间可能为 ``null`` = 仍在更新）、``license``（实测
  ``"other"``）、``sci:citation``、``sci:doi``、``providers[]``、``bands[]``（``name`` /
  ``gsd`` / ``description`` / ``eo:common_name``）、``item_assets``、``summaries``、
  ``auth:schemes.s3`` / ``oidc``、``storage:schemes.cdse-s3`` / ``creodias-s3``。
* ``POST /stac/search`` —— GeoJSON ``FeatureCollection``：``features[].{id, bbox, geometry,
  collection, assets, links, properties}``。``assets.*`` 含 ``href``（**S3 路径**）、
  ``file:size``、``file:checksum``（**小写 hex，算法未在响应里声明**）、``roles``、
  ``alternate.https.href``（**OData ``$value`` 下载 URL**，``auth:refs: ["oidc"]``）。
  ``links`` 中有 ``rel=enclosure`` 的 ``s3://eodata/.../`` 目录与 ``version-history`` 链接。
* ``GET /odata/v1/Products?$top=..&$expand=Attributes&$filter=...&$orderby=...`` ——
  ``{"@odata.context", "value": [...], "@odata.nextLink"}``；产品字段实测含 ``Id``（UUID）、
  ``Name``、``ContentDate.{Start,End}``、``ContentLength``（**字节**）、``S3Path``、
  ``Online``、``Checksum``（``[{Value, Algorithm="MD5"}, {Value, Algorithm="BLAKE3"}]``）、
  ``GeoFootprint``（GeoJSON）、``Footprint``（WKT SRID=4326）、``Attributes[]``
  （``Name``/``Value``/``ValueType``：``cloudCover`` / ``tileId`` / ``orbitNumber`` /
  ``platformShortName`` / ``instrumentShortName`` / ``processingLevel`` 等）。
  **注意**：``GET /odata/v1/Collections`` 与 ``/odata/v1/``、``/$metadata`` 实测返回
  **404**，因此 OData 侧**没有**已验证的集合列表端点；集合枚举走 STAC。
* ``GET /odata/v1/Attributes`` —— 返回 ``{COLLECTION_NAME: [{"Name", "ValueType"}, ...]}``
  （OData 侧的产品属性 schema，按集合分组）。
* Token：``POST /auth/realms/CDSE/protocol/openid-connect/token``（client_credentials）。
  实测用无效凭证返回 ``401 {"error": "invalid_client"}``；
  ``grant_types_supported`` 含 ``client_credentials``，``issuer`` 为 CDSE realm。

本模块**只解析，不发请求**。
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
    "CDSE_HTTPS_DOWNLOAD_HOST",
    "CDSE_ODATA_BYTE_FIELDS",
    "CdseStacCollection",
    "CdseStacItem",
    "OdataProduct",
    "candidate_from_stac_collection",
    "parse_odata_attributes",
    "parse_odata_products",
    "parse_odata_products_node",
    "parse_stac_collection",
    "parse_stac_collections_payload",
    "parse_stac_search_payload",
    "stac_bands_to_variables",
]

CDSE_HTTPS_DOWNLOAD_HOST: Final[str] = "https://download.dataspace.copernicus.eu"
"""CDSE 自己的 HTTPS/OData 下载主机（实测 STAC ``alternate.https.href`` 的主机名）。

只认这个主机的资产 URL；CREODIAS（``datahub.creodias.eu``）是**另一套服务**，
不应被当成 CDSE 的下载端点。
"""

CDSE_ODATA_BYTE_FIELDS: Final[tuple[str, ...]] = ("ContentLength",)
"""OData 产品中的**字节数**字段（实测 ``ContentLength: 607269158``）。"""

_STAC_UNKNOWN_LICENSE: Final[frozenset[str]] = frozenset({"other", "proprietary", ""})


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _text(value: object) -> str | None:
    """把远端值规整为去空白字符串；非字符串/空白返回 ``None``。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_mapping(value: object) -> Mapping[str, Any]:
    """收窄为映射（非映射返回空映射）。"""
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> Sequence[Any]:
    """收窄为序列（字符串不算序列）。"""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _bbox_extent(raw: object) -> SpatialExtent | None:
    """解析 STAC ``bbox``（``[west, south, east, north]`` 或 6 元素 3D 形式）。"""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    values: list[float] = []
    for item in raw:
        if not isinstance(item, (int, float)):
            return None
        values.append(float(item))
    if len(values) == 4:
        west, south, east, north = values
    elif len(values) == 6:
        west, south, _, east, north, _ = values
    else:
        return None
    return build_spatial_extent(north=north, south=south, west=west, east=east)


def _stac_interval_extent(interval: object) -> TemporalExtent | None:
    """解析 STAC ``extent.temporal.interval``（``[[start, end]]``，``end`` 可为 ``null``）。

    ``end is None`` 表示"仍在更新"，**不是**未知结束时间；此时返回 ``None`` 并在
    ``notes`` 上说明（不推断结束日期）。
    """
    for entry in _as_sequence(interval):
        pair = _as_sequence(entry)
        if len(pair) < 2:
            continue
        start = parse_timestamp(pair[0], field="interval start")
        end = parse_timestamp(pair[1], field="interval end")
        if start is None or end is None:
            continue
        extent = build_temporal_extent(start, end)
        if extent is not None:
            return extent
    return None


# --------------------------------------------------------------------------- #
# STAC 集合
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CdseStacCollection:
    """``/stac/collections`` 或 ``/stac/collections/{id}`` 的一条集合。"""

    collection_id: str
    title: str
    description: str | None
    license: str | None
    citation: str | None
    doi: str | None
    temporal: TemporalExtent | None
    """闭合时间区间；``ends_at_present=True`` 时必为 ``None``（结束时间未声明）。"""

    interval_start: datetime | None
    """区间起点（即使结束时间为 ``null`` 也会有值）；缺失则为 ``None``。"""

    spatial: SpatialExtent | None
    bands: tuple[DatasetVariable, ...]
    item_assets: tuple[str, ...]
    providers: tuple[str, ...]
    ends_at_present: bool
    """``extent.temporal.interval`` 的结束时间为 ``null``（数据仍在持续更新）。"""

    extra: Mapping[str, Any] = field(repr=False, default_factory=dict)


def parse_stac_collection(payload: Mapping[str, Any]) -> CdseStacCollection:
    """解析单个 STAC Collection 对象（``GET /stac/collections/{id}``）。

    Raises
    ------
    MetadataError
        缺少 ``id``。
    """
    collection_id = _text(payload.get("id"))
    if collection_id is None:
        raise MetadataError(
            "STAC collection has no 'id'",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    extent = _as_mapping(payload.get("extent"))
    spatial_raw = _as_sequence(_as_mapping(extent.get("spatial")).get("bbox"))
    spatial = _bbox_extent(spatial_raw[0]) if spatial_raw else None
    interval = _as_mapping(extent.get("temporal")).get("interval")
    temporal = _stac_interval_extent(interval)
    ends_at_present = False
    interval_start: datetime | None = None
    for entry in _as_sequence(interval):
        pair = _as_sequence(entry)
        if len(pair) < 2:
            continue
        candidate_start = parse_timestamp(pair[0], field="interval start")
        if candidate_start is not None and interval_start is None:
            interval_start = candidate_start
        if pair[1] is None:
            ends_at_present = True
    license_raw = _text(payload.get("license"))
    license_text = None if license_raw is None else license_raw
    if license_text is not None and license_text in _STAC_UNKNOWN_LICENSE:
        # STAC 的 "other" 是合法取值（非 SPDX），此处保留原值，不伪造 SPDX 标识。
        license_text = f"STAC license field: {license_text}"
    return CdseStacCollection(
        collection_id=collection_id,
        title=_text(payload.get("title")) or collection_id,
        description=_text(payload.get("description")),
        license=license_text,
        citation=_text(payload.get("sci:citation")),
        doi=_text(payload.get("sci:doi")),
        temporal=temporal,
        interval_start=interval_start,
        spatial=spatial,
        bands=stac_bands_to_variables(payload.get("bands")),
        item_assets=tuple(
            key for key in _as_mapping(payload.get("item_assets")) if isinstance(key, str)
        ),
        providers=tuple(
            text
            for text in (
                _text(_as_mapping(item).get("name"))
                for item in _as_sequence(payload.get("providers"))
            )
            if text is not None
        ),
        ends_at_present=ends_at_present,
        extra={
            "stac.license_field": license_raw,
            "stac.stac_version": _text(payload.get("stac_version")),
            "stac.gsd": _stac_gsd(payload),
        },
    )


def _stac_gsd(payload: Mapping[str, Any]) -> tuple[float, ...]:
    """从 ``summaries.gsd`` 读取观测分辨率（米）；没有则空元组（不推断）。"""
    raw = _as_mapping(payload.get("summaries")).get("gsd")
    values: list[float] = []
    for item in _as_sequence(raw):
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            values.append(float(item))
    return tuple(sorted(values))


def stac_bands_to_variables(raw: object) -> tuple[DatasetVariable, ...]:
    """把 STAC ``bands[]`` 转成 :class:`DatasetVariable`。

    ``gsd``（米）与单位无关：STAC 不声明物理单位，因此 ``units`` 保持 ``None``
    （不写成 "W m-2" 之类未经证实的单位）。
    """
    variables: list[DatasetVariable] = []
    for item in _as_sequence(raw):
        body = _as_mapping(item)
        name = _text(body.get("name"))
        if name is None:
            continue
        gsd = body.get("gsd")
        media = _text(body.get("eo:common_name"))
        description = _text(body.get("description"))
        notes = [part for part in (description, f"common_name={media}" if media else None) if part]
        variables.append(
            DatasetVariable(
                name=name,
                long_name=description,
                units=None,
                data_type=f"gsd={gsd}m" if isinstance(gsd, (int, float)) else None,
                description="; ".join(notes) if notes else None,
                source="remote",
            )
        )
    return tuple(variables)


def parse_stac_collections_payload(
    payload: Mapping[str, Any],
) -> tuple[tuple[CdseStacCollection, ...], int | None]:
    """解析 ``/stac/collections`` 响应；返回 ``(集合列表, numberMatched)``。

    Raises
    ------
    MetadataError
        ``collections`` 字段缺失或不是列表。
    """
    collections = payload.get("collections")
    if not isinstance(collections, Sequence) or isinstance(collections, (str, bytes)):
        raise MetadataError(
            "STAC collections payload has no 'collections' list",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    matched = payload.get("numberMatched")
    total = matched if isinstance(matched, int) and not isinstance(matched, bool) else None
    return tuple(parse_stac_collection(_as_mapping(item)) for item in collections), total


def candidate_from_stac_collection(
    collection: CdseStacCollection,
    *,
    provider: str,
) -> DatasetCandidate:
    """把 STAC 集合转成 :class:`DatasetCandidate`。

    ``auth_required=False``：STAC 目录元数据匿名可读（实测 2026-11-05）；
    ``access_modes`` 只列**已验证存在链接**的方式：``https``（OData ``$value``，需 Bearer）
    与 ``s3``（``s3://eodata`` 路径，本 Provider 声明 unsupported，不在 modes 里冒充可用）。
    """
    access_modes = ["https"]
    return DatasetCandidate(
        provider=provider,
        dataset_id=collection.collection_id,
        title=collection.title,
        abstract=collection.description,
        variables=collection.bands,
        variables_known=bool(collection.bands),
        temporal_coverage=collection.temporal,
        spatial_extent=collection.spatial,
        formats=(),
        access_modes=tuple(access_modes),
        auth_required=False,
        license=collection.license,
        citation=collection.citation,
        landing_url=None,
        native_id=collection.collection_id,
        extra={
            "cdse.doi": collection.doi,
            "cdse.providers": list(collection.providers),
            "cdse.item_assets": list(collection.item_assets),
            "cdse.ends_at_present": collection.ends_at_present,
            "cdse.gsd_meters": list(collection.extra.get("stac.gsd") or ()),
        },
    )


# --------------------------------------------------------------------------- #
# STAC 检索结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CdseStacItem:
    """``POST /stac/search`` 返回的一个 item（= 一个产品 / granule）。"""

    item_id: str
    collection: str | None
    datetime_start: datetime | None
    datetime_end: datetime | None
    bbox: SpatialExtent | None
    total_size_bytes: int | None
    asset_count: int
    https_download_urls: tuple[str, ...]
    s3_hrefs: tuple[str, ...]
    properties: Mapping[str, Any] = field(repr=False, default_factory=dict)


def parse_stac_item(raw: Mapping[str, Any]) -> CdseStacItem:
    """解析一个 STAC Feature（item）。

    Raises
    ------
    MetadataError
        缺少 ``id``。
    """
    item_id = _text(raw.get("id"))
    if item_id is None:
        raise MetadataError("STAC item has no 'id'", detail={"observed_keys": sorted(raw)[:20]})
    properties = _as_mapping(raw.get("properties"))
    datetime_start = parse_timestamp(properties.get("datetime"), field="item datetime")
    if datetime_start is None:
        datetime_start = parse_timestamp(
            properties.get("start_datetime"), field="item start_datetime"
        )
    datetime_end = parse_timestamp(properties.get("end_datetime"), field="item end_datetime")
    if datetime_end is None:
        datetime_end = datetime_start
    total: int | None = 0
    asset_count = 0
    https_urls: list[str] = []
    s3_hrefs: list[str] = []
    for _name, asset in _as_mapping(raw.get("assets")).items():
        body = _as_mapping(asset)
        asset_count += 1
        size = body.get("file:size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            total = (total or 0) + size
        else:
            total = None
        alternate = _as_mapping(_as_mapping(body.get("alternate")).get("https"))
        href = _text(alternate.get("href")) or _text(body.get("href"))
        # 只收 CDSE 自己的 HTTPS 端点。实测有资产的 alternate 指向 CREODIAS
        # (``datahub.creodias.eu``)——那是**另一套服务**，不属于 CDSE，不能混进来。
        if href is not None and href.startswith(f"{CDSE_HTTPS_DOWNLOAD_HOST}/"):
            https_urls.append(href)
        raw_href = _text(body.get("href"))
        if raw_href is not None and raw_href.startswith("s3://"):
            s3_hrefs.append(raw_href)
    return CdseStacItem(
        item_id=item_id,
        collection=_text(raw.get("collection")),
        datetime_start=datetime_start,
        datetime_end=datetime_end,
        bbox=_bbox_extent(raw.get("bbox")),
        total_size_bytes=total,
        asset_count=asset_count,
        https_download_urls=tuple(https_urls),
        s3_hrefs=tuple(s3_hrefs),
        properties=properties,
    )


def parse_stac_search_payload(payload: Mapping[str, Any]) -> tuple[CdseStacItem, ...]:
    """解析 ``/stac/search`` 的 ``features``。

    Raises
    ------
    MetadataError
        ``features`` 缺失或不是列表。
    """
    features = payload.get("features")
    if not isinstance(features, Sequence) or isinstance(features, (str, bytes)):
        raise MetadataError(
            "STAC search payload has no 'features' list",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    return tuple(parse_stac_item(_as_mapping(item)) for item in features)


# --------------------------------------------------------------------------- #
# OData 产品
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OdataProduct:
    """``/odata/v1/Products`` 的一个产品（字段来自真实响应）。"""

    product_id: str
    name: str
    start: datetime | None
    end: datetime | None
    content_length: int | None
    s3_path: str | None
    online: bool | None
    footprints: SpatialExtent | None
    attributes: Mapping[str, Any]
    checksums: Mapping[str, str]

    def temporal_extent(self) -> TemporalExtent | None:
        """产品内容时间区间；任一端缺失或起止相同返回 ``None``。

        实测（2026-11-05）：Sentinel-2 单景产品的 ``ContentDate.Start`` 与 ``End``
        **完全相同**（``2026-11-05T12:40:31.025000Z``）——那是"成像时刻"而不是区间。
        核心契约 :class:`TemporalExtent` 要求 ``start < end``，因此这里返回 ``None``
        并保留两个原始时刻在 ``start``/``end`` 字段里，**不**人为加一秒凑成区间。
        """
        if self.start is not None and self.end is not None and self.start >= self.end:
            return None
        return build_temporal_extent(self.start, self.end)


def _odata_attributes(raw: object) -> Mapping[str, Any]:
    """把 ``Attributes[].{Name, Value}`` 摊平为字典（非标量值保留原样，不猜测）。"""
    result: dict[str, Any] = {}
    for item in _as_sequence(raw):
        body = _as_mapping(item)
        name = _text(body.get("Name"))
        if name is None:
            continue
        value = body.get("Value")
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[name] = value
        else:
            result[name] = str(value)
    return result


def _odata_footprint_extent(raw: object) -> SpatialExtent | None:
    """从 ``GeoFootprint``（GeoJSON Polygon）计算外包框。

    ``GeoFootprint.coordinates`` 实测形如 ``[[[lon, lat], ...]]``；只取外包框，
    不保留多边形（Provider 契约只承载包围盒）。
    """
    footprint = _as_mapping(raw)
    if _text(footprint.get("type")) != "Polygon":
        return None
    lons: list[float] = []
    lats: list[float] = []
    for ring in _as_sequence(footprint.get("coordinates")):
        for point in _as_sequence(ring):
            pair = _as_sequence(point)
            if len(pair) < 2:
                continue
            lon, lat = pair[0], pair[1]
            if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
                lons.append(float(lon))
                lats.append(float(lat))
    if not lons or not lats:
        return None
    return build_spatial_extent(north=max(lats), south=min(lats), west=min(lons), east=max(lons))


def _odata_checksums(raw: object) -> Mapping[str, str]:
    """解析 ``Checksum[].{Value, Algorithm}``（实测算法为 MD5 与 BLAKE3）。"""
    result: dict[str, str] = {}
    for item in _as_sequence(raw):
        body = _as_mapping(item)
        value = _text(body.get("Value"))
        algorithm = _text(body.get("Algorithm"))
        if value is not None and algorithm is not None:
            result[algorithm.lower()] = value
    return result


def parse_odata_products_node(raw: Mapping[str, Any]) -> OdataProduct:
    """解析单个 OData 产品对象。

    Raises
    ------
    MetadataError
        缺少 ``Id`` 或 ``Name``（二者是文件级标识的必需事实）。
    """
    product_id = _text(raw.get("Id"))
    name = _text(raw.get("Name"))
    if product_id is None or name is None:
        raise MetadataError(
            "CDSE OData product is missing 'Id' or 'Name'",
            detail={"observed_keys": sorted(raw)[:20]},
        )
    content_date = _as_mapping(raw.get("ContentDate"))
    length = raw.get("ContentLength")
    content_length = (
        length if isinstance(length, int) and not isinstance(length, bool) and length >= 0 else None
    )
    online = raw.get("Online")
    return OdataProduct(
        product_id=product_id,
        name=name,
        start=parse_timestamp(content_date.get("Start"), field="ContentDate.Start"),
        end=parse_timestamp(content_date.get("End"), field="ContentDate.End"),
        content_length=content_length,
        s3_path=_text(raw.get("S3Path")),
        online=online if isinstance(online, bool) else None,
        footprints=_odata_footprint_extent(raw.get("GeoFootprint")),
        attributes=_odata_attributes(raw.get("Attributes")),
        checksums=_odata_checksums(raw.get("Checksum")),
    )


def parse_odata_products(payload: Mapping[str, Any]) -> tuple[OdataProduct, ...]:
    """解析 ``/odata/v1/Products`` 的 ``value`` 数组。

    Raises
    ------
    MetadataError
        ``value`` 缺失或不是列表。
    """
    value = payload.get("value")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MetadataError(
            "CDSE OData products payload has no 'value' list",
            detail={"observed_keys": sorted(payload)[:20]},
        )
    return tuple(parse_odata_products_node(_as_mapping(item)) for item in value)


def parse_odata_attributes(payload: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """解析 ``/odata/v1/Attributes``：``{集合名: [属性名...]}``（**不是**变量/波段清单）。

    该端点是 OData 侧的**产品属性 schema**（``cloudCover`` / ``orbitNumber`` 之类），
    不能当作科学变量清单使用；本函数只供 ``get_metadata`` 输出。
    """
    result: dict[str, tuple[str, ...]] = {}
    for collection_name, attrs in payload.items():
        names = tuple(
            name
            for name in (_text(_as_mapping(item).get("Name")) for item in _as_sequence(attrs))
            if name is not None
        )
        if names:
            result[collection_name] = names
    return result
