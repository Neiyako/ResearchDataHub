"""集成测试：安装后的真实可执行文件。

这里刻意**不**用 CliRunner：entry point 能否被 console_scripts 正确发现是安装问题，
CliRunner 覆盖不到。使用子进程调用真实 ``rdh``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RDH = shutil.which("rdh", path=str(REPO / ".venv" / "bin")) or shutil.which("rdh")


def run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CDSAPI_KEY"}
    env.update({"COLUMNS": "240", "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"})
    if env_extra:
        env.update(env_extra)
    assert RDH is not None, "未找到 rdh 可执行文件"
    return subprocess.run(
        [RDH, *args], capture_output=True, text=True, env=env, timeout=60, check=False
    )


@pytest.mark.integration
def test_entry_point_is_installed() -> None:
    """安装后 rdh 必须存在于 PATH 或项目 venv/bin 中。"""
    assert RDH is not None, (
        "未找到 rdh：请先执行 `.venv/bin/pip install -e .`。"
        "若仓库缺少 README.md，hatchling 会拒绝构建（见 docs/M0-verification.md）。"
    )


@pytest.mark.integration
def test_real_process_help() -> None:
    result = run("--help")
    assert result.returncode == 0, result.stderr
    assert "version" in result.stdout


@pytest.mark.integration
def test_real_process_version() -> None:
    result = run("version")
    assert result.returncode == 0, result.stderr
    assert _declared_version() in result.stdout


@pytest.mark.integration
def test_real_process_config_validate_passes() -> None:
    result = run(
        "config",
        "validate",
        "--defaults",
        str(REPO / "configs" / "defaults.yaml"),
        "--sources",
        str(REPO / "configs" / "sources.example.yaml"),
        env_extra={"CDSAPI_KEY": "test-token-not-real"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout.upper()


@pytest.mark.integration
def test_real_process_config_validate_fails_without_secret() -> None:
    result = run(
        "config",
        "validate",
        "--defaults",
        str(REPO / "configs" / "defaults.yaml"),
        "--sources",
        str(REPO / "configs" / "sources.example.yaml"),
    )
    assert result.returncode == 1
    assert "CDSAPI_KEY" in result.stdout + result.stderr


@pytest.mark.integration
def test_real_process_source_list() -> None:
    result = run(
        "source",
        "list",
        "--sources",
        str(REPO / "configs" / "sources.example.yaml"),
        env_extra={"CDSAPI_KEY": "test-token-not-real"},
    )
    assert result.returncode == 0, result.stderr
    assert "ncar" in result.stdout
    assert "cds" in result.stdout


@pytest.mark.integration
def test_real_process_command_group_without_subcommand_is_not_zero() -> None:
    """真实子进程：命令组裸调用必须非零退出（M4 起 ``plan`` 是真命令，不再是占位符）。

    "假成功禁令" 仍然由这条测试守：任何命令在缺少必需输入时都不得以 0 退出。
    """
    result = run("plan")
    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    assert "usage" in combined or "用法" in combined


def _declared_version() -> str:
    """从 ``pyproject.toml`` 读取本仓声明的版本号（避免测试里写死版本，每次发版都要改测试）。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "pyproject.toml 缺少 version 字段"
    return match.group(1)


@pytest.mark.integration
def test_installed_package_imports() -> None:
    """包元数据可读：确认 __version__ 来自已安装的 dist，而不是代码里的回退值。"""
    import importlib.metadata

    import researchdatahub

    expected = _declared_version()
    assert importlib.metadata.version("researchdatahub") == expected
    assert researchdatahub.__version__ == expected
    assert researchdatahub.MODELS_VERSION == "1.0"
