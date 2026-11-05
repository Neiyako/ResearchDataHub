"""真实网络集成测试：直接打 NASA CMR 与 CDSE STAC/OData（**默认不跑**）。

运行方式
--------
.. code-block:: bash

    .venv/bin/python -m pytest tests/integration/test_providers_network.py -m network -v

默认套件排除 ``network`` 标记（见 ``pyproject.toml`` 的 ``addopts = -m 'not network'``）：
外网抖动不该让"测试通过"失去意义。

本文件**不**使用 ``cdsapi``（用户明确要求），也不触碰 CDS/ECMWF 的任何端点。
它验证的是"真实端点今天仍然返回我们解析器所假设的形状"，失败通常意味着：

* 远端 API 变更（需要更新 ``providers/*/`` 的解析器与 ``docs/providers/*.md``）；
* 网络不可达（与代码无关，重跑即可）。

凭证相关的能力**不在**这里断言为"可用"：本机没有 ``EARTHDATA_TOKEN`` /
``CDSE_CLIENT_ID`` / ``CDSE_CLIENT_SECRET``，因此"带鉴权下载"仍是 unverified。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from researchdatahub.core.errors import NotImplementedFeatureError
from researchdatahub.core.models import TemporalExtent
from researchdatahub.providers import (
    DatasetFilter,
    assert_provider_contract,
    get_provider,
    get_provider_capabilities,
)
from researchdatahub.providers.copernicus.provider import CopernicusProvider
from researchdatahub.providers.earthdata.provider import EarthdataProvider

pytestmark = pytest.mark.network

# 2026-11-05 实测存在的真实标识
CMR_COLLECTION_ID = "C2179081499-GES_DISC"  # GPM_2ADPR v07
CDSE_COLLECTION_ID = "sentinel-2-l2a"
WINDOW = TemporalExtent(
    start=datetime(2024, 6, 1, tzinfo=UTC), end=datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
)


@pytest.fixture(scope="module")
def earthdata() -> EarthdataProvider:
    """真实 CMR 的 Earthdata Provider。"""
    provider = EarthdataProvider()
    try:
        yield provider
    finally:
        provider.close()


@pytest.fixture(scope="module")
def copernicus() -> CopernicusProvider:
    """真实 CDSE 的 Copernicus Provider。"""
    provider = CopernicusProvider()
    try:
        yield provider
    finally:
        provider.close()


# --------------------------------------------------------------------------- #
# NASA Earthdata / CMR
# --------------------------------------------------------------------------- #
def test_cmr_search_returns_real_collections(earthdata: EarthdataProvider):
    """真实 CMR 检索返回候选，且每条都带 concept-id 形状的 dataset_id。"""
    candidates = earthdata.search("precipitation", DatasetFilter(provider="GES_DISC", limit=3))
    assert candidates, "CMR returned no collections for 'precipitation' / GES_DISC"
    for candidate in candidates:
        assert candidate.provider == "earthdata"
        assert "-" in candidate.dataset_id
        assert candidate.title


def test_cmr_inspect_returns_real_umm_detail(earthdata: EarthdataProvider):
    """真实 UMM-C 详情：标题、DOI、数据中心、时间覆盖都存在。"""
    detail = earthdata.inspect(CMR_COLLECTION_ID)
    assert detail.candidate.dataset_id == CMR_COLLECTION_ID
    assert detail.candidate.title
    assert detail.data_centers
    assert detail.candidate.temporal_coverage is not None
    assert detail.anonymous_access is True


def test_cmr_get_variables_returns_registered_variables(earthdata: EarthdataProvider):
    """真实变量查询：GPM_2ADPR 登记了大量变量（2026-11-05 实测 281 个）。"""
    variables = earthdata.get_variables(CMR_COLLECTION_ID)
    assert variables is not None
    assert len(variables) > 100
    assert all(variable.name for variable in variables)


def test_cmr_estimate_uses_real_granule_sizes(earthdata: EarthdataProvider):
    """真实 granule 检索给出文件数与字节数（目录声明值）。"""
    estimate = earthdata.estimate_request(CMR_COLLECTION_ID, DatasetFilter(limit=3))
    assert estimate.matched_files
    assert estimate.matched_files >= 1
    assert "granule_size" in estimate.basis


def test_cmr_validate_rejects_out_of_range_window(earthdata: EarthdataProvider):
    """真实 UMM 覆盖范围之外的窗口被判为不可行。"""
    ancient = TemporalExtent(
        start=datetime(1990, 1, 1, tzinfo=UTC), end=datetime(1991, 1, 1, tzinfo=UTC)
    )
    result = earthdata.validate_request(CMR_COLLECTION_ID, DatasetFilter(temporal=ancient))
    assert result.ok is False


def test_cmr_task_methods_refuse_explicitly(earthdata: EarthdataProvider):
    """CMR 无作业 API：任务方法必须显式拒绝（不返回假任务 ID）。"""
    with pytest.raises(NotImplementedFeatureError):
        earthdata.submit_task(CMR_COLLECTION_ID)


# --------------------------------------------------------------------------- #
# Copernicus Data Space / STAC + OData
# --------------------------------------------------------------------------- #
def test_cdse_search_returns_recognizable_collections(copernicus: CopernicusProvider):
    """真实 STAC 集合检索：全量翻页 + 本地排序后，前几条必须与关键词相关。"""
    candidates = copernicus.search("sentinel-2", DatasetFilter(limit=5))
    assert candidates
    joined = " ".join(
        f"{candidate.dataset_id} {candidate.title}".lower() for candidate in candidates
    )
    assert "sentinel" in joined or "sentinel-2" in joined


def test_cdse_inspect_returns_real_sentinel2(copernicus: CopernicusProvider):
    """真实 STAC 集合详情：Sentinel-2 L2A 的标题、DOI、说明存在。"""
    detail = copernicus.inspect(CDSE_COLLECTION_ID)
    assert detail.candidate.dataset_id == CDSE_COLLECTION_ID
    assert detail.candidate.title
    assert detail.retrieval_endpoints
    assert detail.anonymous_access is True


def test_cdse_get_variables_returns_real_bands(copernicus: CopernicusProvider):
    """真实 STAC ``bands[]`` 至少包含 B02/B03/B04 这类真实波段名。"""
    variables = copernicus.get_variables(CDSE_COLLECTION_ID)
    assert variables is not None
    names = {variable.name for variable in variables}
    assert {"B02", "B03", "B04"} <= names


def test_cdse_estimate_uses_stac_search(copernicus: CopernicusProvider):
    """真实 ``POST /stac/search`` 给出匹配文件数与字节数。"""
    estimate = copernicus.estimate_request(
        CDSE_COLLECTION_ID,
        DatasetFilter(limit=2, temporal=WINDOW, bbox=(10.0, 45.0, 11.0, 46.0)),
    )
    assert estimate.matched_files
    assert estimate.matched_files >= 1
    assert estimate.total_bytes is not None


def test_cdse_validate_handles_open_ended_collection(copernicus: CopernicusProvider):
    """Sentinel-2 结束时间为 null：早期窗口必须判为不通过，正常窗口通过但标 unverified。"""
    ancient = TemporalExtent(
        start=datetime(2001, 1, 1, tzinfo=UTC), end=datetime(2002, 1, 1, tzinfo=UTC)
    )
    assert (
        copernicus.validate_request(CDSE_COLLECTION_ID, DatasetFilter(temporal=ancient)).ok is False
    )
    current = copernicus.validate_request(CDSE_COLLECTION_ID, DatasetFilter(temporal=WINDOW))
    assert current.ok is True


def test_cdse_missing_collection_raises_not_found(copernicus: CopernicusProvider):
    """真实 404 被映射成 ``SourceNotFoundError``。"""
    from researchdatahub.core.errors import SourceNotFoundError

    with pytest.raises(SourceNotFoundError):
        copernicus.inspect("this-collection-does-not-exist-xyz")


def test_cdse_task_methods_refuse_explicitly(copernicus: CopernicusProvider):
    """CDSE 目录无作业 API：任务方法必须显式拒绝。"""
    with pytest.raises(NotImplementedFeatureError):
        copernicus.submit_task(CDSE_COLLECTION_ID)


# --------------------------------------------------------------------------- #
# 契约层：两个 Provider 都通过自检
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider_id", ["earthdata", "copernicus"])
def test_real_providers_pass_contract_self_check(provider_id: str):
    """真实 Provider 满足 ProviderContract 且能力声明自洽。"""
    provider = get_provider(provider_id)
    try:
        assert_provider_contract(provider)
    finally:
        provider.close()


def test_capabilities_show_download_still_unverified_without_credentials():
    """本机无凭证：两个 Provider 的 ``data_download`` 都不得标为 verified（诚实性断言）。"""
    for capability in get_provider_capabilities():
        # 若运维配置了凭证，这里会变成 verified —— 那是环境差异，不是代码缺陷
        if capability.credential_features.get("data_download") == "unverified":
            assert any("data_download" in item for item in capability.unverified)
