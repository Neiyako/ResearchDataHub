"""``DiskSpaceChecker`` 的测试：可写性、剩余空间、不创建目录。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from researchdatahub.environment.models import CheckKind, RequirementStatus
from researchdatahub.environment.storage import (
    DEFAULT_FREE_SPACE_FLOOR_GB,
    DiskSpaceChecker,
)


def test_existing_writable_dir_is_present(tmp_path: Path) -> None:
    result = DiskSpaceChecker(tmp_path).check()
    assert result.status is RequirementStatus.PRESENT
    assert result.kind is CheckKind.STORAGE
    assert result.required is True
    assert result.observed["writable"] is True
    assert result.observed["exists"] is True
    assert float(result.observed["free_gb"]) > 0  # type: ignore[arg-type]


def test_probe_leaves_no_files_behind(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    DiskSpaceChecker(tmp_path).check()
    assert set(tmp_path.iterdir()) == before


def test_missing_directory_is_missing_and_not_created(tmp_path: Path) -> None:
    target = tmp_path / "not-yet" / "rdh"
    result = DiskSpaceChecker(target).check()
    assert result.status is RequirementStatus.MISSING
    assert result.observed["exists"] is False
    assert not target.exists(), "环境检查不得创建目录（创建属于初始化动作）"
    assert "mkdir -p" in result.guidance


def test_path_is_a_file_not_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "iamafile"
    file_path.write_text("x")
    result = DiskSpaceChecker(file_path).check()
    assert result.status is RequirementStatus.MISSING
    assert result.observed["is_dir"] is False
    assert "不是目录" in result.guidance


@pytest.mark.skipif(os.geteuid() == 0, reason="root 无视目录权限")
def test_readonly_directory_is_missing(tmp_path: Path) -> None:
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = DiskSpaceChecker(readonly).check()
    finally:
        readonly.chmod(stat.S_IRWXU)
    assert result.status is RequirementStatus.MISSING
    assert result.observed["writable"] is False
    assert "不可写" in result.guidance


def test_free_space_floor_exceeded(tmp_path: Path) -> None:
    result = DiskSpaceChecker(tmp_path, free_space_floor_gb=10**9).check()
    assert result.status is RequirementStatus.MISSING
    assert "低于下限" in result.guidance


def test_tilde_expansion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "home-rdh"
    target.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(target.parent)))
    checker = DiskSpaceChecker(f"~/{target.name}")
    assert checker.root == target
    assert checker.check().status is RequirementStatus.PRESENT


def test_probe_write_disabled(tmp_path: Path) -> None:
    result = DiskSpaceChecker(tmp_path, probe_write=False).check()
    assert result.status is RequirementStatus.PRESENT
    assert result.observed["writable"] is True


def test_negative_floor_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="free_space_floor_gb"):
        DiskSpaceChecker(tmp_path, free_space_floor_gb=-1.0)


def test_default_floor_matches_config_default() -> None:
    """与 ``config.models.StorageConfig.free_space_floor_gb`` 默认值保持一致。"""
    from researchdatahub.config.models import StorageConfig

    assert StorageConfig().free_space_floor_gb == DEFAULT_FREE_SPACE_FLOOR_GB


def test_unreadable_directory_is_unknown_or_missing(tmp_path: Path) -> None:
    """目录存在但 stat 失败时不得崩溃（返回 missing/unknown 并给出原因）。"""
    target = tmp_path / "vanishing"
    target.mkdir()
    checker = DiskSpaceChecker(target)
    target.rmdir()  # 让 exists() 返回 False，模拟路径消失
    result = checker.check()
    assert result.status in (RequirementStatus.MISSING, RequirementStatus.UNKNOWN)
