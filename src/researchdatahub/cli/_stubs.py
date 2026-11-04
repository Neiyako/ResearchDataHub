"""统一「未实现」机制：M0 的真实拒绝，而不是假成功。

为什么要这个模块
----------------
M0 只交付骨架与契约。任何尚未实现的命令都必须**明确失败**（非零退出码 +
stderr 中文提示），绝不允许打印一行“完成”让用户误以为数据已拿到。
这正是 :class:`researchdatahub.core.errors.NotImplementedFeatureError` 的用途。

命令面
------
* ``rdh plan``   —— 计划生成（planned for M1）
* ``rdh fetch``  —— 数据下载（planned for M1）
* ``rdh status`` —— 任务状态查询（planned for M1）

约束
----
* 执行时写入 stderr 并 ``raise typer.Exit(code=2)``，不构造任何计划/任务对象，
  因此不存在“看起来成功”的路径。
* ``--help`` 文本中同样标注「未实现」，用户查帮助就能知道当前能力边界。
"""

from __future__ import annotations

from typing import Final

import typer
from rich.markup import escape

from researchdatahub.cli._console import err_console, render_error
from researchdatahub.core.errors import NotImplementedFeatureError

__all__ = ["EXIT_NOT_IMPLEMENTED", "refuse", "register_stub_commands"]

#: 未实现功能的统一退出码（与 argparse/typer 的参数错误 2 共用惯例，便于脚本判断）。
EXIT_NOT_IMPLEMENTED: Final[int] = 2


def refuse(feature: str, *, planned_for: str = "M1") -> typer.Exit:
    """构造一次「未实现」拒绝：先向 stderr 打印中文说明，再返回待抛出的 :class:`typer.Exit`。

    Parameters
    ----------
    feature:
        功能标识（英文），例如 ``"plan"``；会写入错误的结构化上下文。
    planned_for:
        计划实现的里程碑，默认 ``"M1"``。

    Returns
    -------
    typer.Exit
        退出码为 :data:`EXIT_NOT_IMPLEMENTED`。调用方必须 ``raise`` 它，
        而不是返回值——这样 Typer 才会以非零码终止进程。
    """
    error = NotImplementedFeatureError(feature, planned_for=planned_for)
    render_error(error)
    err_console.print(f"[yellow]未实现（planned for {escape(planned_for)}）[/]")
    err_console.print("[dim]该命令在 M0 只有骨架，没有执行逻辑；不会产出任何数据。[/]")
    return typer.Exit(code=EXIT_NOT_IMPLEMENTED)


def register_stub_commands(app: typer.Typer) -> None:
    """把 M0 的未实现占位命令注册到根应用上。

    Parameters
    ----------
    app:
        CLI 根应用（``rdh``）。占位命令直接挂在根级，与将来的真实命令同名同位置，
        因此 M1 落地真实实现时只需替换函数体，命令面不变。
    """

    @app.command(
        "plan",
        help="[未实现] 根据数据需求生成采集计划（planned for M1）。M0 只注册命令骨架。",
        rich_help_panel="未实现（M1）",
    )
    def plan() -> None:
        """未实现：计划生成。执行时以退出码 2 失败。"""
        raise refuse("plan")

    @app.command(
        "fetch",
        help="[未实现] 执行采集任务并下载数据（planned for M1）。M0 不会下载任何字节。",
        rich_help_panel="未实现（M1）",
    )
    def fetch() -> None:
        """未实现：数据下载。执行时以退出码 2 失败。"""
        raise refuse("fetch")

    @app.command(
        "status",
        help="[未实现] 查询本地任务状态（planned for M1）。M0 无运行时状态库。",
        rich_help_panel="未实现（M1）",
    )
    def status() -> None:
        """未实现：任务状态查询。执行时以退出码 2 失败。"""
        raise refuse("status")
