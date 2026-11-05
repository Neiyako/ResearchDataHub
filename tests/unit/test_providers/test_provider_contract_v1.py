"""Provider Contract v1 的契约测试：模型校验、能力声明自洽、"不虚构"的机器可读出口。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from researchdatahub.core.errors import MetadataError, NotImplementedFeatureError
from researchdatahub.core.models import SpatialExtent, TemporalExtent
from researchdatahub.providers.base import (
    CAPABILITY_CONTRACT_VERSION,
    CONTRACT_METHODS,
    CREDENTIAL_FEATURES,
    CapabilityStatus,
    DatasetCandidate,
    DatasetDetail,
    DatasetFilter,
    DatasetVariable,
    ProviderCapabilities,
    ProviderContract,
    QueryTranslation,
    RequestEstimate,
    RequestValidation,
    TaskHandle,
    TaskStatusSnapshot,
    assert_provider_contract,
    probe_unsupported,
)


def _caps(**overrides: object) -> ProviderCapabilities:
    """构造一份自洽的能力声明（测试基线），支持字段覆盖。"""
    payload: dict[str, object] = {
        "provider_id": "demo",
        "display_name": "Demo Provider",
        "auth_required": False,
        "implemented": (
            "search",
            "inspect",
            "get_metadata",
            "get_variables",
            "validate_request",
            "estimate_request",
        ),
        "unsupported": (
            "submit_task",
            "get_task_status",
            "retrieve_result",
            "cancel_task",
        ),
    }
    payload.update(overrides)
    return ProviderCapabilities(**payload)  # type: ignore[arg-type]


def _provide_capabilities(_self: object) -> ProviderCapabilities:
    """返回基线能力声明（供 :class:`_FakeProvider` 使用）。"""
    return _caps(provider_id="fake")


def _provide_search(
    _self: object, query: str, filters: DatasetFilter | None = None
) -> tuple[DatasetCandidate, ...]:
    """返回空元组（fake 不检索）。"""
    del query, filters
    return ()


def _provide_inspect(_self: object, dataset_id: str) -> DatasetDetail:
    """fake 不实现详情。"""
    del dataset_id
    raise NotImplementedFeatureError("fake.inspect")


class _FakeProvider:
    """实现了协议**全部成员**的 fake，但能力声明与实际行为不一致。

    用来证明：``@runtime_checkable`` 通过 ≠ 能力声明可信；:
    func:`assert_provider_contract` 额外校验"声明为不支持的方法真的会抛错"。
    """

    provider_id = "fake"
    capabilities = _provide_capabilities
    search = _provide_search
    inspect = _provide_inspect
    translate_query = lambda self, query, filters=None: QueryTranslation(  # noqa: E731
        provider="fake", endpoint="http://example.invalid", query={}
    )
    get_metadata = lambda self, dataset_id: {}  # noqa: E731
    get_variables = lambda self, dataset_id: None  # noqa: E731
    validate_request = lambda self, dataset_id, filters=None: RequestValidation(  # noqa: E731
        ok=True, dataset_id=dataset_id
    )
    estimate_request = lambda self, dataset_id, filters=None: RequestEstimate(  # noqa: E731
        dataset_id=dataset_id, basis="fake"
    )
    submit_task = lambda self, dataset_id, filters=None: TaskHandle(  # noqa: E731
        provider="fake", task_id="t", submitted=False
    )
    get_task_status = lambda self, task_id: TaskStatusSnapshot(  # noqa: E731
        provider="fake", task_id=task_id, state="queued"
    )
    retrieve_result = lambda self, task_id: {}  # noqa: E731
    cancel_task = lambda self, task_id: False  # noqa: E731
    close = lambda self: None  # noqa: E731


def test_contract_version_is_declared_in_capabilities():
    """能力声明必须携带契约版本号，供 MCP 层做兼容判断。"""
    assert CAPABILITY_CONTRACT_VERSION == "provider-contract/1.0"
    assert _caps().contract_version == CAPABILITY_CONTRACT_VERSION


def test_contract_methods_cover_the_ten_required_operations():
    """契约必须覆盖任务书要求的 10 个方法名（顺序稳定）。"""
    assert CONTRACT_METHODS == (
        "search",
        "inspect",
        "get_metadata",
        "get_variables",
        "validate_request",
        "estimate_request",
        "submit_task",
        "get_task_status",
        "retrieve_result",
        "cancel_task",
    )


def test_capabilities_reject_unknown_method_names():
    """拼错的方法名必须被拒绝，否则"未实现"会被静默漏报。"""
    with pytest.raises(ValueError, match="unknown contract method names"):
        _caps(implemented=("serch", "inspect"), unsupported=("submit_task",))


def test_capabilities_reject_a_method_declared_both_ways():
    """同一方法不能既声明已实现又声明不支持。"""
    with pytest.raises(ValueError, match="both implemented and unsupported"):
        _caps(unsupported=("search", "submit_task"))


def test_capabilities_reject_missing_methods():
    """必须覆盖全部方法：漏报会让 Agent 无从判断该方法是否可用。"""
    with pytest.raises(ValueError, match="missing from the capability declaration"):
        _caps(implemented=("search",), unsupported=("submit_task",))


def test_async_job_flag_must_match_implemented_task_methods():
    """``supports_async_job=True`` 却不实现任何任务方法 → 自相矛盾。"""
    with pytest.raises(ValueError, match="supports_async_job=True requires"):
        _caps(supports_async_job=True)


def test_task_methods_require_async_job_flag():
    """实现了任务方法却声明 ``supports_async_job=False`` → 同样自相矛盾。"""
    with pytest.raises(ValueError, match="supports_async_job is False"):
        _caps(
            implemented=(
                "search",
                "submit_task",
                "get_task_status",
                "retrieve_result",
                "cancel_task",
            ),
            unsupported=(
                "inspect",
                "get_metadata",
                "get_variables",
                "validate_request",
                "estimate_request",
            ),
        )


def test_async_job_flag_is_accepted_when_tasks_are_implemented():
    """如实声明异步作业的 Provider 可以通过校验。"""
    caps = _caps(
        supports_async_job=True,
        implemented=("search", "submit_task"),
        unsupported=(
            "inspect",
            "get_metadata",
            "get_variables",
            "validate_request",
            "estimate_request",
            "get_task_status",
            "retrieve_result",
            "cancel_task",
        ),
    )
    assert caps.supports_async_job is True


def test_status_of_returns_three_distinct_states():
    """能力状态只有三种：verified / unverified / unsupported。"""
    caps = _caps(unverified=("get_variables: needs an extra endpoint (not probed)",))
    assert caps.status_of("search") is CapabilityStatus.VERIFIED
    assert caps.status_of("get_variables") is CapabilityStatus.UNVERIFIED
    assert caps.status_of("submit_task") is CapabilityStatus.UNSUPPORTED
    assert caps.status_of("no_such_method") is CapabilityStatus.UNSUPPORTED


def test_credential_features_only_accept_known_keys():
    """凭证矩阵的键必须在 :data:`CREDENTIAL_FEATURES` 里，避免拼写出新维度。"""
    with pytest.raises(ValueError, match="unknown credential feature keys"):
        _caps(credential_features={"wget_download": "verified"})
    caps = _caps(credential_features={"data_download": "unverified"})
    assert caps.credential_features["data_download"] == "unverified"
    assert "metadata_query" in CREDENTIAL_FEATURES


def test_capabilities_are_frozen():
    """能力声明不可变：防止运行期被改写成虚假能力。"""
    caps = _caps()
    with pytest.raises(ValueError, match="frozen"):
        caps.provider_id = "other"  # type: ignore[misc]


def test_dataset_filter_rejects_invalid_bbox_ordering():
    """包围盒顺序错误必须被拒绝（南 > 北）。"""
    with pytest.raises(ValueError, match="south bound must not exceed north"):
        DatasetFilter(bbox=(10.0, 60.0, 11.0, 45.0))


def test_dataset_filter_rejects_out_of_range_longitude():
    """经度必须在 [-180, 180]。"""
    with pytest.raises(ValueError, match="longitudes must be within"):
        DatasetFilter(bbox=(200.0, 10.0, 210.0, 20.0))


def test_dataset_filter_accepts_valid_bbox():
    """合法包围盒被原样保留。"""
    filters = DatasetFilter(bbox=(10.0, 45.0, 11.0, 46.0), limit=25)
    assert filters.bbox == (10.0, 45.0, 11.0, 46.0)
    assert filters.limit == 25


def test_dataset_candidate_unknown_variables_are_explicit():
    """变量未知必须用 ``variables_known=False`` 表达，而不是空列表冒充"没有变量"。"""
    candidate = DatasetCandidate(
        provider="earthdata",
        dataset_id="C2179081499-GES_DISC",
        title="GPM DPR Precipitation Profile",
        auth_required=False,
    )
    assert candidate.variables == ()
    assert candidate.variables_known is False
    assert candidate.temporal_coverage is None
    assert candidate.spatial_extent is None


def test_dataset_candidate_rejects_empty_title():
    """标题不能为空（Agent 需要它来展示与判断）。"""
    with pytest.raises(ValueError, match="title"):
        DatasetCandidate(provider="earthdata", dataset_id="x", title="", auth_required=False)


def test_dataset_candidate_is_frozen():
    """候选对象不可变（可安全地在多个 Agent 调用间共享）。"""
    candidate = DatasetCandidate(
        provider="earthdata", dataset_id="x", title="t", auth_required=False
    )
    with pytest.raises(ValueError, match="frozen"):
        candidate.title = "other"  # type: ignore[misc]


def test_query_translation_records_ignored_filters():
    """``ignored`` 必须能表达"哪个过滤条件没生效"，否则调用方会误读为"远端确认没有"。"""
    translation = QueryTranslation(
        provider="earthdata",
        endpoint="https://cmr.earthdata.nasa.gov/search/collections.json",
        query={"keyword": "gpm"},
        used=("keyword",),
        ignored=("platform: no verified parameter",),
    )
    assert translation.ignored == ("platform: no verified parameter",)
    assert translation.method == "GET"
    assert translation.body is None


def test_request_estimate_allows_unknown_shape():
    """体量未知时字段为 ``None``，并且必须给出估算依据。"""
    estimate = RequestEstimate(dataset_id="x", basis="no remote size fields")
    assert estimate.matched_files is None
    assert estimate.total_bytes is None
    assert estimate.files_without_size == 0


def test_request_validation_separates_errors_warnings_unverified():
    """errors / warnings / unverified 是三种不同语义，不得混用。"""
    result = RequestValidation(
        ok=False,
        dataset_id="x",
        errors=("temporal window outside declared coverage",),
        warnings=("bbox partially outside",),
        unverified=("data_download: no credential detected",),
    )
    assert result.ok is False
    assert result.errors
    assert result.warnings
    assert result.unverified


def test_task_models_default_to_queued():
    """任务句柄与状态快照的默认状态是 queued。"""
    handle = TaskHandle(provider="demo", task_id="t1", submitted=True)
    assert handle.state == "queued"
    snapshot = TaskStatusSnapshot(provider="demo", task_id="t1", state="running")
    assert snapshot.state == "running"
    assert snapshot.progress_percent is None


def test_dataset_variable_does_not_invent_units():
    """变量单位可以缺失（``None``），不得被填充成猜测值。"""
    variable = DatasetVariable(name="B02", description="Blue (band 2)")
    assert variable.units is None
    assert variable.data_type is None


def test_spatial_and_temporal_contracts_are_shared_with_sources_layer():
    """Provider 与 sources 共享 core 的时空契约，是后续桥接的前提。"""
    extent = SpatialExtent(west=10.0, east=11.0, south=45.0, north=46.0)
    window = TemporalExtent(
        start=datetime(2024, 6, 1, tzinfo=UTC), end=datetime(2024, 7, 1, tzinfo=UTC)
    )
    candidate = DatasetCandidate(
        provider="copernicus",
        dataset_id="sentinel-2-l2a",
        title="Sentinel-2 Level-2A",
        auth_required=False,
        spatial_extent=extent,
        temporal_coverage=window,
    )
    assert candidate.spatial_extent is extent
    assert candidate.temporal_coverage is window


def test_fake_provider_satisfies_runtime_protocol_because_members_exist():
    """``runtime_checkable`` 只看成员存在：这个 fake 的成员齐全，因此协议检查通过。"""
    assert isinstance(_FakeProvider(), ProviderContract)


def test_assert_provider_contract_passes_for_consistent_declaration():
    """成员齐全 + 声明自洽 → 完整自检通过。"""
    assert_provider_contract(_FakeProvider())


def test_assert_provider_contract_catches_missing_method():
    """成员缺失（这里是 ``cancel_task``）→ 协议检查失败，自检不允许放行。"""

    class Missing(_FakeProvider):
        """缺少 ``cancel_task`` 的实现。"""

        cancel_task = None  # type: ignore[assignment]

    with pytest.raises(MetadataError, match="does not satisfy ProviderContract"):
        assert_provider_contract(Missing())


class _InconsistentProvider(_FakeProvider):
    """能力声明声称四个任务方法"不支持"，但 ``submit_task`` 却返回了数据。"""

    def capabilities(self) -> ProviderCapabilities:
        """返回与 ``_FakeProvider`` 相同的声明。"""
        return _caps(provider_id="fake")


def test_assert_provider_contract_rejects_provider_built_on_inconsistent_caps():
    """``assert_provider_contract`` 要在能力声明不自洽时直接拒绝构造消费方。"""

    class Bad(_FakeProvider):
        """声明里把同一方法既列为已实现又列为不支持。"""

        def capabilities(self) -> ProviderCapabilities:
            """返回自相矛盾的声明（构造期即被 pydantic 拒绝，这里用 model_construct 绕过）。"""
            return ProviderCapabilities.model_construct(
                provider_id="fake",
                display_name="Fake",
                auth_required=False,
                implemented=("search", "submit_task"),
                unsupported=(*CONTRACT_METHODS,),
                verified=(),
                unverified=(),
                credential_features={},
                notes=(),
            )

    with pytest.raises(MetadataError, match="both implemented and unsupported"):
        assert_provider_contract(Bad())


def test_assert_provider_contract_rejects_non_provider_objects():
    """非 Provider 对象必须被拒绝。"""
    with pytest.raises(MetadataError, match="does not satisfy ProviderContract"):
        assert_provider_contract(object())


def test_probe_unsupported_accepts_honest_refusal():
    """声明为不支持的方法抛 ``NotImplementedFeatureError`` → 自检通过。"""

    def unsupported() -> None:
        raise NotImplementedFeatureError("demo.feature")

    assert probe_unsupported(unsupported) is True


def test_probe_unsupported_rejects_silent_empty_result():
    """声明为不支持却返回空结果 → 自检必须失败（这正是"冒充已实现"的形态）。"""

    def fake_implemented() -> dict[str, object]:
        return {}

    with pytest.raises(MetadataError, match="instead of raising"):
        probe_unsupported(fake_implemented)
