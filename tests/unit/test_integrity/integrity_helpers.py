"""完整性测试的公共装配：真实文件 + 真实字节，不使用 mock 伪造校验结果。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionTask,
    Checksum,
    FileRecord,
    SourceAsset,
    SourceMetadata,
)

FIXED_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
DEFAULT_URI = "https://example.org/ds633/e5.1/data.nc"


def make_checksum(content: bytes, algorithm: str = "sha256") -> Checksum:
    """按内容计算校验和契约对象。"""
    digest = hashlib.new(algorithm, content).hexdigest()
    return Checksum(value=f"{algorithm}:{digest}")


def make_context(
    path: Path,
    *,
    content_type: str | None = "application/x-netcdf",
    checksum: Checksum | None = None,
    size_bytes: int | None = None,
    remote_etag: str | None = None,
    extensions: dict[str, object] | None = None,
    asset_checksum_matches: bool = True,
    file_name: str = "data.nc",
) -> tuple[FileRecord, AcquisitionTask]:
    """为 ``path`` 构造配套的 ``FileRecord`` 与 ``AcquisitionTask``。

    默认记录值来自真实字节（大小与 SHA-256）；需要构造“声明值与实际不符”的场景时，
    显式传入 ``size_bytes`` / ``checksum`` / ``asset_checksum_matches``。
    """
    data = path.read_bytes() if path.is_file() else b""
    actual_checksum = checksum if checksum is not None else make_checksum(data)
    declared_size = size_bytes if size_bytes is not None else len(data)
    metadata = SourceMetadata(
        source_id="ncar",
        display_name="NSF NCAR RDA",
        provider="NSF NCAR",
        access_route=AccessRoute.S3,
        dataset_id="ds633.0",
        origin_uri=DEFAULT_URI,
        retrieved_at=FIXED_NOW,
    )
    asset = SourceAsset(
        asset_id="asset-0001",
        source_id="ncar",
        dataset_id="era5-pressure-levels",
        access_route=AccessRoute.S3,
        origin_uri=DEFAULT_URI,
        file_name=file_name,
        size_bytes=declared_size,
        checksum=actual_checksum if asset_checksum_matches else None,
        content_type=content_type,
        metadata=metadata,
        discovered_at=FIXED_NOW,
    )
    task = AcquisitionTask(
        task_id="task-0001",
        plan_id="plan-0001",
        request_id="req-era5-clwc-2011-2020",
        asset=asset,
        max_attempts=3,
    )
    record = FileRecord(
        file_id="file-0001",
        task_id="task-0001",
        dataset_id="era5-pressure-levels",
        source_id="ncar",
        access_route=AccessRoute.S3,
        origin_uri=DEFAULT_URI,
        local_path=str(path),
        size_bytes=declared_size,
        checksum=actual_checksum,
        content_type=content_type,
        remote_etag=remote_etag,
        retrieved_at=FIXED_NOW,
        extensions=extensions if extensions is not None else {},
    )
    return record, task


def write_netcdf(path: Path, *, variables: tuple[str, ...] = ("t2m",)) -> Path:
    """用真实 netCDF4 写一个小文件（依赖缺席时跳过当前测试）。"""
    netcdf4 = pytest.importorskip("netCDF4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with netcdf4.Dataset(str(path), "w") as dataset:
        dataset.createDimension("x", 3)
        for name in variables:
            variable = dataset.createVariable(name, "f4", ("x",))
            variable[:] = [1.0, 2.0, 3.0]
        dataset.title = "RDH integrity fixture"
    return path


def write_bytes(path: Path, content: bytes) -> Path:
    """写普通文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
