"""Planner 的 service 层：``rdh request validate`` / ``rdh plan create|show`` 的实现。

CLI 只负责参数解析与渲染，不直接调用 ``ManifestRepository``、也不自己组装 planner：
本模块返回结构化对象，CLI 按需打印。所有写入都走 ``core.ports.ManifestRepository``
（``save_request`` / ``save_plan``），本模块不碰 SQLite。

命令面（供 Lead 在 ``cli/main.py`` 注册）
----------------------------------------
``rdh request validate <request.yaml>``
    :func:`validate_request_file` → :class:`LoadedRequest`（含 warnings）。
``rdh plan create --request <request.yaml>``
    :func:`create_plan` → :class:`AcquisitionPlan`（已登记 request + plan 与任务）。
``rdh plan show --plan-id <id>``
    :func:`show_plan` → :class:`AcquisitionPlan`。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from researchdatahub.config.models import DefaultsConfig, PlannerConfig, RetryConfig
from researchdatahub.core.models import AcquisitionPlan, DatasetRequest, JsonValue
from researchdatahub.core.ports import DataSourceAdapter, ManifestRepository
from researchdatahub.planner.planner import DefaultTaskPlanner, SplitGranularity
from researchdatahub.planner.request import LoadedRequest, load_request_detailed

__all__ = [
    "create_plan",
    "instantiate_planner",
    "plan_overview",
    "show_plan",
    "validate_request_file",
]


def validate_request_file(
    path: str,
    *,
    units_catalog: Mapping[str, str] | None = None,
) -> LoadedRequest:
    """``rdh request validate`` 的实现：加载 + 校验需求文件。"""
    return load_request_detailed(path, units_catalog=units_catalog)


def instantiate_planner(
    config: DefaultsConfig | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    split_hints: Mapping[str, str] | None = None,
) -> DefaultTaskPlanner:
    """按配置装配 planner（CLI 与测试共用同一装配路径，不各自 new 一个）。"""
    planner_config: PlannerConfig = config.planner if config is not None else PlannerConfig()
    retry: RetryConfig = config.retry if config is not None else RetryConfig()
    return DefaultTaskPlanner(
        planner_config,
        retry=retry,
        clock=clock,
        split_hints=split_hints,
    )


def create_plan(
    *,
    repo: ManifestRepository,
    request: DatasetRequest,
    adapters: Sequence[DataSourceAdapter],
    config: DefaultsConfig | None = None,
    planner: DefaultTaskPlanner | None = None,
    split_hints: Mapping[str, str] | None = None,
) -> AcquisitionPlan:
    """生成计划并持久化（``requests`` + ``plans`` + ``tasks`` 同库）。

    流程：登记请求 → 规划 → 登记计划。计划里的任务由 ``save_plan`` 落库为 ``PLANNED``；
    之后 ``rdh task run`` 会把它们经状态机推进到 ``QUEUED`` 再原子领取。

    Raises
    ------
    SourceNotFoundError
        没有源满足需求（或 ``allow_partial=false`` 且变量缺失）。
    ScheduleError
        计划规模超过 ``planner.max_tasks_per_plan``。
    ManifestError
        请求/计划写入失败。
    """
    effective_planner = planner or instantiate_planner(config, split_hints=split_hints)
    repo.save_request(request)
    plan = effective_planner.plan(request, adapters)
    repo.save_plan(plan)
    return plan


def show_plan(*, repo: ManifestRepository, plan_id: str) -> AcquisitionPlan:
    """读取已登记计划（任务按计划声明顺序）。

    Raises
    ------
    ManifestError
        计划不存在。
    """
    return repo.load_plan(plan_id)


def plan_overview(plan: AcquisitionPlan) -> dict[str, JsonValue]:
    """``rdh plan show`` 的渲染数据（JSON 安全；任务逐条给出核心字段）。"""
    per_source: dict[str, int] = {}
    for task in plan.tasks:
        per_source[task.asset.source_id] = per_source.get(task.asset.source_id, 0) + 1
    return {
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "policy": plan.policy,
        "generated_at": plan.generated_at.isoformat(),
        "task_count": len(plan.tasks),
        "tasks_by_source": cast("JsonValue", per_source),
        "unmatched_variables": list(plan.unmatched_variables),
        "extensions": dict(plan.extensions),
        "tasks": [
            {
                "task_id": task.task_id,
                "state": task.state.value,
                "source_id": task.asset.source_id,
                "file_name": task.asset.file_name,
                "origin_uri": task.asset.origin_uri,
                "access_route": task.asset.access_route.value,
                "priority": task.priority.value,
                "max_attempts": task.max_attempts,
                "time_slice": (
                    None
                    if task.asset.time_slice is None
                    else [
                        task.asset.time_slice.start.isoformat(),
                        task.asset.time_slice.end.isoformat(),
                    ]
                ),
            }
            for task in plan.tasks
        ],
    }


def split_granularity_values() -> tuple[str, ...]:
    """可用的拆分粒度（CLI 帮助文本用）。"""
    return tuple(item.value for item in SplitGranularity)
