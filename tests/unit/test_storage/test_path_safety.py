"""路径穿越与文件名校验规则（storage/naming.py 的显式规则逐条验证）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from storage_helpers import make_backend, write_staging

from researchdatahub.core.errors import StorageError
from researchdatahub.storage import sanitize_file_name, validate_component


@pytest.mark.parametrize("value", ["task-0001", "plan.1", "asset_2", "a1", "x" * 128])
def test_validate_component_accepts_identifiers(value: str) -> None:
    assert validate_component(value, field="task_id") == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../x",
        "a/b",
        "a\\b",
        "/abs",
        "A-UPPER",
        "with space",
        "ctrl\x00char",
        "x" * 129,
        " leading",
    ],
)
def test_validate_component_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(StorageError) as excinfo:
        validate_component(value, field="task_id")
    assert excinfo.value.detail["field"] == "task_id"


@pytest.mark.parametrize(
    "value",
    [
        "e5.1-pressure-2011-01.nc",
        "a b.nc",
        "数据_2026.nc",
        "x+y,z(1).nc",
        "file-01.nc",
    ],
)
def test_sanitize_file_name_accepts_scientific_names(value: str) -> None:
    assert sanitize_file_name(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../evil.nc",
        "/etc/passwd",
        "a/b.nc",
        "a\\b.nc",
        "\x00.nc",
        "a\nb.nc",
        "a:b.nc",
        "a;b.nc",
        "a'b.nc",
        "a$b.nc",
        ".hidden.nc",
        "x.commit.json",
        ".rdh-partial-x.nc",
        "a" * 201,
    ],
)
def test_sanitize_file_name_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(StorageError) as excinfo:
        sanitize_file_name(value)
    assert excinfo.value.detail["field"] == "file_name"


def test_sanitize_file_name_normalises_nfc_and_strips() -> None:
    composed = "caf\u00e9.nc"
    decomposed = "cafe\u0301.nc"
    assert sanitize_file_name(composed) == sanitize_file_name(decomposed)
    assert sanitize_file_name("  padded.nc  ") == "padded.nc"


def test_allocate_staging_rejects_traversal_task_id(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    with pytest.raises(StorageError):
        backend.allocate_staging("../escape", "ok.nc")


def test_allocate_staging_rejects_traversal_file_name(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    with pytest.raises(StorageError):
        backend.allocate_staging("task-0001", "../../escape.nc")


def test_publish_rejects_traversal_dataset_id(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "ok.nc", b"payload")
    with pytest.raises(StorageError):
        backend.commit_staging(source, dataset_id="../../etc", file_name="ok.nc")
    assert not (tmp_path / "etc").exists()


def test_publish_rejects_traversal_file_name(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "ok.nc", b"payload")
    with pytest.raises(StorageError):
        backend.commit_staging(source, dataset_id="era5", file_name="../../etc/passwd")


def test_publish_rejects_source_outside_managed_dirs(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"payload")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(outside, dataset_id="era5", file_name="outside.bin")
    assert "staging_dir or temp_dir" in excinfo.value.message


def test_publish_rejects_symlink_source(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"payload")
    link = backend.allocate_staging("task-0001", "link.nc")
    link.symlink_to(outside)
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(link, dataset_id="era5", file_name="link.nc")
    assert "symlink" in excinfo.value.message


def test_publish_rejects_symlink_raw_target(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    target = backend.raw_dir / "era5" / "data.nc"
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-original")
    target.symlink_to(outside)
    source = write_staging(backend, "task-0001", "data.nc", b"new-content")
    with pytest.raises(StorageError) as excinfo:
        backend.commit_staging(source, dataset_id="era5", file_name="data.nc")
    assert "symlink" in excinfo.value.message
    assert outside.read_bytes() == b"outside-original"
    assert source.read_bytes() == b"new-content"


def test_archive_rejects_unsafe_name(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = write_staging(backend, "task-0001", "ok.bin", b"payload")
    with pytest.raises(StorageError):
        backend.archive(source, archive_name="../escape.bin")
