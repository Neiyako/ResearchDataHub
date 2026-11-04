"""``rdh request validate`` —— 校验 YAML 数据需求（M4）。

职责边界
--------
YAML 解析与校验规则全部在 :func:`researchdatahub.planner.service.validate_request_file`；
CLI 只负责参数、呈现与退出码。错误信息里已包含 ``文件:行`` 与字段路径，CLI 逐条打印，
不做二次解释（避免把"第 12 行 units 缺失"翻译成"配置有问题"这种丢失信息的话）。

退出码
------
* ``0``：校验通过（可以交给 ``rdh plan create``）。
* ``1``：文件不存在、YAML 语法错误、字段非法、时间/空间/变量校验失败。
* ``2``：Typer 参数错误。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, render_error
from researchdatahub.core.errors import RdhError

__all__ = ["register_request_commands"]

EXIT_OK = 0
EXIT_INVALID = 1


def register_request_commands(app: typer.Typer) -> None:
    """注册 ``rdh request`` 命令组。"""
    request_app = typer.Typer(no_args_is_help=True, help="数据需求（YAML）校验。")
    app.add_typer(request_app, name="request")

    @request_app.command("validate", help="校验数据需求文件（不联网、不生成计划）。")
    def validate(
        path: Annotated[Path, typer.Argument(help="需求 YAML 文件路径。")],
        as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 结果。")] = False,
    ) -> None:
        """加载并校验需求文件，打印规范化后的需求摘要。"""
        from researchdatahub.planner.service import validate_request_file

        if not path.is_file():
            con_console.print(f"[bold red]错误[/] 需求文件不存在：{path}")
            raise typer.Exit(code=EXIT_INVALID)

        try:
            loaded = validate_request_file(str(path))
        except RdhError as exc:
            render_error(exc)
            details = exc.detail.get("errors")
            if isinstance(details, list):
                for line in details:
                    con_console.print(f"  - {line}")
            raise typer.Exit(code=EXIT_INVALID) from exc

        if as_json:
            con_console.print_json(__import__("json").dumps(loaded.as_dict(), ensure_ascii=False))
            raise typer.Exit(code=EXIT_OK)

        request = loaded.request
        metadata = request.metadata
        table = Table(title=f"需求校验通过：{request.request_id}", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_row("文件", str(path))
        table.add_row("数据集", request.dataset_id)
        table.add_row("产品", f"{metadata.product} / {metadata.product_type}")
        table.add_row("数据版本", metadata.data_version)
        table.add_row(
            "变量",
            "，".join(f"{spec.name} ({spec.units})" for spec in metadata.variables),
        )
        table.add_row(
            "时间",
            f"{metadata.temporal_coverage.start.isoformat()} → "
            f"{metadata.temporal_coverage.end.isoformat()}（{metadata.time_definition}）",
        )
        table.add_row(
            "空间",
            f"W{metadata.spatial_extent.west} E{metadata.spatial_extent.east} "
            f"S{metadata.spatial_extent.south} N{metadata.spatial_extent.north}",
        )
        if metadata.vertical_levels.levels:
            table.add_row(
                "垂直层",
                f"{metadata.vertical_levels.kind.value}: "
                + "，".join(f"{level:g}" for level in metadata.vertical_levels.levels),
            )
        table.add_row("候选数据源", "，".join(request.preferred_sources) or "（未指定）")
        table.add_row("完整性检查", "，".join(check.value for check in request.checks))
        con_console.print(table)

        for warning in loaded.warnings:
            con_console.print(f"[yellow]警告：[/] {warning}")
        con_console.print(
            "[dim]下一步：rdh plan create "
            + str(path)
            + "（会联网查询数据源目录，但不会下载数据）[/]",
        )
        raise typer.Exit(code=EXIT_OK)
