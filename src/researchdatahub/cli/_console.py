"""CLI 共享输出设施：Rich :class:`Console` 与统一的中文错误渲染。

设计约束
--------
* 面向用户的提示一律中文；命令名、路径、字段名等标识符保持英文。
* :class:`RdhError` 是可预期的业务失败，渲染为可读信息并给出非零退出码，
  **不打印 traceback**；非预期异常不在此拦截，让它带着 traceback 冒出去（属于缺陷）。
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from researchdatahub.core.errors import RdhError

__all__ = ["con_console", "err_console", "render_error"]

#: 标准输出使用的 Console。
#: ``highlight=False`` 避免 Rich 把路径/ID 里的数字与符号加亮成噪声。
con_console: Console = Console(highlight=False, soft_wrap=False)

#: 错误与警告输出使用的 Console（写入 stderr，便于管道与测试断言分开）。
err_console: Console = Console(stderr=True, highlight=False, soft_wrap=False)


def render_error(exc: RdhError) -> None:
    """把 :class:`RdhError` 渲染为两行中文错误信息（错误码 + 说明）。

    Parameters
    ----------
    exc:
        本项目抛出的预期异常；其 ``code`` 属性提供稳定的机器可读错误码。
    """
    err_console.print(f"[bold red]错误[/] [{escape(exc.code)}] {escape(exc.message)}")
    context: list[str] = []
    if exc.task_id:
        context.append(f"task_id={exc.task_id}")
    if exc.source_id:
        context.append(f"source_id={exc.source_id}")
    if context:
        err_console.print(f"[dim]上下文：{' '.join(context)}[/]")
