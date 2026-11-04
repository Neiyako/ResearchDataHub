"""CLI 测试：帮助、版本、配置校验退出码、数据源列表、未实现命令必须失败。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchdatahub.cli.main import app

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"
SOURCES = REPO / "configs" / "sources.example.yaml"

runner = CliRunner()

# 固定宽终端：Rich 会按终端宽度截断表格，若不固定，断言结果会随运行环境漂移。
WIDE_ENV = {"COLUMNS": "240", "TERM": "dumb", "NO_COLOR": "1"}


def invoke(*args: str) -> tuple[int, str]:
    """执行 CLI，返回 (退出码, 输出)。输出在宽终端下生成，避免被截断。"""
    result = runner.invoke(app, list(args), env=WIDE_ENV)
    return result.exit_code, result.output


def test_help_exits_zero_and_lists_commands() -> None:
    code, output = invoke("--help")
    assert code == 0
    for command in ("version", "config", "source"):
        assert command in output


def test_help_marks_unimplemented_commands() -> None:
    """未实现命令必须在帮助里可见且被标记，不能隐藏成"不存在"。"""
    code, output = invoke("--help")
    assert code == 0
    assert "未实现" in output


@pytest.mark.parametrize("command", ["version", "config", "source", "plan", "fetch", "status"])
def test_subcommand_help_works(command: str) -> None:
    code, output = invoke(command, "--help")
    assert code == 0, output
    assert "Usage" in output or "用法" in output


def test_version_exits_zero_and_prints_versions() -> None:
    code, output = invoke("version")
    assert code == 0
    assert "researchdatahub" in output
    assert "0.1.0" in output
    assert "1.0" in output, "version 应同时暴露模型契约版本"


def test_config_validate_passes_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    code, output = invoke(
        "config", "validate", "--defaults", str(DEFAULTS), "--sources", str(SOURCES)
    )
    assert code == 0, output
    assert "PASS" in output.upper()


def test_config_validate_fails_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验失败必须退出码 1 并在输出中点名缺失的变量，而不是假装成功。"""
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    code, output = invoke(
        "config", "validate", "--defaults", str(DEFAULTS), "--sources", str(SOURCES)
    )
    assert code == 1, output
    assert "CDSAPI_KEY" in output


def test_config_validate_fails_on_broken_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "defaults.yaml"
    bad.write_text("defaults: [unclosed\n", encoding="utf-8")
    code, output = invoke("config", "validate", "--defaults", str(bad), "--sources", str(SOURCES))
    assert code == 1
    assert output.strip() != ""


def test_config_validate_missing_file_is_reported(tmp_path: Path) -> None:
    """文件不存在不得崩溃，而是作为校验失败报告。"""
    code, output = invoke(
        "config",
        "validate",
        "--defaults",
        str(tmp_path / "nope.yaml"),
        "--sources",
        str(tmp_path / "nope2.yaml"),
    )
    assert code == 1
    assert "nope" in output


def test_source_list_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    code, output = invoke("source", "list", "--sources", str(SOURCES))
    assert code == 0, output
    assert "ncar" in output
    assert "cds" in output


def test_source_list_reports_all_unimplemented(monkeypatch: pytest.MonkeyPatch) -> None:
    """M0 没有任何已实现的源适配器：必须如实标注，不得暗示可用。"""
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    code, output = invoke("source", "list", "--sources", str(SOURCES))
    assert code == 0
    assert "False" in output or "否" in output or "未实现" in output


@pytest.mark.parametrize("command", ["plan", "fetch", "status"])
def test_unimplemented_commands_fail_loudly(command: str) -> None:
    """假命令禁令：未实现功能必须非零退出且输出"未实现"。"""
    code, output = invoke(command)
    assert code != 0, f"{command} 不应以退出码 0“假装成功”"
    assert code == 2, f"{command} 应使用约定的退出码 2，实际 {code}"
    assert "未实现" in output or "not implemented" in output.lower()


def test_unknown_command_fails() -> None:
    code, _ = invoke("definitely-not-a-command")
    assert code != 0
