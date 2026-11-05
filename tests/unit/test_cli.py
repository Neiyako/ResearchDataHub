"""CLI 测试：帮助、版本、配置校验退出码、数据源列表、未实现命令必须失败。"""

from __future__ import annotations

import re
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


def test_help_lists_real_command_groups() -> None:
    """M1–M5 的命令组必须都在帮助里可见：它们是真实能力，不是占位符。"""
    code, output = invoke("--help")
    assert code == 0
    for group in ("db", "request", "plan", "task", "file", "report"):
        assert group in output, f"帮助里缺少命令组 {group}"
    assert "未实现" not in output


@pytest.mark.parametrize(
    "command",
    ["version", "config", "source", "db", "request", "plan", "task", "file", "report"],
)
def test_subcommand_help_works(command: str) -> None:
    code, output = invoke(command, "--help")
    assert code == 0, output
    assert "Usage" in output or "用法" in output


def test_version_exits_zero_and_prints_versions() -> None:
    code, output = invoke("version")
    assert code == 0
    assert "researchdatahub" in output
    # 版本号从 pyproject 读取，避免每次发版都改测试（也避免读到过期硬编码值）
    from pathlib import Path as _Path

    _pyproject = _Path(__file__).resolve().parents[2] / "pyproject.toml"
    _declared = re.search(
        r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert _declared is not None
    assert _declared.group(1) in output
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


def test_source_list_reports_implementation_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """``implemented`` 列必须反映配置事实（M3 之后 cds/ncar 为 True，s3/globus/zarr 仍 False）。"""
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    code, output = invoke("source", "list", "--sources", str(SOURCES))
    assert code == 0
    assert "True" in output
    assert "可用传输路径" in output


@pytest.mark.parametrize("command", ["plan", "request", "task"])
def test_command_groups_require_subcommand(command: str) -> None:
    """命令组裸调用必须是非零退出并给出用法，不能"静默成功"。"""
    code, output = invoke(command)
    assert code != 0, f"{command} 不应以退出码 0 结束"
    assert "Usage" in output or "用法" in output


def test_missing_required_argument_fails() -> None:
    """缺必需参数必须失败（而不是默认执行某个危险动作）。"""
    code, output = invoke("plan", "create")
    assert code != 0
    assert "Missing argument" in output or "缺少" in output


def test_unknown_command_fails() -> None:
    code, _ = invoke("definitely-not-a-command")
    assert code != 0
