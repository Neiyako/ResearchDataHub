"""MCP 工具实现（薄适配器）：入参校验 + 调 Core service + 包装返回。

分层红线
--------
本包**只做**三件事：

1. 用 :mod:`researchdatahub.interfaces.mcp.schemas` 校验/收敛入参；
2. 调 Core 或业务包的 service（``planner.service`` / ``scheduler.service`` /
   ``manifest`` / ``integrity`` / ``reporting``）；
3. 把结果整理成信封（:mod:`researchdatahub.interfaces.mcp.errors`）。

任何"应该怎么规划""任务该怎么转换状态""文件是否完整"的判断都**不在这里**：
它们属于 Core 与业务包。本层出现的循环只有"批量调用 service 并汇总"这种转发。

工具登记表
----------
:data:`TOOL_HANDLERS` 是 ``工具名 -> handler`` 的唯一映射，``server.py`` 只按它分发。
handler 是同步函数（Core 是同步的），由 server 放进线程池执行，避免阻塞 MCP 事件循环——
后台进度通知因此不会因为一次长查询而停摆。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from researchdatahub.interfaces.mcp.context import McpContext

# 子模块用``from ... import x as x``的冗余别名形式导入：这是 mypy strict 认可的
# "显式再导出"写法（隐式 re-export 会报 attr-defined），同时让测试可以按名字引用子模块。
from researchdatahub.interfaces.mcp.tools import catalog_tools as catalog_tools
from researchdatahub.interfaces.mcp.tools import environment_tools as environment_tools
from researchdatahub.interfaces.mcp.tools import planning_tools as planning_tools
from researchdatahub.interfaces.mcp.tools import status_tools as status_tools
from researchdatahub.interfaces.mcp.tools import verification_tools as verification_tools
from researchdatahub.interfaces.mcp.tools import worker_tools as worker_tools
from researchdatahub.interfaces.mcp.tools._shared import ToolHandler

__all__ = ["TOOL_HANDLERS", "handler_for"]

#: 上面 6 个子模块以冗余别名导入（``from x import y as y``），即显式对外再导出：
#: 测试与集成脚本按名字使用它们，且 mypy strict 认这种写法为显式 re-export。

TOOL_HANDLERS: Final[Mapping[str, ToolHandler]] = {
    # 只读：目录 / 能力 / 路线
    "search_datasets": catalog_tools.search_datasets,
    "inspect_dataset": catalog_tools.inspect_dataset,
    "compare_datasets": catalog_tools.compare_datasets,
    "get_provider_capabilities": catalog_tools.get_provider_capabilities,
    "get_route_status": catalog_tools.get_route_status,
    # 只读：环境（可选包）
    "get_environment_requirements": environment_tools.get_environment_requirements,
    "check_environment": environment_tools.check_environment,
    "validate_environment": environment_tools.validate_environment,
    # 只读/计算：规划
    "validate_request": planning_tools.validate_request,
    "estimate_acquisition": planning_tools.estimate_acquisition,
    # 副作用：规划落库 + 审批 + 执行
    "create_execution_plan": planning_tools.create_execution_plan,
    "get_execution_plan": planning_tools.get_execution_plan,
    "submit_acquisition": planning_tools.submit_acquisition,
    # 只读/副作用：任务状态与控制
    "get_task_status": status_tools.get_task_status,
    "cancel_task": status_tools.cancel_task,
    # 只读：worker（可选包）
    "list_workers": worker_tools.list_workers,
    # 只读：校验与交付
    "verify_dataset": verification_tools.verify_dataset,
    "get_manifest": verification_tools.get_manifest,
}
"""``工具名 -> 处理函数``。键集合与 ``schemas.TOOL_NAMES`` 必须完全一致（单测守护）。"""


def handler_for(name: str) -> ToolHandler | None:
    """按名称取处理函数；未知工具返回 ``None``（server 转成 ``invalid_arguments``）。"""
    return TOOL_HANDLERS.get(name)


def require_handler(name: str) -> Callable[[McpContext, Mapping[str, Any]], dict[str, Any]]:
    """取处理函数并要求存在（供测试与内部调用）。"""
    handler = handler_for(name)
    if handler is None:
        raise KeyError(f"no handler registered for tool {name!r}")
    return handler
