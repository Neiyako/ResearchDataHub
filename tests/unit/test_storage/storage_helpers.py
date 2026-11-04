"""存储测试的公共装配（真实文件、真实目录，不使用 mock 替代文件系统语义）。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from researchdatahub.config.models import StorageConfig
from researchdatahub.core.models import Checksum
from researchdatahub.storage import LocalStorageBackend


def make_config(tmp_path: Path, *, root_name: str = "store", **overrides: object) -> StorageConfig:
    """在 ``tmp_path`` 下构造一份隔离的存储配置（永不触碰真实 ``~/.rdh``）。"""
    payload: dict[str, object] = {
        "root": str(tmp_path / root_name),
        "free_space_floor_gb": 0.0,
    }
    payload.update(overrides)
    return StorageConfig.model_validate(payload)


def make_backend(tmp_path: Path, *, read_only: bool = True) -> LocalStorageBackend:
    """构造一个根目录位于 ``tmp_path/store`` 的后端。"""
    return LocalStorageBackend(make_config(tmp_path), read_only=read_only)


def write_bytes(path: Path, content: bytes) -> Path:
    """写入文件（自动建父目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def write_staging(backend: LocalStorageBackend, task_id: str, name: str, content: bytes) -> Path:
    """在 staging 区写一个待提交文件。"""
    path = backend.allocate_staging(task_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tree_files(directory: Path) -> list[Path]:
    """递归列出目录下的普通文件（排序，便于断言）。"""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*") if path.is_file())


def assert_no_temporary_files(directory: Path) -> None:
    """断言目录里没有跨文件系统复制遗留的临时文件。"""
    leftovers: list[Path] = []
    if directory.is_dir():
        for path in directory.rglob("*"):
            if path.is_file() and (
                path.name.startswith(".rdh-partial-") or path.name.startswith(".rdh-journal-")
            ):
                leftovers.append(path)
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def names(paths: Iterable[Path]) -> list[str]:
    """路径列表 → 文件名列表（便于断言）。"""
    return [path.name for path in paths]


def sha256_hex(content: bytes) -> str:
    """内容 → SHA-256 十六进制（测试断言用）。"""
    return hashlib.sha256(content).hexdigest()


def make_checksum(content: bytes) -> Checksum:
    """内容 → ``Checksum`` 契约对象。"""
    return Checksum(value=f"sha256:{sha256_hex(content)}")


def install_exdev(monkeypatch, callback=None) -> list[tuple[str, str]]:
    """让**跨目录**的 ``os.replace`` 抛出 EXDEV，模拟跨文件系统提交。

    同目录的 rename（journal 更新、``.rdh-partial-*`` → 目标）仍调用真实实现；
    ``callback(source, target)`` 在抛出前执行，用于制造“复制阶段失败/清理失败”等场景。
    返回跨目录调用的记录列表，供断言“确实走了跨设备分支”。
    """
    import errno
    import os

    real_replace = os.replace
    cross_calls: list[tuple[str, str]] = []

    def fake_replace(source, target):  # type: ignore[no-untyped-def]
        source_path = Path(source)
        target_path = Path(target)
        if source_path.parent != target_path.parent and not source_path.name.startswith(
            ".rdh-partial-"
        ):
            cross_calls.append((str(source_path), str(target_path)))
            if callback is not None:
                callback(source_path, target_path)
            raise OSError(errno.EXDEV, "simulated cross-device link")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fake_replace)
    return cross_calls
