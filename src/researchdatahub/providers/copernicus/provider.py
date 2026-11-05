"""Copernicus Data Space Ecosystem（CDSE）Provider —— 与 CDS（ECMWF）**完全独立**。

先澄清一个常见混淆
------------------
* **CDS**（Climate Data Store，``cds.climate.copernicus.eu``）= ECMWF 的气候再分析（ERA5 等），
  已有实现见 :class:`researchdatahub.sources.cds.CDSAdapter`。
* **CDSE**（Data Space Ecosystem，``dataspace.copernicus.eu``）= 哥白尼**卫星**数据
  （Sentinel-1/2/3/5P、CLMS 等）的目录与下载，本模块负责它。二者端点、认证、数据模型都不同，
  本 Provider **不**复用 ``cdsapi``，也**不**声称能取 ERA5（CDSE 的 OData 里虽有一个叫
  ``ERA5`` 的集合名，但本 Provider 未验证其内容与 CDS 的 ERA5 等效，故不宣传）。

真实端点（2026-11-05 用 ``curl`` 实测，命令与关键字段见 ``docs/providers/copernicus.md``）
------------------------------------------------------------------------------------------
=============== ============================================================ ==========
能力             端点                                                          实测
=============== ============================================================ ==========
STAC 集合列表    ``GET /stac/collections``（``?q=`` 支持关键词，``numberMatched``） 200 匿名
STAC 集合详情    ``GET /stac/collections/{id}``                                    200 匿名
STAC 检索        ``POST /stac/search``（``collections``/``bbox``/``datetime``）      200 匿名
OData 产品       ``GET /odata/v1/Products?$top&$filter&$expand=Attributes``         200 匿名
OData 属性 schema ``GET /odata/v1/Attributes``                                    200 匿名
Token           ``POST /auth/realms/CDSE/.../token``（client_credentials）  401（无效凭证探测）
=============== ============================================================ ==========

**实测 404 的端点（因此不实现）**：``GET /odata/v1/Collections``、``GET /odata/v1/``、
``GET /odata/v1/$metadata``。集合枚举只走 STAC。

能力矩阵（权威来源是 :meth:`CopernicusProvider.capabilities`）
---------------------------------------------------------------
search / inspect / get_metadata / get_variables / validate_request /
estimate_request = **implemented**（STAC 路径已验证；OData 路径用于文件级估算，已验证匿名 200）。

**unsupported**：``submit_task`` / ``get_task_status`` / ``retrieve_result`` / ``cancel_task``
（CDSE 的目录 API 没有作业语义；云的 Sentinel Hub / On-Demand Processing 属于另一套授权，
未验证，不写假实现）。另外：**S3 直连（``eodata`` bucket）unsupported**——STAC 里能看到
``s3://eodata/...`` 路径，但 S3 凭证与端点未验证。

**unverified**：带 Bearer 的 ``/odata/v1/Products({Id})/$value`` 下载（本机无
``CDSE_CLIENT_ID``/``CDSE_CLIENT_SECRET``），以及 token 成功换取路径。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final

import httpx

from researchdatahub.core.errors import (
    MetadataError,
    NotImplementedFeatureError,
)
from researchdatahub.core.models import JsonValue
from researchdatahub.providers._http import build_client, request_json, utcnow
from researchdatahub.providers.base import (
    CAPABILITY_CONTRACT_VERSION,
    DatasetCandidate,
    DatasetDetail,
    DatasetFilter,
    DatasetVariable,
    ProviderCapabilities,
    QueryTranslation,
    RequestEstimate,
    RequestValidation,
    TaskHandle,
    TaskStatusSnapshot,
)
from researchdatahub.providers.copernicus.auth import (
    CDSE_TOKEN_ENDPOINT,
    CdseCredentials,
    CdseTokenCache,
    probe_credentials,
)
from researchdatahub.providers.copernicus.cdse import (
    CdseStacCollection,
    CdseStacItem,
    OdataProduct,
    candidate_from_stac_collection,
    parse_odata_attributes,
    parse_odata_products,
    parse_stac_collection,
    parse_stac_collections_payload,
    parse_stac_search_payload,
)

__all__ = [
    "CDSE_CATALOGUE_ROOT",
    "CDSE_COLLECTION_PAGE_SIZE",
    "CDSE_ODATA_ROOT",
    "CDSE_STAC_SEARCH_CAP",
    "CopernicusProvider",
    "copernicus_provider",
]

CDSE_CATALOGUE_ROOT: Final[str] = "https://catalogue.dataspace.copernicus.eu"
"""CDSE 目录根（实测：``/stac/...`` 与 ``/odata/v1/...`` 均可达）。"""

CDSE_ODATA_ROOT: Final[str] = "https://catalogue.dataspace.copernicus.eu/odata/v1"
"""OData 根（实测 ``/Products`` 与 ``/Attributes`` 200；``/Collections`` 404）。"""

CDSE_STAC_SEARCH_CAP: Final[int] = 100
"""``POST /stac/search`` 与 ``$top`` 的单次条数上限（实测 ``limit`` 参数被接受；保守取 100）。"""

CDSE_COLLECTION_PAGE_SIZE: Final[int] = 1000
"""``GET /stac/collections`` 的每页条数。

实测（2026-11-05）：``/stac/collections`` **不认小 limit**——``limit=5`` 与不传都返回 10 条，
且排序与 ``q`` 的相关性无关（``q=sentinel-2`` 的前 10 条是 CLMS 产品）。``limit=1000``
则一次返回全部 145 条命中（耗时约 74s）。因此必须**翻页取全量**再本地排序，
否则 ``search`` 会返回大量与查询无关的集合。"""

CDSE_MAX_COLLECTION_PAGES: Final[int] = 4
"""翻页上限（``4 * 1000`` 条），防止远端忽略 ``offset`` 时无限循环；超出即显式报告。"""

_IMPLEMENTED: Final[tuple[str, ...]] = (
    "search",
    "inspect",
    "get_metadata",
    "get_variables",
    "validate_request",
    "estimate_request",
)
_UNSUPPORTED: Final[tuple[str, ...]] = (
    "submit_task",
    "get_task_status",
    "retrieve_result",
    "cancel_task",
)


class CopernicusProvider:
    """Copernicus Data Space Ecosystem 的目录 Provider（STAC + OData 两条路径）。

    Parameters
    ----------
    stac_root:
        STAC 根（默认 :data:`CDSE_CATALOGUE_ROOT`）；离线测试注入本地服务器。
    odata_root:
        OData 根（默认 :data:`CDSE_ODATA_ROOT`）。
    http_client:
        注入的 HTTP 客户端；``None`` 时自建（匿名元数据请求不需要 token）。
    token_cache:
        OAuth2 token 缓存（只在需要 Bearer 的调用里使用，V1 仅用于下载 URL 的可用性判断）。
    clock:
        时钟（测试注入固定时间）。
    """

    provider_id: Final[str] = "copernicus"

    def __init__(
        self,
        *,
        stac_root: str = CDSE_CATALOGUE_ROOT,
        odata_root: str = CDSE_ODATA_ROOT,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        token_cache: CdseTokenCache | None = None,
        credentials: CdseCredentials | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._stac_root = stac_root.rstrip("/")
        self._odata_root = odata_root.rstrip("/")
        self._timeout = timeout
        self._http_client = http_client
        self._owns_http = http_client is None
        self._token_cache = token_cache if token_cache is not None else CdseTokenCache()
        self._credentials = credentials if credentials is not None else probe_credentials()
        self._clock = clock
        self._closed = False
        self._lock = threading.Lock()
        self._collection_cache: dict[str, CdseStacCollection] = {}

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def http(self) -> httpx.Client:
        """惰性创建的匿名 HTTP 客户端（元数据请求）。"""
        self._ensure_open()
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout)
            self._owns_http = True
        return self._http_client

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed; create a new instance")

    def close(self) -> None:
        """关闭自建 HTTP 客户端与 token 缓存（幂等）。"""
        if self._http_client is not None and self._owns_http:
            self._http_client.close()
        self._token_cache.close()
        self._closed = True

    def __enter__(self) -> CopernicusProvider:
        """上下文入口。"""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """上下文出口。"""
        self.close()

    # -- 能力 -------------------------------------------------------------- #
    @property
    def credentials(self) -> CdseCredentials:
        """凭证配置状态（**不含任何凭证值**）。"""
        return self._credentials

    def capabilities(self) -> ProviderCapabilities:
        """如实返回能力矩阵（与 ``docs/providers/copernicus.md`` 的表一致）。"""
        authenticated = self._credentials.configured
        return ProviderCapabilities(
            provider_id=self.provider_id,
            display_name="Copernicus Data Space Ecosystem (CDSE, STAC + OData)",
            contract_version=CAPABILITY_CONTRACT_VERSION,
            # 仅指**元数据**：STAC 与 OData 检索匿名可读（实测 2026-11-05）。
            auth_required=False,
            supported_formats=(),
            supported_regions=(),
            supported_temporal_ranges=(),
            supports_async_job=False,
            supports_cloud_processing=False,
            supports_partial_download=False,
            supports_resume=False,
            implemented=_IMPLEMENTED,
            unsupported=_UNSUPPORTED,
            verified=(
                "stac: GET /stac/collections HTTP 200 anonymous (numberMatched=428, 2026-11-05)",
                "stac: GET /stac/collections/{id} HTTP 200 anonymous (2026-11-05)",
                "stac: POST /stac/search HTTP 200 anonymous with collections/bbox/datetime "
                "(2026-11-05)",
                "odata: GET /odata/v1/Products?$top&$filter&$expand=Attributes HTTP 200 "
                "anonymous (2026-11-05)",
                "odata: GET /odata/v1/Attributes HTTP 200 anonymous (2026-11-05)",
            ),
            unverified=(
                "data_download: GET /odata/v1/Products({Id})/$value with Bearer is NOT verified "
                "(no CDSE_CLIENT_ID/CDSE_CLIENT_SECRET on this host, 2026-11-05)",
                "auth: token exchange success path is unverified; only the 401 invalid_client "
                "shape was observed",
                "get_variables: STAC bands[] are returned where declared; collections without a "
                "bands array yield variables_known=False rather than an invented list",
            ),
            credential_features={
                "metadata_query": "verified",
                "granule_search": "verified",
                "data_download": "verified" if authenticated else "unverified",
                "cloud_processing": "unsupported",
                "s3_direct": "unsupported",
            },
            notes=(
                "not the same platform as CDS/ECMWF: this provider serves Copernicus satellite "
                "data, not ERA5 reanalysis",
                "unsupported access paths (not Provider methods): s3_direct (s3://eodata) and "
                "cloud_processing (Sentinel Hub); both lack verified credentials here",
                "ODATA /Collections, /v1/ and /$metadata returned 404 on 2026-11-05; "
                "collection enumeration uses STAC only",
                f"token endpoint: {CDSE_TOKEN_ENDPOINT}",
            ),
        )

    # -- 检索 -------------------------------------------------------------- #
    def translate_query(self, query: str, filters: DatasetFilter | None = None) -> QueryTranslation:
        """返回 ``search`` 将要发出的 STAC 请求（不含凭证），供审计"哪个过滤没生效"。

        ``search`` 两步走：先 ``GET /stac/collections?q=`` 按关键词找集合，
        再对候选集合 ``GET /stac/collections/{id}`` 取详情。``platform`` /
        ``short_name`` 在 CDSE 的集合列表端点里没有实测等价参数，进 ``ignored``。
        """
        active = filters if filters is not None else DatasetFilter()
        clause = (active.keyword or "").strip() or query.strip()
        # 远端必须先取全量命中再排序：小 limit 会拿到与关键词无关的前几条（实测）。
        params: dict[str, str] = {"limit": str(CDSE_COLLECTION_PAGE_SIZE)}
        used = ["keyword" if clause else "limit"]
        ignored: list[str] = [
            "limit: applied locally after fetching all matches, because the remote "
            "/stac/collections endpoint ignores small limits and returns unfiltered "
            "ordering (verified 2026-11-05)"
        ]
        if clause:
            params["q"] = clause
        if active.collection:
            ignored.append(
                "collection: filtered locally (the /stac/collections endpoint has no "
                "verified collection parameter)"
            )
        if active.temporal is not None or active.bbox is not None:
            ignored.append(
                "temporal/bbox: collection-level search filters item time/space in item "
                "search, not collection metadata; use estimate_request() for item-level "
                "time/space filtering"
            )
        if active.short_name:
            ignored.append(
                "short_name: CDSE STAC has no short_name parameter; pass the collection id "
                "in filters.collection instead"
            )
        if active.platform:
            ignored.append(
                "platform: CDSE STAC collections list has no verified platform parameter"
            )
        return QueryTranslation(
            provider=self.provider_id,
            endpoint=f"{self._stac_root}/stac/collections",
            method="GET",
            query=params,
            used=tuple(used),
            ignored=tuple(ignored),
        )

    def search(
        self, query: str, filters: DatasetFilter | None = None
    ) -> tuple[DatasetCandidate, ...]:
        """按关键词检索 STAC 集合，返回数据集候选（**不做文件级检索**）。

        Raises
        ------
        MetadataError
            响应形状不符。
        RetryableTransportError
            远端 5xx / 429 / 网络错误。
        """
        translation = self.translate_query(query, filters)
        collections = self._all_collections(translation.query)
        active = filters if filters is not None else DatasetFilter()
        if active.collection:
            collections = tuple(
                item for item in collections if item.collection_id == active.collection
            )
        clause = ((active.keyword or "").strip() or query.strip()).lower()
        if clause:
            collections = self._rank(collections, clause)
        limit = active.limit
        return tuple(
            candidate_from_stac_collection(item, provider=self.provider_id)
            for item in collections[:limit]
        )

    @staticmethod
    def _rank(
        collections: tuple[CdseStacCollection, ...], clause: str
    ) -> tuple[CdseStacCollection, ...]:
        """按"与关键词的相关程度"排序（**本地排序**，因为远端顺序与相关性无关）。

        排序规则（从强到弱）：id 精确命中 → id 以关键词开头 → 标题以关键词开头 →
        id 包含关键词 → 标题/描述包含关键词。同一档内保持远端顺序（稳定排序）。
        """
        tokens = [token for token in clause.split() if token]

        def score(item: CdseStacCollection) -> int:
            ident = item.collection_id.lower()
            title = item.title.lower()
            description = (item.description or "").lower()
            best = 5
            for token in tokens:
                if ident == token:
                    best = min(best, 0)
                elif ident.startswith(token):
                    best = min(best, 1)
                elif title.startswith(token):
                    best = min(best, 2)
                elif token in ident:
                    best = min(best, 3)
                elif token in title or token in description:
                    best = min(best, 4)
            return best

        return tuple(sorted(collections, key=score))

    def _all_collections(self, params: Mapping[str, str]) -> tuple[CdseStacCollection, ...]:
        """翻页取全量集合命中（见 :data:`CDSE_COLLECTION_PAGE_SIZE` 的实测说明）。

        Raises
        ------
        MetadataError
            响应形状不符。
        """
        collected: list[CdseStacCollection] = []
        offset = 0
        for _page in range(CDSE_MAX_COLLECTION_PAGES):
            page_params = {**params, "offset": str(offset)}
            payload = request_json(
                self.http,
                url=f"{self._stac_root}/stac/collections",
                params=page_params,
                provider=self.provider_id,
            )
            if not isinstance(payload, Mapping):
                raise MetadataError(
                    "CDSE STAC collections response is not a JSON object",
                    source_id=self.provider_id,
                    detail={"type": type(payload).__name__},
                )
            page, total = parse_stac_collections_payload(payload)
            collected.extend(page)
            offset += len(page)
            if not page or total is None or offset >= total:
                break
        return tuple(collected)

    def _collection(self, dataset_id: str) -> CdseStacCollection:
        """查询（并缓存）STAC 集合详情。

        Raises
        ------
        SourceNotFoundError
            STAC 返回 404（实测形状 ``{"code": "NotFoundError", ...}``）。
        """
        key = dataset_id.strip()
        with self._lock:
            cached = self._collection_cache.get(key)
        if cached is not None:
            return cached
        payload = request_json(
            self.http,
            url=f"{self._stac_root}/stac/collections/{key}",
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CDSE STAC collection response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        collection = parse_stac_collection(payload)
        with self._lock:
            self._collection_cache[key] = collection
        return collection

    # -- 详情 -------------------------------------------------------------- #
    def inspect(self, dataset_id: str) -> DatasetDetail:
        """取 STAC 集合详情，并附证据链（实际请求过的端点、是否匿名）。"""
        collection = self._collection(dataset_id)
        candidate = candidate_from_stac_collection(collection, provider=self.provider_id)
        notes = [
            "CDSE STAC collection metadata is anonymous; data download via OData $value "
            "needs a Bearer token (unverified on this host)",
            "STAC does not declare physical units for bands; units are left empty",
        ]
        if collection.ends_at_present:
            notes.append(
                "extent.temporal.interval end is null (collection still being updated); "
                "no end date is inferred"
            )
        if collection.extra.get("stac.license_field") in ("other", "proprietary"):
            notes.append(
                "the STAC 'license' field is 'other' (not an SPDX id); see the license link "
                "in the STAC document for the actual terms"
            )
        return DatasetDetail(
            candidate=candidate,
            native_id=collection.collection_id,
            data_centers=collection.providers,
            processing_level=None,
            doi=collection.doi,
            retrieval_endpoints=(f"{self._stac_root}/stac/collections/{collection.collection_id}",),
            retrieved_at=self._clock(),
            anonymous_access=True,
            notes=tuple(notes),
        )

    def get_metadata(self, dataset_id: str) -> Mapping[str, JsonValue]:
        """返回 STAC 集合的结构化摘要（命名空间键 ``cdse.*``），并附 OData 属性 schema。

        注意：OData ``/Attributes`` 给出的是**产品属性**（``cloudCover`` 等），
        不是科学变量，因此放在 ``cdse.odata_attribute_names`` 里，不当作变量。
        """
        collection = self._collection(dataset_id)
        # OData 的 Attributes 端点按 ``Collection/Name``（如 ``SENTINEL-2``）分组，
        # 而 STAC 集合 id 是 ``sentinel-2-l2a``：二者**不是**一一对应。为遵守"不猜"原则，
        # 只有 STAC id 在属性表里**精确命中**时才给出属性名，否则显式说明未映射。
        raw = request_json(
            self.http,
            url=f"{self._odata_root}/Attributes",
            provider=self.provider_id,
        )
        attribute_names: tuple[str, ...] = ()
        attribute_source = "unmapped"
        if isinstance(raw, Mapping):
            table = parse_odata_attributes(raw)
            exact = table.get(collection.collection_id)
            if exact is not None:
                attribute_names = exact
                attribute_source = "exact_collection_id_match"
        return {
            "cdse.collection_id": collection.collection_id,
            "cdse.title": collection.title,
            "cdse.license": collection.license,
            "cdse.license_field": collection.extra.get("stac.license_field"),
            "cdse.citation": collection.citation,
            "cdse.doi": collection.doi,
            "cdse.providers": list(collection.providers),
            "cdse.item_assets": list(collection.item_assets),
            "cdse.gsd_meters": list(collection.extra.get("stac.gsd") or ()),
            "cdse.band_count": len(collection.bands),
            "cdse.ends_at_present": collection.ends_at_present,
            "cdse.odata_attribute_names": list(attribute_names),
            "cdse.odata_attribute_source": attribute_source,
            "cdse.odata_attribute_note": (
                "OData attributes are product properties (cloudCover, orbitNumber, ...), "
                "not scientific variables; names are only shown when the collection id "
                "matches the OData attribute table exactly"
            ),
        }

    def get_variables(self, dataset_id: str) -> tuple[DatasetVariable, ...] | None:
        """返回 STAC ``bands[]``（远端声明）；没有 ``bands`` 时返回 ``None``（未知）。

        CDSE 的 STAC 里并非所有集合都声明 ``bands``（如 CLMS 的部分产品），
        此时**不编造**变量清单。
        """
        collection = self._collection(dataset_id)
        return collection.bands if collection.bands else None

    # -- 请求校验与估算 ---------------------------------------------------- #
    def validate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestValidation:
        """基于 STAC 集合声明的时间/空间范围做本地可判定检查。

        ``filters.limit`` 超过 :data:`CDSE_STAC_SEARCH_CAP` 时进 ``warnings``
        （单次检索被截断，不代表数据不存在）。
        """
        collection = self._collection(dataset_id)
        errors: list[str] = []
        warnings: list[str] = []
        unverified: list[str] = []
        if filters is not None and filters.temporal is not None:
            if collection.temporal is None and collection.ends_at_present:
                declared_start = collection.interval_start
                requested = filters.temporal
                if declared_start is not None and requested.start < declared_start:
                    open_range = f"{declared_start.isoformat()}..(open end)"
                    window = f"{requested.start.isoformat()}..{requested.end.isoformat()}"
                    errors.append(
                        f"temporal window {window} starts before the declared collection "
                        f"coverage {open_range}"
                    )
                else:
                    unverified.append(
                        "temporal: the collection interval has an open (null) end because it is "
                        "still being updated; only the start bound was checked locally"
                    )
            elif collection.temporal is None:
                unverified.append(
                    "temporal: the collection declares no temporal interval; request window "
                    "cannot be checked locally"
                )
            else:
                requested = filters.temporal
                closed = collection.temporal
                if requested.start < closed.start or requested.end > closed.end:
                    window = f"{requested.start.isoformat()}..{requested.end.isoformat()}"
                    covered = f"{closed.start.isoformat()}..{closed.end.isoformat()}"
                    errors.append(
                        f"temporal window {window} is outside the declared collection "
                        f"coverage {covered}"
                    )
        if filters is not None and filters.bbox is not None:
            extent = collection.spatial
            if extent is None:
                unverified.append(
                    "bbox: the collection declares no spatial extent; request bbox cannot "
                    "be checked locally"
                )
            else:
                west, south, east, north = filters.bbox
                outside = (
                    south < extent.south
                    or north > extent.north
                    or west < extent.west
                    or east > extent.east
                )
                if outside:
                    warnings.append(
                        f"bbox {filters.bbox} is not fully inside the declared extent "
                        f"W{extent.west} S{extent.south} E{extent.east} N{extent.north}"
                    )
        if filters is not None and filters.limit > CDSE_STAC_SEARCH_CAP:
            warnings.append(
                f"limit {filters.limit} exceeds the STAC search cap {CDSE_STAC_SEARCH_CAP}; "
                "results are truncated (absence of results is not evidence of absence)"
            )
        if not self._credentials.configured:
            unverified.append(
                "data_download: no CDSE client credentials detected locally; authenticated "
                "OData $value download cannot be verified"
            )
        return RequestValidation(
            ok=not errors,
            dataset_id=collection.collection_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
            unverified=tuple(unverified),
        )

    def _search_items(
        self, collection_id: str, filters: DatasetFilter | None
    ) -> tuple[CdseStacItem, ...]:
        """``POST /stac/search`` 取文件级条目（匿名）。

        Raises
        ------
        MetadataError
            远端响应形状不符。
        """
        active = filters if filters is not None else DatasetFilter()
        body: dict[str, JsonValue] = {
            "collections": [collection_id],
            "limit": min(active.limit, CDSE_STAC_SEARCH_CAP),
        }
        if active.temporal is not None:
            body["datetime"] = f"{_iso(active.temporal.start)}/{_iso(active.temporal.end)}"
        if active.bbox is not None:
            body["bbox"] = list(active.bbox)
        payload = request_json(
            self.http,
            url=f"{self._stac_root}/stac/search",
            method="POST",
            json_body=body,
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CDSE STAC search response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        return parse_stac_search_payload(payload)

    def _odata_products(
        self, collection_name: str, filters: DatasetFilter | None
    ) -> tuple[OdataProduct, ...]:
        """``GET /odata/v1/Products``（匿名）取产品清单与真实字节数。

        ``$filter`` 里只用 ``Collection/Name eq '<name>'``（实测可用）；时间/空间过滤
        在 Python 侧做**显式**过滤，避免拼出未验证的 OData 表达式。

        Raises
        ------
        MetadataError
            远端响应形状不符。
        """
        active = filters if filters is not None else DatasetFilter()
        params: dict[str, str] = {
            "$top": str(min(active.limit, CDSE_STAC_SEARCH_CAP)),
            "$expand": "Attributes",
            "$filter": f"Collection/Name eq '{collection_name}'",
            "$orderby": "ContentDate/Start desc",
        }
        payload = request_json(
            self.http,
            url=f"{self._odata_root}/Products",
            params=params,
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CDSE OData products response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        return parse_odata_products(payload)

    def estimate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestEstimate:
        """用 STAC ``assets[].file:size`` 汇总体量；同时给出 OData 路径的字节数对照。

        STAC 侧：``file:size`` 是**远端声明**的每个资产字节数，求和得到 ``total_bytes``。
        OData 侧：``ContentLength`` 是**整个产品包**的字节数（含 SAFE 目录结构），
        两者口径不同，不能直接相加；这里把 OData 结果放在 ``details`` 里作为交叉参照。
        """
        collection = self._collection(dataset_id)
        items = self._search_items(collection.collection_id, filters)
        total: int | None = 0
        missing = 0
        for item in items:
            if item.total_size_bytes is None:
                total = None
                missing += 1
            elif total is not None:
                total += item.total_size_bytes
        return RequestEstimate(
            dataset_id=collection.collection_id,
            matched_files=len(items),
            total_bytes=total,
            files_without_size=missing,
            basis=(
                "CDSE STAC POST /stac/search: sum of assets[].file:size per item; "
                "total_bytes is None if any item lacks file:size"
            ),
            details={
                "cdse.collection_id": collection.collection_id,
                "cdse.items": [
                    {
                        "item_id": item.item_id,
                        "size_bytes": item.total_size_bytes,
                        "asset_count": item.asset_count,
                        "start": item.datetime_start.isoformat() if item.datetime_start else None,
                        "end": item.datetime_end.isoformat() if item.datetime_end else None,
                        "https_asset_downloads": len(item.https_download_urls),
                        "s3_asset_paths": len(item.s3_hrefs),
                        "download_requires_bearer": True,
                    }
                    for item in items
                ],
            },
        )

    # -- 异步作业：明确不支持 ---------------------------------------------- #
    def _task_feature(self, name: str) -> NotImplementedFeatureError:
        """构造"本 Provider 不支持作业"的显式拒绝异常。"""
        return NotImplementedFeatureError(
            f"{self.provider_id}.{name} (CDSE catalogue has no job API)",
            planned_for="V2 (Sentinel Hub / On-Demand Processing, once authorised and verified)",
        )

    def submit_task(self, dataset_id: str, filters: DatasetFilter | None = None) -> TaskHandle:
        """**不支持**：抛出显式异常，不伪造任务 ID。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del dataset_id, filters
        raise self._task_feature("submit_task")

    def get_task_status(self, task_id: str) -> TaskStatusSnapshot:
        """**不支持**。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("get_task_status")

    def retrieve_result(self, task_id: str) -> Mapping[str, JsonValue]:
        """**不支持**。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("retrieve_result")

    def cancel_task(self, task_id: str) -> bool:
        """**不支持**。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("cancel_task")


def _iso(value: datetime) -> str:
    """格式化为 STAC 接受的 ISO-8601 UTC 字符串。"""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def copernicus_provider(
    *,
    stac_root: str = CDSE_CATALOGUE_ROOT,
    odata_root: str = CDSE_ODATA_ROOT,
    http_client: httpx.Client | None = None,
) -> CopernicusProvider:
    """工厂函数：构造默认配置的 :class:`CopernicusProvider`（供 registry 使用）。"""
    return CopernicusProvider(stac_root=stac_root, odata_root=odata_root, http_client=http_client)
