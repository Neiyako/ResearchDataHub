"""``rdh plan create`` / ``rdh plan show`` —— 采集计划生成与查看（M4）。

Planner 决定"取什么"，不下载任何字节；计划生成后以 ``PLANNED`` 任务落库，
执行由 ``rdh task run`` 负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.core.errors import RdhError

__all__ = ["register_plan_commands"]

EXIT_OK = 0
EXIT_FAILED = 1

_ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="defaults.yaml 路径（默认 configs/defaults.yaml）。"),
]
_SourcesOption = Annotated[
    Path | None,
    typer.Option("--sources", help="数据源配置路径（默认 configs/sources.yaml，缺失时用示例）。"),
]
_RootOption = Annotated[
    str | None,
    typer.Option("--root", help="覆盖 storage.root。"),
]


def register_plan_commands(app: typer.Typer) -> None:
    """注册 ``rdh plan`` 命令组。"""
    plan_app = typer.Typer(no_args_is_help=True, help="采集计划生成与查看。")
    app.add_typer(plan_app, name="plan")

    @plan_app.command("create", help="根据需求文件生成采集计划（联网查询目录，不下载数据）。")
    def create(
        request_file: Annotated[Path, typer.Argument(help="需求 YAML 文件路径。")],
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        root: _RootOption = None,
    ) -> None:
        """校验需求 → 源匹配 → 生成并持久化计划。"""
        from researchdatahub.cli._services import (
            build_registry,
            load_context,
            open_repository,
        )
        from researchdatahub.planner.service import (
            create_plan,
            plan_overview,
            validate_request_file,
        )

        if not request_file.is_file():
            con_console.print(f"[bold red]错误[/] 需求文件不存在：{request_file}")
            raise typer.Exit(code=EXIT_FAILED)

        try:
            context = load_context(
                config_path=config, sources_path=sources, storage_root=root, with_sources=True
            )
            loaded = validate_request_file(str(request_file))
            registry = build_registry(context)
            entries = registry.entries
            adapters = [entry.adapter for entry in entries if entry.enabled]
            if not adapters:
                raise RdhError(
                    "没有可用的数据源适配器（配置里全部 disabled 或构造失败）",
                    detail={"sources": [entry.source_id for entry in entries]},
                )
            with open_repository(context) as repository:
                plan = create_plan(
                    repo=repository,
                    request=loaded.request,
                    adapters=adapters,
                    config=context.app_config.defaults,
                )
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        overview = plan_overview(plan)
        table = Table(title=f"采集计划：{plan.plan_id}", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_row("请求", str(overview.get("request_id")))
        table.add_row("数据集", str(overview.get("dataset_id")))
        table.add_row("任务数", str(overview.get("task_count")))
        unmatched = overview.get("unmatched_variables")
        unmatched_text = (
            "，".join(str(item) for item in unmatched) if isinstance(unmatched, list) else "（无）"
        )
        table.add_row("未满足变量", unmatched_text or "（无）")
        table.add_row("预计总量", str(overview.get("total_expected_bytes", "unknown")))
        table.add_row("按数据源", str(overview.get("tasks_per_source")))
        con_console.print(table)
        err_console.print("[dim]下一步：rdh task run " + plan.plan_id + "[/]")
        for warning in loaded.warnings:
            con_console.print(f"[yellow]警告：[/] {warning}")
        raise typer.Exit(code=EXIT_OK)

    @plan_app.command("show", help="查看已登记计划（含任务拆分）。")
    def show(
        plan_id: Annotated[str, typer.Argument(help="计划 ID。")],
        config: _ConfigOption = None,
        root: _RootOption = None,
        as_json: Annotated[bool, typer.Option("--json", help="输出 JSON。")] = False,
    ) -> None:
        """读取计划并渲染任务拆分。"""
        from researchdatahub.cli._services import load_context, open_repository
        from researchdatahub.planner.service import plan_overview, show_plan

        try:
            context = load_context(config_path=config, storage_root=root)
            with open_repository(context) as repository:
                plan = show_plan(repo=repository, plan_id=plan_id)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        overview = plan_overview(plan)
        if as_json:
            con_console.print_json(__import__("json").dumps(overview, ensure_ascii=False))
            raise typer.Exit(code=EXIT_OK)

        table = Table(title=f"计划 {plan.plan_id}", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_row("请求", plan.request_id)
        table.add_row("生成时间", plan.generated_at.isoformat())
        table.add_row("策略", plan.policy)
        table.add_row("任务数", str(len(plan.tasks)))
        table.add_row("未满足变量", "，".join(plan.unmatched_variables) or "（无）")
        con_console.print(table)

        tasks = Table(title="任务拆分", header_style="bold")
        tasks.add_column("task_id")
        tasks.add_column("状态")
        tasks.add_column("数据源")
        tasks.add_column("文件")
        tasks.add_column("时间片")
        for task in plan.tasks[:50]:
            asset = task.asset
            window = ""
            if asset.time_slice is not None:
                window = f"{asset.time_slice.start.date()} → {asset.time_slice.end.date()}"
            tasks.add_row(
                task.task_id,
                task.state.value,
                asset.source_id,
                asset.file_name,
                window or "-",
            )
        con_console.print(tasks)
        if len(plan.tasks) > 50:
            con_console.print(f"[dim]仅显示前 50 个任务（共 {len(plan.tasks)} 个）。[/]")
        raise typer.Exit(code=EXIT_OK)
