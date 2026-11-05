"""目录/能力/路线类工具测试（只读，不发网络请求）。"""

from __future__ import annotations

from tests.unit.test_mcp.conftest import DATASET_ID, Harness

from researchdatahub.config.models import SourceConfig, SourcesConfig, TransportBinding
from researchdatahub.core.models import AccessRoute
from researchdatahub.interfaces.mcp.server import call_tool
from researchdatahub.interfaces.mcp.tools import catalog_tools


def _sources_config(*, source_id: str = "ncar", datasets: list[str] | None = None) -> SourcesConfig:
    config = SourceConfig(
        provider="NSF NCAR RDA",
        display_name="NCAR Research Data Archive",
        adapter="rda",
        implemented=True,
        datasets=datasets if datasets is not None else [DATASET_ID, "era5-single-levels"],
        transports=[
            TransportBinding(route=AccessRoute.HTTPS, adapter="https", implemented=True),
            TransportBinding(route=AccessRoute.S3, adapter="s3", implemented=False),
        ],
        endpoint="https://rda.ucar.edu",
    )
    return SourcesConfig(sources={source_id: config})


def _with_sources(harness: Harness, sources: SourcesConfig) -> object:
    context = harness.context()
    context.sources_config = sources
    return context


def test_search_datasets_lists_configuration_entries(harness: Harness, body: bytes) -> None:
    harness.add_source([harness.make_asset(http_path="/a.nc", body=body, file_name="a.nc")])
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "search_datasets", {})
    assert envelope["ok"] is True
    assert envelope["data"]["total"] == 2
    assert {item["dataset_id"] for item in envelope["data"]["datasets"]} == {
        DATASET_ID,
        "era5-single-levels",
    }
    assert envelope["data"]["source"] == "configuration"


def test_search_datasets_query_filter(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "search_datasets", {"query": "single"})
    assert envelope["ok"] is True
    assert [item["dataset_id"] for item in envelope["data"]["datasets"]] == ["era5-single-levels"]


def test_search_datasets_without_config_is_empty_with_note(harness: Harness) -> None:
    envelope = call_tool(harness.context(), "search_datasets", {})
    assert envelope["ok"] is True
    assert envelope["data"]["datasets"] == []
    assert "no sources configuration" in envelope["data"]["note"]


def test_search_datasets_unknown_provider_is_not_found(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "search_datasets", {"provider": "nope"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"


def test_search_datasets_rejects_unknown_argument(harness: Harness) -> None:
    envelope = call_tool(harness.context(), "search_datasets", {"bogus": 1})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"
    assert envelope["error"]["detail"]["unknown"] == ["bogus"]


def test_search_datasets_limit_and_truncation(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "search_datasets", {"limit": 1})
    assert envelope["ok"] is True
    assert len(envelope["data"]["datasets"]) == 1
    assert envelope["data"]["truncated"] is True


def test_inspect_dataset_returns_capabilities(harness: Harness, body: bytes) -> None:
    source = harness.add_source(
        [harness.make_asset(http_path="/a.nc", body=body, file_name="a.nc")]
    )
    context = _with_sources(harness, _sources_config())
    # 注册表里没有真实适配器时会话里 capabilities 为 null（不伪造）
    envelope = call_tool(context, "inspect_dataset", {"dataset_id": DATASET_ID})
    assert envelope["ok"] is True
    assert envelope["data"]["providers"][0]["provider"] == "ncar"
    assert envelope["data"]["providers"][0]["usable_routes"] == ["https"]
    assert source.discover_calls == []  # 不访问远端


def test_inspect_dataset_unknown_dataset(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "inspect_dataset", {"dataset_id": "no-such-dataset"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"


def test_inspect_dataset_requires_dataset_id(harness: Harness) -> None:
    envelope = call_tool(harness.context(), "inspect_dataset", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"


def test_compare_datasets_returns_dimensions(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(
        context, "compare_datasets", {"dataset_ids": [DATASET_ID, "era5-single-levels"]}
    )
    assert envelope["ok"] is True
    assert set(envelope["data"]["comparison"]) == {
        "variables",
        "temporal_coverage",
        "spatial_extent",
        "vertical_levels",
        "providers",
        "grid",
    }
    # 配置层没有权威范围：如实给 null，而不是编造全球/全年
    assert all(
        value is None for value in envelope["data"]["comparison"]["temporal_coverage"].values()
    )


def test_compare_datasets_requires_two(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "compare_datasets", {"dataset_ids": [DATASET_ID]})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"


def test_compare_datasets_rejects_unknown_dimension(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(
        context,
        "compare_datasets",
        {"dataset_ids": [DATASET_ID, "era5-single-levels"], "dimensions": ["bogus"]},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["detail"]["field"] == "dimensions"


def test_get_provider_capabilities_reports_routes_and_optional_packages(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "get_provider_capabilities", {})
    assert envelope["ok"] is True
    provider = envelope["data"]["providers"][0]
    assert provider["provider"] == "ncar"
    assert {item["route"] for item in provider["routes"]} == {"https", "s3"}
    assert "optional_capabilities" in envelope["data"]
    assert "credential_policy" in envelope["data"]


def test_get_provider_capabilities_never_returns_secret_values(harness: Harness) -> None:
    sources = _sources_config()
    context = _with_sources(harness, sources)
    text = str(call_tool(context, "get_provider_capabilities", {}))
    assert "CDC_SECRET_VALUE" not in text


def test_get_route_status_marks_unimplemented_routes(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "get_route_status", {})
    assert envelope["ok"] is True
    routes = {item["route"]: item for item in envelope["data"]["routes"]}
    assert routes["https"]["usable"] is True
    assert routes["s3"]["usable"] is False
    assert "implemented=false" in routes["s3"]["reason"]
    assert envelope["data"]["probe"]["performed"] is False


def test_get_route_status_filters_by_dataset(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "get_route_status", {"dataset_id": DATASET_ID})
    assert envelope["ok"] is True
    assert envelope["data"]["total"] == 2


def test_get_route_status_unknown_dataset(harness: Harness) -> None:
    context = _with_sources(harness, _sources_config())
    envelope = call_tool(context, "get_route_status", {"dataset_id": "nope"})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"


def test_route_reason_helper() -> None:
    assert (
        catalog_tools._route_reason(False, True, None)
        == "route is declared in configuration but marked implemented=false"
    )
    assert (
        catalog_tools._route_reason(True, False, None)
        == "adapter does not report support for this access route"
    )
    assert catalog_tools._route_reason(True, True, None) == (
        "no adapter instance is constructed for this source"
    )
    assert catalog_tools._route_reason(True, True, object()) is None
