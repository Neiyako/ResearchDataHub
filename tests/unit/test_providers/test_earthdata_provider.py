"""NASA Earthdata Provider 测试：CMR 真实响应解析、能力声明、错误映射与"不虚构"边界。

fixtures 全部是 2026-11-05 对 ``https://cmr.earthdata.nasa.gov`` 的真实响应快照
（见 ``conftest.py`` 的样本清单与 ``docs/providers/earthdata.md`` 的 curl 实证）。
本地服务器用这些真实形状提供服务；**没有 mock socket**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_providers.conftest import (
    CMR_COLLECTION_ID,
    CMR_PROVIDER,
    FIXED_NOW,
    LocalHttpServer,
    Response,
    install_cmr_routes,
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
from researchdatahub.providers.earthdata.cmr import (
    granule_size_bytes,
    parse_collection_entries,
    parse_granule_entries,
    parse_umm_collection,
    parse_umm_variables,
    pick_download_link,
)
from researchdatahub.providers.earthdata.credentials import (
    EARTHDATA_LOGIN_HOST,
    TOKEN_ENV_VAR,
    EarthdataCredentialState,
    probe_credentials,
)
from researchdatahub.providers.earthdata.provider import EarthdataProvider

FIXTURES = Path(__file__).parent / "fixtures"
NO_CREDENTIALS = EarthdataCredentialState(
    token_present=False,
    netrc_present=False,
    netrc_has_edl_machine=False,
    netrc_path="/nonexistent/.netrc",
)


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def _provider(
    server: LocalHttpServer, *, credentials: EarthdataCredentialState | None = None
) -> EarthdataProvider:
    """构造指向本地服务器的 Provider（注入固定时钟与凭证快照）。"""
    return EarthdataProvider(
        base_url=server.url,
        http_client=make_client(server.url),
        credentials=credentials or NO_CREDENTIALS,
        clock=lambda: FIXED_NOW,
    )


@pytest.fixture
def provider(httpserver: LocalHttpServer) -> EarthdataProvider:
    """已注册真实 CMR 路由的 Provider。"""
    install_cmr_routes(httpserver)
    instance = _provider(httpserver)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------------- #
# CMR 解析：collections.json
# --------------------------------------------------------------------------- #
def test_parse_collection_entries_reads_real_feed_shape():
    """``feed.entry`` 是真实形状：concept-id 在 ``id`` 字段，``concept-id`` 字段实测为 null。"""
    payload = load_fixture("cmr_collections_keyword_precipitation.json")
    entries = parse_collection_entries(payload)
    assert len(entries) == 3
    first = entries[0]
    assert first.concept_id == CMR_COLLECTION_ID
    assert first.dataset_id == CMR_COLLECTION_ID
    assert first.short_name == "GPM_2ADPR"
    assert first.provider_id == CMR_PROVIDER
    assert first.version_id == "07"
    assert first.processing_level == "2"
    assert first.original_format == "UMM_JSON"
    assert first.has_variables is True
    assert first.cloud_hosted is True


def test_parse_collection_entries_parses_real_temporal_bounds():
    """真实 ``time_start`` 形如 ``2014-03-08T00:00:00.000Z``（毫秒 + Z）。"""
    entries = parse_collection_entries(load_fixture("cmr_collections_keyword_precipitation.json"))
    first = entries[0]
    assert first.time_start == datetime(2014, 3, 8, tzinfo=UTC)
    assert first.time_end is not None
    window = first.temporal_extent()
    assert window is not None
    assert window.start == datetime(2014, 3, 8, tzinfo=UTC)


def test_parse_collection_entries_parses_real_boxes():
    """``boxes`` 真实值是 ``"-70 -180 70 180"``（顺序 S W N E）。"""
    entries = parse_collection_entries(load_fixture("cmr_collections_keyword_precipitation.json"))
    spatial = entries[0].spatial
    assert spatial is not None
    assert (spatial.south, spatial.west, spatial.north, spatial.east) == (
        -70.0,
        -180.0,
        70.0,
        180.0,
    )


def test_parse_collection_entries_rejects_missing_feed():
    """没有 ``feed`` 的响应形状不符 → ``MetadataError``（不静默返回空列表）。"""
    with pytest.raises(MetadataError, match="no 'feed' object"):
        parse_collection_entries({"items": []})


def test_parse_collection_entries_rejects_non_list_entry():
    """``feed.entry`` 不是列表 → ``MetadataError``。"""
    with pytest.raises(MetadataError, match=r"feed\.entry is not a list"):
        parse_collection_entries({"feed": {"entry": "oops"}})


def test_parse_collection_entries_rejects_entry_without_concept_id():
    """条目录缺少 concept-id → 形状错误（concept-id 是必需事实）。"""
    with pytest.raises(MetadataError, match="no 'id'"):
        parse_collection_entries({"feed": {"entry": [{"title": "no id"}]}})


def test_empty_feed_is_a_valid_empty_result():
    """实测：不存在的 concept-id 返回 200 + 空 ``feed.entry``（不是 404）。"""
    entries = parse_collection_entries(load_fixture("cmr_collections_empty.json"))
    assert entries == ()


# --------------------------------------------------------------------------- #
# CMR 解析：collections.umm_json
# --------------------------------------------------------------------------- #
def test_parse_umm_collection_reads_real_singular_spatial_extent():
    """空间范围在 ``SpatialExtent``（单数）里；实测 ``SpatialExtents``（复数）为 null。"""
    detail = parse_umm_collection(load_fixture("cmr_umm_collection_gpm_2adpr.json"))
    assert detail.concept_id == CMR_COLLECTION_ID
    assert detail.spatial is not None
    assert (detail.spatial.west, detail.spatial.south) == (-180.0, -70.0)
    assert (detail.spatial.east, detail.spatial.north) == (180.0, 70.0)


def test_parse_umm_collection_reads_temporal_ranges():
    """``TemporalExtents[].RangeDateTimes[]`` 的起止时间被解析为 UTC。"""
    detail = parse_umm_collection(load_fixture("cmr_umm_collection_gpm_2adpr.json"))
    assert detail.temporal is not None
    assert detail.temporal.start == datetime(2014, 3, 8, tzinfo=UTC)
    assert detail.temporal.end == datetime(2026, 3, 1, 23, 59, 59, 999000, tzinfo=UTC)


def test_parse_umm_collection_reads_doi_and_formats():
    """DOI、原生格式（HDF5）、数据中心来自真实 UMM-C 字段。"""
    detail = parse_umm_collection(load_fixture("cmr_umm_collection_gpm_2adpr.json"))
    assert detail.doi == "10.5067/GPM/DPR/GPM/2A/07"
    assert detail.formats == ("HDF5",)
    assert "NASA/GSFC/SED/ESD/TISL/GESDISC" in detail.data_centers
    assert detail.processing_level == "2"
    assert detail.version == "07"


def test_parse_umm_collection_citation_is_built_from_real_fields():
    """引用由 ``CollectionCitations`` 的真实字段拼出（Creator/Title/Publisher/ReleaseDate）。"""
    detail = parse_umm_collection(load_fixture("cmr_umm_collection_gpm_2adpr.json"))
    assert detail.citation is not None
    assert detail.citation.startswith("Toshio Iguchi, Robert Meneghini. GPM DPR")
    assert detail.license_text is not None
    assert detail.license_text.startswith("The Earth Observing")


def test_parse_umm_collection_rejects_empty_items():
    """``items`` 为空（concept-id 不存在）→ ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no collection for the requested concept-id"):
        parse_umm_collection({"hits": 0, "items": []})


def test_parse_umm_collection_rejects_missing_umm():
    """``items[0]`` 没有 ``umm`` → ``MetadataError``。"""
    with pytest.raises(MetadataError, match="no 'umm' object"):
        parse_umm_collection({"items": [{"meta": {}}]})


# --------------------------------------------------------------------------- #
# CMR 解析：variables.umm_json
# --------------------------------------------------------------------------- #
def test_parse_umm_variables_reads_real_variable():
    """真实变量样本：``HS/navigation/scLon``、单位 degrees、类型 float32。"""
    variables = parse_umm_variables(load_fixture("cmr_variables_gpm_2adpr.json"))
    assert variables is not None
    assert len(variables) == 1
    assert variables[0].name == "HS/navigation/scLon"
    assert variables[0].units == "degrees"
    assert variables[0].data_type == "float32"
    assert variables[0].source == "remote"


def test_parse_umm_variables_returns_none_when_nothing_registered():
    """没有登记变量返回 ``None``（"未知"），**不是**空元组（"确认没有变量"）。"""
    assert parse_umm_variables({"hits": 0, "items": []}) is None


# --------------------------------------------------------------------------- #
# CMR 解析：granules.json
# --------------------------------------------------------------------------- #
def test_granule_size_bytes_converts_real_megabyte_string():
    """实测 ``granule_size`` 是 MB 字符串 ``"823.5269718170166"`` → 字节（1024²）。

    ``823.5269718170166 * 1024 * 1024 = 863_530_618``（四舍五入）。
    """
    assert granule_size_bytes("823.5269718170166") == 863_530_618


def test_granule_size_bytes_rejects_unparseable_values():
    """非法/负数/布尔值返回 ``None``（不猜测）。"""
    assert granule_size_bytes("not-a-number") is None
    assert granule_size_bytes(-1) is None
    assert granule_size_bytes(True) is None
    assert granule_size_bytes(None) is None


def test_parse_granule_entries_reads_real_granule_fields():
    """granule concept-id、时间、大小、原生格式来自真实响应。"""
    granules = parse_granule_entries(load_fixture("cmr_granules_gpm_2adpr.json"))
    assert len(granules) == 2
    first = granules[0]
    assert first.concept_id == "G2187436353-GES_DISC"
    assert first.collection_concept_id == CMR_COLLECTION_ID
    assert first.original_format == "ECHO10"
    assert first.size_bytes == granule_size_bytes("823.5269718170166")
    assert first.time_start == datetime(2014, 3, 8, 22, 9, 50, 674000, tzinfo=UTC)


def test_parse_granule_entries_separates_https_and_s3_links():
    """``data#`` 是 HTTPS 下载链接；``s3://`` 链接被单列（本 Provider 不支持 S3 直连）。

    真实响应里 ``.../metadata#`` 也以 ``data#`` 结尾（``s3credentials`` 链接），
    这是本实现踩到的真实坑：必须按路径段 ``/data#`` 匹配。
    """
    granules = parse_granule_entries(load_fixture("cmr_granules_gpm_2adpr.json"))
    first = granules[0]
    assert len(first.data_links) == 1
    assert first.data_links[0].rel.endswith("/data#")
    assert first.data_links[0].href.startswith("https://data.gesdisc.earthdata.nasa.gov/")
    assert len(first.s3_links) == 1
    assert first.s3_links[0].startswith("s3://gesdisc-cumulus-prod-protected/")


def test_s3credentials_link_is_not_treated_as_a_data_file():
    """``s3credentials`` 链接的 ``rel`` 是 ``metadata#``，不得被当成数据文件。"""
    granules = parse_granule_entries(load_fixture("cmr_granules_gpm_2adpr.json"))
    hrefs = [link.href for link in granules[0].data_links]
    assert not any("s3credentials" in href for href in hrefs)


def test_parse_granule_entries_ignores_inherited_collection_links():
    """``inherited=True`` 的集合级链接（virtual-directory / search.earthdata）**不是文件**。"""
    payload = load_fixture("cmr_granules_gpm_2adpr.json")
    entry = payload["feed"]["entry"][0]
    inherited = [link for link in entry["links"] if link.get("inherited") is True]
    assert inherited, "样本里应当存在 inherited 链接（真实响应确实有）"
    granules = parse_granule_entries(payload)
    hrefs = {link.href for link in granules[0].data_links}
    for link in inherited:
        assert link["href"] not in hrefs


def test_pick_download_link_requires_uniqueness():
    """多文件 granule 时不挑"第一个"冒充全部（返回 ``None``）。"""
    two_data_links = [
        {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#", "href": "https://a/1.nc"},
        {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#", "href": "https://a/2.nc"},
    ]
    assert pick_download_link(two_data_links) is None
    single = [
        {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#", "href": "https://a/1.nc"},
        {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#", "href": "s3://bucket/1.nc"},
    ]
    assert pick_download_link(single) == "https://a/1.nc"


def test_pick_download_link_returns_none_without_data_links():
    """没有 ``/data#`` 链接时返回 ``None``（``metadata#`` 不算数据链接）。"""
    assert pick_download_link([{"rel": "http://x/1.1/metadata#", "href": "https://x"}]) is None


# --------------------------------------------------------------------------- #
# 凭证探测：只检查存在性，不读取值
# --------------------------------------------------------------------------- #
def test_probe_credentials_reports_missing_by_default(tmp_path):
    """没有环境变量与 netrc 时，两个来源都报告缺失（本机 2026-11-05 的真实状态）。"""
    state = probe_credentials(environ={}, netrc_path=tmp_path / ".netrc")
    assert state.token_present is False
    assert state.netrc_has_edl_machine is False
    assert state.download_credential_available() is False
    assert len(state.missing()) == 2


def test_probe_credentials_detects_token_presence_only():
    """``EARTHDATA_TOKEN`` 存在即视为"可能可用"；快照**不含 token 值**。"""
    state = probe_credentials(
        environ={TOKEN_ENV_VAR: "secret-value"}, netrc_path=Path("/nonexistent/.netrc")
    )
    assert state.token_present is True
    assert state.download_credential_available() is True
    dumped = str(state.to_dict())
    assert "secret-value" not in dumped
    assert "secret-value" not in repr(state)


def test_probe_credentials_detects_netrc_machine_entry(tmp_path):
    """``~/.netrc`` 里有 EDL machine 条目（权限 600）才算存在。"""
    netrc = tmp_path / ".netrc"
    netrc.write_text(f"machine {EARTHDATA_LOGIN_HOST} login x password y\n", encoding="utf-8")
    netrc.chmod(0o600)
    state = probe_credentials(environ={}, netrc_path=netrc)
    assert state.netrc_present is True
    assert state.netrc_has_edl_machine is True
    assert state.download_credential_available() is True


def test_probe_credentials_ignores_netrc_with_unsafe_permissions(tmp_path):
    """权限过宽（group/other 可读）的 netrc 视为不可信，不报告"有凭证"。"""
    netrc = tmp_path / ".netrc"
    netrc.write_text(f"machine {EARTHDATA_LOGIN_HOST} login x password y\n", encoding="utf-8")
    netrc.chmod(0o644)
    state = probe_credentials(environ={}, netrc_path=netrc)
    assert state.netrc_present is False
    assert state.netrc_has_edl_machine is False


def test_probe_credentials_ignores_netrc_without_edl_machine(tmp_path):
    """netrc 存在但没有 EDL 条目 → 仍不算有下载凭证。"""
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine example.com login a password b\n", encoding="utf-8")
    netrc.chmod(0o600)
    state = probe_credentials(environ={}, netrc_path=netrc)
    assert state.netrc_present is True
    assert state.netrc_has_edl_machine is False


# --------------------------------------------------------------------------- #
# Provider：能力声明
# --------------------------------------------------------------------------- #
def test_capabilities_declare_implemented_and_unsupported_disjointly(provider):
    """能力声明自洽：已实现 6 项检索能力，任务类 4 项明确不支持。"""
    caps = provider.capabilities()
    assert caps.provider_id == "earthdata"
    assert set(caps.implemented) == {
        "search",
        "inspect",
        "get_metadata",
        "get_variables",
        "validate_request",
        "estimate_request",
    }
    assert set(caps.unsupported) == {
        "submit_task",
        "get_task_status",
        "retrieve_result",
        "cancel_task",
    }
    assert caps.supports_async_job is False
    assert caps.supports_cloud_processing is False
    assert caps.supports_partial_download is False
    assert caps.supports_resume is False


def test_capabilities_mark_metadata_anonymous_but_download_unverified(provider):
    """元数据匿名可读（auth_required=False），但**带凭证下载**在无凭证时必须标 unverified。"""
    caps = provider.capabilities()
    assert caps.auth_required is False
    assert caps.credential_features["metadata_query"] == "verified"
    assert caps.credential_features["data_download"] == "unverified"
    assert any("data_download" in item for item in caps.unverified)


def test_capabilities_mark_download_verifiable_when_credential_present(httpserver):
    """检测到凭证时，凭证矩阵最多升到 verified（仍不等于真的下载过）。"""
    install_cmr_routes(httpserver)
    with_creds = EarthdataCredentialState(
        token_present=True,
        netrc_present=False,
        netrc_has_edl_machine=False,
        netrc_path="/nonexistent/.netrc",
    )
    instance = _provider(httpserver, credentials=with_creds)
    try:
        assert instance.capabilities().credential_features["data_download"] == "verified"
    finally:
        instance.close()


def test_capabilities_declare_s3_and_cloud_processing_unsupported(provider):
    """S3 直连与 Harmony 云处理都不在本实现内，必须在 notes 中如实声明。"""
    caps = provider.capabilities()
    assert caps.supports_cloud_processing is False
    assert caps.credential_features["s3_direct"] == "unsupported"
    assert caps.credential_features["cloud_processing"] == "unsupported"
    joined = " ".join(caps.notes)
    assert "s3_direct" in joined
    assert "s3credentials" in joined


def test_provider_passes_contract_self_check(provider):
    """Provider 满足 ProviderContract 且能力声明与实际行为一致。"""
    assert_provider_contract(provider)


# --------------------------------------------------------------------------- #
# Provider：检索
# --------------------------------------------------------------------------- #
def test_translate_query_maps_supported_filters(provider):
    """``keyword`` / ``provider`` / ``temporal`` / ``bbox`` / ``limit`` 被翻译成真实 CMR 参数。"""
    window = TemporalExtent(
        start=datetime(2014, 3, 8, tzinfo=UTC), end=datetime(2014, 3, 9, tzinfo=UTC)
    )
    translation = provider.translate_query(
        "precipitation",
        DatasetFilter(
            provider=CMR_PROVIDER,
            temporal=window,
            bbox=(-10.0, -20.0, 10.0, 20.0),
            limit=3,
        ),
    )
    assert translation.endpoint.endswith("/search/collections.json")
    assert translation.query["keyword"] == "precipitation"
    assert translation.query["provider"] == CMR_PROVIDER
    assert translation.query["temporal"] == "2014-03-08T00:00:00Z,2014-03-09T00:00:00Z"
    assert translation.query["bounding_box"] == "-10.0,-20.0,10.0,20.0"
    assert translation.query["page_size"] == "3"
    assert set(translation.used) == {"keyword", "provider", "temporal", "bbox", "limit"}


def test_translate_query_reports_ignored_filters(provider):
    """``platform`` / ``collection`` 没有实测可用的 CMR 参数 → 必须进 ``ignored``。"""
    translation = provider.translate_query(
        "x", DatasetFilter(platform="GPM", collection=CMR_COLLECTION_ID)
    )
    assert any(item.startswith("platform:") for item in translation.ignored)
    assert any(item.startswith("collection:") for item in translation.ignored)


def test_search_returns_candidates_from_real_response(provider):
    """``search`` 解析真实响应，返回带概念/时间/空间/落地页的候选。"""
    candidates = provider.search("precipitation", DatasetFilter(provider=CMR_PROVIDER, limit=3))
    assert len(candidates) == 3
    first = candidates[0]
    assert first.provider == "earthdata"
    assert first.dataset_id == CMR_COLLECTION_ID
    assert first.title.startswith("GPM DPR Precipitation Profile")
    assert first.temporal_coverage is not None
    assert first.spatial_extent is not None
    assert first.native_id == CMR_COLLECTION_ID
    assert first.auth_required is False


def test_search_candidate_marks_variables_unknown(provider):
    """``collections.json`` 不含变量名：候选必须标 ``variables_known=False``。"""
    candidates = provider.search("precipitation", DatasetFilter(provider=CMR_PROVIDER, limit=1))
    assert candidates[0].variables == ()
    assert candidates[0].variables_known is False
    assert candidates[0].extra["cmr.has_variables"] is True


def test_search_sends_anonymous_request(httpserver, provider):
    """CMR 元数据检索是匿名的：请求不得携带 ``Authorization`` 头。"""
    provider.search("precipitation", DatasetFilter(limit=1))
    sent = httpserver.requests_to("/search/collections.json")
    assert sent
    assert all(item.authorization is None for item in sent)


# --------------------------------------------------------------------------- #
# Provider：详情 / 变量 / 元数据
# --------------------------------------------------------------------------- #
def test_inspect_returns_detail_with_evidence_chain(provider):
    """``inspect`` 返回详情并记录实际请求过的端点（可审计、匿名）。"""
    detail = provider.inspect(CMR_COLLECTION_ID)
    assert detail.candidate.dataset_id == CMR_COLLECTION_ID
    assert detail.doi == "10.5067/GPM/DPR/GPM/2A/07"
    assert detail.processing_level == "2"
    assert detail.anonymous_access is True
    assert detail.retrieved_at == FIXED_NOW
    assert any("collections.umm_json" in endpoint for endpoint in detail.retrieval_endpoints)
    assert "NASA/GSFC/SED/ESD/TISL/GESDISC" in detail.data_centers


def test_inspect_uses_umm_spatial_extent(provider):
    """详情里的空间范围来自 UMM-C（比 ``boxes`` 更权威）。"""
    detail = provider.inspect(CMR_COLLECTION_ID)
    spatial = detail.candidate.spatial_extent
    assert spatial is not None
    assert (spatial.west, spatial.south, spatial.east, spatial.north) == (
        -180.0,
        -70.0,
        180.0,
        70.0,
    )


def test_inspect_does_not_invent_variables(provider):
    """详情阶段不逐条查变量（避免 N+1）；变量标记为未知并提示调用 get_variables。"""
    detail = provider.inspect(CMR_COLLECTION_ID)
    assert detail.candidate.variables == ()
    assert detail.candidate.variables_known is False
    assert any("get_variables" in note for note in detail.notes)


def test_inspect_accepts_short_name_and_resolves_newest_version(httpserver, provider):
    """传 short_name 时先查 ``collections.json?short_name=``，并按 version_id 取最新版本。

    真实情况（2026-11-05）：``GPM_2ADPR`` 在 CMR 里同时登记了 v07 与 v08，
    规则是"同 short_name 精确匹配 → 取最高版本号"，因此这里解析到 v07 的样本
    （样本数据集只有 v07 一个版本条目）。
    """
    detail = provider.inspect("GPM_2ADPR")
    assert detail.candidate.dataset_id == CMR_COLLECTION_ID
    sent = httpserver.requests_to("/search/collections.json")
    assert any("short_name" in item.query for item in sent)


def test_short_name_resolution_picks_newest_version():
    """``_version_key`` 把 ``"07"`` / ``"08"`` 转成可比较的数值（取最大 = 最新版本）。"""
    from researchdatahub.providers.earthdata.provider import _version_key

    assert _version_key("07") < _version_key("08")
    assert _version_key("10") > _version_key("09")
    assert _version_key(None) == (0, 0)
    assert _version_key("v2") == (2, 0)


def test_get_variables_returns_real_variable_list(provider):
    """``get_variables`` 按概念登记的变量 concept-id 逐个查询。

    本地服务器对每个变量 concept-id 都返回同一份真实变量样本，因此数量等于
    UMM-C 里 ``associations.variables`` 的真实条目数（实测 281），不是编造的列表。
    """
    detail = provider.inspect(CMR_COLLECTION_ID)
    registered = detail.candidate.extra["cmr.variable_count"]
    variables = provider.get_variables(CMR_COLLECTION_ID)
    assert variables is not None
    assert registered == 281
    assert len(variables) == registered
    assert variables[0].name == "HS/navigation/scLon"
    assert variables[0].units == "degrees"


def test_get_metadata_exposes_namespaced_summary(provider):
    """``get_metadata`` 的键必须命名空间化（``cmr.*``），便于审计与排障。"""
    metadata = provider.get_metadata(CMR_COLLECTION_ID)
    assert all(key.startswith("cmr.") for key in metadata)
    assert metadata["cmr.concept_id"] == CMR_COLLECTION_ID
    assert metadata["cmr.doi"] == "10.5067/GPM/DPR/GPM/2A/07"
    assert metadata["cmr.granule_size_unit"] == "MB"


# --------------------------------------------------------------------------- #
# Provider：校验与估算
# --------------------------------------------------------------------------- #
def test_estimate_request_sums_declared_granule_sizes(provider):
    """体量来自 ``granule_size``（目录声明值），并说明单位换算依据。"""
    estimate = provider.estimate_request(CMR_COLLECTION_ID, DatasetFilter(limit=2))
    assert estimate.matched_files == 2
    assert estimate.files_without_size == 0
    assert estimate.total_bytes is not None
    assert estimate.total_bytes > 0
    assert "granule_size" in estimate.basis
    assert estimate.details["cmr.size_unit"] == "MB"


def test_estimate_request_exposes_download_urls_with_edl_flag(provider):
    """估算结果里给出真实下载 URL 与"需要 EDL"标记（**不执行下载**）。"""
    estimate = provider.estimate_request(CMR_COLLECTION_ID, DatasetFilter(limit=2))
    granules = estimate.details["cmr.granules"]
    assert isinstance(granules, list)
    assert granules
    first = granules[0]
    assert isinstance(first, dict)
    assert str(first["download_url"]).startswith("https://data.gesdisc.earthdata.nasa.gov/")
    assert first["download_links_ambiguous"] is False
    assert first["requires_edl"] is True
    assert first["download_links_ambiguous"] is False


def test_validate_request_rejects_window_outside_declared_coverage(provider):
    """时间窗口落在 UMM 声明覆盖之外 → ``ok=False`` 且给出两端时间。"""
    window = TemporalExtent(
        start=datetime(2010, 1, 1, tzinfo=UTC), end=datetime(2011, 1, 1, tzinfo=UTC)
    )
    result = provider.validate_request(CMR_COLLECTION_ID, DatasetFilter(temporal=window))
    assert result.ok is False
    assert result.errors
    assert "outside the declared collection coverage" in result.errors[0]


def test_validate_request_accepts_window_inside_coverage(provider):
    """落在覆盖范围内的时间窗口可以通过。"""
    window = TemporalExtent(
        start=datetime(2015, 1, 1, tzinfo=UTC), end=datetime(2015, 2, 1, tzinfo=UTC)
    )
    result = provider.validate_request(CMR_COLLECTION_ID, DatasetFilter(temporal=window))
    assert result.ok is True
    assert result.errors == ()


def test_validate_request_reports_missing_credential_as_unverified(provider):
    """无凭证时"能否下载"必须进 ``unverified``（不是 warning，更不是 ok 的一部分）。"""
    result = provider.validate_request(CMR_COLLECTION_ID, DatasetFilter())
    assert any("data_download" in item for item in result.unverified)


def test_validate_request_warns_on_bbox_outside_extent(provider):
    """包围盒超出声明范围只给 warning（CMR bbox 语义与 UMM 不完全等价）。"""
    result = provider.validate_request(
        CMR_COLLECTION_ID, DatasetFilter(bbox=(-180.0, -89.0, 180.0, 89.0))
    )
    assert result.ok is True
    assert any("not fully inside" in item for item in result.warnings)


# --------------------------------------------------------------------------- #
# Provider：不支持的能力必须显式拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method_name",
    ["submit_task", "get_task_status", "retrieve_result", "cancel_task"],
)
def test_task_methods_raise_not_implemented(provider, method_name):
    """CMR 没有作业 API：任务类方法必须抛 ``NotImplementedFeatureError``，不返回假 ID。"""
    method = getattr(provider, method_name)
    args = (CMR_COLLECTION_ID,) if method_name in ("submit_task",) else ("task-1",)
    with pytest.raises(NotImplementedFeatureError) as excinfo:
        method(*args)
    assert excinfo.value.code == "RDH_NOT_IMPLEMENTED"
    assert "CMR exposes no job API" in excinfo.value.message


def test_probe_unsupported_confirms_honest_declaration(provider):
    """能力声明与行为一致：``probe_unsupported`` 通过。"""
    assert probe_unsupported(provider.submit_task, CMR_COLLECTION_ID) is True


# --------------------------------------------------------------------------- #
# 错误映射：HTTP 状态码 → 具体异常
# --------------------------------------------------------------------------- #
def _provider_with(server: LocalHttpServer, path: str, response: Response) -> EarthdataProvider:
    """注册一个特定响应的路由并返回 Provider。"""
    server.add("GET", path, response)
    return _provider(server)


def test_http_400_maps_to_metadata_error(httpserver):
    """实测 ``page_size=notanumber`` → 400 + ``{"errors": [...]}`` → ``MetadataError``。"""
    instance = _provider_with(
        httpserver,
        "/search/collections.json",
        Response.json(load_fixture("cmr_error_400_page_size.json"), status=400),
    )
    try:
        with pytest.raises(MetadataError, match="rejected the request shape"):
            instance.search("x", DatasetFilter(limit=1))
    finally:
        instance.close()


def test_http_404_maps_to_source_not_found(httpserver):
    """404 → ``SourceNotFoundError``。"""
    instance = _provider_with(
        httpserver,
        "/search/collections.umm_json",
        Response.json({"errors": ["not found"]}, status=404),
    )
    try:
        with pytest.raises(SourceNotFoundError):
            instance.inspect(CMR_COLLECTION_ID)
    finally:
        instance.close()


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_map_to_source_unavailable(httpserver, status):
    """401/403 表示凭证缺失/不足 → ``SourceUnavailableError``。"""
    instance = _provider_with(
        httpserver,
        "/search/collections.json",
        Response.json({"errors": ["unauthorized"]}, status=status),
    )
    try:
        with pytest.raises(SourceUnavailableError, match="denied access"):
            instance.search("x", DatasetFilter(limit=1))
    finally:
        instance.close()


def test_retry_after_header_is_surfaced_on_429(httpserver):
    """429 携带 ``Retry-After`` → ``RetryableTransportError.retry_after_seconds``。"""
    httpserver.add(
        "GET",
        "/search/collections.json",
        Response(
            status=429,
            body=b'{"errors": ["slow down"]}',
            headers={"Content-Type": "application/json", "Retry-After": "30"},
        ),
    )
    instance = _provider(httpserver)
    try:
        with pytest.raises(RetryableTransportError) as excinfo:
            instance.search("x", DatasetFilter(limit=1))
        assert excinfo.value.retryable is True
        assert excinfo.value.retry_after_seconds == 30.0
    finally:
        instance.close()


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retryable(httpserver, status):
    """5xx 是可重试错误（调度层据此重试，而不是标记永久失败）。"""
    httpserver.add("GET", "/search/collections.json", Response(status=status, body=b"boom"))
    instance = _provider(httpserver)
    try:
        with pytest.raises(RetryableTransportError) as excinfo:
            instance.search("x", DatasetFilter(limit=1))
        assert excinfo.value.retryable is True
        assert excinfo.value.retry_after_seconds is None
    finally:
        instance.close()


def test_non_json_body_maps_to_metadata_error(httpserver):
    """返回 HTML 之类的非 JSON 体 → ``MetadataError``（不是 KeyError 崩溃）。"""
    httpserver.add(
        "GET",
        "/search/collections.json",
        Response(status=200, body=b"<html>oops</html>", headers={"Content-Type": "text/html"}),
    )
    instance = _provider(httpserver)
    try:
        with pytest.raises(MetadataError, match="non-JSON body"):
            instance.search("x", DatasetFilter(limit=1))
    finally:
        instance.close()


def test_connection_failure_is_retryable(httpserver):
    """服务器停止后请求失败 → ``RetryableTransportError``（可重试，不是永久失败）。

    注意：停止后的具体表现取决于本机 TCP 行为（可能是连接被拒，也可能是本地代理
    返回 5xx），两者都必须是**可重试**错误。
    """
    httpserver.add_json("GET", "/search/collections.json", {"feed": {"entry": []}})
    instance = _provider(httpserver)
    httpserver.stop()
    try:
        with pytest.raises(RetryableTransportError) as excinfo:
            instance.search("x", DatasetFilter(limit=1))
        assert excinfo.value.retryable is True
    finally:
        instance.close()


def test_unreachable_host_is_retryable():
    """无法连接的地址 → ``RetryableTransportError``（可重试，不当作永久失败）。

    本机经 HTTP(S)_PROXY 时，连不上目标会表现为代理的 502；直连时是 connection
    refused。两条路径都映射到可重试错误。
    """
    instance = EarthdataProvider(
        base_url="http://127.0.0.1:1",
        http_client=make_client("http://127.0.0.1:1"),
        credentials=NO_CREDENTIALS,
        clock=lambda: FIXED_NOW,
    )
    try:
        with pytest.raises(RetryableTransportError) as excinfo:
            instance.search("x", DatasetFilter(limit=1))
        assert excinfo.value.retryable is True
    finally:
        instance.close()


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_provider_refuses_calls_after_close(provider):
    """关闭后拒绝调用（``RuntimeError``），避免使用已释放的客户端。"""
    provider.close()
    with pytest.raises(RuntimeError, match="closed"):
        provider.search("x", DatasetFilter(limit=1))


def test_provider_close_is_idempotent(provider):
    """``close`` 幂等。"""
    provider.close()
    provider.close()


def test_provider_supports_context_manager(httpserver):
    """用作上下文管理器时自动关闭。"""
    install_cmr_routes(httpserver)
    with _provider(httpserver) as instance:
        assert instance.provider_id == "earthdata"
    with pytest.raises(RuntimeError):
        instance.search("x", DatasetFilter(limit=1))
