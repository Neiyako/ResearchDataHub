"""staging/sidecar 的单元测试：路径约定、原子写入、损坏处理。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchdatahub.transfer import (
    PartSidecar,
    part_path_for,
    read_sidecar,
    remove_staged,
    sidecar_path_for,
    write_sidecar,
)

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def make_sidecar(**overrides: object) -> PartSidecar:
    payload: dict[str, object] = {
        "origin_uri": "https://example.test/f.bin",
        "transport_id": "https",
        "task_id": "task-0001",
        "expected_size": 1000,
        "etag": '"v1"',
        "last_modified": NOW,
        "size_written": 400,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return PartSidecar.model_validate(payload)


def test_part_and_sidecar_paths_are_siblings_of_destination(tmp_path: Path) -> None:
    destination = tmp_path / "sub" / "file.nc"
    assert part_path_for(destination) == tmp_path / "sub" / "file.nc.part"
    assert sidecar_path_for(destination) == tmp_path / "sub" / "file.nc.part.json"


def test_sidecar_roundtrip(tmp_path: Path) -> None:
    path = sidecar_path_for(tmp_path / "payload.bin")
    sidecar = make_sidecar()
    write_sidecar(path, sidecar)
    loaded = read_sidecar(path)
    assert loaded == sidecar
    # 原子写入不应残留 .tmp
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_missing_sidecar_returns_none(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path / "nothing.part.json") is None


def test_corrupt_sidecar_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin.part.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt sidecar"):
        read_sidecar(path)


def test_non_object_sidecar_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin.part.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        read_sidecar(path)


def test_sidecar_rejects_unknown_fields_and_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        make_sidecar(unknown_field=1)
    with pytest.raises(ValidationError):
        make_sidecar(updated_at=datetime(2026, 11, 4, 12, 0, 0))


def test_sidecar_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        make_sidecar(size_written=-1)


def test_remove_staged_removes_part_and_sidecar(tmp_path: Path) -> None:
    destination = tmp_path / "payload.bin"
    part = part_path_for(destination)
    sidecar = sidecar_path_for(destination)
    part.write_bytes(b"partial")
    write_sidecar(sidecar, make_sidecar())
    remove_staged(destination)
    assert part.exists() is False
    assert sidecar.exists() is False
    remove_staged(destination)  # 幂等：不存在时无操作
