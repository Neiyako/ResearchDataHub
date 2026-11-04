"""``rdh file verify <path>`` —— 对任意文件做完整性体检。

适用场景
--------
* 交付盘/归档盘上的历史文件，**没有** Manifest 记录（不属于任何任务）；
* 从别处拷来的文件，想知道"它是不是完整的、能不能打开"；
* 采集完成后想复核某个具体文件。

因此本命令**不伪造** ``task_id`` / ``file_id`` / ``dataset_id``：它走
:meth:`researchdatahub.integrity.checker.IntegrityChecker.check_path`，
报告的每个字段都来自真实检查，缺失上下文的地方就留空。

可选期望值
----------
``--expected-size`` 与 ``--expected-sha256`` 来自用户（例如数据源发布的清单）。
不提供时：大小检查记 SKIPPED、SHA-256 只**记录**不判定——记录不等于通过。
这与"不推断元数据"的原则一致：没有期望值就不假装比对过。

退出码
------
* ``0``：``PASSED``（所有被要求的检查都通过）。
* ``1``：``FAILED`` / ``SKIPPED`` / ``UNKNOWN`` 或路径不可检查。

注意：``SKIPPED`` 与 ``UNKNOWN`` **不算通过**，因此退出码为 1。脚本里若要区分
"失败"与"没查全"，读 ``--json`` 输出的 ``status`` 字段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, render_error
from researchdatahub.core.errors import RdhError

__all__ = ["DEFAULT_DEFAULTS_PATH", "register_file_commands"]

DEFAULT_DEFAULTS_PATH: Path = Path("configs/defaults.yaml")

EXIT_OK = 0
EXIT_FAILED = 1

_ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="defaults.yaml 路径（默认 configs/defaults.yaml）。"),
]


def register_file_commands(app: typer.Typer) -> None:
    """注册 ``rdh file`` 命令组。"""
    file_app = typer.Typer(no_args_is_help=True, help="本地文件完整性检查。")
    app.add_typer(file_app, name="file")

    @file_app.command("verify", help="校验任意文件的完整性（不需要 Manifest 记录）。")
    def verify(
        path: Annotated[Path, typer.Argument(help="待校验文件路径。")],
        expected_size: Annotated[
            int | None, typer.Option("--expected-size", help="期望字节数（来自数据源声明）。")
        ] = None,
        expected_sha256: Annotated[
            str | None,
            typer.Option("--expected-sha256", help="期望 SHA-256（64 位 hex，来自可信来源）。"),
        ] = None,
        kinds: Annotated[
            list[str] | None,
            typer.Option(
                "--checks",
                help="只跑指定检查（可重复）；缺省用配置 default_checks。[]=不检查。",
            ),
        ] = None,
        config: _ConfigOption = None,
        as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 结果。")] = False,
    ) -> None:
        """执行路径级完整性检查并渲染结果。"""
        from researchdatahub.config.loader import load_defaults
        from researchdatahub.core.models import Checksum
        from researchdatahub.integrity.checker import IntegrityChecker

        try:
            app_config = load_defaults(DEFAULT_DEFAULTS_PATH if config is None else config)
            expected_checksum: Checksum | None = None
            if expected_sha256 is not None:
                digest = expected_sha256.strip().lower()
                if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    con_console.print(
                        "[bold red]错误[/] --expected-sha256 必须是 64 位十六进制字符。",
                    )
                    raise typer.Exit(code=EXIT_FAILED)
                expected_checksum = Checksum(value=f"sha256:{digest}")
            checker = IntegrityChecker(app_config.defaults.integrity)
            result = checker.check_path(
                path,
                kinds=kinds if kinds is not None else None,
                expected_size=expected_size,
                expected_checksum=expected_checksum,
            )
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        if as_json:
            con_console.print_json(json.dumps(result.as_json(), ensure_ascii=False))
            raise typer.Exit(code=EXIT_OK if result.ok else EXIT_FAILED)

        table = Table(title=f"文件校验：{result.path}", header_style="bold")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_column("说明")
        table.add_row("结论", result.status.value, "PASSED 才允许交付")
        table.add_row("大小", f"{result.size_bytes} B", "")
        table.add_row(
            "SHA-256",
            result.sha256 or "（未计算）",
            "本地计算值：只能自证此后未变，不等于与远端一致",
        )
        table.add_row("格式", result.actual_format or "unknown", "")
        if result.netcdf is not None:
            table.add_row(
                "NetCDF",
                f"{result.netcdf.engine}/{result.netcdf.data_model}",
                f"变量 {len(result.netcdf.variables)} 个，维度 {len(result.netcdf.dimensions)} 个",
            )
        con_console.print(table)

        checks = Table(title="逐项检查", header_style="bold")
        checks.add_column("检查")
        checks.add_column("层级")
        checks.add_column("结果")
        checks.add_column("期望")
        checks.add_column("实际")
        for check in result.checks:
            status = check.status.value
            style = {"passed": "green", "failed": "red"}.get(status, "yellow")
            checks.add_row(
                check.kind.value,
                check.level.value,
                f"[{style}]{status}[/]",
                _render(check.expected),
                _render(check.actual),
            )
        con_console.print(checks)
        if result.failures:
            con_console.print("[red]失败原因：[/]")
            for failure in result.failures:
                con_console.print(f"  - {failure}")
        if not result.ok and not result.failures:
            con_console.print(
                "[yellow]注意：结论不是 PASSED（存在 SKIPPED/UNKNOWN 检查），"
                "不能据此认为文件已通过交付校验。[/]",
            )
        raise typer.Exit(code=EXIT_OK if result.ok else EXIT_FAILED)


def _render(value: object) -> str:
    """把检查的期望/实际值渲染成短文本（``None`` 显示为 ``-``）。"""
    if value is None:
        return "-"
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text if len(text) <= 40 else text[:37] + "..."
    return json.dumps(value, ensure_ascii=False)[:40]
