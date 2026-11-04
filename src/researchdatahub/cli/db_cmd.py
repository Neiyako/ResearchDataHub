"""``rdh db init`` / ``rdh db status`` / ``rdh db migrate`` —— Manifest 数据库管理。

职责边界
--------
CLI 只负责**参数解析、调用 service、呈现结果**；schema 创建与迁移逻辑全部在
:mod:`researchdatahub.manifest.cli_service` 内实现，CLI 不得自行拼 SQL 或判断版本。

三个命令的语义
--------------
* ``db init``    创建数据库目录与 schema（幂等）；按配置决定是否顺带创建存储目录。
* ``db migrate`` 应用待执行迁移（幂等）；不创建存储目录。
* ``db status``  只读快照：路径、schema 版本、WAL/外键开关、各状态任务数、文件数。

退出码
------
* ``0``：成功。
* ``1``：配置缺失/校验失败、数据库不可读、迁移失败（``RdhError``）。
* ``2``：Typer 参数错误。

为什么 ``init`` 会创建存储目录
------------------------------
``storage.create_dirs_on_init`` 默认 ``true``。下载链路要求 ``raw/`` ``staging/``
``quarantine/`` ``temp/`` ``archive/`` 在第一次传输前就存在且**互不重叠**；
等到第一个任务开始时才发现目录缺失，会把"配置错误"推迟成"运行期失败"。
存储目录的创建与校验交给 :class:`~researchdatahub.storage.local.LocalStorageBackend`
（它负责路径穿越防护与"互不重叠"校验），CLI 不重复实现该逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from researchdatahub.cli._console import con_console, err_console, render_error
from researchdatahub.core.errors import RdhError

__all__ = ["DEFAULT_DEFAULTS_PATH", "register_db_commands"]

#: 默认的全局配置文件路径（相对当前工作目录）。
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


def _prepare_storage_dirs(defaults_path: Path | None, storage_root: str | None) -> tuple[str, ...]:
    """按配置创建存储子目录（幂等），返回实际使用的目录名。

    只有 ``storage.create_dirs_on_init`` 为 ``True`` 时才动作；目录布局与冲突校验
    由 :class:`~researchdatahub.storage.local.LocalStorageBackend` 负责。

    ``storage_root`` 必须与 ``--root`` 保持一致：数据库与存储目录指向不同根会让
    "库在 /tmp 而数据在 ~/.rdh" 这种难以察觉的半分离状态成为默认行为。
    """
    # 延迟导入：`rdh version` 等命令不应因为存储层依赖（httpx/boto3 等）而变慢或失败。
    from researchdatahub.config.loader import load_defaults

    app_config = load_defaults(DEFAULT_DEFAULTS_PATH if defaults_path is None else defaults_path)
    storage_config = app_config.defaults.storage
    if storage_root is not None:
        # 配置模型 frozen，用 model_copy 派生而不是就地改写（也避免和其它命令共享状态）。
        storage_config = storage_config.model_copy(update={"root": storage_root})
    if not storage_config.create_dirs_on_init:
        return ()
    from researchdatahub.storage.local import LocalStorageBackend

    backend = LocalStorageBackend(storage_config, create_dirs=True)
    return (
        str(backend.raw_dir),
        str(backend.staging_dir),
        str(backend.quarantine_dir),
        str(backend.temp_dir),
        str(backend.archive_dir),
    )


def register_db_commands(app: typer.Typer) -> None:
    """把 ``rdh db`` 子命令组注册到根应用。

    Parameters
    ----------
    app:
        CLI 根应用（``rdh``）。
    """
    db_app = typer.Typer(no_args_is_help=True, help="SQLite Manifest 数据库管理（M1）。")
    app.add_typer(db_app, name="db")

    @db_app.command("init", help="创建数据库并初始化/升级 schema（幂等）。")
    def init(config: _ConfigOption = None, root: _RootOption = None) -> None:
        """初始化 Manifest 数据库（可选顺带创建存储目录）。"""
        from researchdatahub.manifest.cli_service import db_init, resolve_db_target

        try:
            target = resolve_db_target(defaults_path=config, storage_root=root)
            result = db_init(target)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        con_console.print(f"数据库：{result.db_path}")
        con_console.print(
            f"新建：{'是' if result.created else '否'}    "
            f"schema：v{result.previous_version} -> v{result.schema_version}"
        )
        if result.applied_versions:
            con_console.print("已应用迁移：" + ", ".join(f"v{v}" for v in result.applied_versions))
        try:
            dirs = _prepare_storage_dirs(config, root)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc
        if dirs:
            con_console.print("存储目录已就绪：")
            for directory in dirs:
                con_console.print(f"  {directory}")

    @db_app.command("migrate", help="应用待执行迁移（幂等）。")
    def migrate(config: _ConfigOption = None, root: _RootOption = None) -> None:
        """应用待执行迁移。"""
        from researchdatahub.manifest.cli_service import db_migrate, resolve_db_target

        try:
            target = resolve_db_target(defaults_path=config, storage_root=root)
            result = db_migrate(target)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        con_console.print(f"数据库：{result.db_path}")
        con_console.print(f"schema：v{result.previous_version} -> v{result.schema_version}")
        if result.applied:
            for line in result.applied:
                con_console.print(f"  {line}")
        else:
            con_console.print("[dim]没有待应用的迁移。[/]")

    @db_app.command("status", help="显示数据库状态与任务计数（只读）。")
    def status(config: _ConfigOption = None, root: _RootOption = None) -> None:
        """打印数据库状态快照。"""
        from researchdatahub.manifest.cli_service import db_status, resolve_db_target

        try:
            target = resolve_db_target(defaults_path=config, storage_root=root)
            snapshot = db_status(target)
        except RdhError as exc:
            render_error(exc)
            raise typer.Exit(code=EXIT_FAILED) from exc

        table = Table(title="Manifest 数据库状态")
        table.add_column("项", style="bold")
        table.add_column("值")
        table.add_row("路径", snapshot.db_path)
        table.add_row("schema 版本", str(snapshot.db_schema_version))
        table.add_row("journal_mode", snapshot.journal_mode)
        table.add_row("foreign_keys", "on" if snapshot.foreign_keys else "off")
        table.add_row("文件记录", str(snapshot.file_count))
        table.add_row("校验失败", str(snapshot.integrity_failures))
        counts = ", ".join(
            f"{state}={count}" for state, count in snapshot.task_counts.items() if count
        )
        table.add_row("任务状态", counts or "（无任务）")
        con_console.print(table)
        if snapshot.delivery_ready_requests:
            con_console.print(
                "可交付请求：" + ", ".join(snapshot.delivery_ready_requests),
            )
        err_console.print("[dim]提示：db status 只读，不修改任何数据。[/]", highlight=False)
