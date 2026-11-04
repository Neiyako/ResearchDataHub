"""本地后端布局、分配路径与磁盘余量（全部使用 tmp_path，绝不触碰真实数据区）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from storage_helpers import make_backend, make_config, write_bytes

from researchdatahub.config.models import StorageConfig
from researchdatahub.core.errors import StorageError
from researchdatahub.storage import LocalStorageBackend


def test_layout_follows_config_subdirectory_names(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        raw_dir="delivered",
        staging_dir="incoming",
        quarantine_dir="bad",
        temp_dir="scratch",
        archive_dir="archive-v1",
    )
    backend = LocalStorageBackend(config)
    assert backend.raw_dir == backend.root / "delivered"
    assert backend.staging_dir == backend.root / "incoming"
    assert backend.quarantine_dir == backend.root / "bad"
    assert backend.temp_dir == backend.root / "scratch"
    assert backend.archive_dir == backend.root / "archive-v1"
    for directory in (
        backend.raw_dir,
        backend.staging_dir,
        backend.quarantine_dir,
        backend.temp_dir,
        backend.archive_dir,
    ):
        assert directory.is_dir()
    # 不得顺带创建默认名字的目录（证明没有硬编码布局）
    assert not (backend.root / "raw").exists()
    assert not (backend.root / "staging").exists()


def test_create_dirs_false_creates_directories_lazily(tmp_path: Path) -> None:
    config = make_config(tmp_path, create_dirs_on_init=False)
    backend = LocalStorageBackend(config)
    assert not backend.raw_dir.exists()
    path = backend.allocate_staging("task-0001", "x.nc")
    assert path.parent.is_dir()
    assert backend.raw_dir.exists() is False


def test_root_exists_as_file_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root-is-a-file"
    root.write_text("not a directory")
    config = make_config(tmp_path, root_name="root-is-a-file")
    with pytest.raises(StorageError) as excinfo:
        LocalStorageBackend(config)
    assert "not a directory" in excinfo.value.message


@pytest.mark.parametrize("bad_dir", ["../outside", "sub/../../outside"])
def test_subdirectory_escaping_root_rejected(tmp_path: Path, bad_dir: str) -> None:
    config = make_config(tmp_path, raw_dir=bad_dir)
    with pytest.raises(StorageError) as excinfo:
        LocalStorageBackend(config)
    assert "escapes storage.root" in excinfo.value.message


def test_overlapping_subdirectories_after_normalisation_rejected(tmp_path: Path) -> None:
    # "data" 与 "data/../data" 字符串不同，但解析后是同一目录：必须在运行时拒绝。
    config = make_config(tmp_path, raw_dir="data", staging_dir="data/../data")
    with pytest.raises(StorageError) as excinfo:
        LocalStorageBackend(config)
    assert "overlap" in excinfo.value.message


def test_free_bytes_is_positive_and_floor_enforced(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    assert backend.free_bytes() > 0
    backend.ensure_free_space(0)
    strict = LocalStorageBackend(
        StorageConfig.model_validate(
            {"root": str(tmp_path / "strict"), "free_space_floor_gb": 1_000_000.0}
        )
    )
    assert strict.has_free_space(0) is False
    with pytest.raises(StorageError) as excinfo:
        strict.ensure_free_space(0)
    assert excinfo.value.detail["floor_bytes"] > 0


def test_allocate_temp_is_unique_and_parent_exists(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    first = backend.allocate_temp("task-0001")
    second = backend.allocate_temp("task-0001")
    assert first != second
    assert first.parent.is_dir()
    assert first.name.endswith(".part")


def test_allocate_temp_with_explicit_file_name(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    path = backend.allocate_temp("task-0001", file_name="downloaded.nc")
    assert path.name == "downloaded.nc"
    assert path.parent == backend.temp_dir / "task-0001"


def test_allocate_staging_is_resume_friendly(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    first = backend.allocate_staging("task-0001", "resume.nc")
    first.write_bytes(b"partial-bytes")
    second = backend.allocate_staging("task-0001", "resume.nc")
    assert first == second
    assert second.read_bytes() == b"partial-bytes"


def test_expected_original_path_does_not_touch_disk(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    expected = backend.expected_original_path(dataset_id="era5", file_name="a.nc")
    assert expected == backend.raw_dir / "era5" / "a.nc"
    assert not expected.exists()
    assert not expected.parent.exists()


def test_read_only_protection_can_be_disabled(tmp_path: Path) -> None:
    backend = LocalStorageBackend(make_config(tmp_path), read_only=False)
    source = write_bytes(backend.allocate_staging("task-0001", "plain.nc"), b"bytes")
    result = backend.commit_staging(source, dataset_id="era5", file_name="plain.nc")
    mode = result.path.stat().st_mode & 0o777
    assert mode & 0o200, "read_only=False must keep the owner write bit"


def test_implements_storage_backend_protocol(tmp_path: Path) -> None:
    from researchdatahub.core.ports import StorageBackend, assert_implements

    backend = make_backend(tmp_path)
    assert_implements(backend, StorageBackend)
