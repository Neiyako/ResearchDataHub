"""共享测试装配：样例对象构造器与 fake 实现。

原则：fixture 数据来自 ``tests/fixtures/`` 的 JSON/YAML 文件，不在测试里内联巨型字典，
保证测试数据与 schema 解耦、可被文档与手工实验复用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionTask,
    Checksum,
    DatasetMetadata,
    DatasetRequest,
    FileRecord,
    IntegrityReport,
    IntegrityStatus,
    SourceAsset,
    SourceMetadata,
    TransferResult,
    TransferStatus,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def load_json(name: str) -> dict[str, Any]:
    """读取 ``tests/fixtures`` 下的 JSON 样例。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 数据构造器（显式、可覆盖，不使用 MagicMock 伪造数据）
# --------------------------------------------------------------------------- #
def make_metadata(**overrides: Any) -> DatasetMetadata:
    """根据 fixture 构造 :class:`DatasetMetadata`，支持字段覆盖。"""
    payload = dict(load_json("era5_request.json")["metadata"])
    payload.update(overrides)
    return DatasetMetadata.model_validate(payload)


def make_request(**overrides: Any) -> DatasetRequest:
    """根据 fixture 构造 :class:`DatasetRequest`。"""
    payload = load_json("era5_request.json")
    payload.update(overrides)
    return DatasetRequest.model_validate(payload)


def make_source_metadata(**overrides: Any) -> SourceMetadata:
    """构造数据源元数据。"""
    payload = dict(load_json("source_metadata.json"))
    payload.update(overrides)
    return SourceMetadata.model_validate(payload)


def make_asset(**overrides: Any) -> SourceAsset:
    """构造远端资产。"""
    meta = overrides.pop("metadata", None) or make_source_metadata()
    payload: dict[str, Any] = {
        "asset_id": "asset-era5-2011-01",
        "source_id": meta.source_id,
        "dataset_id": "era5-pressure-levels",
        "access_route": AccessRoute.S3,
        "origin_uri": meta.origin_uri,
        "file_name": "e5.1-pressure-2011-01.nc",
        "size_bytes": 536870912,
        "checksum": {"value": "sha256:" + "a" * 64},
        "remote_etag": "etag-2011-01",
        "remote_last_modified": datetime(2026, 10, 1, tzinfo=UTC),
        "content_type": "application/x-netcdf",
        "metadata": meta,
        "discovered_at": FIXED_NOW,
    }
    payload.update(overrides)
    return SourceAsset.model_validate(payload)


def make_task(**overrides: Any) -> AcquisitionTask:
    """构造采集任务（默认 PENDING）。"""
    asset = overrides.pop("asset", None) or make_asset()
    payload: dict[str, Any] = {
        "task_id": "task-0001",
        "plan_id": "plan-0001",
        "request_id": "req-era5-clwc-2011-2020",
        "asset": asset,
        "max_attempts": 3,
    }
    payload.update(overrides)
    return AcquisitionTask.model_validate(payload)


def make_file_record(**overrides: Any) -> FileRecord:
    """构造本地文件记录。"""
    payload: dict[str, Any] = {
        "file_id": "file-0001",
        "task_id": "task-0001",
        "dataset_id": "era5-pressure-levels",
        "source_id": "ncar",
        "access_route": AccessRoute.S3,
        "origin_uri": "s3://rda-data/ds633.0/e5.1/pressure/2011/e5.1-pressure-2011-01.nc",
        "local_path": "/data/raw/era5/e5.1-pressure-2011-01.nc",
        "size_bytes": 536870912,
        "checksum": {"value": "sha256:" + "b" * 64},
        "retrieved_at": FIXED_NOW,
    }
    payload.update(overrides)
    return FileRecord.model_validate(payload)


def make_transfer_result(**overrides: Any) -> TransferResult:
    """构造传输结果。"""
    payload: dict[str, Any] = {
        "task_id": "task-0001",
        "status": TransferStatus.SUCCEEDED,
        "transport_id": "s3",
        "bytes_transferred": 536870912,
        "duration_seconds": 42.5,
        "destination_path": "/tmp/rdh/task-0001/e5.1-pressure-2011-01.nc.part",
        "checksum": {"value": "sha256:" + "b" * 64},
        "completed_at": FIXED_NOW,
    }
    payload.update(overrides)
    return TransferResult.model_validate(payload)


def make_integrity_report(**overrides: Any) -> IntegrityReport:
    """构造校验报告。"""
    payload: dict[str, Any] = {
        "report_id": "report-0001",
        "task_id": "task-0001",
        "file_id": "file-0001",
        "status": IntegrityStatus.PASSED,
        "checks": ["size_match", "checksum_match"],
        "failures": [],
        "expected": {"size_bytes": 536870912},
        "observed": {"size_bytes": 536870912},
        "checked_at": FIXED_NOW,
    }
    payload.update(overrides)
    return IntegrityReport.model_validate(payload)


# --------------------------------------------------------------------------- #
# pytest fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def now() -> datetime:
    """固定的“当前时间”，避免测试依赖真实时钟。"""
    return FIXED_NOW


@pytest.fixture
def metadata() -> DatasetMetadata:
    return make_metadata()


@pytest.fixture
def request_() -> DatasetRequest:
    return make_request()


@pytest.fixture
def asset() -> SourceAsset:
    return make_asset()


@pytest.fixture
def task() -> AcquisitionTask:
    return make_task()


@pytest.fixture
def file_record() -> FileRecord:
    return make_file_record()


@pytest.fixture
def checksum() -> Checksum:
    return Checksum(value="sha256:" + "c" * 64)
