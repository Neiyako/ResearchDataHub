"""ResearchDataHub 命令行接口（``rdh``）。

对外只导出根 Typer 应用 :data:`app`；具体命令按模块拆分，见
:func:`researchdatahub.cli.main.register_commands`。
"""

from __future__ import annotations

from researchdatahub.cli.main import app

__all__ = ["app"]
