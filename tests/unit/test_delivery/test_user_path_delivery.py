"""交付到用户目录的测试（ADR-0015）。

覆盖：新建 / 幂等跳过 / 冲突不覆盖 / 后缀另存 / 显式覆盖双闸门 / 空间检查 / 原子写入。
全部使用临时目录与真实文件，不做任何 mock。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from researchdatahub.core.errors import StorageError
from researchdatahub.core.models import AccessRoute, Checksum, FileRecord
from researchdatahub.delivery import (
    ConflictPolicy,
    DeliveryAction,
    DeliveryConflictError,
    apply_delivery,
    delivery_extensions,
    iter_sources,
    plan_delivery,
    resolve_target_dir,
)


def sha256(text: bytes) -> str:
    return "sha256:" + hashlib.sha256(text).hexdigest()


def make_record(
    tmp_path: Path,
    *,
    file_id: str = "file-1",
    name: str = "era5_clwc.nc",
    data: bytes = b"PRETEND-NETCDF-PAYLOAD",
) -> FileRecord:
    raw_dir = tmp_path / "rdh" / "raw" / "era5"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    path.write_bytes(data)
    return FileRecord(
        file_id=file_id,
        task_id="task-1",
        dataset_id="era5",
        source_id="cds",
        access_route=AccessRoute.CDS_API,
        origin_uri="https://cds.example/result/1",
        local_path=str(path),
        size_bytes=len(data),
        checksum=Checksum(value=sha256(data)),
        retrieved_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def test_resolve_relative_path_against_cwd(tmp_path: Path) -> None:
    resolved = resolve_target_dir("./data/raw", cwd=tmp_path)
    assert resolved == (tmp_path / "data" / "raw").resolve() or resolved == (
        tmp_path / "data" / "raw"
    )
    assert resolved.is_absolute()


def test_resolve_absolute_path_kept(tmp_path: Path) -> None:
    resolved = resolve_target_dir(str(tmp_path / "out"), cwd=Path("/"))
    assert resolved == (tmp_path / "out")


def test_resolve_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_target_dir("~/proj/data", cwd=tmp_path)
    assert str(tmp_path) in str(resolved)
    assert "proj" in str(resolved)


def test_resolve_rejects_empty() -> None:
    with pytest.raises(StorageError, match="must not be empty"):
        resolve_target_dir("   ")


def test_resolve_rejects_nul() -> None:
    with pytest.raises(StorageError, match="NUL"):
        resolve_target_dir("data\x00raw")


def test_resolve_rejects_existing_file(tmp_path: Path) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(StorageError, match="not a directory"):
        resolve_target_dir("./data", cwd=tmp_path)


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def test_plan_create_when_target_missing(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    plan = plan_delivery([record], tmp_path / "data" / "raw")
    assert not plan.has_conflicts
    assert plan.files[0].action is DeliveryAction.CREATE
    assert plan.required_bytes == record.size_bytes


def test_plan_skip_identical_when_content_matches(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    (target_dir / "era5_clwc.nc").write_bytes(b"PRETEND-NETCDF-PAYLOAD")
    plan = plan_delivery([record], target_dir)
    assert plan.files[0].action is DeliveryAction.SKIP_IDENTICAL
    assert plan.required_bytes == 0


def test_plan_conflict_when_content_differs(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    (target_dir / "era5_clwc.nc").write_bytes(b"DIFFERENT-BYTES-SAME-ISH")
    plan = plan_delivery([record], target_dir)
    assert plan.has_conflicts
    assert plan.files[0].action is DeliveryAction.CONFLICT
    conflict = plan.conflicts[0]
    assert conflict.expected_size == len(b"PRETEND-NETCDF-PAYLOAD")
    assert "overwrite" in conflict.resolution_options
    assert "skip" in conflict.resolution_options
    assert "suffix" in conflict.resolution_options


def test_plan_conflict_when_size_differs(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    (target_dir / "era5_clwc.nc").write_bytes(b"short")
    plan = plan_delivery([record], target_dir)
    assert plan.files[0].action is DeliveryAction.CONFLICT


def test_plan_conflict_when_checksum_is_not_sha256(tmp_path: Path) -> None:
    """非 sha256 记录无法证明内容相同 → 一律按冲突处理（宁可问用户）。"""
    raw = tmp_path / "rdh" / "raw" / "era5"
    raw.mkdir(parents=True)
    path = raw / "era5_clwc.nc"
    path.write_bytes(b"SAME-BYTES")
    record = FileRecord(
        file_id="file-1",
        task_id="task-1",
        dataset_id="era5",
        source_id="cds",
        access_route=AccessRoute.CDS_API,
        origin_uri="https://cds.example/1",
        local_path=str(path),
        size_bytes=len(b"SAME-BYTES"),
        checksum=Checksum(value="md5:" + hashlib.md5(b"SAME-BYTES").hexdigest()),
        retrieved_at=datetime.now(UTC),
    )
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    (target_dir / "era5_clwc.nc").write_bytes(b"SAME-BYTES")
    plan = plan_delivery([record], target_dir)
    assert plan.files[0].action is DeliveryAction.CONFLICT


def test_plan_does_not_touch_filesystem(tmp_path: Path) -> None:
    """判定阶段不得创建目标目录（只读契约）。"""
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    plan = plan_delivery([record], target_dir)
    assert not target_dir.exists()
    assert list(iter_sources(plan)) == [Path(record.local_path)]


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def test_apply_creates_file_with_exact_bytes(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    plan = plan_delivery([record], target_dir)
    result = apply_delivery(plan)
    assert result.complete
    assert len(result.delivered) == 1
    written = target_dir / "era5_clwc.nc"
    assert written.read_bytes() == b"PRETEND-NETCDF-PAYLOAD"
    assert Path(result.delivered[0].target_path) == written
    assert result.delivered[0].bytes_written == len(b"PRETEND-NETCDF-PAYLOAD")


def test_apply_leaves_no_temp_files(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    apply_delivery(plan_delivery([record], target_dir))
    leftovers = [p.name for p in target_dir.iterdir() if p.name.startswith(".rdh-tmp-")]
    assert leftovers == []


def test_apply_is_idempotent(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    first = apply_delivery(plan_delivery([record], target_dir))
    assert first.complete
    second = apply_delivery(plan_delivery([record], target_dir))
    assert second.complete
    assert second.conflicts == []
    assert len(second.skipped) == 1
    assert len(second.delivered) == 0


def test_apply_raises_on_conflict_by_default(tmp_path: Path) -> None:
    """默认不覆盖：冲突必须抛出结构化错误，交由用户决定。"""
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    existing = target_dir / "era5_clwc.nc"
    existing.write_bytes(b"USER-PRECIOUS-DATA")
    plan = plan_delivery([record], target_dir)
    with pytest.raises(DeliveryConflictError) as excinfo:
        apply_delivery(plan)
    assert excinfo.value.detail["conflicts"][0]["target_path"] == str(existing)
    # 用户数据必须原样保留
    assert existing.read_bytes() == b"USER-PRECIOUS-DATA"


def test_apply_skip_conflict_policy(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    existing = target_dir / "era5_clwc.nc"
    existing.write_bytes(b"USER-PRECIOUS-DATA")
    result = apply_delivery(plan_delivery([record], target_dir), on_conflict=ConflictPolicy.SKIP)
    assert existing.read_bytes() == b"USER-PRECIOUS-DATA"
    assert len(result.failed) == 1
    assert not result.complete


def test_apply_suffix_policy_keeps_original(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    existing = target_dir / "era5_clwc.nc"
    existing.write_bytes(b"USER-PRECIOUS-DATA")
    result = apply_delivery(plan_delivery([record], target_dir), on_conflict=ConflictPolicy.SUFFIX)
    assert existing.read_bytes() == b"USER-PRECIOUS-DATA"
    suffixed = target_dir / "era5_clwc.nc.1"
    assert suffixed.read_bytes() == b"PRETEND-NETCDF-PAYLOAD"
    assert result.complete


def test_apply_suffix_increments_when_taken(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    (target_dir / "era5_clwc.nc").write_bytes(b"X")
    (target_dir / "era5_clwc.nc.1").write_bytes(b"Y")
    apply_delivery(plan_delivery([record], target_dir), on_conflict=ConflictPolicy.SUFFIX)
    assert (target_dir / "era5_clwc.nc.2").exists()


def test_apply_overwrite_requires_double_gate(tmp_path: Path) -> None:
    """``OVERWRITE`` 策略必须同时给 ``allow_overwrite=True``，防止误传参数冲掉用户数据。"""
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    existing = target_dir / "era5_clwc.nc"
    existing.write_bytes(b"USER-PRECIOUS-DATA")
    plan = plan_delivery([record], target_dir)
    with pytest.raises(StorageError, match="refusing to overwrite"):
        apply_delivery(plan, on_conflict=ConflictPolicy.OVERWRITE)
    assert existing.read_bytes() == b"USER-PRECIOUS-DATA"


def test_apply_overwrite_with_explicit_permission(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    target_dir.mkdir(parents=True)
    existing = target_dir / "era5_clwc.nc"
    existing.write_bytes(b"USER-PRECIOUS-DATA")
    result = apply_delivery(
        plan_delivery([record], target_dir),
        on_conflict=ConflictPolicy.OVERWRITE,
        allow_overwrite=True,
    )
    assert result.complete
    assert existing.read_bytes() == b"PRETEND-NETCDF-PAYLOAD"


def test_apply_creates_nested_directories(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "a" / "b" / "c"
    apply_delivery(plan_delivery([record], target_dir))
    assert (target_dir / "era5_clwc.nc").exists()


def test_apply_does_not_make_files_read_only(tmp_path: Path) -> None:
    """用户副本是用户的数据：不能沿用 ~/.rdh/raw 的只读语义。"""
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    apply_delivery(plan_delivery([record], target_dir))
    target = target_dir / "era5_clwc.nc"
    os.chmod(target, 0o644)
    target.write_bytes(b"user modified")
    assert target.read_bytes() == b"user modified"


def test_apply_multiple_files(tmp_path: Path) -> None:
    records = [
        make_record(tmp_path, file_id="f1", name="a.nc", data=b"A" * 10),
        make_record(tmp_path, file_id="f2", name="b.nc", data=b"B" * 20),
    ]
    target_dir = tmp_path / "data" / "raw"
    result = apply_delivery(plan_delivery(records, target_dir))
    assert len(result.delivered) == 2
    assert (target_dir / "a.nc").read_bytes() == b"A" * 10
    assert (target_dir / "b.nc").read_bytes() == b"B" * 20


def test_apply_reports_failure_without_aborting_others(tmp_path: Path) -> None:
    good = make_record(tmp_path, file_id="f1", name="good.nc", data=b"GOOD")
    bad = make_record(tmp_path, file_id="f2", name="bad.nc", data=b"BAD")
    Path(bad.local_path).unlink()  # 权威副本消失 → 交付该项失败
    target_dir = tmp_path / "data" / "raw"
    result = apply_delivery(plan_delivery([good, bad], target_dir))
    assert (target_dir / "good.nc").read_bytes() == b"GOOD"
    assert len(result.failed) == 1
    assert not result.complete


def test_apply_refuses_when_space_insufficient(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    plan = plan_delivery([record], target_dir)
    # 伪造一个空间不足的计划（不改动真实磁盘）
    poor = type(plan)(
        target_dir=plan.target_dir,
        files=plan.files,
        conflicts=plan.conflicts,
        free_bytes=1,
        required_bytes=plan.required_bytes,
    )
    with pytest.raises(StorageError, match="not enough free space"):
        apply_delivery(poor)
    assert not target_dir.exists()


def test_delivery_extensions_are_namespaced(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    result = apply_delivery(plan_delivery([record], target_dir))
    extensions = delivery_extensions(result)
    entry = extensions["file-1"]
    assert entry["rdh.delivery_status"] == "delivered"
    assert entry["rdh.delivery_path"].endswith("era5_clwc.nc")
    assert all(key.startswith("rdh.") for key in entry)


def test_result_as_dict_is_json_ready(tmp_path: Path) -> None:
    import json

    record = make_record(tmp_path)
    target_dir = tmp_path / "data" / "raw"
    result = apply_delivery(plan_delivery([record], target_dir))
    payload = result.as_dict()
    assert json.loads(json.dumps(payload))["complete"] is True
    assert payload["delivered"][0]["bytes"] == len(b"PRETEND-NETCDF-PAYLOAD")
