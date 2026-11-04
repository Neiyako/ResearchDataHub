"""``rdh task`` 命令组：执行、状态、暂停/恢复/重试/取消、崩溃对账（M4）。

关键约束
--------
* 任务状态只能通过 :mod:`researchdatahub.core.state` 的状态机变更，service 层已保证；
  CLI 不直接改状态。
* ``task run`` 从 **Manifest** 领取任务（不是内存队列），因此进程崩溃后可以 ``task run``
  接着跑，不需要"重新提交"。
* 调度默认并发取配置（``scheduler.max_workers``，保守默认 4；M4 装配再按
  ``DEFAULT_WORKER_LIMIT`` 封顶），8 GB 设备建议 ``--max-workers 2``。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.core.errors import RdhError

if TYPE_CHECKING:
    from researchdatahub.transfer.base import TransportAdapterBase

__all__ = ["register_task_commands"]

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


def register_task_commands(app: typer.Typer) -> None:
    """注册 ``rdh task`` 命令组。"""
    task_app = typer.Typer(no_args_is_help=True, help="采集任务执行与控制。")
    app.add_typer(task_app, name="task")

    @task_app.command("run", help="执行计划中的任务（从 Manifest 领取，可断点续跑）。")
    def run(
        plan_id: Annotated[
            str | None, typer.Argument(help="计划 ID；缺省时按 --request-id 过滤。")
        ] = None,
        request_id: Annotated[
            str | None, typer.Option("--request-id", help="只执行该请求下的任务。")
        ] = None,
        max_workers: Annotated[
            int | None, typer.Option("--max-workers", help="并发 worker 数（默认取配置）。")
        ] = None,
        max_tasks: Annotated[
            int | None, typer.Option("--max-tasks", help="本次最多执行多少个任务。")
        ] = None,
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        root: _RootOption = None,
    ) -> None:
        """装配调度器并阻塞执行。"""
        from researchdatahub.cli._services import (
            build_transports,
            close_transports,
            load_context,
            open_repository,
        )
        from researchdatahub.integrity.checker import IntegrityChecker
        from researchdatahub.manifest.cli_service import open_manifest
        from researchdatahub.scheduler.scheduler import build_scheduler
        from researchdatahub.storage.local import LocalStorageBackend

        transports: Sequence[TransportAdapterBase] = ()
        try:
            context = load_context(
                config_path=config, sources_path=sources, storage_root=root, with_sources=True
            )
            app_config = context.app_config
            storage = LocalStorageBackend(app_config.defaults.storage, create_dirs=True)
            integrity = IntegrityChecker(app_config.defaults.integrity)
            transports = build_transports(context)
            scheduler = build_scheduler(
                repo_factory=lambda: open_manifest(context.db_target),
                storage=storage,
                integrity=integrity,
                transports=list(transports),
                config=app_config.defaults,
            )
            with open_repository(context) as repository:
                if plan_id is not None:
                    plan = repository.load_plan(plan_id)
                    queued = scheduler.submit(plan)
                else:
                    queued = 0
            con_console.print(
                f"入队 {queued} 个任务（计划 {plan_id or '-'}，请求 {request_id or '-'}）",
            )
            summary = scheduler.run(
                max_workers=max_workers,
                request_id=request_id,
                max_tasks=max_tasks,
            )
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        finally:
            close_transports(transports)

        table = Table(title="执行汇总", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_row("开始", summary.started_at.isoformat())
        table.add_row("结束", summary.finished_at.isoformat())
        table.add_row("并发", str(summary.workers))
        for key, value in sorted(summary.counts.items()):
            table.add_row(key, str(value))
        table.add_row("未完成", str(summary.unfinished))
        table.add_row("恢复动作", str(len(summary.recovered)))
        con_console.print(table)

        if summary.errors:
            con_console.print("[yellow]错误：[/]")
            for message in summary.errors:
                con_console.print(f"  - {message}")
        failures = [record for record in summary.records if record.outcome.value != "completed"]
        if failures:
            errors = Table(title="未成功任务", header_style="bold")
            errors.add_column("task_id")
            errors.add_column("结果")
            errors.add_column("原因")
            for record in failures[:50]:
                errors.add_row(
                    record.task_id,
                    record.outcome.value,
                    (record.detail or "")[:120] or "-",
                )
            con_console.print(errors)
        raise typer.Exit(code=EXIT_OK if summary.unfinished == 0 else EXIT_FAILED)

    @task_app.command("status", help="查看任务状态（不传 task_id 时列出任务）。")
    def status(
        task_id: Annotated[str | None, typer.Argument(help="任务 ID；缺省时列出任务。")] = None,
        request_id: Annotated[str | None, typer.Option("--request-id", help="按请求过滤。")] = None,
        limit: Annotated[int, typer.Option("--limit", help="列表最多显示多少条。")] = 50,
        config: _ConfigOption = None,
        root: _RootOption = None,
    ) -> None:
        """展示单个任务详情或任务列表。"""
        from researchdatahub.cli._services import load_context, open_repository
        from researchdatahub.scheduler.service import TaskControlService

        try:
            context = load_context(config_path=config, storage_root=root)
            with open_repository(context) as repository:
                if task_id is not None:
                    view = TaskControlService(repository).status(task_id)
                    payload = view.as_dict()
                    table = Table(title=f"任务 {task_id}", header_style="bold")
                    table.add_column("项", style="bold")
                    table.add_column("值")
                    for key in (
                        "state",
                        "plan_id",
                        "request_id",
                        "attempt",
                        "max_attempts",
                        "priority",
                        "worker_id",
                        "heartbeat_at",
                        "lease_expires_at",
                        "last_error",
                        "file_count",
                        "latest_report_status",
                    ):
                        table.add_row(key, str(payload.get(key)))
                    con_console.print(table)
                    events = _as_list(payload.get("events"))
                    if events:
                        con_console.print("[dim]最近事件：[/]")
                        for event in events:
                            con_console.print(f"  - {event}")
                    raise typer.Exit(code=EXIT_OK)
                tasks = list(repository.list_tasks(request_id=request_id, limit=limit))
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        table = Table(title="任务列表", header_style="bold")
        table.add_column("task_id")
        table.add_column("状态")
        table.add_column("尝试")
        table.add_column("计划")
        table.add_column("worker")
        table.add_column("最后错误")
        for task in tasks:
            table.add_row(
                task.task_id,
                task.state.value,
                f"{task.attempt}/{task.max_attempts}",
                task.plan_id,
                task.worker_id or "-",
                (task.last_error or "")[:60] or "-",
            )
        con_console.print(table)
        if not tasks:
            err_console.print("[dim]没有匹配的任务。[/]")
        raise typer.Exit(code=EXIT_OK)

    def _control(action: str) -> None:
        """生成 pause/resume/retry/cancel 四个同构命令（避免四份重复代码）。"""
        from researchdatahub.cli._services import load_context, open_repository
        from researchdatahub.scheduler.service import TaskControlService

        def command(
            task_id: Annotated[str, typer.Argument(help="任务 ID。")],
            reason: Annotated[
                str | None, typer.Option("--reason", help="操作原因（写入审计）。")
            ] = None,
            config: _ConfigOption = None,
            root: _RootOption = None,
        ) -> None:
            labels = {
                "pause": "用户暂停",
                "resume": "用户恢复",
                "retry": "用户重试",
                "cancel": "用户取消",
            }
            try:
                context = load_context(config_path=config, storage_root=root)
                with open_repository(context) as repository:
                    service = TaskControlService(repository)
                    method = getattr(service, action)
                    task = method(task_id, reason) if reason else method(task_id)
            except RdhError as exc:
                render_error(exc)
                raise typer.Exit(code=EXIT_FAILED) from exc
            con_console.print(
                f"{labels[action]}：{task.task_id} → {task.state.value}",
            )
            raise typer.Exit(code=EXIT_OK)

        command.__name__ = action
        task_app.command(action, help=f"{action} 任务（经状态机校验）。")(command)

    for _action in ("pause", "resume", "retry", "cancel"):
        _control(_action)

    @task_app.command("reconcile", help="崩溃窗口对账：回收过期租约并核对文件与 Manifest。")
    def reconcile(
        config: _ConfigOption = None,
        sources: _SourcesOption = None,
        root: _RootOption = None,
    ) -> None:
        """执行恢复与对账（只读 + 必要的状态修复，不会重新下载）。"""
        from researchdatahub.cli._services import (
            build_transports,
            close_transports,
            load_context,
        )
        from researchdatahub.integrity.checker import IntegrityChecker
        from researchdatahub.manifest.cli_service import open_manifest
        from researchdatahub.scheduler.scheduler import build_scheduler
        from researchdatahub.storage.local import LocalStorageBackend

        transports: Sequence[TransportAdapterBase] = ()
        try:
            context = load_context(
                config_path=config, sources_path=sources, storage_root=root, with_sources=True
            )
            app_config = context.app_config
            storage = LocalStorageBackend(app_config.defaults.storage, create_dirs=True)
            transports = build_transports(context)
            scheduler = build_scheduler(
                repo_factory=lambda: open_manifest(context.db_target),
                storage=storage,
                integrity=IntegrityChecker(app_config.defaults.integrity),
                transports=list(transports),
                config=app_config.defaults,
            )
            recovered = scheduler.release_expired()
            report = scheduler.reconcile()
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        finally:
            close_transports(transports)

        con_console.print(f"回收过期任务：{len(recovered)} 个")
        payload = report.as_dict()
        entries = _as_list(payload.get("entries"))
        needs_attention = payload.get("needs_attention")
        con_console.print(
            f"对账条目：{len(entries)} 个，需人工处理：{needs_attention}",
        )
        if entries:
            table = Table(title="对账明细", header_style="bold")
            table.add_column("task_id")
            table.add_column("动作")
            table.add_column("说明")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                table.add_row(
                    str(entry.get("task_id")),
                    str(entry.get("action")),
                    str(entry.get("detail") or "")[:100],
                )
            con_console.print(table)
        raise typer.Exit(code=EXIT_OK if needs_attention in (0, None) else EXIT_FAILED)


def _as_list(value: object) -> list[object]:
    """把 JSON 值安全地当列表用（非列表一律视为空，避免类型逃逸）。"""
    return value if isinstance(value, list) else []
