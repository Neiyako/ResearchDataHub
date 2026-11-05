"""18 个 MCP 工具的 ``inputSchema`` 常量。

文件组织
--------
按"读 / 写"分成三段，与 ``docs/mcp.md`` 的表格顺序一致：

1. **只读查询**（12 个）：不产生任何副作用，可被 Agent 自由调用；
2. **规划**（2 个）：``validate_request`` / ``estimate_acquisition`` 只计算不落库；
3. **副作用**（4 个）：``create_execution_plan`` 落库、``submit_acquisition`` 真正入队、
   ``cancel_task`` 改状态、``get_manifest`` 可写文件——都带明确的前置条件说明。

所有 schema 顶层 ``additionalProperties: false``：未知字段必须报错，不能静默忽略。
"""

from __future__ import annotations

from typing import Any, Final

type Schema = dict[str, Any]

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
    "VALIDATE_ENVIRONMENT_SCHEMA",
    "VALIDATE_REQUEST_SCHEMA",
    "VERIFY_DATASET_SCHEMA",
]

_STRING: Final[Schema] = {"type": "string"}
_IDENTIFIER: Final[Schema] = {
    "type": "string",
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    "description": "标识符（字母数字开头，允许 . _ : -）。",
}
_PATH: Final[Schema] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "description": "文件系统路径（支持 ~ 展开，相对路径以状态根为基准）。",
}
_PROVIDER_ID: Final[Schema] = {
    "type": "string",
    "description": "Provider / 数据源 ID（如 'ncar'、'cds'）；省略时返回全部。",
}


def _object(properties: dict[str, Schema], *, required: list[str] | None = None) -> Schema:
    schema: Schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


# --------------------------------------------------------------------------- #
# 1. 只读查询（12）
# --------------------------------------------------------------------------- #
SEARCH_DATASETS_SCHEMA: Final[Schema] = _object(
    {
        "query": {
            "type": "string",
            "maxLength": 256,
            "description": "模糊关键词（匹配 Provider 名与逻辑数据集 ID）。省略表示不筛。",
        },
        "variables": {
            "type": "array",
            "items": _STRING,
            "maxItems": 64,
            "description": "要求包含的变量名列表（如 ['t', 'clwc']）。",
        },
        "provider": _PROVIDER_ID,
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "description": "最多返回条数，默认 50。",
        },
    }
)

INSPECT_DATASET_SCHEMA: Final[Schema] = _object(
    {
        "dataset_id": {**_IDENTIFIER, "description": "逻辑数据集 ID（Config 声明的 datasets）。"},
        "provider": {**_PROVIDER_ID, "description": "限定在该 Provider 内查询（可选）。"},
    },
    required=["dataset_id"],
)

COMPARE_DATASETS_SCHEMA: Final[Schema] = _object(
    {
        "dataset_ids": {
            "type": "array",
            "items": _IDENTIFIER,
            "minItems": 2,
            "maxItems": 8,
            "description": "要比较的 2–8 个逻辑数据集 ID。",
        },
        "dimensions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "variables",
                    "temporal_coverage",
                    "spatial_extent",
                    "vertical_levels",
                    "providers",
                    "grid",
                ],
            },
            "description": "比较维度；省略表示全部维度。",
        },
    },
    required=["dataset_ids"],
)

GET_PROVIDER_CAPABILITIES_SCHEMA: Final[Schema] = _object({"provider": _PROVIDER_ID})

GET_ENVIRONMENT_REQUIREMENTS_SCHEMA: Final[Schema] = _object(
    {
        "provider": {
            **_PROVIDER_ID,
            "description": "查询该 Provider 的环境要求（依赖/凭据/端点）。",
        },
    },
    required=["provider"],
)

CHECK_ENVIRONMENT_SCHEMA: Final[Schema] = _object(
    {
        "provider": {**_PROVIDER_ID, "description": "检查该 Provider 的运行时环境。"},
        "checks": {
            "type": "array",
            "items": {"type": "string", "enum": ["dependencies", "credentials", "network", "disk"]},
            "description": "只跑指定检查项；省略表示全部。",
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 120,
            "description": "网络/磁盘检查的单次超时，默认 5 秒。",
        },
    },
    required=["provider"],
)

VALIDATE_ENVIRONMENT_SCHEMA: Final[Schema] = _object(
    {
        "provider": {**_PROVIDER_ID, "description": "校验该 Provider 是否满足执行前提。"},
        "require_network": {
            "type": "boolean",
            "description": "是否必须联网通过才算可用，默认 false。",
        },
        "checks": {
            "type": "array",
            "items": {"type": "string", "enum": ["dependencies", "credentials", "network", "disk"]},
            "description": "只校验指定检查项；省略表示全部。",
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 120,
            "description": "网络/磁盘检查的单次超时，默认 5 秒。",
        },
    },
    required=["provider"],
)

GET_EXECUTION_PLAN_SCHEMA: Final[Schema] = _object(
    {"plan_id": {**_IDENTIFIER, "description": "计划 ID（create_execution_plan 的返回值）。"}},
    required=["plan_id"],
)

GET_TASK_STATUS_SCHEMA: Final[Schema] = _object(
    {
        "task_ids": {
            "type": "array",
            "items": _IDENTIFIER,
            "minItems": 1,
            "maxItems": 200,
            "description": "要查询的任务 ID 列表（状态从 SQLite 读取，跨会话可查）。",
        }
    },
    required=["task_ids"],
)

LIST_WORKERS_SCHEMA: Final[Schema] = _object(
    {
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded", "unhealthy", "offline"],
            "description": "按健康状态过滤（可选）。",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "默认 50。"},
    }
)

GET_ROUTE_STATUS_SCHEMA: Final[Schema] = _object(
    {
        "dataset_id": {
            **_IDENTIFIER,
            "description": "只返回能为该数据集提供文件的路线（可选）。",
        },
        "probe": {
            "type": "boolean",
            "description": "是否对端点发起真实探测（HEAD 请求）。默认 false，仅做配置层推导。",
        },
        "timeout_seconds": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 60,
            "description": "probe=true 时的单端点超时，默认 3 秒。",
        },
    }
)

VERIFY_DATASET_SCHEMA: Final[Schema] = _object(
    {
        "paths": {
            "type": "array",
            "items": _PATH,
            "minItems": 1,
            "maxItems": 200,
            "description": "要校验的文件路径列表。",
        },
        "request_id": {
            **_IDENTIFIER,
            "description": "用该请求的交付清单核对文件大小/SHA-256（可选）。",
        },
    },
    required=["paths"],
)

GET_MANIFEST_SCHEMA: Final[Schema] = _object(
    {
        "request_id": {**_IDENTIFIER, "description": "请求 ID（与 plan_id 二选一）。"},
        "plan_id": {**_IDENTIFIER, "description": "计划 ID（与 request_id 二选一）。"},
        "output_path": {
            **_PATH,
            "description": "把交付清单写成 JSON 到这里；目标已存在时返回 conflict，不覆盖。",
        },
        "verify_checksums": {
            "type": "boolean",
            "description": "写文件/返回前逐文件重算 SHA-256 与清单核对，默认 false（只核对大小）。",
        },
    }
)

# --------------------------------------------------------------------------- #
# 2. 规划（只计算、不落库）（2）
# --------------------------------------------------------------------------- #
VALIDATE_REQUEST_SCHEMA: Final[Schema] = _object(
    {
        "request": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "需求对象，支持两种形态：分段格式（request/dataset/variables/temporal/"
                "spatial/vertical/...）或扁平 DatasetRequest 格式。"
            ),
        },
        "request_path": {**_PATH, "description": "改为从 YAML 文件读取需求（与 request 二选一）。"},
        "units_catalog": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "变量 -> 单位 的权威映射（数据源快照）；未提供时不得猜测单位。",
        },
    }
)

ESTIMATE_ACQUISITION_SCHEMA: Final[Schema] = _object(
    {
        "request": {
            "type": "object",
            "additionalProperties": True,
            "description": "需求对象（同 validate_request 的两种格式）。",
        },
        "request_path": {**_PATH, "description": "改为从 YAML 文件读取需求。"},
        "split_granularity": {
            "type": "string",
            "enum": ["auto", "file", "year", "month", "request"],
            "description": "拆分粒度提示，默认 auto（由数据源能力决定）。",
        },
    }
)

# --------------------------------------------------------------------------- #
# 3. 副作用（4）
# --------------------------------------------------------------------------- #
CREATE_EXECUTION_PLAN_SCHEMA: Final[Schema] = _object(
    {
        "request": {
            "type": "object",
            "additionalProperties": True,
            "description": "需求对象；本工具会把请求与计划一起写入 Manifest。",
        },
        "request_path": {**_PATH, "description": "改为从 YAML 文件读取需求。"},
        "split_granularity": {
            "type": "string",
            "enum": ["auto", "file", "year", "month", "request"],
            "description": "拆分粒度提示，默认 auto。",
        },
        "approval_ttl_seconds": {
            "type": "integer",
            "minimum": 60,
            "maximum": 86400,
            "description": "审批令牌有效期（秒），默认 3600。",
        },
    }
)

SUBMIT_ACQUISITION_SCHEMA: Final[Schema] = _object(
    {
        "plan_id": {**_IDENTIFIER, "description": "create_execution_plan 返回的计划 ID。"},
        "approval_token": {
            "type": "string",
            "minLength": 16,
            "maxLength": 4096,
            "description": (
                "create_execution_plan 返回的一次性审批令牌；缺失/不匹配/过期/已使用都会拒绝，"
                "且不产生任何副作用。"
            ),
        },
        "max_workers": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8,
            "description": "本次执行的并发 worker 数；省略时只入队不执行。",
        },
        "max_tasks": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "max_workers 生效时本次最多执行的任务数。",
        },
    },
    required=["plan_id", "approval_token"],
)

CANCEL_TASK_SCHEMA: Final[Schema] = _object(
    {
        "task_ids": {
            "type": "array",
            "items": _IDENTIFIER,
            "minItems": 1,
            "maxItems": 200,
            "description": "要取消的任务 ID 列表；已是终态的任务返回 skipped 并说明原因。",
        },
        "reason": {"type": "string", "maxLength": 512, "description": "取消原因（写入审计事件）。"},
    },
    required=["task_ids"],
)
