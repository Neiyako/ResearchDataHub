"""staging（``.part``）文件与 sidecar 元数据：断点续传的本地事实来源。

文件约定
--------
* 下载过程中只写 ``<destination>.part``；只有大小校验通过后才 ``os.replace``
  成 ``<destination>``（同目录内 rename 在 POSIX 上是原子的，读者永远看不到半个文件）。
* 每个 ``.part`` 旁边有一个 ``<destination>.part.json`` sidecar，记录远端身份：
  ``origin_uri`` / ``etag`` / ``last_modified`` / ``expected_size``。续传前必须
  把现场探测到的远端身份与 sidecar 逐项比对，**一致才允许续传**。
* sidecar 里的 ``size_written`` 只作为观测信息：进程崩溃时它必然落后于磁盘上
  ``.part`` 的真实大小，因此续传偏移量一律以 ``os.stat(part).st_size`` 为准，
  绝不能用 sidecar 的数字去猜。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "PART_SUFFIX",
    "SIDECAR_SUFFIX",
    "PartSidecar",
    "part_path_for",
    "read_sidecar",
    "remove_staged",
    "sidecar_path_for",
    "write_sidecar",
]

PART_SUFFIX = ".part"
SIDECAR_SUFFIX = ".part.json"


class PartSidecar(BaseModel):
    """``.part`` 文件的旁路元数据；用于证明“这份半成品属于哪个远端对象”。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    origin_uri: str = Field(min_length=1, max_length=4096)
    transport_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    expected_size: int | None = Field(default=None, ge=0)
    etag: str | None = Field(default=None, max_length=256)
    last_modified: datetime | None = None
    size_written: int = Field(default=0, ge=0, description="写入 sidecar 时已知的字节数（观测用）")
    updated_at: datetime

    @field_validator("last_modified", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


def part_path_for(destination: Path) -> Path:
    """``.part`` 路径：``<destination>.part``（与目标同目录，保证 rename 原子）。"""
    return destination.with_name(destination.name + PART_SUFFIX)


def sidecar_path_for(destination: Path) -> Path:
    """Sidecar 路径：``<destination>.part.json``。"""
    return destination.with_name(destination.name + SIDECAR_SUFFIX)


def write_sidecar(path: Path, sidecar: PartSidecar) -> None:
    """原子写入 sidecar（先写临时文件再 ``os.replace``），避免读到半截 JSON。

    Raises
    ------
    OSError
        目录不可写 / 磁盘满；调用方决定是否降级为“不可续传”。
    """
    payload = sidecar.model_dump(mode="json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_sidecar(path: Path) -> PartSidecar | None:
    """读取 sidecar。

    返回 ``None`` 表示文件不存在（从没下过 / 已清理）。

    Raises
    ------
    ValueError
        文件存在但内容损坏或与契约不符——**不能当成“没有 sidecar”静默续传**，
        调用方必须把它当作“身份不可证明”并按策略重下或要求人工介入。
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt sidecar {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"corrupt sidecar {path}: expected a JSON object")
    return PartSidecar.model_validate(data)


def remove_staged(destination: Path) -> None:
    """删除 ``.part`` 与 sidecar（不存在时无操作）；用于成功提交或明确放弃续传。"""
    for path in (part_path_for(destination), sidecar_path_for(destination)):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
