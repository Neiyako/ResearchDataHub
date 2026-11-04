"""``rdh version`` —— 打印工具版本、数据契约版本与运行环境。

输出字段
--------
* ``name``：包名（``researchdatahub``）。
* ``version``：``researchdatahub.__version__``（安装元数据优先，未安装时回退）。
* ``models``：:data:`researchdatahub.core.models.MODELS_VERSION`，即数据契约版本。
  它与工具版本无关：工具可以升级而契约不变，反之亦然，因此必须分别打印。
* ``python``：解释器版本。
* ``platform``：操作系统平台字符串。

退出码始终为 0：这是一条纯查询命令，不依赖配置文件或网络。
"""

from __future__ import annotations

import platform
import sys

import typer
from rich.table import Table

import researchdatahub
from researchdatahub.cli._console import con_console
from researchdatahub.core.models import MODELS_VERSION

__all__ = ["register_version_command"]


def _collect_facts() -> list[tuple[str, str]]:
    """收集版本信息键值对（保持稳定的展示顺序）。"""
    return [
        ("name", "researchdatahub"),
        ("version", researchdatahub.__version__),
        ("models_version", MODELS_VERSION),
        ("python", f"{platform.python_version()} ({sys.implementation.name})"),
        ("platform", platform.platform()),
    ]


def register_version_command(app: typer.Typer) -> None:
    """把 ``version`` 子命令注册到根应用。

    Parameters
    ----------
    app:
        CLI 根应用（``rdh``）。
    """

    @app.command("version", help="显示工具版本、数据契约版本与运行环境。")
    def version() -> None:
        """打印版本与环境信息，退出码 0。"""
        table = Table(
            title="ResearchDataHub 版本信息",
            title_justify="left",
            title_style="bold",
            show_header=True,
            header_style="bold",
        )
        table.add_column("项", style="cyan", no_wrap=True)
        table.add_column("值", overflow="fold")
        for key, value in _collect_facts():
            table.add_row(key, value)
        con_console.print(table)
