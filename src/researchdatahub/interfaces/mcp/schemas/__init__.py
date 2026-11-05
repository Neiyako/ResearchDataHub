"""MCP 工具的稳定输入 schema 与工具目录。

为什么手写 JSON Schema 而不是从函数签名自动生成
----------------------------------------------
外部 AI Agent 依赖 ``tools/list`` 返回的 ``inputSchema`` 做参数构造与纠错。自动生成
（例如从 pydantic 模型）会把**实现细节**泄露成外部契约：改个函数参数名、加个默认值、
换个类型注解都可能改变对外 schema。这里手工维护 ``.*_SCHEMA`` 常量 + 集中校验函数，
含义见 :func:`researchdatahub.interfaces.mcp.schemas.validate.coerce_arguments`。

约定
----
* 顶层一律 ``{"type": "object", "additionalProperties": False}``：未知字段直接拒绝，
  而不是静默忽略——静默忽略会让 Agent 以为参数生效了。
* 每个属性都有 ``description``（中文），因为它就是给模型看的用法文档。
* ``required`` 只列真正必须的字段；其余有默认值，默认值写在 ``description`` 里。
"""

from __future__ import annotations

from researchdatahub.interfaces.mcp.schemas.catalog import (
    TOOL_CATALOG,
    TOOL_NAMES,
    SideEffect,
    ToolSpec,
    build_tool_list,
    spec_for,
    tool_names_by_side_effect,
)
from researchdatahub.interfaces.mcp.schemas.specs import (
    CANCEL_TASK_SCHEMA,
    CHECK_ENVIRONMENT_SCHEMA,
    COMPARE_DATASETS_SCHEMA,
    CREATE_EXECUTION_PLAN_SCHEMA,
    ESTIMATE_ACQUISITION_SCHEMA,
    GET_ENVIRONMENT_REQUIREMENTS_SCHEMA,
    GET_EXECUTION_PLAN_SCHEMA,
    GET_MANIFEST_SCHEMA,
    GET_PROVIDER_CAPABILITIES_SCHEMA,
    GET_ROUTE_STATUS_SCHEMA,
    GET_TASK_STATUS_SCHEMA,
    INSPECT_DATASET_SCHEMA,
    LIST_WORKERS_SCHEMA,
    SEARCH_DATASETS_SCHEMA,
    SUBMIT_ACQUISITION_SCHEMA,
    VALIDATE_ENVIRONMENT_SCHEMA,
    VALIDATE_REQUEST_SCHEMA,
    VERIFY_DATASET_SCHEMA,
)
from researchdatahub.interfaces.mcp.schemas.validate import (
    Arguments,
    coerce_arguments,
    optional_bool,
    optional_int,
    optional_str,
    optional_str_list,
    require_mapping,
    require_str,
    require_str_list,
)

__all__ = [
    "CANCEL_TASK_SCHEMA",
    "CHECK_ENVIRONMENT_SCHEMA",
    "COMPARE_DATASETS_SCHEMA",
    "CREATE_EXECUTION_PLAN_SCHEMA",
    "ESTIMATE_ACQUISITION_SCHEMA",
    "GET_ENVIRONMENT_REQUIREMENTS_SCHEMA",
    "GET_EXECUTION_PLAN_SCHEMA",
    "GET_MANIFEST_SCHEMA",
    "GET_PROVIDER_CAPABILITIES_SCHEMA",
    "GET_ROUTE_STATUS_SCHEMA",
    "GET_TASK_STATUS_SCHEMA",
    "INSPECT_DATASET_SCHEMA",
    "LIST_WORKERS_SCHEMA",
    "SEARCH_DATASETS_SCHEMA",
    "SUBMIT_ACQUISITION_SCHEMA",
    "TOOL_CATALOG",
    "TOOL_NAMES",
    "VALIDATE_ENVIRONMENT_SCHEMA",
    "VALIDATE_REQUEST_SCHEMA",
    "VERIFY_DATASET_SCHEMA",
    "Arguments",
    "SideEffect",
    "ToolSpec",
    "build_tool_list",
    "coerce_arguments",
    "optional_bool",
    "optional_int",
    "optional_str",
    "optional_str_list",
    "require_mapping",
    "require_str",
    "require_str_list",
    "spec_for",
    "tool_names_by_side_effect",
]
