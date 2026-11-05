"""版本号一致性：``pyproject.toml`` 是唯一来源。

任何地方出现"三处版本号各自为政"的情况都会在发布时变成事故（dist 是 0.2.0、
CLI 打印 0.1.0、CITATION 又是另一个数）。本测试把这一致性钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _declared() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_package_allowlist_version_matches_pyproject() -> None:
    """``researchdatahub/__init__.py`` 里的回退版本必须与 pyproject 一致。"""
    init = (REPO / "src" / "researchdatahub" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{_declared()}"' in init


def test_citation_version_matches_pyproject() -> None:
    """``CITATION.cff`` 的 version 必须与 pyproject 一致。"""
    citation = (REPO / "CITATION.cff").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", citation, re.MULTILINE)
    assert match is not None, "CITATION.cff 缺少 version 字段"
    assert match.group(1) == _declared()


def test_changelog_has_entry_for_current_version() -> None:
    """CHANGELOG 必须有一条与当前版本对应的记录（发版不该忘记写变更）。"""
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{_declared()}]" in changelog
