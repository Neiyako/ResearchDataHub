"""工具目录：18 个工具的元数据（名称 / 描述 / schema / 副作用等级）。

副作用等级是给外部 Agent 的**能力声明**，不是注释：

* ``read_only``：无副作用，可任意调用、可并行；
* ``computes``：只读且只做计算（规划/估算），不落库；
* ``mutates_state``：写 Manifest / SQLite / 文件系统，需先确认前置条件；
* ``requires_approval``：必须携带 ``create_execution_plan`` 生成的审批令牌才能执行。

``mcp.types.ToolAnnotations`` 的 ``read_only_hint`` / ``destructive_hint`` 由本模块推导，
因此 Agent 在两处看到一致的结论（``tools/list`` 的 annotations 与描述文字）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from researchdatahub.interfaces.mcp.schemas import specs

__all__ = [
    "TOOL_CATALOG",
    "TOOL_NAMES",
    "SideEffect",
    "ToolSpec",
    "build_tool_list",
    "tool_names_by_side_effect",
]


class SideEffect(StrEnum):
    """工具对系统状态的影响等级。"""

    READ_ONLY = "read_only"
    COMPUTES = "computes"
    MUTATES_STATE = "mutates_state"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass(frozen=True)
class ToolSpec:
    """一个 MCP 工具的静态声明。"""

    name: str
    description: str
    schema: Mapping[str, Any]
    side_effect: SideEffect

    @property
    def read_only(self) -> bool:
        """是否只读（``read_only`` / ``computes`` 都是只读：``computes`` 不落库）。"""
        return self.side_effect in (SideEffect.READ_ONLY, SideEffect.COMPUTES)

    @property
    def destructive(self) -> bool:
        """是否可能破坏既有数据（只有入队执行与取消属于这一类）。"""
        return self.side_effect is SideEffect.REQUIRES_APPROVAL

    @property
    def argument_names(self) -> tuple[str, ...]:
        """Schema 声明的参数名（用于顶层未知字段拒绝）。"""
        properties = self.schema.get("properties")
        if not isinstance(properties, Mapping):
            return ()
        return tuple(str(key) for key in properties)


TOOL_CATALOG: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="search_datasets",
        description=(
            "按关键词/变量/Provider 搜索当前配置中**已声明**的逻辑数据集。"
            "只读，返回 provider、dataset_id、可访问路线与已实现状态；不访问远端。"
        ),
        schema=specs.SEARCH_DATASETS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="inspect_dataset",
        description=(
            "查看单个逻辑数据集的详情：哪些 Provider 提供、可用访问路线、"
            "该 Provider 的适配器能力边界（verified/unverified）。"
        ),
        schema=specs.INSPECT_DATASET_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="compare_datasets",
        description="按指定维度比较 2–8 个逻辑数据集（变量/时间覆盖/空间范围/垂直层/Provider）。",
        schema=specs.COMPARE_DATASETS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="get_provider_capabilities",
        description=(
            "列出数据源适配器的能力边界：可用路线、已验证/未验证能力、"
            "缺失依赖与缺失凭证**名称**（绝不返回凭证值）。"
        ),
        schema=specs.GET_PROVIDER_CAPABILITIES_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="get_environment_requirements",
        description=(
            "查询某 Provider 的环境要求（Python 依赖/凭据引用/网络端点）。"
            "依赖 researchdatahub.environment 包；该包不可用时返回结构化 not_available。"
        ),
        schema=specs.GET_ENVIRONMENT_REQUIREMENTS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="check_environment",
        description=(
            "**真实探测**当前机器是否满足某 Provider 的执行前提（依赖可导入/凭据存在/"
            "端点可达/磁盘充足）。会发起网络请求，但不下载数据。"
        ),
        schema=specs.CHECK_ENVIRONMENT_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="validate_environment",
        description=(
            "判断某 Provider 现在能否真正执行采集：把 check_environment 的结果折叠成"
            "可用/不可用 + 阻断原因列表。不执行任何采集。"
        ),
        schema=specs.VALIDATE_ENVIRONMENT_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="validate_request",
        description=(
            "校验数据需求（支持分段格式与扁平 DatasetRequest 格式），返回规范化后的需求、"
            "警告与可满足性预判。不落库、不访问远端。"
        ),
        schema=specs.VALIDATE_REQUEST_SCHEMA,
        side_effect=SideEffect.COMPUTES,
    ),
    ToolSpec(
        name="estimate_acquisition",
        description=(
            "估算采集规模：任务数、按源分布、按拆分粒度的明细、文件字节总量。"
            "只调用 planner 计算，不落库、不访问远端。"
        ),
        schema=specs.ESTIMATE_ACQUISITION_SCHEMA,
        side_effect=SideEffect.COMPUTES,
    ),
    ToolSpec(
        name="create_execution_plan",
        description=(
            "生成并**落库**执行计划（requests + plans + tasks 同库），返回 plan_id、任务清单"
            "与一次性 approval_token。没有令牌无法执行，这是设计上的两阶段确认。"
        ),
        schema=specs.CREATE_EXECUTION_PLAN_SCHEMA,
        side_effect=SideEffect.MUTATES_STATE,
    ),
    ToolSpec(
        name="get_execution_plan",
        description="按 plan_id 读取已落库的计划、任务状态与审批状态（SQLite 读取，跨会话可查）。",
        schema=specs.GET_EXECUTION_PLAN_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="submit_acquisition",
        description=(
            "**执行**已审批的计划：校验 approval_token（HMAC 签名 + 计划指纹 + 未过期 + 未使用），"
            "通过后把任务推进到 QUEUED；带 max_workers 时同步执行。"
            "令牌无效时不做任何副作用。同一 plan 重复提交返回既有任务组，不重复入队。"
        ),
        schema=specs.SUBMIT_ACQUISITION_SCHEMA,
        side_effect=SideEffect.REQUIRES_APPROVAL,
    ),
    ToolSpec(
        name="get_task_status",
        description=(
            "批量查询任务状态：状态、尝试次数、租约/心跳、最近事件、文件数与最新校验结论。"
            "全部从 SQLite 读取，不依赖服务进程内存。"
        ),
        schema=specs.GET_TASK_STATUS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="cancel_task",
        description=(
            "取消任务（任意非终态 → CANCELLED，经状态机与乐观并发）。"
            "终态任务不会被改写，逐条返回 skipped 与原因。"
        ),
        schema=specs.CANCEL_TASK_SCHEMA,
        side_effect=SideEffect.MUTATES_STATE,
    ),
    ToolSpec(
        name="list_workers",
        description=(
            "列出分布式 worker 及其健康状态与能力。依赖 researchdatahub.network 包；"
            "该包不可用时返回结构化 not_available（不伪造 worker 列表）。"
        ),
        schema=specs.LIST_WORKERS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="get_route_status",
        description=(
            "列出数据源可用的访问路线（https/s3/cds_api/...）及其可用性与不可用原因；"
            "probe=true 时对端点发起真实 HEAD 探测。"
        ),
        schema=specs.GET_ROUTE_STATUS_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="verify_dataset",
        description=(
            "逐文件校验完整性：调用 IntegrityChecker 做字节/格式检查，并用交付清单核对"
            "大小与 SHA-256。**未验证（skipped/unknown）绝不标记为通过**。"
        ),
        schema=specs.VERIFY_DATASET_SCHEMA,
        side_effect=SideEffect.READ_ONLY,
    ),
    ToolSpec(
        name="get_manifest",
        description=(
            "返回交付清单（文件路径/大小/sha256/provider/worker/route/校验结论/manifest 路径）。"
            "可选写 JSON 到 output_path；目标已存在时返回 conflict，不覆盖。"
        ),
        schema=specs.GET_MANIFEST_SCHEMA,
        side_effect=SideEffect.MUTATES_STATE,
    ),
)

TOOL_NAMES: Final[tuple[str, ...]] = tuple(spec.name for spec in TOOL_CATALOG)
"""全部工具名（顺序即 ``tools/list`` 的返回顺序，便于人类阅读）。"""

_CATALOG_BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in TOOL_CATALOG}


def spec_for(name: str) -> ToolSpec | None:
    """按名称查工具声明；未知工具返回 ``None``。"""
    return _CATALOG_BY_NAME.get(name)


def tool_names_by_side_effect(effect: SideEffect) -> tuple[str, ...]:
    """某个副作用等级下的全部工具名（文档与测试用）。"""
    return tuple(spec.name for spec in TOOL_CATALOG if spec.side_effect is effect)


def build_tool_list() -> list[dict[str, Any]]:
    """构造 ``tools/list`` 的返回内容（纯字典，避免接口层 import MCP SDK）。

    字典字段名与 MCP 规范的 ``Tool`` 对象一致（``name`` / ``description`` / ``inputSchema`` /
    ``annotations``），由 ``server.py`` 转成 SDK 类型。
    """
    result: list[dict[str, Any]] = []
    for spec in TOOL_CATALOG:
        result.append(
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": dict(spec.schema),
                "annotations": {
                    "title": spec.name,
                    "readOnlyHint": spec.read_only,
                    "destructiveHint": spec.destructive,
                    "idempotentHint": spec.side_effect is not SideEffect.REQUIRES_APPROVAL,
                    "openWorldHint": spec.name
                    in {"check_environment", "get_route_status", "search_datasets"},
                },
            }
        )
    return result
