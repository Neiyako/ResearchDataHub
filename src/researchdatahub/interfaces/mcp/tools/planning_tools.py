"""规划类工具：``validate_request`` / ``estimate_acquisition`` / ``create_execution_plan`` /
``get_execution_plan`` / ``submit_acquisition``。

副作用边界（本模块最重要的一段）
--------------------------------
* ``validate_request`` / ``estimate_acquisition``：**纯计算**，不写数据库；
* ``create_execution_plan``：把 request + plan + tasks 落库，并签发审批令牌；
* ``submit_acquisition``：**唯一真正会消耗资源的入口**。它必须依次通过：

  1. 计划存在于 Manifest（否则 ``not_found``）；
  2. 计划状态允许执行（所有任务都是 ``PLANNED``；已有任务在跑 → 幂等返回既有任务组）；
  3. 令牌签名正确、未过期、绑定当前计划指纹、记录未被消费（否则 ``approval_invalid``，
     **且不产生任何副作用**）；
  4. 原子消费令牌成功（并发/重放只有一个能过）。

任何一步失败都不入队、不改任务状态。这条链路的每一步都有单测覆盖
（``tests/unit/test_mcp/test_approval.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import yaml

from researchdatahub.core.errors import SourceNotFoundError
from researchdatahub.core.models import AcquisitionPlan, DatasetRequest, JsonValue
from researchdatahub.core.state import TaskState, transition
from researchdatahub.interfaces.mcp.auth import DEFAULT_APPROVAL_TTL_SECONDS
from researchdatahub.interfaces.mcp.context import McpContext, attach_transports
from researchdatahub.interfaces.mcp.errors import (
    ApprovalInvalidError,
    approval_required_error,
    invalid_arguments,
    invalid_state_error,
)
from researchdatahub.interfaces.mcp.schemas import (
    CREATE_EXECUTION_PLAN_SCHEMA,
    ESTIMATE_ACQUISITION_SCHEMA,
    GET_EXECUTION_PLAN_SCHEMA,
    SUBMIT_ACQUISITION_SCHEMA,
    VALIDATE_REQUEST_SCHEMA,
    coerce_arguments,
    optional_int,
    optional_str,
    require_mapping,
    require_str,
)
from researchdatahub.interfaces.mcp.tools._shared import (
    approval_summary,
    ensure_plan_exists,
    json_ready,
    plan_fingerprint_of,
    read_approval_record,
)
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.planner.request import (
    LoadedRequest,
    load_request_detailed,
    load_request_text_detailed,
)
from researchdatahub.planner.service import create_plan, instantiate_planner
from researchdatahub.scheduler.models import DEFAULT_WORKER_LIMIT
from researchdatahub.scheduler.scheduler import build_scheduler

__all__ = [
    "create_execution_plan",
    "estimate_acquisition",
    "get_execution_plan",
    "submit_acquisition",
    "validate_request",
]

_EXECUTABLE_PLAN_STATES: frozenset[TaskState] = frozenset({TaskState.PLANNED, TaskState.QUEUED})
"""允许 ``submit_acquisition`` 推进的计划状态。

``PLANNED``：新计划，正常入队；
``QUEUED``：已入队（重复提交 → 幂等返回既有任务组）。
其余状态（含任何 RUNNING/COMPLETED）都视为"该计划已在执行链路中"，不做二次入队。
"""

_SPLIT_VALUES: frozenset[str] = frozenset({"auto", "file", "year", "month", "request"})


# --------------------------------------------------------------------------- #
# 需求加载（只读）
# --------------------------------------------------------------------------- #
def _load_request(
    arguments: Mapping[str, Any], *, tool: str
) -> tuple[DatasetRequest, tuple[str, ...]]:
    """从 ``request``（分段/扁平对象）或 ``request_path``（YAML）加载需求。

    两种来源互斥；都缺失时报 ``invalid_arguments`` 而不是猜一个空需求。
    分段格式与扁平格式的解析、单位处理、未知字段拒绝**全部**由
    :mod:`researchdatahub.planner.request` 完成，这里不重复实现。
    """
    inline = arguments.get("request")
    path = arguments.get("request_path")
    units_catalog = arguments.get("units_catalog")
    catalog = _units_catalog(units_catalog)
    if inline is None and path is None:
        raise invalid_arguments(
            f"{tool} requires either 'request' (object) or 'request_path' (yaml file)",
            detail={"tool": tool, "required_one_of": ["request", "request_path"]},
        )
    if inline is not None and path is not None:
        raise invalid_arguments(
            f"{tool} accepts only one of 'request' and 'request_path'",
            detail={"tool": tool, "fields": ["request", "request_path"]},
        )
    loaded: LoadedRequest
    if path is not None:
        source_path = require_str(arguments, "request_path")
        loaded = load_request_detailed(source_path, units_catalog=catalog)
    else:
        mapping = require_mapping(arguments, "request")
        # 版本化契约 v1.2（``schema_version: "1.2"`` + ``selection``/``output``）与既有的
        # 分段格式是**两种不同的形状**：v1.2 由 ``planner.request_v12`` 校验后转成同一个
        # 执行契约，既有格式仍走 ``planner.request``。混用会被各自拒绝（都不会猜）。
        if _is_contract_v12(mapping):
            return _load_contract_v12(mapping, tool=tool)
        text = yaml.safe_dump(dict(mapping), allow_unicode=True, sort_keys=False)
        loaded = load_request_text_detailed(text, source=f"<{tool}:request>", units_catalog=catalog)
    return loaded.request, loaded.warnings


def _is_contract_v12(mapping: Mapping[str, Any]) -> bool:
    """判断入参是否是请求契约 v1.2。

    判别依据是**结构标记** ``selection`` 段：v1.2 把变量/时间/空间收在 ``selection`` 下，
    既有分段格式把它们平铺成 ``variables`` / ``temporal`` / ``spatial`` 三个顶层段。
    两个标记同时出现说明调用方把两种格式混用了，交给 v1.2 校验器报错（不猜）。
    """
    version = mapping.get("schema_version")
    if isinstance(version, str) and version.startswith("1.2"):
        return True
    return "selection" in mapping


def _load_contract_v12(
    mapping: Mapping[str, Any], *, tool: str
) -> tuple[DatasetRequest, tuple[str, ...]]:
    """用契约 v1.2 解析并转换成执行契约。

    ``auto`` / ``auto_discover`` 无法在此解析（需要先做数据源发现），因此拒绝并提示
    调用方先跑 ``search_datasets`` / ``inspect_dataset`` 明确 provider 与 dataset_id——
    不在这里编造一个数据集身份。
    """
    from researchdatahub.core.errors import RdhError
    from researchdatahub.planner.request_v12 import (
        DatasetRefError,
        RequestContractError,
        parse_agent_request,
    )

    try:
        contract = parse_agent_request(dict(mapping))
    except RequestContractError as exc:
        raise invalid_arguments(
            f"{tool}: request does not match contract v1.2",
            detail=exc.detail,
        ) from exc
    try:
        request = contract.to_dataset_request()
    except DatasetRefError as exc:
        hint = (
            "run search_datasets / inspect_dataset first, then pass an explicit "
            "dataset.provider and dataset.dataset_id"
            if contract.dataset.needs_discovery
            else "set dataset.product_family to the dataset family (e.g. ERA5)"
        )
        raise invalid_arguments(
            f"{tool}: {exc.message}", detail={"hint": hint, **exc.detail}
        ) from exc
    except RdhError as exc:  # pragma: no cover - 兜底：其它契约错误仍结构化成 invalid_arguments
        raise invalid_arguments(f"{tool}: {exc.message}", detail=exc.detail) from exc
    return request, ()


def _units_catalog(raw: object) -> Mapping[str, str] | None:
    """``units_catalog`` 必须是 ``变量名 -> 单位`` 的字符串映射（缺失即 ``None``）。"""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise invalid_arguments(
            "units_catalog must be an object mapping variable name to unit",
            detail={"field": "units_catalog"},
        )
    catalog: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise invalid_arguments(
                f"units_catalog[{key!r}] must be a string",
                detail={"field": f"units_catalog.{key}"},
            )
        catalog[str(key)] = value
    return catalog


def _split_hints(context: McpContext, split: str | None) -> Mapping[str, str] | None:
    """把 ``split_granularity`` 转成 planner 的 ``split_hints``。

    planner 的契约是 ``{source_id: granularity}``，**没有**"所有源"的通配语义，
    因此这里按键展开成每个已配置源一项；``auto`` 等于不提示（由数据源能力决定）。
    不额外发明通配符，否则会与 planner 的严格校验冲突。
    """
    if split is None or split == "auto":
        return None
    if split not in _SPLIT_VALUES:
        raise invalid_arguments(
            f"split_granularity must be one of {sorted(_SPLIT_VALUES)}",
            detail={"field": "split_granularity", "received": split},
        )
    source_ids = {adapter.source_id for adapter in context.adapters}
    if context.sources_config is not None:
        source_ids |= set(context.sources_config.sources)
    return dict.fromkeys(sorted(source_ids), split)


def _plan_adapters(context: McpContext) -> tuple[Any, ...]:
    """用于规划的适配器；空时返回空元组（planner 会明确报 ``source_not_found``）。"""
    return tuple(context.adapters)


# --------------------------------------------------------------------------- #
# validate_request
# --------------------------------------------------------------------------- #
def validate_request(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """校验需求并返回规范化结果（不落库、不访问远端）。"""
    args = coerce_arguments("validate_request", arguments, _fields(VALIDATE_REQUEST_SCHEMA))
    request, warnings = _load_request(args, tool="validate_request")
    payload: dict[str, Any] = {
        "request": json_ready(request.to_json_dict()),
        "request_id": request.request_id,
        "dataset_id": request.dataset_id,
        "variables": [spec.name for spec in request.metadata.variables],
        "warnings": list(warnings),
        "satisfiable_now": _satisfiability(context, request),
    }
    return payload


def _satisfiability(context: McpContext, request: DatasetRequest) -> dict[str, Any]:
    """只回答"配置层有没有候选源"，**不**声称远端一定有数据。"""
    declared: dict[str, list[str]] = {}
    if context.sources_config is not None:
        declared = {
            source_id: config.datasets
            for source_id, config in context.sources_config.sources.items()
        }
    candidates = sorted(
        source_id for source_id, datasets in declared.items() if request.dataset_id in datasets
    )
    preferred = [item for item in request.preferred_sources if item in candidates]
    return {
        "candidate_providers": candidates,
        "preferred_available": preferred,
        "note": (
            "configuration-level only; actual availability requires discover_assets at plan time"
        ),
    }


# --------------------------------------------------------------------------- #
# estimate_acquisition
# --------------------------------------------------------------------------- #
def estimate_acquisition(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """估算采集规模（调用 planner 计算，不落库）。"""
    args = coerce_arguments("estimate_acquisition", arguments, _fields(ESTIMATE_ACQUISITION_SCHEMA))
    split = optional_str(args, "split_granularity", default="auto")
    request, warnings = _load_request(args, tool="estimate_acquisition")
    adapters = _plan_adapters(context)
    if not adapters:
        raise SourceNotFoundError(
            "no data source adapter is configured; cannot estimate acquisition size"
        )
    plan = _build_plan(context, request=request, split=split)
    return {
        "request_id": request.request_id,
        "warnings": list(warnings),
        "task_count": len(plan.tasks),
        "tasks_by_source": _count_by_source(plan),
        "tasks_by_route": _count_by_route(plan),
        "total_bytes": sum(task.asset.size_bytes for task in plan.tasks),
        "per_task": [
            {
                "source_id": task.asset.source_id,
                "access_route": task.asset.access_route.value,
                "file_name": task.asset.file_name,
                "size_bytes": task.asset.size_bytes,
            }
            for task in plan.tasks
        ],
        "unmatched_variables": list(plan.unmatched_variables),
        "persisted": False,
    }


def _build_plan(
    context: McpContext, *, request: DatasetRequest, split: str | None
) -> AcquisitionPlan:
    """调 ``planner`` 生成计划对象（**不落库**：需要落库时由 create_execution_plan 负责）。"""
    planner = instantiate_planner(context.defaults, split_hints=_split_hints(context, split))
    return planner.plan(request, _plan_adapters(context))


# --------------------------------------------------------------------------- #
# create_execution_plan（落库 + 签发令牌）
# --------------------------------------------------------------------------- #
def create_execution_plan(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """生成计划、落库，并签发一次性审批令牌。"""
    args = coerce_arguments(
        "create_execution_plan", arguments, _fields(CREATE_EXECUTION_PLAN_SCHEMA)
    )
    split = optional_str(args, "split_granularity", default="auto")
    ttl = optional_int(
        args,
        "approval_ttl_seconds",
        default=DEFAULT_APPROVAL_TTL_SECONDS,
        minimum=60,
        maximum=86_400,
    )
    effective_ttl = ttl if ttl is not None else DEFAULT_APPROVAL_TTL_SECONDS
    request, warnings = _load_request(args, tool="create_execution_plan")
    if context.approval_authorizer is None:
        raise ApprovalInvalidError(
            "approval subsystem is not configured on this server instance; cannot issue tokens"
        )
    with context.repository() as repo:
        plan = create_plan(
            repo=repo,
            request=request,
            adapters=_plan_adapters(context),
            config=context.defaults,
            split_hints=_split_hints(context, split),
        )
        fingerprint = plan_fingerprint_of(plan)
        with context.approval_store() as store:
            token, record = context.approval_authorizer.issue(
                store,
                plan_id=plan.plan_id,
                fingerprint=fingerprint,
                ttl_seconds=effective_ttl,
            )
    return {
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "task_count": len(plan.tasks),
        "plan": _plan_payload(plan),
        "approval_token": token,
        "approval": approval_summary(record),
        "approval_usage": {
            "submit_tool": "submit_acquisition",
            "one_shot": True,
            "note": "令牌只对本 plan 生效，提交一次后失效；过期需重新生成计划。",
        },
        "warnings": list(warnings),
        "persisted": True,
    }


# --------------------------------------------------------------------------- #
# get_execution_plan
# --------------------------------------------------------------------------- #
def get_execution_plan(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """读取已落库的计划、任务状态与审批状态（跨会话可查）。"""
    args = coerce_arguments("get_execution_plan", arguments, _fields(GET_EXECUTION_PLAN_SCHEMA))
    plan_id = require_str(args, "plan_id")
    with context.repository() as repo:
        plan = ensure_plan_exists(repo, plan_id)
        task_states = {task.task_id: task.state for task in plan.tasks}
    record = read_approval_record(context, plan_id)
    return {
        "plan": _plan_payload(plan),
        "approval": approval_summary(record),
        "task_states": {task_id: state.value for task_id, state in task_states.items()},
        "executable": _plan_executable(task_states),
    }


# --------------------------------------------------------------------------- #
# submit_acquisition（唯一真正执行入口）
# --------------------------------------------------------------------------- #
def submit_acquisition(context: McpContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """校验审批令牌并把计划推进到 QUEUED（可选同步执行）。"""
    args = coerce_arguments("submit_acquisition", arguments, _fields(SUBMIT_ACQUISITION_SCHEMA))
    plan_id = require_str(args, "plan_id")
    token = require_str(args, "approval_token")
    max_workers = optional_int(args, "max_workers", minimum=1, maximum=8)
    max_tasks = optional_int(args, "max_tasks", minimum=1, maximum=1000)

    authorizer = context.approval_authorizer
    if authorizer is None:
        raise approval_required_error(
            "approval subsystem is not configured on this server instance; nothing was submitted"
        )

    with context.repository() as repo:
        plan = ensure_plan_exists(repo, plan_id)
        current_states = {task.task_id: repo.load_task(task.task_id).state for task in plan.tasks}

        # ---- 幂等：已入队/已在执行 → 返回既有任务组，不再消费令牌 ------------------ #
        # 必须在验签之前判断：此时令牌**已经**被上一次提交消费掉，若先验签就会把
        # "重复提交"误报成 approval_invalid。幂等返回不产生任何副作用，因此不算越权。
        if _already_submitted(current_states):
            return _existing_submission(context, plan, current_states)

        fingerprint = plan_fingerprint_of(plan)
        with context.approval_store() as store:
            decision = authorizer.verify(
                store, plan_id=plan_id, token=token, fingerprint=fingerprint
            )
            if not decision.ok:
                raise ApprovalInvalidError(
                    f"approval token rejected: {decision.reason}",
                    detail={
                        "plan_id": plan_id,
                        "reason": decision.reason,
                        "side_effects_performed": False,
                    },
                )
            consumed = authorizer.consume(store, plan_id=plan_id, token=token)
        if not consumed:
            # 并发提交：另一个请求已经消费了令牌。此时**可能**已入队，因此按幂等口径重新判断。
            latest = {task.task_id: repo.load_task(task.task_id).state for task in plan.tasks}
            if _already_submitted(latest):
                return _existing_submission(context, plan, latest)
            raise ApprovalInvalidError(
                "approval token was consumed by a concurrent submission and no tasks were queued",
                detail={"plan_id": plan_id, "side_effects_performed": False},
            )

        queued = _queue_plan(repo, plan)
        with context.approval_store() as store:
            store.attach_task_group(plan_id, plan.plan_id)

    run_summary: dict[str, Any] | None = None
    if max_workers is not None:
        run_summary = _run_sync(context, plan, max_workers=max_workers, max_tasks=max_tasks)

    return {
        "plan_id": plan.plan_id,
        "submitted": True,
        "queued_tasks": queued,
        "task_count": len(plan.tasks),
        "approval": {"consumed": True, "one_shot": True},
        "executed": run_summary is not None,
        "run": run_summary,
    }


def _existing_submission(
    context: McpContext, plan: AcquisitionPlan, states: Mapping[str, TaskState]
) -> dict[str, Any]:
    """重复提交的幂等返回：给出既有任务组，不重复入队。"""
    return {
        "plan_id": plan.plan_id,
        "submitted": False,
        "duplicate": True,
        "message": "plan was already submitted; returning the existing task group",
        "queued_tasks": 0,
        "task_count": len(plan.tasks),
        "task_states": {task_id: state.value for task_id, state in states.items()},
        "approval": approval_summary(read_approval_record(context, plan.plan_id)),
        "executed": False,
        "run": None,
    }


def _already_submitted(states: Mapping[str, TaskState]) -> bool:
    """计划是否**已经**被提交过（任一任务不再是 PLANNED 即视为已提交）。

    语义说明：计划一旦提交，所有任务都会经同一个事务链推进到 ``QUEUED``；此后再调用
    ``submit_acquisition`` 属于重复提交，应回答"任务组已存在"，而不是再消费一次审批。
    """
    return bool(states) and any(state is not TaskState.PLANNED for state in states.values())


def _plan_executable(states: Mapping[str, TaskState]) -> bool:
    """所有任务都处于"可执行"状态（``PLANNED`` 或 ``QUEUED``）。"""
    return bool(states) and all(state in _EXECUTABLE_PLAN_STATES for state in states.values())


def _queue_plan(repo: SqliteManifestRepository, plan: AcquisitionPlan) -> int:
    """把 ``PLANNED`` 任务逐个经状态机推进到 ``QUEUED``（状态机是唯一权威）。

    不用 ``TaskScheduler.submit`` 的原因：``build_scheduler`` 需要 storage/integrity/transports
    一整套执行装配，而"入队"本身只需要状态机 + 乐观并发写。每个任务单独事务：
    部分失败时前面的任务保持已入队（可重试，不会回滚成不一致的半状态）。
    """
    queued = 0
    now = datetime.now(UTC)
    for task in plan.tasks:
        current = repo.load_task(task.task_id)
        if current.state is not TaskState.PLANNED:
            continue
        updated = transition(current, TaskState.QUEUED, reason="mcp submit_acquisition", now=now)
        repo.save_task_and_event(
            updated,
            expected_state=current.state,
            event="task_submitted",
            payload={"via": "mcp", "plan_id": plan.plan_id},
        )
        queued += 1
    return queued


def _run_sync(
    context: McpContext, plan: AcquisitionPlan, *, max_workers: int, max_tasks: int | None
) -> dict[str, Any]:
    """同步执行（``max_workers`` 显式给出时）：真实调度器、真实传输、真实校验。"""
    with_transports = attach_transports(context)
    if with_transports.storage is None or with_transports.integrity is None:
        raise invalid_state_error(
            "execution is not available: storage/integrity backends are not assembled",
            detail={"plan_id": plan.plan_id},
        )
    scheduler = build_scheduler(
        repo_factory=with_transports.scheduler_repository_factory,
        storage=with_transports.storage,
        integrity=with_transports.integrity,
        transports=list(with_transports.transports),
        config=with_transports.defaults,
    )
    workers = min(max_workers, DEFAULT_WORKER_LIMIT * 4)
    summary = scheduler.run(max_workers=workers, request_id=plan.request_id, max_tasks=max_tasks)
    payload = summary.as_dict()
    return {str(key): json_ready(value) for key, value in payload.items()}


# --------------------------------------------------------------------------- #
# 共用输出
# --------------------------------------------------------------------------- #
def _plan_payload(plan: AcquisitionPlan) -> dict[str, Any]:
    """计划的稳定表示（任务清单含源/路线/字节数/优先级/时间片，不含内部扩展原文）。"""
    return {
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "policy": plan.policy,
        "generated_at": plan.generated_at.isoformat(),
        "task_count": len(plan.tasks),
        "tasks_by_source": _count_by_source(plan),
        "unmatched_variables": list(plan.unmatched_variables),
        "total_bytes": sum(task.asset.size_bytes for task in plan.tasks),
        "tasks": [
            {
                "task_id": task.task_id,
                "source_id": task.asset.source_id,
                "access_route": task.asset.access_route.value,
                "origin_uri": task.asset.origin_uri,
                "file_name": task.asset.file_name,
                "size_bytes": task.asset.size_bytes,
                "state": task.state.value,
                "priority": task.priority.value,
                "max_attempts": task.max_attempts,
                "time_slice": None
                if task.asset.time_slice is None
                else [
                    task.asset.time_slice.start.isoformat(),
                    task.asset.time_slice.end.isoformat(),
                ],
            }
            for task in plan.tasks
        ],
    }


def _count_by_source(plan: AcquisitionPlan) -> dict[str, JsonValue]:
    counts: dict[str, int] = {}
    for task in plan.tasks:
        key = task.asset.source_id
        counts[key] = counts.get(key, 0) + 1
    return dict(counts)


def _count_by_route(plan: AcquisitionPlan) -> dict[str, JsonValue]:
    counts: dict[str, int] = {}
    for task in plan.tasks:
        key = task.asset.access_route.value
        counts[key] = counts.get(key, 0) + 1
    return dict(counts)


def _fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)
