"""Copernicus Data Space Ecosystem（CDSE）Provider 测试：STAC/OData 真实响应解析与能力声明。

fixtures 全部是 2026-11-05 对 ``https://catalogue.dataspace.copernicus.eu`` 与
``https://identity.dataspace.copernicus.eu`` 的真实响应快照（见 ``conftest.py`` 清单与
``docs/providers/copernicus.md`` 的 curl 实证）。本地服务器回放这些真实形状；**没有 mock socket**。

关键实测事实（测试据此断言，而不是依据想象）
--------------------------------------------
* ``GET /stac/collections`` **忽略小 limit**（不传/``limit=5`` 都返回 10 条），排序与 ``q``
  的相关性无关；``limit=1000`` 一次返回全部 145 条命中。因此 Provider 必须翻页取全量、
  再本地排序。
* ``GET /odata/v1/Collections``、``/odata/v1/``、``/$metadata`` 实测 **404**。
* ``sentinel-2-l2a`` 的 ``extent.temporal.interval`` 结束时间是 ``null``（仍在更新）。
* token 端点用无效凭证返回 ``401 {"error": "invalid_client"}``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_providers.conftest import (
    CDSE_S2_COLLECTION,
    FIXED_NOW,
    LocalHttpServer,
    Response,
    install_cdse_routes,
    load_fixture,
    make_client,
)

from researchdatahub.core.errors import (
    MetadataError,
    NotImplementedFeatureError,
    RetryableTransportError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import TemporalExtent
from researchdatahub.providers.base import (
    DatasetFilter,
    assert_provider_contract,
    probe_unsupported,
)
from researchdatahub.providers.copernicus.auth import (
    CDSE_TOKEN_ENDPOINT,
    CdseCredentials,
    CdseTokenCache,
    probe_credentials,
)
from researchdatahub.providers.copernicus.cdse import (
    candidate_from_stac_collection,
    parse_odata_attributes,
    parse_odata_products,
    parse_odata_products_node,
    parse_stac_collection,
    parse_stac_collections_payload,
    parse_stac_item,
    parse_stac_search_payload,
    stac_bands_to_variables,
)
from researchdatahub.providers.copernicus.provider import (
    CDSE_COLLECTION_PAGE_SIZE,
    CopernicusProvider,
)

FIXTURES = Path(__file__).parent / "fixtures"
NO_CREDENTIALS = CdseCredentials(configured=False)
WITH_CREDENTIALS = CdseCredentials(configured=True)
_SEARCH_PATH = "/stac/collections"


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def _provider(
    server: LocalHttpServer,
    *,
    credentials: CdseCredentials | None = None,
    token_cache: CdseTokenCache | None = None,
) -> CopernicusProvider:
    """构造指向本地服务器的 Provider（注入固定时钟与凭证快照）。"""
    return CopernicusProvider(
        stac_root=server.url,
        odata_root=f"{server.url}/odata/v1",
        http_client=make_client(server.url),
        credentials=credentials or NO_CREDENTIALS,
        token_cache=token_cache,
        clock=lambda: FIXED_NOW,
    )


@pytest.fixture
def provider(httpserver: LocalHttpServer) -> CopernicusProvider:
    """已注册真实 CDSE 路由的 Provider。"""
    install_cdse_routes(httpserver)
    instance = _provider(httpserver)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------------- #
# STAC 集合解析
# --------------------------------------------------------------------------- #
def test_parse_stac_collections_payload_reports_total_matches():
    """真实响应 ``numberMatched=428`` / ``numberReturned=10``；样本裁为 3 条。"""
    payload = load_fixture("cdse_stac_collections.json")
    collections, total = parse_stac_collections_payload(payload)
    assert total == 428
    assert len(collections) == 3
    assert collections[0].collection_id.startswith("ccm-")


def test_parse_stac_collection_reads_real_sentinel2_fields():
    """``sentinel-2-l2a`` 的真实字段：license=other、sci:citation、sci:doi、providers。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    assert collection.collection_id == CDSE_S2_COLLECTION
    assert collection.title == "Sentinel-2 Level-2A"
    assert collection.doi == "10.5270/S2_-znk9xsj"
    assert collection.citation == "Copernicus Sentinel data [Year]"
    assert "ESA" in collection.providers
    # STAC 的 license 字段实测是 "other"（不是 SPDX）→ 保留原值并显式标注
    assert collection.license == "STAC license field: other"
    assert collection.extra["stac.license_field"] == "other"


def test_parse_stac_collection_reads_global_spatial_extent():
    """Sentinel-2 的空间范围是真实全球框 ``[-180, -90, 180, 90]``。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    spatial = collection.spatial
    assert spatial is not None
    assert (spatial.west, spatial.south, spatial.east, spatial.north) == (
        -180.0,
        -90.0,
        180.0,
        90.0,
    )


def test_parse_stac_collection_handles_open_ended_interval():
    """``interval`` 结束时间为 null（仍在更新）→ ``temporal=None`` 但保留起点，不推断结束。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    assert collection.temporal is None
    assert collection.ends_at_present is True
    assert collection.interval_start == datetime(2015, 6, 27, 10, 25, 31, tzinfo=UTC)


def test_parse_stac_collection_reads_bands():
    """真实 ``bands[]`` 含 name/description/gsd/eo:common_name。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    assert len(collection.bands) >= 10
    first = collection.bands[0]
    assert first.name == "B01"
    assert first.long_name == "Coastal aerosol (band 1)"
    # description 里附带 eo:common_name；单位仍是 None（STAC 不声明物理单位 → 不编造）
    assert first.description == "Coastal aerosol (band 1); common_name=coastal"
    assert first.units is None


def test_parse_stac_collection_reads_gsd_summary():
    """``summaries.gsd`` 实测为 ``[10]``（米）。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    assert collection.extra["stac.gsd"] == (10.0,)


def test_parse_stac_collection_rejects_missing_id():
    """没有 ``id`` 的集合对象 → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'id'"):
        parse_stac_collection({"type": "Collection", "title": "x"})


def test_parse_stac_collections_payload_rejects_missing_list():
    """没有 ``collections`` 列表 → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'collections' list"):
        parse_stac_collections_payload({"links": []})


def test_stac_bands_to_variables_keeps_unknown_units_empty():
    """波段转换不填充单位，但把 gsd 作为可机读提示放到 data_type 里（不改写为物理单位）。"""
    variables = stac_bands_to_variables(
        [{"name": "B02", "description": "Blue (band 2)", "gsd": 10, "eo:common_name": "blue"}]
    )
    assert len(variables) == 1
    assert variables[0].name == "B02"
    assert variables[0].units is None
    assert variables[0].data_type == "gsd=10m"


def test_candidate_from_stac_collection_does_not_claim_s3_access():
    """``access_modes`` 只列 HTTPS（S3 直连未验证，不在 modes 里冒充可用）。"""
    collection = parse_stac_collection(load_fixture("cdse_stac_collection_sentinel2_l2a.json"))
    candidate = candidate_from_stac_collection(collection, provider="copernicus")
    assert candidate.access_modes == ("https",)
    assert candidate.auth_required is False
    assert candidate.variables_known is True


# --------------------------------------------------------------------------- #
# STAC 检索结果
# --------------------------------------------------------------------------- #
def test_parse_stac_search_payload_reads_real_features():
    """``POST /stac/search`` 返回 FeatureCollection；样本保留 1 条真实 feature。"""
    items = parse_stac_search_payload(load_fixture("cdse_stac_search_sentinel2.json"))
    assert len(items) == 1
    first = items[0]
    assert first.item_id.startswith("S2A_MSIL2A_20240629T102021_")
    assert first.collection == CDSE_S2_COLLECTION


def test_parse_stac_item_sums_asset_sizes():
    """``assets[].file:size`` 是远端声明的字节数，汇总为该 item 的体量。"""
    items = parse_stac_search_payload(load_fixture("cdse_stac_search_sentinel2.json"))
    first = items[0]
    assert first.asset_count == 47
    assert first.total_size_bytes is not None
    assert first.total_size_bytes > 1_000_000_000


def test_parse_stac_item_collects_https_and_s3_references():
    """资产同时带 S3 href 与 OData ``$value`` 的 HTTPS alternate。"""
    items = parse_stac_search_payload(load_fixture("cdse_stac_search_sentinel2.json"))
    first = items[0]
    assert first.https_download_urls
    # 只收 CDSE 自己的下载主机；CREODIAS(datahub.creodias.eu) 的资产被排除
    assert all(
        url.startswith("https://download.dataspace.copernicus.eu/")
        for url in first.https_download_urls
    )
    # 47 个资产里只有 1 个（thumbnail）指向 CREODIAS，其余 46 个都是 CDSE 端点
    assert len(first.https_download_urls) == first.asset_count - 1
    assert not any("creodias" in url for url in first.https_download_urls)
    assert first.s3_hrefs
    assert all(path.startswith("s3://eodata/") for path in first.s3_hrefs)


def test_parse_stac_item_reads_bbox_and_time():
    """item 的 bbox 与采集时间来自真实 ``bbox`` / ``properties``。"""
    items = parse_stac_search_payload(load_fixture("cdse_stac_search_sentinel2.json"))
    bbox = items[0].bbox
    assert bbox is not None
    assert (bbox.west, bbox.south) == pytest.approx((10.290484, 45.939530), rel=1e-5)
    assert items[0].datetime_start is not None
    assert items[0].datetime_start.year == 2024


def test_parse_stac_item_rejects_missing_id():
    """缺 ``id`` 的 feature → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'id'"):
        parse_stac_item({"type": "Feature", "properties": {}})


def test_parse_stac_search_payload_rejects_missing_features():
    """没有 ``features`` 列表 → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'features' list"):
        parse_stac_search_payload({"type": "FeatureCollection"})


# --------------------------------------------------------------------------- #
# OData 产品解析
# --------------------------------------------------------------------------- #
def test_parse_odata_products_reads_real_product():
    """OData 产品：UUID、SAFE 名称、ContentLength（字节）、S3Path、Online。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    assert len(products) == 2
    first = products[0]
    assert first.product_id == "018805e8-b419-4040-b6e0-12b3cd883320"
    assert first.name.startswith("S2C_MSIL2A_")
    assert first.content_length == 607_269_158
    assert first.online is True
    assert first.s3_path is not None
    assert first.s3_path.startswith("/eodata/Sentinel-2/")


def test_parse_odata_products_reads_real_checksums():
    """``Checksum[]`` 实测含 MD5 与 BLAKE3 两种算法。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    checksums = products[0].checksums
    assert checksums["md5"] == "31aeeec5930af4f765efca3af4d98566"
    assert "blake3" in checksums


def test_parse_odata_products_reads_geo_footprint_bbox():
    """``GeoFootprint`` 是真 GeoJSON Polygon；只取外包框。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    spatial = products[0].footprints
    assert spatial is not None
    assert spatial.west == pytest.approx(-25.887, abs=0.01)
    assert spatial.south == pytest.approx(36.039, abs=0.01)


def test_parse_odata_products_reads_content_dates():
    """``ContentDate.{Start,End}`` 的原始时刻被解析成 UTC。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    product = products[0]
    expected = datetime(2026, 11, 5, 12, 40, 31, 25000, tzinfo=UTC)
    assert product.start == expected
    assert product.end == expected


def test_zero_length_content_date_yields_no_interval():
    """实测单景产品 ``ContentDate.Start == End``（是时刻不是区间）→ 不构造区间，也不加一秒凑数。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    product = products[0]
    assert product.start == product.end
    assert product.temporal_extent() is None


def test_parse_odata_products_flattens_attributes():
    """``Attributes[].{Name,Value}`` 被摊平成字典（cloudCover / tileId 等真实属性）。"""
    products = parse_odata_products(load_fixture("cdse_odata_products_sentinel2.json"))
    attributes = products[0].attributes
    assert attributes["origin"] == "ESA"
    assert attributes["tileId"] == "26SPF"
    assert attributes["cloudCover"] == pytest.approx(47.305813)
    assert attributes["orbitNumber"] == 11319


def test_parse_odata_products_node_rejects_missing_identity():
    """缺 ``Id``/``Name`` → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="missing 'Id' or 'Name'"):
        parse_odata_products_node({"ContentLength": 1})


def test_parse_odata_products_rejects_missing_value_list():
    """没有 ``value`` 列表 → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'value' list"):
        parse_odata_products({"@odata.context": "x"})


def test_parse_odata_attributes_is_property_schema_not_variables():
    """``/odata/v1/Attributes`` 是**产品属性 schema**（26 个集合名），不是科学变量清单。"""
    table = parse_odata_attributes(load_fixture("cdse_odata_attributes_sentinel2.json"))
    assert len(table) == 26
    assert "SENTINEL-2" in table
    assert "productType" in table["COP-DEM"]
    # 集合名用的是 OData 的 ``SENTINEL-2``，与 STAC id ``sentinel-2-l2a`` 不是一一对应
    assert "sentinel-2-l2a" not in table


# --------------------------------------------------------------------------- #
# 认证：token 只在内存、错误形状真实
# --------------------------------------------------------------------------- #
def test_probe_credentials_reports_missing_by_default():
    """未设置 ``CDSE_CLIENT_ID``/``CDSE_CLIENT_SECRET`` 时报告未配置（本机真实状态）。"""
    credentials = probe_credentials(environ={})
    assert credentials.configured is False
    assert len(credentials.missing()) == 2


def test_probe_credentials_detects_presence_without_exposing_values():
    """两个变量都存在才算已配置；快照**不含任何值**。"""
    credentials = probe_credentials(
        environ={"CDSE_CLIENT_ID": "abc", "CDSE_CLIENT_SECRET": "super-secret"}
    )
    assert credentials.configured is True
    assert credentials.missing() == ()
    assert "super-secret" not in str(credentials.to_dict())
    assert "super-secret" not in repr(credentials)


def test_token_cache_raises_when_not_configured():
    """未配置凭证时取 token → ``SourceUnavailableError``（不返回空字符串冒充 token）。"""
    cache = CdseTokenCache(environ={})
    try:
        with pytest.raises(SourceUnavailableError, match="not configured"):
            cache.token()
    finally:
        cache.close()


def test_token_cache_never_exposes_token_in_repr(httpserver):
    """成功换到 token 后，``repr``/``str`` 都不得包含 token 值。"""
    httpserver.add_json(
        "POST",
        "/identity/token",
        {"access_token": "eyJhbGciOi.very-secret-token", "expires_in": 600, "token_type": "Bearer"},
    )
    cache = CdseTokenCache(
        token_endpoint=f"{httpserver.url}/identity/token",
        http_client=make_client(httpserver.url),
        environ={"CDSE_CLIENT_ID": "cid", "CDSE_CLIENT_SECRET": "csecret"},
    )
    try:
        token = cache.token()
        assert token.startswith("eyJhbGciOi")
        assert "very-secret-token" not in repr(cache)
        assert "very-secret-token" not in str(cache)
        assert "csecret" not in repr(cache)
    finally:
        cache.close()


def test_token_cache_reuses_cached_token(httpserver):
    """未过期时复用缓存，只向端点请求一次。"""
    httpserver.add_json("POST", "/identity/token", {"access_token": "t1", "expires_in": 600})
    cache = CdseTokenCache(
        token_endpoint=f"{httpserver.url}/identity/token",
        http_client=make_client(httpserver.url),
        environ={"CDSE_CLIENT_ID": "cid", "CDSE_CLIENT_SECRET": "csecret"},
    )
    try:
        assert cache.token() == "t1"
        assert cache.token() == "t1"
        assert len(httpserver.requests_to("/identity/token")) == 1
        assert cache.has_valid_token() is True
    finally:
        cache.close()


def test_token_cache_refreshes_after_invalidate(httpserver):
    """``invalidate()`` 后强制重新换取（401 时的处理路径）。"""
    httpserver.add_json("POST", "/identity/token", {"access_token": "t1", "expires_in": 600})
    cache = CdseTokenCache(
        token_endpoint=f"{httpserver.url}/identity/token",
        http_client=make_client(httpserver.url),
        environ={"CDSE_CLIENT_ID": "cid", "CDSE_CLIENT_SECRET": "csecret"},
    )
    try:
        cache.token()
        cache.invalidate()
        assert cache.has_valid_token() is False
        cache.token()
        assert len(httpserver.requests_to("/identity/token")) == 2
    finally:
        cache.close()


def test_token_endpoint_401_maps_to_source_unavailable(httpserver):
    """真实 401 形状（``invalid_client``）→ ``SourceUnavailableError`` 且带 oauth_error。"""
    install_cdse_routes(httpserver)
    cache = CdseTokenCache(
        token_endpoint=f"{httpserver.url}/identity/token",
        http_client=make_client(httpserver.url),
        environ={"CDSE_CLIENT_ID": "bad", "CDSE_CLIENT_SECRET": "bad"},
    )
    try:
        with pytest.raises(SourceUnavailableError) as excinfo:
            cache.token()
        assert excinfo.value.detail["status"] == 401
        assert excinfo.value.detail["oauth_error"] == "invalid_client"
    finally:
        cache.close()


def test_token_endpoint_missing_access_token_is_rejected(httpserver):
    """200 但响应里没有 ``access_token`` → 显式失败（不把空值当 token）。"""
    httpserver.add_json("POST", "/identity/token", {"token_type": "Bearer"})
    cache = CdseTokenCache(
        token_endpoint=f"{httpserver.url}/identity/token",
        http_client=make_client(httpserver.url),
        environ={"CDSE_CLIENT_ID": "cid", "CDSE_CLIENT_SECRET": "csecret"},
    )
    try:
        with pytest.raises(SourceUnavailableError, match="no access_token"):
            cache.token()
    finally:
        cache.close()


def test_default_token_endpoint_is_the_verified_one():
    """默认 token 端点必须与实测 ``.well-known`` 一致。"""
    assert CDSE_TOKEN_ENDPOINT == (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )


# --------------------------------------------------------------------------- #
# Provider：能力声明
# --------------------------------------------------------------------------- #
def test_provider_is_not_the_cds_platform(provider):
    """CDSE ≠ CDS：provider_id 与展示名必须区分卫星数据与 ERA5 再分析。"""
    caps = provider.capabilities()
    assert caps.provider_id == "copernicus"
    assert "Copernicus Data Space" in caps.display_name
    assert any("not the same platform as CDS" in note for note in caps.notes)


def test_capabilities_declare_task_methods_unsupported(provider):
    """CDSE 目录没有作业语义 → 四个任务方法明确不支持。"""
    caps = provider.capabilities()
    assert set(caps.unsupported) == {
        "submit_task",
        "get_task_status",
        "retrieve_result",
        "cancel_task",
    }
    assert caps.supports_async_job is False


def test_capabilities_mark_download_unverified_without_credentials(provider):
    """无凭证时下载必须标 unverified；元数据检索匿名可读（verified）。"""
    caps = provider.capabilities()
    assert caps.auth_required is False
    assert caps.credential_features["data_download"] == "unverified"
    assert caps.credential_features["metadata_query"] == "verified"
    assert caps.credential_features["s3_direct"] == "unsupported"
    assert any("data_download" in item for item in caps.unverified)


def test_capabilities_reflect_configured_credentials(httpserver):
    """配置了凭证时凭证矩阵升到 verified（仍不等于已经验证过下载）。"""
    install_cdse_routes(httpserver)
    instance = _provider(httpserver, credentials=WITH_CREDENTIALS)
    try:
        assert instance.capabilities().credential_features["data_download"] == "verified"
    finally:
        instance.close()


def test_capabilities_document_404_odata_endpoints(provider):
    """实测 404 的 OData 端点必须在 notes 里说明，避免误导调用方。"""
    caps = provider.capabilities()
    joined = " ".join(caps.notes)
    assert "/Collections" in joined
    assert "404" in joined


def test_provider_passes_contract_self_check(provider):
    """Provider 满足 ProviderContract 且声明与行为一致。"""
    assert_provider_contract(provider)


# --------------------------------------------------------------------------- #
# Provider：检索（含翻页与本地排序）
# --------------------------------------------------------------------------- #
def test_translate_query_records_why_limit_is_local(provider):
    """``limit`` 是本地生效的：远端忽略小 limit 且排序与相关性无关（实测）。"""
    translation = provider.translate_query("sentinel-2", DatasetFilter(limit=5))
    assert translation.endpoint.endswith("/stac/collections")
    assert translation.query["q"] == "sentinel-2"
    assert translation.query["limit"] == str(CDSE_COLLECTION_PAGE_SIZE)
    assert any(item.startswith("limit:") for item in translation.ignored)


def test_translate_query_reports_unsupported_filters(provider):
    """``short_name`` / ``platform`` / ``temporal`` 在集合检索里没有实测等价参数。"""
    window = TemporalExtent(
        start=datetime(2024, 6, 1, tzinfo=UTC), end=datetime(2024, 6, 2, tzinfo=UTC)
    )
    translation = provider.translate_query(
        "x",
        DatasetFilter(short_name="S2", platform="Sentinel-2", temporal=window),
    )
    joined = " ".join(translation.ignored)
    assert "short_name" in joined
    assert "platform" in joined
    assert "temporal/bbox" in joined


def test_search_pages_through_all_matches(httpserver):
    """远端 ``numberMatched=428`` 但本页只给 3 条 → Provider 必须继续翻页直到取全。"""
    install_cdse_routes(httpserver)
    provider = _provider(httpserver)
    try:
        candidates = provider.search("sentinel-2", DatasetFilter(limit=3))
        sent = httpserver.requests_to(_SEARCH_PATH)
        # 第 1 页 offset=0；第 2 页 offset=3（按实际返回条数前进）
        assert len(sent) >= 2
        assert "offset=0" in sent[0].query
        assert "offset=3" in sent[1].query
        assert len(candidates) == 3  # 本地 limit 生效
    finally:
        provider.close()


def test_search_sends_anonymous_requests(httpserver, provider):
    """STAC 集合检索必须匿名（请求不带 ``Authorization``）。"""
    provider.search("sentinel-2", DatasetFilter(limit=2))
    sent = httpserver.requests_to(_SEARCH_PATH)
    assert sent
    assert all(item.authorization is None for item in sent)


def test_search_ranking_prefers_id_matches():
    """本地排序：id 精确/前缀命中排在前面（远端顺序与相关性无关）。"""
    ranked = CopernicusProvider._rank(
        (parse_stac_collection(load_fixture("cdse_stac_collections.json")["collections"][0]),),
        "ccm",
    )
    assert ranked[0].collection_id.startswith("ccm-")


def test_search_returns_candidates_with_declared_bands(provider):
    """集合列表本身不含 bands（真实响应），因此候选的 ``variables_known`` 为 False。"""
    candidates = provider.search("ccm", DatasetFilter(limit=3))
    assert candidates
    assert candidates[0].provider == "copernicus"
    assert candidates[0].variables_known is False
    assert candidates[0].auth_required is False


# --------------------------------------------------------------------------- #
# Provider：详情 / 变量 / 元数据
# --------------------------------------------------------------------------- #
def test_inspect_returns_real_sentinel2_detail(provider):
    """``inspect`` 返回 Sentinel-2 的真实 DOI、空间覆盖、证据链与说明。"""
    detail = provider.inspect(CDSE_S2_COLLECTION)
    assert detail.candidate.dataset_id == CDSE_S2_COLLECTION
    assert detail.candidate.title == "Sentinel-2 Level-2A"
    assert detail.doi == "10.5270/S2_-znk9xsj"
    assert detail.anonymous_access is True
    assert detail.retrieved_at == FIXED_NOW
    assert detail.retrieval_endpoints == (f"{detail.retrieval_endpoints[0]}",)
    assert "sentinel-2-l2a" in detail.retrieval_endpoints[0]


def test_inspect_explains_open_ended_temporal_interval(provider):
    """结束时间为 null 的集合：详情里显式说明"未推断结束日期"。"""
    detail = provider.inspect(CDSE_S2_COLLECTION)
    assert detail.candidate.temporal_coverage is None
    assert any("null" in note and "inferred" in note for note in detail.notes)


def test_inspect_does_not_invent_processing_level(provider):
    """STAC 顶层没有 processing level 字段 → 保持 ``None``（不猜 L2A 以外的值）。"""
    detail = provider.inspect(CDSE_S2_COLLECTION)
    assert detail.processing_level is None


def test_inspect_404_maps_to_source_not_found(httpserver):
    """真实 404 形状（``{"code": "NotFoundError", ...}``）→ ``SourceNotFoundError``。"""
    install_cdse_routes(httpserver)
    provider = _provider(httpserver)
    try:
        with pytest.raises(SourceNotFoundError):
            provider.inspect("not-a-real-collection-xyz")
    finally:
        provider.close()


def test_get_variables_returns_real_bands(provider):
    """``get_variables`` 返回 STAC ``bands[]``（Sentinel-2 真实 12 个波段条目）。"""
    variables = provider.get_variables(CDSE_S2_COLLECTION)
    assert variables is not None
    assert len(variables) == 12
    assert variables[0].name == "B01"
    assert variables[0].description == "Coastal aerosol (band 1); common_name=coastal"


def test_get_metadata_includes_attribute_provenance(provider):
    """``get_metadata`` 键名空间化，并说明 OData 属性表未与 STAC id 精确对应。"""
    metadata = provider.get_metadata(CDSE_S2_COLLECTION)
    assert all(key.startswith("cdse.") for key in metadata)
    assert metadata["cdse.collection_id"] == CDSE_S2_COLLECTION
    assert metadata["cdse.odata_attribute_source"] == "unmapped"
    assert metadata["cdse.band_count"] == 12
    assert metadata["cdse.ends_at_present"] is True


# --------------------------------------------------------------------------- #
# Provider：校验与估算
# --------------------------------------------------------------------------- #
def test_estimate_request_uses_stac_asset_sizes(httpserver, provider):
    """体量来自 STAC ``assets[].file:size``（item 级汇总），并走真实 ``POST /stac/search``。"""
    estimate = provider.estimate_request(
        CDSE_S2_COLLECTION, DatasetFilter(limit=1, bbox=(10.0, 45.0, 11.0, 46.0))
    )
    assert estimate.matched_files == 1
    assert estimate.total_bytes is not None
    assert estimate.total_bytes > 1_000_000_000
    assert estimate.files_without_size == 0
    assert "file:size" in estimate.basis
    posts = httpserver.requests_to("/stac/search")
    assert posts
    assert posts[0].method == "POST"


def test_estimate_request_sends_datetime_and_bbox(httpserver, provider):
    """时间与空间过滤被翻译成 STAC search 请求体的 ``datetime`` / ``bbox``。"""
    window = TemporalExtent(
        start=datetime(2024, 6, 1, tzinfo=UTC), end=datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
    )
    provider.estimate_request(CDSE_S2_COLLECTION, DatasetFilter(limit=1, temporal=window))
    body = httpserver.requests_to("/stac/search")[0].json_body()
    assert body["collections"] == [CDSE_S2_COLLECTION]
    assert body["datetime"] == "2024-06-01T00:00:00Z/2024-06-30T23:59:59Z"


def test_estimate_request_reports_item_download_requires_bearer(provider):
    """估算明细显式标注"下载需要 Bearer"（不假装能匿名下载）。"""
    estimate = provider.estimate_request(CDSE_S2_COLLECTION, DatasetFilter(limit=1))
    items = estimate.details["cdse.items"]
    assert isinstance(items, list)
    assert items
    first = items[0]
    assert isinstance(first, dict)
    assert first["download_requires_bearer"] is True
    assert first["asset_count"] == 47


def test_validate_request_rejects_window_before_declared_start(provider):
    """集合仍在更新（end=null）：早于声明起点的窗口必须判为不通过。"""
    window = TemporalExtent(
        start=datetime(2001, 1, 1, tzinfo=UTC), end=datetime(2002, 1, 1, tzinfo=UTC)
    )
    result = provider.validate_request(CDSE_S2_COLLECTION, DatasetFilter(temporal=window))
    assert result.ok is False
    assert result.errors
    assert "starts before the declared collection coverage" in result.errors[0]


def test_validate_request_flags_open_end_as_unverified(provider):
    """闭合窗口但集合结束时间未知 → 只检查了起点，其余进 ``unverified``（不得说"已检查"）。"""
    window = TemporalExtent(
        start=datetime(2024, 6, 1, tzinfo=UTC), end=datetime(2024, 7, 1, tzinfo=UTC)
    )
    result = provider.validate_request(CDSE_S2_COLLECTION, DatasetFilter(temporal=window))
    assert result.ok is True
    assert any("open (null) end" in item for item in result.unverified)


def test_validate_request_warns_when_limit_exceeds_cap(provider):
    """``limit`` 超过单次检索上限时给 warning（截断 ≠ 数据不存在）。"""
    result = provider.validate_request(CDSE_S2_COLLECTION, DatasetFilter(limit=150))
    assert result.ok is True
    assert any("exceeds the STAC search cap" in item for item in result.warnings)


def test_validate_request_reports_missing_credentials_as_unverified(provider):
    """无凭证时"能否下载"进 ``unverified``。"""
    result = provider.validate_request(CDSE_S2_COLLECTION, DatasetFilter())
    assert any("data_download" in item for item in result.unverified)


# --------------------------------------------------------------------------- #
# Provider：不支持的能力必须显式拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method_name",
    ["submit_task", "get_task_status", "retrieve_result", "cancel_task"],
)
def test_task_methods_raise_not_implemented(provider, method_name):
    """CDSE 目录没有作业 API → 任务方法抛 ``NotImplementedFeatureError``。"""
    method = getattr(provider, method_name)
    args = (CDSE_S2_COLLECTION,) if method_name == "submit_task" else ("task-1",)
    with pytest.raises(NotImplementedFeatureError) as excinfo:
        method(*args)
    assert "CDSE catalogue has no job API" in excinfo.value.message


def test_probe_unsupported_confirms_honest_declaration(provider):
    """声明与行为一致。"""
    assert probe_unsupported(provider.submit_task, CDSE_S2_COLLECTION) is True


# --------------------------------------------------------------------------- #
# 错误映射与生命周期
# --------------------------------------------------------------------------- #
def test_server_error_is_retryable(httpserver):
    """5xx → 可重试错误。"""
    httpserver.add("GET", _SEARCH_PATH, Response(status=503, body=b"unavailable"))
    provider = _provider(httpserver)
    try:
        with pytest.raises(RetryableTransportError) as excinfo:
            provider.search("x", DatasetFilter(limit=1))
        assert excinfo.value.retryable is True
    finally:
        provider.close()


def test_bad_request_maps_to_metadata_error(httpserver):
    """400 → ``MetadataError``（请求形状不被接受）。"""
    httpserver.add("GET", _SEARCH_PATH, Response.json({"description": "bad"}, status=400))
    provider = _provider(httpserver)
    try:
        with pytest.raises(MetadataError, match="rejected the request shape"):
            provider.search("x", DatasetFilter(limit=1))
    finally:
        provider.close()


def test_forbidden_maps_to_source_unavailable(httpserver):
    """403 → ``SourceUnavailableError``。"""
    httpserver.add("GET", _SEARCH_PATH, Response.json({"description": "no"}, status=403))
    provider = _provider(httpserver)
    try:
        with pytest.raises(SourceUnavailableError):
            provider.search("x", DatasetFilter(limit=1))
    finally:
        provider.close()


def test_provider_refuses_calls_after_close(provider):
    """关闭后拒绝调用。"""
    provider.close()
    with pytest.raises(RuntimeError, match="closed"):
        provider.search("x", DatasetFilter(limit=1))


def test_provider_close_is_idempotent(provider):
    """``close`` 幂等（同时关闭内部 token 缓存）。"""
    provider.close()
    provider.close()


def test_provider_supports_context_manager(httpserver):
    """上下文管理器自动关闭。"""
    install_cdse_routes(httpserver)
    with _provider(httpserver) as instance:
        assert instance.provider_id == "copernicus"
    with pytest.raises(RuntimeError):
        instance.search("x", DatasetFilter(limit=1))
