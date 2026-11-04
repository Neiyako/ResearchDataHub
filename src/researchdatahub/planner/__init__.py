"""计划器：把数据需求映射为采集计划（源匹配、去重、拆分、未满足变量登记）。

公开 API
--------
* 需求加载：:func:`~researchdatahub.planner.request.load_request` /
  :func:`~researchdatahub.planner.request.load_request_detailed`（兼容 §6.2 分段格式与
  仓库既有的扁平格式，错误定位到 ``文件:行`` + 字段路径）。
* 计划生成：:class:`~researchdatahub.planner.planner.DefaultTaskPlanner`
  （``core.ports.TaskPlanner`` 实现，只发现不下载）。
* service 层（CLI 直接调用）：:func:`~researchdatahub.planner.service.create_plan` /
  :func:`~researchdatahub.planner.service.show_plan` /
  :func:`~researchdatahub.planner.service.validate_request_file`。
"""

from researchdatahub.planner.planner import (
    DEFAULT_MAX_ATTEMPTS,
    DefaultTaskPlanner,
    DiscoveryAttempt,
    SplitGranularity,
)
from researchdatahub.planner.request import (
    LoadedRequest,
    RequestLoadError,
    load_request,
    load_request_detailed,
    load_request_text,
    load_request_text_detailed,
    parse_request,
)
from researchdatahub.planner.service import (
    create_plan,
    instantiate_planner,
    plan_overview,
    show_plan,
    validate_request_file,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DefaultTaskPlanner",
    "DiscoveryAttempt",
    "LoadedRequest",
    "RequestLoadError",
    "SplitGranularity",
    "create_plan",
    "instantiate_planner",
    "load_request",
    "load_request_detailed",
    "load_request_text",
    "load_request_text_detailed",
    "parse_request",
    "plan_overview",
    "show_plan",
    "validate_request_file",
]
