"""``rdh db`` 子命令的 CLI 行为测试（M1）。

测试边界
--------
CLI 只做参数解析与呈现，因此这里断言的是**命令面契约**：
退出码、关键输出、幂等性、以及"引用不存在数据库时明确失败"。
SQLite schema 与迁移细节由 ``tests/unit/test_manifest/`` 覆盖，不在此重复。

所有路径都指向 ``tmp_path``：绝不能因为跑测试而在用户真实的 ``~/.rdh`` 里建库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchdatahub.cli.main import app

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"

runner = CliRunner()

#: 固定宽终端：Rich 表格会按终端宽度截断，断言必须与宽度无关。
WIDE_ENV = {"COLUMNS": "240", "TERM": "dumb", "NO_COLOR": "1"}


def invoke(*args: str) -> tuple[int, str]:
    """执行 CLI，返回 ``(退出码, 输出)``。"""
    result = runner.invoke(app, list(args), env=WIDE_ENV)
    return result.exit_code, result.output


def test_db_help_lists_three_commands() -> None:
    code, output = invoke("db", "--help")
    assert code == 0, output
    for command in ("init", "status", "migrate"):
        assert command in output


def test_db_init_creates_database_and_storage_dirs(tmp_path: Path) -> None:
    root = tmp_path / "rdh"
    code, output = invoke("db", "init", "--root", str(root))
    assert code == 0, output
    assert (root / "manifest.db").is_file()
    for subdir in ("raw", "staging", "quarantine", "temp", "archive"):
        assert (root / subdir).is_dir(), f"{subdir} 未创建"
    assert "schema：v0 -> v2" in output


def test_db_init_is_idempotent_over_cli(tmp_path: Path) -> None:
    root = tmp_path / "rdh"
    assert invoke("db", "init", "--root", str(root))[0] == 0
    code, output = invoke("db", "init", "--root", str(root))
    assert code == 0, output
    assert "新建：否" in output
    assert "v2 -> v2" in output


def test_db_init_twice_keeps_existing_rows(tmp_path: Path) -> None:
    """重复 init 不得清库：用真实写入一行来验证，而不是只看命令退出码。"""
    root = tmp_path / "rdh"
    assert invoke("db", "init", "--root", str(root))[0] == 0
    db_path = root / "manifest.db"
    with sqlite3.connect(db_path) as handle:
        handle.execute(
            "INSERT INTO sources (source_id, display_name, provider, access_route, dataset_id,"
            " origin_uri, retrieved_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "ncar",
                "NCAR RDA",
                "NSF NCAR RDA",
                "https",
                "era5-pressure-levels",
                "https://rda.ucar.edu/datasets/ds633.0/",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        handle.commit()

    assert invoke("db", "init", "--root", str(root))[0] == 0
    with sqlite3.connect(db_path) as handle:
        rows = handle.execute("SELECT source_id FROM sources").fetchall()
    assert rows == [("ncar",)]


def test_db_status_reports_wal_foreign_keys_and_schema_version(tmp_path: Path) -> None:
    root = tmp_path / "rdh"
    assert invoke("db", "init", "--root", str(root))[0] == 0
    code, output = invoke("db", "status", "--root", str(root))
    assert code == 0, output
    assert "wal" in output
    assert "on" in output
    assert "schema 版本" in output
    assert "（无任务）" in output


def test_db_status_on_missing_database_fails_explicitly(tmp_path: Path) -> None:
    """未初始化的库必须明确失败（退出码 1），不能打印一张空表让人以为一切正常。"""
    code, output = invoke("db", "status", "--root", str(tmp_path / "never-created"))
    assert code == 1, output
    assert "错误" in output


def test_db_migrate_on_missing_database_creates_and_migrates(tmp_path: Path) -> None:
    """``migrate`` 对不存在的库按空库处理（等价于 init），这是 cli_service 的既定语义。

    与 ``db status`` 不同：status 是只读的，遇到不存在的库必须失败而不是显示空表；
    migrate 的职责本来就是"把库带到最新版本"，空库开始是合法输入。
    """
    root = tmp_path / "never-created"
    code, output = invoke("db", "migrate", "--root", str(root))
    assert code == 0, output
    assert (root / "manifest.db").is_file()
    assert "v0 -> v2" in output


def test_db_migrate_is_idempotent_after_init(tmp_path: Path) -> None:
    root = tmp_path / "rdh"
    assert invoke("db", "init", "--root", str(root))[0] == 0
    code, output = invoke("db", "migrate", "--root", str(root))
    assert code == 0, output
    assert "没有待应用的迁移" in output


def test_db_init_with_missing_config_fails_with_config_error(tmp_path: Path) -> None:
    """配置文件不存在时必须是可读的 ConfigError，而不是 traceback。"""
    code, output = invoke(
        "db", "init", "--config", str(tmp_path / "nope.yaml"), "--root", str(tmp_path / "rdh")
    )
    assert code == 1, output
    assert "错误" in output


def test_db_commands_respect_config_option(tmp_path: Path) -> None:
    """``--config`` 必须真的被使用：把 root 指到临时目录，避免污染默认配置。"""
    config = tmp_path / "defaults.yaml"
    config.write_text(
        DEFAULTS.read_text(encoding="utf-8").replace('root: "~/.rdh"', f'root: "{tmp_path}/cfg"'),
        encoding="utf-8",
    )
    code, output = invoke("db", "init", "--config", str(config))
    assert code == 0, output
    assert (tmp_path / "cfg" / "manifest.db").is_file()


@pytest.mark.parametrize("command", ["init", "migrate", "status"])
def test_db_subcommand_help_exits_zero(command: str) -> None:
    code, output = invoke("db", command, "--help")
    assert code == 0, output
