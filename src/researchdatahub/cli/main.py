"""``rdh`` 命令行入口。

结构
----
* :data:`app` 是 Typer 根应用，``pyproject.toml`` 的 ``[project.scripts]`` 指向
  ``researchdatahub.cli.main:app``。
* 各命令按模块拆分（``version_cmd`` / ``config_cmd`` / ``source_cmd`` / ``_stubs``），
  由 :func:`register_commands` 统一挂载；新增命令只需在这里加一行注册。

M0 能力边界
-----------
可用：``version``、``config validate``、``source list``、``db init|status|migrate``。
未实现（执行即失败，退出码 2）：``plan``、``fetch``、``status``（M4 落地）。
见 :mod:`researchdatahub.cli._stubs`。

异常策略
--------
:class:`~researchdatahub.core.errors.RdhError` 在命令内部被渲染为中文可读信息；
非预期异常不拦截，让它带着 traceback 冒出去（那是缺陷，不能被吞掉）。
"""

from __future__ import annotations

import sys

import typer

from researchdatahub.cli._console import render_error
from researchdatahub.cli._stubs import register_stub_commands
from researchdatahub.cli.config_cmd import register_config_commands
from researchdatahub.cli.db_cmd import register_db_commands
from researchdatahub.cli.file_cmd import register_file_commands
from researchdatahub.cli.report_cmd import register_report_commands
from researchdatahub.cli.source_cmd import register_source_commands
from researchdatahub.cli.version_cmd import register_version_command
from researchdatahub.core.errors import RdhError

__all__ = ["app", "main", "register_commands"]

_HELP_EPILOG = (
    "可用：version / config validate / source list / db / file / report。\n"
    "M2–M5 落地中：plan / fetch / status 仍为显式拒绝（退出码 2），不会假装成功。"
)

app: typer.Typer = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="ResearchDataHub：多源科研数据采集与同步（M1：SQLite Manifest 可用）。",
    epilog=_HELP_EPILOG,
)


def register_commands(target: typer.Typer) -> None:
    """把所有命令模块注册到给定 Typer 应用。

    Parameters
    ----------
    target:
        目标根应用；传入独立实例即可在不触碰全局 :data:`app` 的前提下构造子应用（便于测试）。
    """
    register_version_command(target)
    register_config_commands(target)
    register_source_commands(target)
    register_db_commands(target)
    register_file_commands(target)
    register_report_commands(target)
    register_stub_commands(target)


register_commands(app)


def main(argv: list[str] | None = None) -> int:
    """程序入口封装：调用 Typer，并把预期异常渲染为中文信息。

    Parameters
    ----------
    argv:
        参数列表；``None`` 时由 Typer 读取 ``sys.argv``。

    Returns
    -------
    int
        进程退出码。参数错误/未实现命令由 Typer 抛出 ``SystemExit``，不会被这里改写。
    """
    try:
        app(args=argv, prog_name="rdh")
    except RdhError as exc:
        render_error(exc)
        return 1
    except SystemExit as exc:  # Typer/Click 的正常退出路径，原样透出退出码
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动执行 `python -m ...` 时
    sys.exit(main())
