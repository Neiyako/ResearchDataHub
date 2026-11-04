"""报告测试的公共装配：真实文件 + 契约模型工厂。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from researchdatahub.core.models import (
    AccessRoute,
    AcquisitionTask,
    Checksum,
    DatasetMetadata,
    FileRecord,
    IntegrityCheckKind,
    IntegrityReport,
    IntegrityStatus,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.core.state import TaskState

FIXED_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
URI = "https://example.org/ds633/e5.1/data.nc"


def make_dataset_metadata() -> DatasetMetadata:
    """从仓库测试夹具构造数据集元数据。"""
    payload = json.loads((FIXTURES / "era5_request.json").read_text(encoding="utf-8"))
    return DatasetMetadata.model_validate(payload["metadata"])


def make_source(source_id: str = "ncar") -> SourceMetadata:
    """构造数据源元数据。"""
    return SourceMetadata(
        source_id=source_id,
        display_name="NSF NCAR RDA",
        provider="NSF NCAR",
        access_route=AccessRoute.S3,
        dataset_id="ds633.0",
        origin_uri=URI,
        retrieved_at=FIXED_NOW,
    )


def make_task(
    task_id: str = "task-0001",
    state: TaskState = TaskState.COMPLETED,
    *,
    last_error: str | None = None,
    source_id: str = "ncar",
) -> AcquisitionTask:
    """构造指定状态的采集任务。"""
    source = make_source(source_id)
    asset = SourceAsset(
        asset_id=f"asset-{task_id}",
        source_id=source_id,
        dataset_id="era5-pressure-levels",
        access_route=AccessRoute.S3,
        origin_uri=URI,
        file_name="data.nc",
        size_bytes=100,
        metadata=source,
        discovered_at=FIXED_NOW,
    )
    return AcquisitionTask(
        task_id=task_id,
        plan_id="plan-0001",
        request_id="req-era5-clwc-2011-2020",
        asset=asset,
        state=state,
        max_attempts=3,
        last_error=last_error,
    )


def write_data(path: Path, content: bytes = b"payload") -> Path:
    """写入文件（自动建父目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_record(
    path: Path,
    *,
    file_id: str = "file-0001",
    task_id: str = "task-0001",
    content: bytes | None = None,
    size_bytes: int | None = None,
    checksum: Checksum | None = None,
    source_id: str = "ncar",
) -> FileRecord:
    """构造文件记录；默认大小/校验和来自真实文件。"""
    data = content if content is not None else path.read_bytes()
    declared_size = size_bytes if size_bytes is not None else len(data)
    digest = (
        checksum
        if checksum is not None
        else Checksum(value="sha256:" + hashlib.sha256(data).hexdigest())
    )
    return FileRecord(
        file_id=file_id,
        task_id=task_id,
        dataset_id="era5-pressure-levels",
        source_id=source_id,
        access_route=AccessRoute.S3,
        origin_uri=URI,
        local_path=str(path),
        size_bytes=declared_size,
        checksum=digest,
        retrieved_at=FIXED_NOW,
    )


def make_report(
    *,
    file_id: str | None = "file-0001",
    task_id: str = "task-0001",
    status: IntegrityStatus = IntegrityStatus.PASSED,
    failures: list[str] | None = None,
    report_id: str = "report-0001",
    checked_at: datetime = FIXED_NOW,
    checks: list[IntegrityCheckKind] | None = None,
) -> IntegrityReport:
    """构造完整性报告（默认一份通过报告，带一条 checksum 检查证据）。"""
    if failures is None:
        failures = [] if status is not IntegrityStatus.FAILED else ["checksum_match: mismatch"]
    return IntegrityReport(
        report_id=report_id,
        task_id=task_id,
        file_id=file_id,
        status=status,
        checks=checks if checks is not None else [IntegrityCheckKind.CHECKSUM_MATCH],
        failures=failures,
        expected={"size_bytes": 7},
        observed={
            "checks": [
                {
                    "kind": "checksum_match",
                    "level": "checksum",
                    "status": "passed" if status is IntegrityStatus.PASSED else "failed",
                    "expected": "sha256:00",
                    "actual": "sha256:00",
                    "detail": "checksum checksum matches",
                }
            ]
        },
        checked_at=checked_at,
    )
