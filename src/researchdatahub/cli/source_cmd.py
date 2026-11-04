"""``rdh source list`` —— 列出配置中声明的数据源及其能力边界。

呈现内容
--------
表格列：``id`` / ``display_name`` / ``provider`` / ``routes`` / ``implemented``。

* 数据来自 :func:`researchdatahub.config.loader.load_sources` 返回的
  :class:`~researchdatahub.config.models.SourcesConfig`；CLI 不解析 YAML，也不补默认值。
* ``routes`` 取自 :attr:`~researchdatahub.config.models.SourceConfig.routes`（访问路径集合）。
* ``implemented`` 在 M0 **恒为 False**：M0 只交付契约与骨架，没有任何真实适配器。
  表格下方固定打印一句说明，避免用户把“配置里有”误读成“现在能下载”。
* 凭证只在 loader 内展开，本命令**不读取、不打印**任何凭证值。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.core.errors import RdhError

if TYPE_CHECKING:
    from researchdatahub.config.models import SourcesConfig

__all__ = ["register_source_commands", "source_implemented_in_m0"]

#: M0 没有任何真实数据源适配器；该常量是「implemented」列的唯一事实来源。
source_implemented_in_m0: bool = False

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NOT_AVAILABLE = 2

DEFAULT_SOURCES_PATH: Path = Path("configs/sources.example.yaml")


@dataclass(frozen=True, slots=True)
class _SourceRow:
    """表格中的一行数据源信息。"""

    source_id: str
    display_name: str
    provider: str
    routes: str


def _rows_from_sources(loaded: SourcesConfig) -> list[_SourceRow]:
    """把已加载的数据源声明转换为表格行（按 ``id`` 排序，展示顺序稳定）。"""
    rows = [
        _SourceRow(
            source_id=source_id,
            display_name=config.display_name,
            provider=config.provider,
            routes=", ".join(sorted(route.value for route in config.routes)) or "-",
        )
        for source_id, config in loaded.sources.items()
    ]
    rows.sort(key=lambda row: row.source_id)
    return rows


def register_source_commands(app: typer.Typer) -> None:
    """注册 ``source`` 命令组及其 ``list`` 子命令。

    Parameters
    ----------
    app:
        CLI 根应用（``rdh``）。
    """
    source_app = typer.Typer(
        no_args_is_help=True,
        add_completion=False,
        help="数据源相关操作（M0 只提供只读清单）。",
    )
    app.add_typer(source_app, name="source")

    @source_app.command("list", help="列出配置中声明的数据源（不下载、不联网）。")
    def list_sources(
        sources: Annotated[
            Path,
            typer.Option("--sources", help="数据源配置文件路径。", show_default=True),
        ] = DEFAULT_SOURCES_PATH,
    ) -> None:
        """渲染数据源表格；M0 全部 ``implemented=False``。"""
        if not sources.is_file():
            err_console.print(f"[bold red]错误[/] 数据源配置文件不存在：{sources}")
            raise typer.Exit(code=EXIT_INVALID)

        try:
            from researchdatahub.config.loader import load_sources
        except ImportError as exc:
            err_console.print(
                f"[bold red]错误[/] 数据源加载器不可用（researchdatahub.config.loader 缺失）：{exc}"
            )
            raise typer.Exit(code=EXIT_NOT_AVAILABLE) from exc

        try:
            loaded = load_sources(sources)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_INVALID) from exc

        rows = _rows_from_sources(loaded)
        if not rows:
            err_console.print(f"[bold red]错误[/] 配置中没有任何数据源：{sources}")
            raise typer.Exit(code=EXIT_INVALID)

        table = Table(title="已声明的数据源", header_style="bold")
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("display_name")
        table.add_column("provider")
        table.add_column("routes")
        table.add_column("implemented", justify="center")
        for row in rows:
            implemented = loaded.sources[row.source_id].implemented
            cell = "[green]True[/]" if implemented else "[red]False[/]"
            table.add_row(row.source_id, row.display_name, row.provider, row.routes, cell)
        con_console.print(table)

        pending = sum(1 for config in loaded.sources.values() if not config.implemented)
        con_console.print(
            f"[yellow]说明[/] M0 只交付契约与命令行骨架：共 {len(rows)} 个已声明数据源，"
            f"其中 {pending} 个尚未实现（implemented=False）。"
            "本命令只读取配置，不联网、不下载任何数据。"
        )
        raise typer.Exit(code=EXIT_OK)
