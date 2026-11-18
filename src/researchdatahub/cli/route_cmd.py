"""``rdh route`` —— 面向 Agent 的 route discovery/evaluate/select 命令。

这些命令只做规划与低字节量 probe，不执行科研数据下载。真正的下载仍由 task/submit
链路执行，但 Route Intelligence 的 materialize/authorize 接口会先检查 qualification。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.config.loader import load_defaults, load_sources
from researchdatahub.core.errors import RdhError
from researchdatahub.core.models import DatasetRequest, RouteQualification
from researchdatahub.routing.engine import RouteIntelligence
from researchdatahub.routing.errors import NoQualifiedRouteError
from researchdatahub.routing.models import RoutePlan
from researchdatahub.routing.registry import RouteExperienceRegistry
from researchdatahub.sources.registry import SourceRegistry

__all__ = ["register_route_commands"]

EXIT_OK = 0
EXIT_FAILED = 1

_DEFAULTS = Path("configs/defaults.yaml")
_SOURCES = Path("configs/sources.yaml")
_SOURCES_EXAMPLE = Path("configs/sources.example.yaml")
_ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="defaults.yaml 路径。")]
_SourcesOption = Annotated[Path | None, typer.Option("--sources", help="sources.yaml 路径。")]


@dataclass
class _RouteContext:
    engine: RouteIntelligence
    registry: SourceRegistry
    sources_path: Path


def _load_route_context(
    *,
    config: Path | None,
    sources: Path | None,
) -> _RouteContext:
    defaults_path = config or _DEFAULTS
    chosen_sources = sources or (_SOURCES if _SOURCES.is_file() else _SOURCES_EXAMPLE)
    if not chosen_sources.is_file():
        raise FileNotFoundError(chosen_sources)
    defaults = load_defaults(defaults_path)
    sources_config = load_sources(chosen_sources)
    registry = SourceRegistry.with_defaults().build(sources_config)
    root = Path(defaults.defaults.storage.root).expanduser()
    routing = defaults.defaults.routing
    experience = RouteExperienceRegistry(root / routing.registry_filename)
    engine = RouteIntelligence(
        sources=sources_config,
        adapters=[entry.adapter for entry in registry.entries if entry.enabled],
        registry=experience,
        qualification_ttl=timedelta(days=routing.qualification_ttl_days),
        medium_bytes=routing.small_max_bytes,
        large_bytes=routing.medium_max_bytes,
        max_probe_routes=routing.max_probe_routes,
    )
    return _RouteContext(engine=engine, registry=registry, sources_path=chosen_sources)


def _load_request(path: Path) -> DatasetRequest:
    from researchdatahub.planner.service import validate_request_file

    if not path.is_file():
        raise FileNotFoundError(path)
    return validate_request_file(str(path)).request


def _print_routes(plan: RoutePlan) -> None:
    table = Table(title="候选 routes", header_style="bold")
    for column in (
        "route",
        "source",
        "transport",
        "adapter",
        "capability",
        "adapter_status",
        "qualification",
    ):
        table.add_column(column)
    for route in plan.candidate_routes:
        table.add_row(
            route.id,
            route.source,
            route.transport,
            route.adapter,
            route.capability_summary.value,
            route.adapter_status.value,
            route.qualification.value,
        )
    con_console.print(table)
    if plan.notes:
        for note in plan.notes:
            con_console.print(f"[yellow]note[/] {note}")


def register_route_commands(app: typer.Typer) -> None:
    """注册 ``rdh route`` 命令组。"""
    route_app = typer.Typer(no_args_is_help=True, help="路线发现、能力检查、探活与选择。")
    app.add_typer(route_app, name="route")

    @route_app.command("plan", help="发现多源/多传输候选 routes（不下载数据）。")
    def plan(
        request_file: Annotated[Path, typer.Argument(help="需求 YAML 文件。")],
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        as_json: Annotated[bool, typer.Option("--json", help="输出 JSON。")] = False,
    ) -> None:
        """列出全部候选 route，包含未实现与不匹配项。"""
        try:
            ctx = _load_route_context(config=config, sources=sources)
            try:
                requirement = _load_request(request_file)
                route_plan = ctx.engine.plan(requirement)
            finally:
                ctx.registry.close()
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        except FileNotFoundError as exc:
            err_console.print(f"[bold red]错误[/] 文件不存在：{exc}")
            raise typer.Exit(code=EXIT_FAILED) from exc
        if as_json:
            con_console.print_json(
                json.dumps(route_plan.model_dump(mode="json"), ensure_ascii=False)
            )
        else:
            _print_routes(route_plan)
        raise typer.Exit(code=EXIT_OK)

    @route_app.command("evaluate", help="检查能力、探活并按可解释指标排序。")
    def evaluate(
        request_file: Annotated[Path, typer.Argument(help="需求 YAML 文件。")],
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        probe: Annotated[
            bool, typer.Option("--probe/--no-probe", help="是否探活未知 route。")
        ] = True,
        objective: Annotated[
            str, typer.Option("--objective", help="min_time|balanced|reliability")
        ] = "min_time",
        as_json: Annotated[bool, typer.Option("--json", help="输出 JSON。")] = False,
    ) -> None:
        """执行 route evaluation；不下载科研数据。"""
        try:
            ctx = _load_route_context(config=config, sources=sources)
            try:
                requirement = _load_request(request_file)
                evaluation = ctx.engine.evaluate(
                    ctx.engine.plan(requirement), objective=objective, auto_probe=probe
                )
            finally:
                ctx.registry.close()
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        except FileNotFoundError as exc:
            err_console.print(f"[bold red]错误[/] 文件不存在：{exc}")
            raise typer.Exit(code=EXIT_FAILED) from exc
        if as_json:
            con_console.print_json(
                json.dumps(evaluation.model_dump(mode="json"), ensure_ascii=False)
            )
        else:
            table = Table(
                title=f"route evaluation ({evaluation.workload.value})", header_style="bold"
            )
            for column in ("rank", "route", "eligible", "score", "reason"):
                table.add_column(column)
            for item in evaluation.decisions:
                table.add_row(
                    str(item.rank),
                    item.route_id,
                    "yes" if item.eligible else "no",
                    f"{item.score:.1f}",
                    item.rejection or item.rationale,
                )
            con_console.print(table)
            con_console.print(
                f"selected: {evaluation.selected.id if evaluation.selected else 'none'} | "
                f"needs_probe: {', '.join(evaluation.needs_probe) or 'none'}"
            )
        raise typer.Exit(code=EXIT_OK)

    @route_app.command("select", help="选择一条已获得资质的 route。")
    def select(
        request_file: Annotated[Path, typer.Argument(help="需求 YAML 文件。")],
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        objective: Annotated[str, typer.Option("--objective")] = "min_time",
        preferred: Annotated[
            str | None, typer.Option("--preferred", help="显式 route ID。")
        ] = None,
    ) -> None:
        """选择 route；未 qualification 时明确失败。"""
        try:
            ctx = _load_route_context(config=config, sources=sources)
            try:
                requirement = _load_request(request_file)
                selection = ctx.engine.select(
                    ctx.engine.plan(requirement), objective=objective, preferred_route_id=preferred
                )
            finally:
                ctx.registry.close()
        except NoQualifiedRouteError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        except FileNotFoundError as exc:
            err_console.print(f"[bold red]错误[/] 文件不存在：{exc}")
            raise typer.Exit(code=EXIT_FAILED) from exc
        con_console.print(f"selected: {selection.route.id}")
        con_console.print(f"reason: {selection.reason}")
        raise typer.Exit(code=EXIT_OK)

    @route_app.command("qualify", help="对指定 route 执行一次低字节量 probe。")
    def qualify(
        request_file: Annotated[Path, typer.Argument(help="需求 YAML 文件。")],
        route_id: Annotated[str, typer.Argument(help="route ID。")],
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
    ) -> None:
        """探活 route 并把经验写入 routes.json；不下载科研数据。"""
        try:
            ctx = _load_route_context(config=config, sources=sources)
            try:
                requirement = _load_request(request_file)
                result, updated = ctx.engine.qualify(ctx.engine.plan(requirement), route_id)
            finally:
                ctx.registry.close()
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        except FileNotFoundError as exc:
            err_console.print(f"[bold red]错误[/] 文件不存在：{exc}")
            raise typer.Exit(code=EXIT_FAILED) from exc
        con_console.print(f"route: {updated.id}")
        con_console.print(f"probe: {result.status.value}")
        con_console.print(f"qualification: {updated.qualification.value}")
        if result.detail:
            con_console.print(f"detail: {result.detail}")
        raise typer.Exit(
            code=EXIT_OK if updated.qualification is RouteQualification.QUALIFIED else EXIT_FAILED
        )
