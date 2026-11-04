"""``rdh report generate`` / ``rdh report show`` —— 采集报告与交付清单。

职责边界
--------
CLI 是**组合根**：它从 Manifest 读取事实（请求、任务、文件、校验报告、数据源），
把数据交给 :mod:`researchdatahub.reporting` 的构造函数，再写盘并呈现。
报告数字一律来自数据库中的实测记录，CLI 不做任何"补全"或"估算"。

产物位置（都在 ``storage.root`` 下，与配置一致）：

```
<root>/reports/<report_id>.json        采集报告（成功/失败/跳过/隔离全量）
<root>/deliveries/<delivery_id>.json   交付清单（仅当存在已落盘文件）
```

``report_id`` 形如 ``report-<request_id>-<UTC 时间戳>``，可直接用于 ``report show``。

退出码
------
* ``0``：报告已生成/展示。
* ``1``：请求不存在、请求下没有任务、数据库不可读等预期失败。
* ``2``：Typer 参数错误。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, render_error
from researchdatahub.core.errors import RdhError

__all__ = ["DEFAULT_DEFAULTS_PATH", "register_report_commands"]

DEFAULT_DEFAULTS_PATH: Path = Path("configs/defaults.yaml")

EXIT_OK = 0
EXIT_FAILED = 1

_ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="defaults.yaml 路径（默认 configs/defaults.yaml）。"),
]
_RootOption = Annotated[
    str | None,
    typer.Option("--root", help="覆盖 storage.root（便携部署/测试用）。"),
]


def _storage_root(config_path: Path | None, root_override: str | None) -> Path:
    """解析 ``storage.root``（与 ``--root`` 覆盖保持一致）。"""
    from researchdatahub.config.loader import load_defaults

    app_config = load_defaults(DEFAULT_DEFAULTS_PATH if config_path is None else config_path)
    raw = root_override if root_override is not None else app_config.defaults.storage.root
    return Path(raw).expanduser().resolve()


def _report_dir(root: Path) -> Path:
    return root / "reports"


def _delivery_dir(root: Path) -> Path:
    return root / "deliveries"


def _report_id(request_id: str, now: datetime) -> str:
    return f"report-{request_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def _write_json_atomic(path: Path, payload: object) -> Path:
    """原子写 JSON（同目录临时文件 + ``os.replace``），避免半写的报告。"""
    import os
    import uuid

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def register_report_commands(app: typer.Typer) -> None:
    """注册 ``rdh report`` 命令组。"""
    report_app = typer.Typer(no_args_is_help=True, help="采集报告与交付清单。")
    app.add_typer(report_app, name="report")

    @report_app.command("generate", help="为某个请求生成采集报告（并尽力生成交付清单）。")
    def generate(
        request_id: Annotated[str, typer.Argument(help="请求 ID（DatasetRequest.request_id）。")],
        plan_id: Annotated[
            str | None, typer.Option("--plan", help="指定计划 ID；缺省取任务上的 plan_id。")
        ] = None,
        config: _ConfigOption = None,
        root: _RootOption = None,
    ) -> None:
        """从 Manifest 事实生成采集报告。"""
        from researchdatahub.core.state import TaskState
        from researchdatahub.manifest.cli_service import open_manifest, resolve_db_target
        from researchdatahub.reporting.delivery import (
            build_delivery_manifest,
            write_delivery_manifest,
        )
        from researchdatahub.reporting.report import (
            build_collection_report,
            render_collection_report,
            write_collection_report,
        )

        try:
            target = resolve_db_target(defaults_path=config, storage_root=root)
            storage_root = _storage_root(config, root)
            repository = open_manifest(target)
            try:
                request = repository.get_request(request_id)
                tasks = list(repository.list_tasks(request_id=request_id))
                if not tasks:
                    raise RdhError(
                        f"请求 {request_id!r} 下没有任何任务，无法生成报告",
                        detail={"request_id": request_id},
                    )
                files = list(repository.list_file_records(request_id=request_id))
                reports = [
                    report
                    for task in tasks
                    for report in repository.list_integrity_reports(task_id=task.task_id)
                ]
                sources = list(repository.list_sources())
            finally:
                repository.close()

            now = datetime.now(UTC)
            effective_plan = plan_id or tasks[0].plan_id
            delivery_path: Path | None = None
            delivery_note: str | None = None
            if files:
                try:
                    unfinished = sum(
                        1
                        for task in tasks
                        if task.state
                        not in {
                            TaskState.COMPLETED,
                            TaskState.SKIPPED,
                            TaskState.FAILED,
                            TaskState.CANCELLED,
                            TaskState.QUARANTINED,
                        }
                    )
                    scope_note = (
                        f"本次交付仅包含 {len(files)} 个已完成文件；"
                        f"{unfinished} 个任务尚未完成，不计入本清单。"
                        if unfinished
                        else None
                    )
                    manifest = build_delivery_manifest(
                        request_id=request_id,
                        plan_id=effective_plan,
                        dataset_metadata=request.metadata,
                        files=files,
                        integrity_reports=reports,
                        sources=sources,
                        created_at=now,
                        notes=scope_note,
                        verify_files=True,
                        base_dir=storage_root,
                    )
                    delivery_path = _delivery_dir(storage_root) / f"{manifest.delivery_id}.json"
                    # 必须用 reporting 的导出函数：它统一处理相对路径、排除项与校验状态，
                    # 手写字典容易漏掉"路径必须相对"这条交付要求（实测踩过）。
                    write_delivery_manifest(manifest, delivery_path, relative_to=storage_root)
                except RdhError as exc:
                    # 交付清单构建失败（例如存在未通过的校验报告）不应阻止生成报告：
                    # 报告本身正是用来解释"为什么还不能交付"的。
                    delivery_note = f"交付清单未生成：{exc.message}"

            report = build_collection_report(
                request_id=request_id,
                plan_id=effective_plan,
                tasks=tasks,
                files=files,
                integrity_reports=reports,
                sources=sources,
                dataset_id=request.dataset_id,
                delivery_manifest_path=delivery_path,
                base_dir=storage_root,
                generated_at=now,
            )
            report_path = _report_dir(storage_root) / f"{_report_id(request_id, now)}.json"
            write_collection_report(report, report_path)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        con_console.print(render_collection_report(report))
        con_console.print(f"\n报告：{report_path}")
        if delivery_path is not None:
            con_console.print(f"交付清单：{delivery_path}")
        if delivery_note is not None:
            con_console.print(f"[yellow]{delivery_note}[/]")
        tally = report.integrity
        if tally.failed or tally.missing or tally.unknown or tally.skipped:
            con_console.print(
                "[yellow]注意：存在未通过/未经校验的文件"
                f"（failed={tally.failed}, skipped={tally.skipped}, "
                f"unknown={tally.unknown}, missing={tally.missing}），"
                "本次不作为完整交付。[/]",
            )

    @report_app.command("show", help="展示已生成的报告（按 report_id）。")
    def show(
        report_id: Annotated[str, typer.Argument(help="报告 ID（report-<request>-<ts>）。")],
        config: _ConfigOption = None,
        root: _RootOption = None,
        as_json: Annotated[bool, typer.Option("--json", help="输出报告原始 JSON。")] = False,
    ) -> None:
        """读取并展示报告 JSON。"""
        storage_root = _storage_root(config, root)
        path = _report_dir(storage_root) / f"{report_id}.json"
        if not path.is_file():
            con_console.print(f"[bold red]错误[/] 报告不存在：{path}")
            raise typer.Exit(code=EXIT_FAILED)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if as_json:
            con_console.print_json(json.dumps(payload, ensure_ascii=False))
            raise typer.Exit(code=EXIT_OK)

        table = Table(title=f"采集报告 {report_id}", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        counts = payload.get("counts", {})
        table.add_row("请求", str(payload.get("request_id")))
        table.add_row("计划", str(payload.get("plan_id")))
        table.add_row("数据集", str(payload.get("dataset_id")))
        table.add_row("生成时间", str(payload.get("generated_at")))
        table.add_row("工具版本", str(payload.get("tool_version")))
        table.add_row(
            "任务",
            "，".join(f"{key}={value}" for key, value in counts.items()) or "（无）",
        )
        integrity = payload.get("integrity", {})
        table.add_row(
            "校验",
            "，".join(f"{key}={value}" for key, value in integrity.items()) or "（无）",
        )
        table.add_row("交付清单", str(payload.get("delivery_manifest_path") or "（未生成）"))
        table.add_row("路径", str(path))
        con_console.print(table)
        failures = payload.get("failures", [])
        if failures:
            con_console.print(f"[yellow]失败记录 {len(failures)} 条：[/]")
            for failure in failures:
                con_console.print(
                    f"  - {failure.get('task_id')}: [{failure.get('code')}] "
                    f"{failure.get('message')}",
                )
        raise typer.Exit(code=EXIT_OK)
