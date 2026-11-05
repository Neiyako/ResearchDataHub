"""NASA Earthdata Provider（基于真实 CMR API）。

真实端点（2026-11-05 用 ``curl`` 实测，命令与关键字段见 ``docs/providers/earthdata.md``）
----------------------------------------------------------------------------------------
``https://cmr.earthdata.nasa.gov/search/`` 下：

* ``collections.json`` —— 集合检索（``keyword`` / ``short_name`` / ``provider`` /
  ``temporal`` / ``bounding_box`` / ``page_size``），HTTP 200 匿名可读。
* ``collections.umm_json`` —— 集合详情（UMM-C），HTTP 200 匿名可读。
* ``granules.json`` —— 文件级检索（``concept_id`` = 集合 concept-id + 时间/空间参数），
  HTTP 200 匿名可读。
* ``variables.umm_json`` —— 变量元数据（``concept_id`` = 变量 concept-id），HTTP 200 匿名可读。

能力矩阵（逐项，权威来源是 :meth:`EarthdataProvider.capabilities`）
------------------------------------------------------------------
=============== ========== =====================================================
能力             状态        证据
=============== ========== =====================================================
search          verified   curl collections.json HTTP 200（2026-11-05）
inspect         verified   curl collections.umm_json HTTP 200
get_metadata    verified   同上（UMM-C 字段解析）
get_variables   verified   curl variables.umm_json HTTP 200
validate_request verified  基于 UMM 声明的时间/空间范围
estimate_request verified   granules.json 的 granule_size（MB，目录声明值）
submit_task     unsupported CMR 无作业 API → NotImplementedFeatureError
get_task_status unsupported 同上
retrieve_result unsupported 同上
cancel_task     unsupported 同上
=============== ========== =====================================================

**未验证**（不得当成已验证）
----------------------------
* 受保护 granule 的 HTTPS 下载需要 EDL 凭证；本机没有 ``EARTHDATA_TOKEN`` 与
  ``~/.netrc`` 的 EDL 条目，因此"带凭证下载"**未验证**。本 Provider 只返回下载 URL
  与所需凭证类型，**不执行下载**。
* OPeNDAP（``service#`` 链接）子集化能力：未验证，也不在本 V1 实现。
* Harmony 云处理（``service_features.harmony``）：CMR 元数据里能看到声明，但提交作业的
  服务端点未经本环境验证 → **unsupported**。
* S3 直连（``s3://`` 链接 + ``s3credentials``）：**unsupported**（需要临时凭证服务与
  Cumulus 授权，未验证）。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

import httpx

from researchdatahub.core.errors import MetadataError, NotImplementedFeatureError
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
from researchdatahub.providers.earthdata.cmr import (
    CMR_GRANULE_SIZE_UNIT,
    CmrGranule,
    UmmCollectionDetail,
    candidate_from_collection,
    parse_collection_entries,
    parse_granule_entries,
    parse_umm_collection,
    parse_umm_variables,
)
from researchdatahub.providers.earthdata.credentials import (
    EARTHDATA_LOGIN_HOST,
    EarthdataCredentialState,
    probe_credentials,
)

__all__ = [
    "CMR_ROOT",
    "EarthdataProvider",
    "earthdata_provider",
]

_CONCEPT_ID_PATTERN: Final = re.compile(r"^[CGPVS]\d+-[A-Z0-9_]+$")
"""CMR concept-id 形态（实测）：``C2179081499-GES_DISC`` / ``G2187436353-GES_DISC`` /
``V2758121925-GES_DISC``。前缀字母表示 collection/granule/variable/service。"""


CMR_ROOT: Final[str] = "https://cmr.earthdata.nasa.gov"
"""CMR 根地址（实测 2026-11-05：``/search/collections.json`` 等均 200）。"""

_SEARCH: Final[str] = "/search"
_UNSUPPORTED_TASKS: Final[tuple[str, ...]] = (
    "submit_task",
    "get_task_status",
    "retrieve_result",
    "cancel_task",
)
_IMPLEMENTED: Final[tuple[str, ...]] = (
    "search",
    "inspect",
    "get_metadata",
    "get_variables",
    "validate_request",
    "estimate_request",
)


class EarthdataProvider:
    """NASA Earthdata 的目录 Provider：

    * 目录检索走 CMR ``/search/collections.json``（匿名可读，实测）；
    * 详情走 ``/search/collections.umm_json``（匿名可读，实测）；
    * 变量走 ``/search/variables.umm_json``（匿名可读，实测）；
    * 文件级清单与体量估算走 ``/search/granules.json``（匿名可读，实测）。

    线程安全：无状态请求 + 受锁保护的 ``_detail_cache``。
    """

    provider_id: Final[str] = "earthdata"

    def __init__(
        self,
        *,
        base_url: str = CMR_ROOT,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
        credentials: EarthdataCredentialState | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """构造 Provider。

        Parameters
        ----------
        base_url:
            CMR 根地址；离线测试注入本地服务器 URL（回放真实响应样本）。
        timeout:
            单次元数据请求超时（秒）。
        http_client:
            注入的 HTTP 客户端（测试用）；``None`` 时自建并负责关闭。
        credentials:
            凭证存在性快照；``None`` 时本地探测（**只检查存在性，不读取值**）。
        clock:
            时钟（测试注入固定时间）。
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http_client = http_client
        self._owns_http = http_client is None
        self._credentials = credentials if credentials is not None else probe_credentials()
        self._clock = clock
        self._closed = False
        self._cache_lock = threading.Lock()
        self._detail_cache: dict[str, UmmCollectionDetail] = {}

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def http(self) -> httpx.Client:
        """惰性创建的 HTTP 客户端（元数据请求专用超时）。"""
        self._ensure_open()
        if self._http_client is None:
            self._http_client = build_client(base_url=self._base_url, timeout=self._timeout)
            self._owns_http = True
        return self._http_client

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed; create a new instance")

    def close(self) -> None:
        """关闭自建 HTTP 客户端（幂等；注入的客户端不关闭）。"""
        if self._http_client is not None and self._owns_http:
            self._http_client.close()
        self._closed = True

    def __enter__(self) -> EarthdataProvider:
        """上下文入口。"""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """上下文出口，调用 :meth:`close`。"""
        self.close()

    # -- 能力 -------------------------------------------------------------- #
    @property
    def credentials(self) -> EarthdataCredentialState:
        """凭证存在性快照（**不含任何凭证值**）。"""
        return self._credentials

    def capabilities(self) -> ProviderCapabilities:
        """如实返回能力矩阵（与 :mod:`docs/providers/earthdata.md` 的表一致）。"""
        download_ready = self._credentials.download_credential_available()
        unverified = [
            "data_download: protected granules need Earthdata Login credentials; "
            "no EARTHDATA_TOKEN and no EDL entry in ~/.netrc on this host (2026-11-05), "
            "so authenticated byte retrieval is NOT verified",
            "opendap_subsetting: OPeNDAP service links are present in CMR metadata but the "
            "subsetting service was not exercised",
            "harmony_cloud_processing: CMR advertises service_features.harmony, but no job was "
            "submitted to verify it",
        ]
        return ProviderCapabilities(
            provider_id=self.provider_id,
            display_name="NASA Earthdata (CMR)",
            contract_version=CAPABILITY_CONTRACT_VERSION,
            # 仅指**元数据**：CMR 检索匿名可读（实测 2026-11-05）。
            auth_required=False,
            supported_formats=(),
            supported_regions=(),
            supported_temporal_ranges=(),
            supports_async_job=False,
            supports_cloud_processing=False,
            supports_partial_download=False,
            supports_resume=False,
            implemented=_IMPLEMENTED,
            unsupported=_UNSUPPORTED_TASKS,
            verified=(
                "search: CMR /search/collections.json HTTP 200 anonymous (2026-11-05)",
                "inspect: CMR /search/collections.umm_json HTTP 200 anonymous (2026-11-05)",
                "get_metadata: UMM-C fields parsed from a real response (2026-11-05)",
                "get_variables: CMR /search/variables.umm_json HTTP 200 anonymous (2026-11-05)",
                "estimate_request: CMR /search/granules.json granule_size is MB and "
                "catalog-declared (HTTP 200 anonymous, 2026-11-05)",
            ),
            unverified=tuple(unverified),
            credential_features={
                "metadata_query": "verified",
                "granule_search": "verified",
                "data_download": "verified" if download_ready else "unverified",
                "cloud_processing": "unsupported",
                "s3_direct": "unsupported",
            },
            notes=(
                "unsupported access paths (not Provider methods, so listed here instead of "
                "capabilities.unsupported): s3_direct, cloud_processing (Harmony), "
                "opendap_subsetting",
                f"granule_size is reported in {CMR_GRANULE_SIZE_UNIT} (catalog-declared, "
                "not verified against bytes)",
                "S3 direct access (s3:// links) is declared unsupported: no verified "
                "s3credentials flow in this build",
                f"EDL host is {EARTHDATA_LOGIN_HOST}; credentials are only checked for presence",
            ),
        )

    # -- 检索 -------------------------------------------------------------- #
    def translate_query(self, query: str, filters: DatasetFilter | None = None) -> QueryTranslation:
        """把 ``query`` + ``filters`` 翻译成真实 CMR 查询（**不含凭证**，可审计）。

        ``CMR`` 的 ``collections.json`` 实测支持的参数：``keyword`` / ``short_name`` /
        ``provider`` / ``temporal`` / ``bounding_box`` / ``page_size``。
        ``platform`` 与 ``collection`` 在集合级检索里**没有**实测可用的等价参数，
        因此进 ``ignored``（不静默丢弃，也不瞎猜参数名）。
        """
        active = filters if filters is not None else DatasetFilter()
        # short_name 是 CMR 的精确匹配参数，优先于自由文本 keyword（两者都发会被远端取交集）。
        if active.short_name is not None:
            clause = active.short_name
        else:
            clause = (active.keyword or "").strip() or query.strip()
        params: dict[str, str] = {}
        used: list[str] = []
        ignored: list[str] = []
        if clause:
            params["keyword" if active.short_name is None else "short_name"] = clause
            used.append("keyword" if active.short_name is None else "short_name")
        if active.provider:
            params["provider"] = active.provider
            used.append("provider")
        if active.temporal is not None:
            params["temporal"] = f"{_iso(active.temporal.start)},{_iso(active.temporal.end)}"
            used.append("temporal")
        if active.bbox is not None:
            west, south, east, north = active.bbox
            params["bounding_box"] = f"{west},{south},{east},{north}"
            used.append("bbox")
        params["page_size"] = str(active.limit)
        used.append("limit")
        if active.platform:
            ignored.append("platform: CMR collections.json has no verified platform parameter")
        if active.collection:
            ignored.append(
                "collection: use dataset_id directly with inspect() instead of a "
                "collection filter (no verified CMR parameter)"
            )
        return QueryTranslation(
            provider=self.provider_id,
            endpoint=f"{self._base_url}{_SEARCH}/collections.json",
            method="GET",
            query=params,
            used=tuple(used),
            ignored=tuple(ignored),
        )

    def search(
        self, query: str, filters: DatasetFilter | None = None
    ) -> tuple[DatasetCandidate, ...]:
        """检索 CMR 集合，返回 :class:`DatasetCandidate` 元组（不检索文件级清单）。

        Raises
        ------
        MetadataError
            远端响应形状不符，或请求参数被 CMR 拒绝（HTTP 400）。
        RetryableTransportError
            CMR 5xx / 429 / 网络错误。
        """
        translation = self.translate_query(query, filters)
        payload = request_json(
            self.http,
            url=translation.endpoint,
            params=translation.query,
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CMR collections response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        collections = parse_collection_entries(payload)
        return tuple(
            candidate_from_collection(
                collection,
                provider=self.provider_id,
                short_name=collection.short_name or collection.concept_id,
                login_host=EARTHDATA_LOGIN_HOST,
            )
            for collection in collections
        )

    # -- 详情 -------------------------------------------------------------- #
    def _concept_id(self, dataset_id: str) -> str:
        """把 ``dataset_id`` 规范为 CMR concept-id。

        接受三种输入：``C2179081499-GES_DISC``（concept-id，原样）、``GPM_2ADPR``
        （short_name，经 ``collections.json?short_name=`` 查询解析）、
        ``C2179081499-GES_DISC.json``（带扩展名，去掉）。

        short_name 的解析规则（**实测**：CMR 对同一 short_name 可能登记多个版本，
        如 v07 ``C2179081499-GES_DISC`` 与 v08 ``C4054954501-GES_DISC``）：

        1. 只保留 ``cmr.short_name`` **精确等于**输入（忽略大小写）的候选；
        2. 命中多个时取 ``version_id`` **数值最高**的一个；
        3. 最高版本仍并列 → 无法确定，抛错并列出全部候选（不静默挑一个）。
        需要旧版本时请直接传 concept-id。

        Raises
        ------
        MetadataError
            没有命中，或最高版本并列无法确定。
        """
        text = dataset_id.strip()
        if text.endswith(".json"):
            text = text[: -len(".json")]
        if _CONCEPT_ID_PATTERN.match(text):
            return text
        candidates = self.search("", DatasetFilter(short_name=text, limit=50))
        exact = [
            item
            for item in candidates
            if str(item.extra.get("cmr.short_name", "")).strip().lower() == text.lower()
        ]
        pool = exact or list(candidates)
        if not pool:
            raise MetadataError(
                f"CMR has no collection with short_name {text!r}",
                source_id=self.provider_id,
                detail={"short_name": text},
            )
        best_version = max(_version_key(item.extra.get("cmr.version_id")) for item in pool)
        newest = [
            item for item in pool if _version_key(item.extra.get("cmr.version_id")) == best_version
        ]
        if len(newest) > 1:
            raise MetadataError(
                f"CMR short_name {text!r} is ambiguous at the newest version; "
                "pass the concept-id explicitly",
                source_id=self.provider_id,
                detail={"concept_ids": sorted(item.dataset_id for item in newest)},
            )
        return newest[0].dataset_id

    def _collection_detail(self, dataset_id: str) -> UmmCollectionDetail:
        """查询（并缓存）UMM-C 详情。

        Raises
        ------
        MetadataError
            UMM 响应形状不符。
        """
        concept_id = self._concept_id(dataset_id)
        with self._cache_lock:
            cached = self._detail_cache.get(concept_id)
        if cached is not None:
            return cached
        payload = request_json(
            self.http,
            url=f"{self._base_url}{_SEARCH}/collections.umm_json",
            params={"concept_id": concept_id},
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CMR UMM collection response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        detail = parse_umm_collection(payload)
        with self._cache_lock:
            self._detail_cache[concept_id] = detail
        return detail

    def inspect(self, dataset_id: str) -> DatasetDetail:
        """取数据集详情（UMM-C），并记录**实际请求过的端点**作为证据链。

        Notes
        -----
        ``variables`` 通过 ``variables.umm_json`` 的**变量 concept-id 列表**得知其存在，
        但变量名需要逐条查询；为避免 N+1 请求，``inspect`` 只给出变量计数与
        ``variables_known`` 标记，变量名由 :meth:`get_variables` 提供。
        """
        detail = self._collection_detail(dataset_id)
        endpoints = (
            f"{self._base_url}{_SEARCH}/collections.umm_json?concept_id={detail.concept_id}",
        )
        candidate = DatasetCandidate(
            provider=self.provider_id,
            dataset_id=detail.concept_id,
            title=detail.entry_title,
            abstract=detail.abstract,
            variables=(),
            variables_known=False,
            temporal_coverage=detail.temporal,
            spatial_extent=detail.spatial,
            formats=detail.formats,
            access_modes=("https", "s3") if detail.formats else ("https",),
            auth_required=False,
            license=detail.license_text,
            citation=detail.citation,
            landing_url=detail.landing_url,
            native_id=detail.concept_id,
            extra={
                "cmr.short_name": detail.short_name,
                "cmr.version": detail.version,
                "cmr.doi": detail.doi,
                "cmr.processing_level": detail.processing_level,
                "cmr.variable_count": len(detail.variable_concept_ids),
            },
        )
        notes = [
            "variables are registered in CMR by concept-id only; call get_variables() "
            "for names/units",
            "license is the UMM-C UseConstraints description, not a SPDX identifier",
        ]
        if detail.spatial is None:
            notes.append("spatial extent is absent from UMM-C for this collection")
        if detail.temporal is None:
            notes.append("temporal extent is absent from UMM-C for this collection")
        return DatasetDetail(
            candidate=candidate,
            native_id=detail.concept_id,
            data_centers=detail.data_centers,
            processing_level=detail.processing_level,
            doi=detail.doi,
            retrieval_endpoints=endpoints,
            retrieved_at=self._clock(),
            anonymous_access=True,
            notes=tuple(notes),
        )

    def get_metadata(self, dataset_id: str) -> Mapping[str, JsonValue]:
        """返回 UMM-C 的结构化摘要（命名空间键 ``cmr.*``；不含凭证）。"""
        detail = self._collection_detail(dataset_id)
        return {
            "cmr.concept_id": detail.concept_id,
            "cmr.short_name": detail.short_name,
            "cmr.entry_title": detail.entry_title,
            "cmr.version": detail.version,
            "cmr.doi": detail.doi,
            "cmr.processing_level": detail.processing_level,
            "cmr.data_centers": list(detail.data_centers),
            "cmr.formats": list(detail.formats),
            "cmr.license_text": detail.license_text,
            "cmr.citation": detail.citation,
            "cmr.landing_url": detail.landing_url,
            "cmr.variable_concept_ids": list(detail.variable_concept_ids),
            "cmr.temporal_start": (
                detail.temporal.start.isoformat() if detail.temporal is not None else None
            ),
            "cmr.temporal_end": (
                detail.temporal.end.isoformat() if detail.temporal is not None else None
            ),
            "cmr.granule_size_unit": CMR_GRANULE_SIZE_UNIT,
        }

    def get_variables(self, dataset_id: str) -> tuple[DatasetVariable, ...] | None:
        """查询集合登记的变量（逐个变量 concept-id 查 ``variables.umm_json``）。

        返回 ``None`` 表示"没有可用的变量清单"（未登记或查询不出），**不返回空元组**
        冒充"确认没有变量"。
        """
        detail = self._collection_detail(dataset_id)
        variables: list[DatasetVariable] = []
        for variable_id in detail.variable_concept_ids:
            payload = request_json(
                self.http,
                url=f"{self._base_url}{_SEARCH}/variables.umm_json",
                params={"concept_id": variable_id},
                provider=self.provider_id,
            )
            if not isinstance(payload, Mapping):
                continue
            found = parse_umm_variables(payload)
            if found is not None:
                variables.extend(found)
        return tuple(variables) if variables else None

    # -- 请求校验与估算 ---------------------------------------------------- #
    def validate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestValidation:
        """基于 UMM-C 的时间/空间覆盖做本地可判定的检查。

        判定规则
        --------
        * 数据集不存在 → ``ok=False``（异常由内部查询抛出，调用方先捕获或用 search 确认）。
        * ``filters.temporal`` 超出 UMM 声明的时间覆盖 → ``ok=False``（远端**已声明**的范围）。
        * ``filters.bbox`` 不在声明的空间范围内 → 进 ``warnings``（CMR 的 bbox 与
          ``BoundingRectangles`` 语义不完全等价，不据此硬拒）。
        * 时间/空间覆盖缺失 → 进 ``unverified``（无法判定，不得当成已检查）。
        """
        detail = self._collection_detail(dataset_id)
        errors: list[str] = []
        warnings: list[str] = []
        unverified: list[str] = []
        temporal = detail.temporal
        if filters is not None and filters.temporal is not None:
            if temporal is None:
                unverified.append(
                    "temporal: the collection declares no temporal extent; "
                    "request window cannot be checked locally"
                )
            else:
                requested = filters.temporal
                if requested.start < temporal.start or requested.end > temporal.end:
                    window = f"{requested.start.isoformat()}..{requested.end.isoformat()}"
                    declared = f"{temporal.start.isoformat()}..{temporal.end.isoformat()}"
                    errors.append(
                        f"temporal window {window} is outside the declared "
                        f"collection coverage {declared}"
                    )
        spatial = detail.spatial
        if filters is not None and filters.bbox is not None:
            if spatial is None:
                unverified.append(
                    "bbox: the collection declares no spatial extent; "
                    "request bbox cannot be checked locally"
                )
            else:
                west, south, east, north = filters.bbox
                outside = (
                    south < spatial.south
                    or north > spatial.north
                    or west < spatial.west
                    or east > spatial.east
                )
                if outside:
                    warnings.append(
                        f"bbox {filters.bbox} is not fully inside the declared extent "
                        f"W{spatial.west} S{spatial.south} E{spatial.east} N{spatial.north}; "
                        "CMR bounding_box semantics differ from UMM BoundingRectangles"
                    )
        if not self._credentials.download_credential_available():
            unverified.append(
                "data_download: no Earthdata Login credential detected locally; "
                "protected granule access cannot be verified"
            )
        return RequestValidation(
            ok=not errors,
            dataset_id=detail.concept_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
            unverified=tuple(unverified),
        )

    def _granules(self, concept_id: str, filters: DatasetFilter | None) -> tuple[CmrGranule, ...]:
        """查询 granule 清单（带时间/空间/条数过滤）。

        Raises
        ------
        MetadataError
            远端响应形状不符。
        """
        limit = filters.limit if filters is not None else 100
        params: dict[str, str] = {"concept_id": concept_id, "page_size": str(limit)}
        if filters is not None and filters.temporal is not None:
            params["temporal"] = f"{_iso(filters.temporal.start)},{_iso(filters.temporal.end)}"
        if filters is not None and filters.bbox is not None:
            west, south, east, north = filters.bbox
            params["bounding_box"] = f"{west},{south},{east},{north}"
        payload = request_json(
            self.http,
            url=f"{self._base_url}{_SEARCH}/granules.json",
            params=params,
            provider=self.provider_id,
        )
        if not isinstance(payload, Mapping):
            raise MetadataError(
                "CMR granules response is not a JSON object",
                source_id=self.provider_id,
                detail={"type": type(payload).__name__},
            )
        return parse_granule_entries(payload)

    def estimate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestEstimate:
        """用 ``granules.json`` 的 ``granule_size``（MB，目录声明值）估算体量。

        只有远端给了 ``granule_size`` 的 granule 才计入 ``total_bytes``；
        缺大小的计入 :attr:`RequestEstimate.files_without_size`，因此 ``total_bytes``
        是**下界**而不是精确值——这在 ``basis`` 里显式写明。
        """
        concept_id = self._concept_id(dataset_id)
        granules = self._granules(concept_id, filters)
        sizes = [item.size_bytes for item in granules if item.size_bytes is not None]
        missing = len(granules) - len(sizes)
        return RequestEstimate(
            dataset_id=concept_id,
            matched_files=len(granules),
            total_bytes=sum(sizes) if granules and missing == 0 else (sum(sizes) or None),
            files_without_size=missing,
            basis=(
                "CMR /search/granules.json: sum of granule_size (declared in MB, converted "
                "with 1024^2); total_bytes is a lower bound when files_without_size > 0"
            ),
            details={
                "cmr.concept_id": concept_id,
                "cmr.granule_count": len(granules),
                "cmr.size_unit": CMR_GRANULE_SIZE_UNIT,
                "cmr.granules": [
                    {
                        "concept_id": item.concept_id,
                        "granule_size_mb": item.size_declared_mb,
                        "time_start": _iso(item.time_start) if item.time_start else None,
                        "time_end": _iso(item.time_end) if item.time_end else None,
                        # data_links 已按 CMR 的 data# 规则筛过一遍；这里再要求"唯一"，
                        # 多文件 granule 时返回 None（不挑第一个冒充全部）。
                        "download_url": (
                            item.data_links[0].href if len(item.data_links) == 1 else None
                        ),
                        "download_links": [link.href for link in item.data_links],
                        "download_links_ambiguous": len(item.data_links) != 1,
                        "s3_paths": list(item.s3_links),
                        "requires_edl": True,
                    }
                    for item in granules
                ],
            },
        )

    # -- 异步作业：明确不支持 ---------------------------------------------- #
    def _task_feature(self, name: str) -> NotImplementedFeatureError:
        """构造"本 Provider 不支持作业"的显式拒绝异常。"""
        return NotImplementedFeatureError(
            f"{self.provider_id}.{name} (CMR exposes no job API)",
            planned_for="V2 (Harmony / OPeNDAP subsetting, once verified)",
        )

    def submit_task(self, dataset_id: str, filters: DatasetFilter | None = None) -> TaskHandle:
        """**不支持**：CMR 没有作业提交 API；抛出显式异常而不是伪造任务 ID。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del dataset_id, filters
        raise self._task_feature("submit_task")

    def get_task_status(self, task_id: str) -> TaskStatusSnapshot:
        """**不支持**：无作业状态 API。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("get_task_status")

    def retrieve_result(self, task_id: str) -> Mapping[str, JsonValue]:
        """**不支持**：无作业产物 API。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("retrieve_result")

    def cancel_task(self, task_id: str) -> bool:
        """**不支持**：无作业取消 API。

        Raises
        ------
        NotImplementedFeatureError
            总是抛出。
        """
        del task_id
        raise self._task_feature("cancel_task")


def _version_key(raw: object) -> tuple[int, int]:
    """把 CMR ``version_id``（实测形如 ``"07"`` / ``"08"``）转成可比较的排序键。

    非数字版本（如 ``"V1"``）按 0 处理并排在最前；无法比较时不猜测语义。
    """
    text = str(raw or "").strip().lstrip("vV")
    return (int(text), 0) if text.isdigit() else (0, 0)


def _iso(value: datetime) -> str:
    """把时间格式化为 CMR 接受的 ISO-8601 UTC 字符串（``2024-06-01T00:00:00Z``）。"""
    from datetime import UTC

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def earthdata_provider(
    *,
    base_url: str = CMR_ROOT,
    http_client: httpx.Client | None = None,
) -> EarthdataProvider:
    """工厂函数：构造默认配置的 :class:`EarthdataProvider`（供 registry 使用）。"""
    return EarthdataProvider(base_url=base_url, http_client=http_client)
