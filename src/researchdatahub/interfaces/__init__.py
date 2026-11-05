"""外部接口层：把 Core service 暴露给外部调用方（当前只有 MCP）。

分层约定
--------
``core`` → ``planner`` / ``scheduler`` / ``manifest`` / ``integrity`` / ``reporting``
→ ``interfaces``：依赖方向单向，上层不得被下层 import。
本层**只做**入参校验、调用 service、包装返回；任何业务规则都不得写在这里。
Core 与各业务包**不依赖**本层（``interfaces`` 是叶子包，可被整体删除而业务不受影响）。
"""

from __future__ import annotations

__all__: list[str] = []
