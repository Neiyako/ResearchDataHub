"""``rdh config validate`` —— 校验默认配置与数据源配置，输出逐条结果。

职责边界
--------
CLI 只负责**路径解析、调用与呈现**；校验规则全部在
:func:`researchdatahub.config.loader.validate_config_files` 内实现。CLI 不得自行放宽
或兜底任何规则——校验不过就是不过，退出码 1。

退出码
------
* ``0``：所有文件通过校验。
* ``1``：至少一个文件不通过、文件缺失，或 loader 抛
  :class:`~researchdatahub.core.errors.ConfigError`。
* ``2``：Typer 层面的参数/命令错误（含未实现命令），由 Typer 自身产生。

延迟导入
--------
``researchdatahub.config.loader`` 由配置模块负责交付，CLI 在**函数体内**导入它，
这样 CLI 其余命令（``version`` / ``source list`` 等）不会因为配置模块暂时缺席而整体崩掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.core.errors import RdhError

if TYPE_CHECKING:
    from researchdatahub.config.models import ValidationResult

__all__ = ["DEFAULT_DEFAULTS_PATH", "DEFAULT_SOURCES_PATH", "register_config_commands"]

#: 默认配置路径（相对当前工作目录，与仓库目录结构一致）。
DEFAULT_DEFAULTS_PATH: Path = Path("configs/defaults.yaml")
DEFAULT_SOURCES_PATH: Path = Path("configs/sources.example.yaml")

EXIT_OK = 0
EXIT_INVALID = 1


def _check_presence(path: Path, label: str) -> bool:
    """检查配置文件是否存在；不存在时打印中文提示并返回 ``False``。"""
    if path.is_file():
        return True
    err_console.print(f"[bold red]错误[/] [{label}] 配置文件不存在：{path}")
    return False


def _render_result(result: ValidationResult) -> list[tuple[Path, list[str]]]:
    """把 loader 返回的 :class:`ValidationResult` 规整为 ``[(文件路径, 错误列表)]``。

    使用 loader 的稳定结构 ``files[i].path`` / ``files[i].errors``（顺序固定为
    ``[defaults, sources]``）。此处只做类型收敛，**不修改、不重排、不过滤**任何错误内容。

    Parameters
    ----------
    result:
        :func:`validate_config_files` 的返回值。

    Returns
    -------
    list[tuple[Path, list[str]]]
        每个条目对应一个被校验的文件及其错误信息（通过时列表为空）。
    """
    return [(Path(item.path), list(item.errors)) for item in result.files]


def register_config_commands(app: typer.Typer) -> None:
    """注册 ``config`` 命令组及其 ``validate`` 子命令。

    Parameters
    ----------
    app:
        CLI 根应用（``rdh``）。
    """
    config_app = typer.Typer(
        no_args_is_help=True,
        add_completion=False,
        help="配置相关操作（M0 只提供校验）。",
    )
    app.add_typer(config_app, name="config")

    @config_app.command("validate", help="校验默认配置与数据源配置文件。")
    def validate(
        defaults: Annotated[
            Path,
            typer.Option("--defaults", help="默认配置文件路径。", show_default=True),
        ] = DEFAULT_DEFAULTS_PATH,
        sources: Annotated[
            Path,
            typer.Option("--sources", help="数据源配置文件路径。", show_default=True),
        ] = DEFAULT_SOURCES_PATH,
    ) -> None:
        """调用配置校验器并呈现结果；全部通过退出码 0，否则 1。"""
        files: list[tuple[Path, str]] = [(defaults, "defaults"), (sources, "sources")]
        missing = [label for path, label in files if not _check_presence(path, label)]
        if missing:
            con_console.print(f"[bold red]FAIL[/] 缺少配置文件：{', '.join(missing)}")
            raise typer.Exit(code=EXIT_INVALID)

        try:
            from researchdatahub.config.loader import validate_config_files
        except ImportError as exc:  # 配置模块尚未交付：这是环境问题，不是校验结果
            err_console.print(
                f"[bold red]错误[/] 配置校验器不可用（researchdatahub.config.loader 缺失）：{exc}"
            )
            raise typer.Exit(code=EXIT_INVALID) from exc

        try:
            result = validate_config_files(defaults, sources)
        except RdhError as exc:
            render_error(exc)
            con_console.print("[bold red]FAIL[/] 配置校验未能完成。")
            raise typer.Exit(code=EXIT_INVALID) from exc

        rows = _render_result(result)

        if result.ok:
            passed = len(rows)
            con_console.print(f"[bold green]PASS[/] 配置校验通过：{passed} 个文件，0 条错误。")
            raise typer.Exit(code=EXIT_OK)

        table = Table(title="配置校验失败", header_style="bold red")
        table.add_column("文件", style="cyan", no_wrap=True)
        table.add_column("错误", overflow="fold")
        for path, errors in rows:
            for message in errors:
                table.add_row(str(path), message)
        con_console.print(table)
        failed = sum(1 for _, errors in rows if errors)
        total = sum(len(errors) for _, errors in rows)
        con_console.print(
            f"[bold red]FAIL[/] 配置校验未通过：{len(rows)} 个文件中 {failed} 个失败，"
            f"{total} 条错误。"
        )
        raise typer.Exit(code=EXIT_INVALID)
