"""MCP 接口层：面向外部 AI Coding Agent 的唯一正式执行接口。

模块地图
--------
* :mod:`.server` —— stdio MCP 服务器与 ``rdh-mcp`` 命令行入口；
* :mod:`.schemas` —— 18 个工具的稳定 ``inputSchema`` 与工具目录（副作用等级）；
* :mod:`.tools` —— 18 个薄适配器：校验入参 → 调 Core service → 包装返回；
* :mod:`.errors` —— 统一返回信封与错误码表；
* :mod:`.auth` —— 副作用审批令牌（HMAC）与 ``mcp_approvals`` 记录表；
* :mod:`.context` —— 组合根（配置/数据库/适配器/可选包装配）；
* :mod:`.capabilities` —— M2/M3/M4 可选能力探测与结果解码。

返回信封（所有工具的返回都是这个形状）::

    {"ok": bool, "data": {...}, "error": {"code","message","hint","retryable"} | null,
     "meta": {"tool", "schema_version", "duration_ms"}}

启动方式::

    rdh-mcp [--root ~/.rdh]
    python -m researchdatahub.interfaces.mcp.server [--root ~/.rdh]
"""

from __future__ import annotations

from researchdatahub.interfaces.mcp.errors import ENVELOPE_SCHEMA_VERSION
from researchdatahub.interfaces.mcp.schemas import TOOL_CATALOG, TOOL_NAMES

__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "TOOL_CATALOG",
    "TOOL_NAMES",
]
