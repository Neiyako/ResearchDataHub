"""工具 schema 与目录的一致性测试。

这些断言保护的是**外部契约**：``tools/list`` 返回的 18 个工具名、``inputSchema`` 形状与
错误码表，都是 Agent 会依赖的东西，不能因为一次重构改变。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from researchdatahub.interfaces.mcp import schemas
from researchdatahub.interfaces.mcp.schemas.catalog import (
    TOOL_CATALOG,
    TOOL_NAMES,
    SideEffect,
    ToolSpec,
    build_tool_list,
    spec_for,
    tool_names_by_side_effect,
)
from researchdatahub.interfaces.mcp.tools import TOOL_HANDLERS

EXPECTED_TOOLS = (
    "search_datasets",
    "inspect_dataset",
    "compare_datasets",
    "get_provider_capabilities",
    "get_environment_requirements",
    "check_environment",
    "validate_environment",
    "validate_request",
    "estimate_acquisition",
    "create_execution_plan",
    "get_execution_plan",
    "submit_acquisition",
    "get_task_status",
    "cancel_task",
    "list_workers",
    "get_route_status",
    "verify_dataset",
    "get_manifest",
)


def test_exactly_the_18_required_tools() -> None:
    assert TOOL_NAMES == EXPECTED_TOOLS
    assert len(TOOL_NAMES) == 18


def test_every_tool_has_a_handler_and_vice_versa() -> None:
    assert set(TOOL_NAMES) == set(TOOL_HANDLERS)


def test_tool_names_are_unique() -> None:
    assert len(set(TOOL_NAMES)) == len(TOOL_NAMES)


@pytest.mark.parametrize("spec", TOOL_CATALOG, ids=lambda spec: spec.name)
def test_schema_is_a_closed_object(spec: ToolSpec) -> None:
    schema = spec.schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    for name, definition in properties.items():
        assert "description" in definition, f"{spec.name}.{name} lacks a description"
        assert "type" in definition, f"{spec.name}.{name} lacks a type"
    for required in schema.get("required", []):
        assert required in properties


@pytest.mark.parametrize("spec", TOOL_CATALOG, ids=lambda spec: spec.name)
def test_side_effect_flags_are_honest(spec: ToolSpec) -> None:
    assert spec.read_only is (spec.side_effect in (SideEffect.READ_ONLY, SideEffect.COMPUTES))
    assert spec.destructive is (spec.side_effect is SideEffect.REQUIRES_APPROVAL)


def test_side_effect_partition_covers_every_tool() -> None:
    covered = [name for effect in SideEffect for name in tool_names_by_side_effect(effect)]
    assert sorted(covered) == sorted(TOOL_NAMES)


def test_submit_acquisition_is_the_only_approval_gated_tool() -> None:
    assert tool_names_by_side_effect(SideEffect.REQUIRES_APPROVAL) == ("submit_acquisition",)


def test_submit_acquisition_requires_plan_id_and_token() -> None:
    schema = spec_for("submit_acquisition")
    assert schema is not None
    assert set(schema.schema["required"]) == {"plan_id", "approval_token"}


def test_tools_list_payload_matches_schema() -> None:
    payload = build_tool_list()
    assert len(payload) == 18
    by_name = {item["name"]: item for item in payload}
    assert set(by_name) == set(EXPECTED_TOOLS)
    for item in payload:
        assert item["description"]
        assert item["inputSchema"]["additionalProperties"] is False
        annotations = item["annotations"]
        assert annotations["readOnlyHint"] in (True, False)
        assert annotations["destructiveHint"] in (True, False)
    assert by_name["submit_acquisition"]["annotations"]["readOnlyHint"] is False
    assert by_name["search_datasets"]["annotations"]["readOnlyHint"] is True


def test_unknown_tool_lookup_returns_none() -> None:
    assert spec_for("no_such_tool") is None


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_handler_signature_takes_context_and_arguments(name: str) -> None:
    handler = TOOL_HANDLERS[name]
    parameters = list(inspect.signature(handler).parameters.values())
    assert len(parameters) == 2
    assert parameters[0].name == "context"
    assert parameters[1].name == "arguments"


def test_schemas_package_reexports_validation_helpers() -> None:
    for helper in (
        "coerce_arguments",
        "require_str",
        "optional_int",
        "optional_bool",
        "optional_str_list",
        "require_mapping",
    ):
        assert callable(getattr(schemas, helper))


def test_module_level_schema_constants_match_catalog() -> None:
    mapping: dict[str, Any] = {
        "SEARCH_DATASETS_SCHEMA": "search_datasets",
        "INSPECT_DATASET_SCHEMA": "inspect_dataset",
        "COMPARE_DATASETS_SCHEMA": "compare_datasets",
        "GET_PROVIDER_CAPABILITIES_SCHEMA": "get_provider_capabilities",
        "GET_ENVIRONMENT_REQUIREMENTS_SCHEMA": "get_environment_requirements",
        "CHECK_ENVIRONMENT_SCHEMA": "check_environment",
        "VALIDATE_ENVIRONMENT_SCHEMA": "validate_environment",
        "VALIDATE_REQUEST_SCHEMA": "validate_request",
        "ESTIMATE_ACQUISITION_SCHEMA": "estimate_acquisition",
        "CREATE_EXECUTION_PLAN_SCHEMA": "create_execution_plan",
        "GET_EXECUTION_PLAN_SCHEMA": "get_execution_plan",
        "SUBMIT_ACQUISITION_SCHEMA": "submit_acquisition",
        "GET_TASK_STATUS_SCHEMA": "get_task_status",
        "CANCEL_TASK_SCHEMA": "cancel_task",
        "LIST_WORKERS_SCHEMA": "list_workers",
        "GET_ROUTE_STATUS_SCHEMA": "get_route_status",
        "VERIFY_DATASET_SCHEMA": "verify_dataset",
        "GET_MANIFEST_SCHEMA": "get_manifest",
    }
    assert len(mapping) == 18
    for constant_name, tool_name in mapping.items():
        spec = spec_for(tool_name)
        assert spec is not None
        assert getattr(schemas, constant_name) is spec.schema
