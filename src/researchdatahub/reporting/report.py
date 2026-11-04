"""采集报告：把请求、任务、文件、校验结果汇总成人类可读 + 机器可读的报告。

报告必须回答的问题
------------------
* 请求了多少、发现了多少资产、成功/失败/跳过/隔离各多少（**不得只报成功数**）；
* 总下载量（以 ``FileRecord.size_bytes`` 为事实来源，不以“尝试字节数”冒充）；
* 完整性校验通过/失败/跳过/未知各多少；
* 失败原因（按错误码聚合）与失败任务、隔离文件、文件落点、数据源；
* 交付清单路径（如果本次采集生成了 ``DeliveryManifest``）。

输出形态
--------
* :meth:`CollectionReport.to_json_dict`：稳定 JSON（写文件用
  :func:`write_collection_report`）；
* :func:`render_collection_report`：用 ``rich`` 渲成终端表格字符串（测试可直接断言内容）。
"""

from __future__ import annotations

import io
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rich import box
from rich.console import Console
from rich.table import Table

from researchdatahub import __version__
from researchdatahub.core.errors import ModelValidationError
from researchdatahub.core.models import (
    AcquisitionTask,
    FileRecord,
    IntegrityReport,
    IntegrityStatus,
    JsonValue,
    SourceMetadata,
)
from researchdatahub.core.state import TaskState
from researchdatahub.reporting.delivery import latest_reports_by_file

__all__ = [
    "CollectionReport",
    "FailureRecord",
    "FileSummary",
    "IntegrityTally",
    "QuarantinedRecord",
    "SourceSummary",
    "build_collection_report",
    "format_bytes",
    "render_collection_report",
    "write_collection_report",
]

_BYTES_UNITS: Final[tuple[str, ...]] = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


@dataclass(frozen=True)
class FailureRecord:
    """一个失败任务的结构化原因。"""

    task_id: str
    code: str
    message: str
    source_id: str | None = None

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "task_id": self.task_id,
            "source_id": self.source_id,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class QuarantinedRecord:
    """一个被隔离的文件；隔离保留原因与位置，文件不删除。"""

    task_id: str
    reason: str
    path: str
    file_id: str | None = None
    source_id: str | None = None
    size_bytes: int = 0

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "task_id": self.task_id,
            "file_id": self.file_id,
            "source_id": self.source_id,
            "reason": self.reason,
            "path": self.path,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class FileSummary:
    """报告里的单文件摘要（含最新校验状态）。"""

    file_id: str
    task_id: str
    source_id: str
    path: str
    size_bytes: int
    checksum: str
    integrity_status: IntegrityStatus | None

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "file_id": self.file_id,
            "task_id": self.task_id,
            "source_id": self.source_id,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "integrity_status": (
                self.integrity_status.value if self.integrity_status is not None else "missing"
            ),
        }


@dataclass(frozen=True)
class SourceSummary:
    """数据源摘要。"""

    source_id: str
    display_name: str
    provider: str
    access_route: str

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "provider": self.provider,
            "access_route": self.access_route,
        }


@dataclass(frozen=True)
class IntegrityTally:
    """完整性结论分布（按每个文件的最新报告统计）。"""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    unknown: int = 0
    missing: int = 0

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "unknown": self.unknown,
            "missing": self.missing,
        }


@dataclass(frozen=True)
class CollectionReport:
    """一次采集的完整报告快照（不可变；所有数字都是构造时的实测值）。"""

    request_id: str
    plan_id: str
    generated_at: datetime
    tool_version: str
    request_count: int = 1
    discovered_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    skipped_tasks: int = 0
    quarantined_tasks: int = 0
    unfinished_tasks: int = 0
    total_bytes: int = 0
    integrity: IntegrityTally = IntegrityTally()
    failures: tuple[FailureRecord, ...] = ()
    quarantined: tuple[QuarantinedRecord, ...] = ()
    files: tuple[FileSummary, ...] = ()
    sources: tuple[SourceSummary, ...] = ()
    dataset_id: str | None = None
    delivery_manifest_path: str | None = None
    warnings: tuple[str, ...] = ()

    def failure_reasons(self) -> tuple[tuple[str, int], ...]:
        """按错误码聚合失败原因，按次数降序、错误码升序排列。"""
        counts: dict[str, int] = {}
        for failure in self.failures:
            counts[failure.code] = counts.get(failure.code, 0) + 1
        return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def as_json(self) -> dict[str, JsonValue]:
        """导出为稳定 JSON 字典。"""
        return {
            "schema_version": "1.0",
            "generated_at": self.generated_at.isoformat(),
            "tool_version": self.tool_version,
            "request_id": self.request_id,
            "plan_id": self.plan_id,
            "dataset_id": self.dataset_id,
            "counts": {
                "requests": self.request_count,
                "discovered": self.discovered_tasks,
                "completed": self.completed_tasks,
                "failed": self.failed_tasks,
                "skipped": self.skipped_tasks,
                "quarantined": self.quarantined_tasks,
                "unfinished": self.unfinished_tasks,
            },
            "total_bytes": self.total_bytes,
            "integrity": self.integrity.as_json(),
            "failure_reasons": [
                {"code": code, "count": count} for code, count in self.failure_reasons()
            ],
            "failures": [failure.as_json() for failure in self.failures],
            "quarantined": [item.as_json() for item in self.quarantined],
            "files": [item.as_json() for item in self.files],
            "sources": [item.as_json() for item in self.sources],
            "delivery_manifest_path": self.delivery_manifest_path,
            "warnings": list(self.warnings),
        }


def format_bytes(value: int) -> str:
    """把字节数格式化为人读形式（``12.0 KiB (12288 B)`` 这类风格）。"""
    if value < 0:
        raise ValueError("byte count must be non-negative")
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    unit = _BYTES_UNITS[0]
    for candidate in _BYTES_UNITS[1:]:
        scaled /= 1024.0
        unit = candidate
        if scaled < 1024.0:
            break
    return f"{scaled:.1f} {unit} ({value} B)"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _display_path(path: str, base_dir: Path | None) -> str:
    candidate = Path(path)
    if base_dir is None:
        return path
    if candidate.is_absolute():
        try:
            return Path(os.path.relpath(candidate, base_dir)).as_posix()
        except ValueError:  # pragma: no cover - 仅 Windows 跨盘符
            return candidate.name
    return candidate.as_posix()


def build_collection_report(
    *,
    request_id: str,
    plan_id: str,
    tasks: Sequence[AcquisitionTask],
    files: Sequence[FileRecord],
    integrity_reports: Sequence[IntegrityReport],
    request_count: int = 1,
    failures: Sequence[FailureRecord] = (),
    quarantined: Sequence[QuarantinedRecord] = (),
    sources: Sequence[SourceMetadata] = (),
    dataset_id: str | None = None,
    delivery_manifest_path: Path | str | None = None,
    base_dir: Path | None = None,
    generated_at: datetime | None = None,
    tool_version: str | None = None,
) -> CollectionReport:
    """从任务/文件/报告计算采集报告。

    Parameters
    ----------
    request_id / plan_id:
        本次采集的请求与计划标识。
    tasks:
        计划内的全部任务（用于统计发现数、各状态数量与未完成项）。
    files:
        已落盘文件记录（总下载量与文件落点的事实来源）。
    integrity_reports:
        完整性报告；每个文件按最新一份报告计入统计。
    request_count:
        本次报告覆盖的请求数量（单次 CLI 调用通常为 1）。
    failures:
        调用方已知的失败记录；``last_error`` 非空的失败任务会被自动补充。
    quarantined:
        调用方已知的隔离记录；``QUARANTINED`` 状态任务会被自动补充。
    sources:
        本次涉及的数据源。
    base_dir:
        给定时把文件路径转成相对路径（报告更易携带）；不会修改文件记录本身。
    generated_at / tool_version:
        可注入的时间与工具版本，便于测试与复现。

    Returns
    -------
    CollectionReport
        所有计数都来自输入序列本身；不做“猜测补全”。

    Raises
    ------
    ModelValidationError
        ``request_id`` / ``plan_id`` 为空，或路径无法读取导致统计失败。
    """
    if request_id == "" or plan_id == "":
        raise ModelValidationError(
            "collection report requires request_id and plan_id",
            detail={"request_id": request_id, "plan_id": plan_id},
        )
    if request_count < 1:
        raise ModelValidationError(
            "request_count must be >= 1", detail={"request_count": request_count}
        )
    states = [task.state for task in tasks]
    completed = sum(1 for state in states if state is TaskState.COMPLETED)
    failed = sum(1 for state in states if state is TaskState.FAILED)
    skipped = sum(1 for state in states if state is TaskState.SKIPPED)
    quarantined_tasks = sum(1 for state in states if state is TaskState.QUARANTINED)
    unfinished = len(tasks) - completed - failed - skipped - quarantined_tasks
    latest, report_issues = latest_reports_by_file(files, integrity_reports)
    tally = _tally(files, latest)
    failure_records = list(failures)
    known_failed = {failure.task_id for failure in failure_records}
    warnings: list[str] = [
        f"integrity report inconsistency [{issue.code}]: {issue.detail}" for issue in report_issues
    ]
    for task in tasks:
        if task.state is TaskState.FAILED and task.task_id not in known_failed:
            failure_records.append(
                FailureRecord(
                    task_id=task.task_id,
                    code="task_failed",
                    message=task.last_error if task.last_error is not None else "no last_error",
                    source_id=task.asset.source_id,
                )
            )
        if task.state is TaskState.COMPLETED and not any(
            record.task_id == task.task_id for record in files
        ):
            warnings.append(
                f"task {task.task_id} is COMPLETED but has no FileRecord (manifest inconsistency)"
            )
        if task.state in {TaskState.RUNNING, TaskState.VERIFYING, TaskState.QUEUED}:
            warnings.append(f"task {task.task_id} is unfinished in state {task.state.value}")
    quarantined_records = list(quarantined)
    known_quarantined = {item.task_id for item in quarantined_records}
    for task in tasks:
        if task.state is TaskState.QUARANTINED and task.task_id not in known_quarantined:
            quarantined_records.append(
                QuarantinedRecord(
                    task_id=task.task_id,
                    reason=task.last_error if task.last_error is not None else "integrity failure",
                    path="",
                    source_id=task.asset.source_id,
                )
            )
    file_summaries: list[FileSummary] = []
    for record in files:
        report = latest.get(record.file_id)
        file_summaries.append(
            FileSummary(
                file_id=record.file_id,
                task_id=record.task_id,
                source_id=record.source_id,
                path=_display_path(record.local_path, base_dir),
                size_bytes=record.size_bytes,
                checksum=record.checksum.value,
                integrity_status=report.status if report is not None else None,
            )
        )
    source_summaries = [
        SourceSummary(
            source_id=source.source_id,
            display_name=source.display_name,
            provider=source.provider,
            access_route=source.access_route.value,
        )
        for source in sources
    ]
    total_bytes = sum(record.size_bytes for record in files)
    if files and failed > 0:
        warnings.append("some tasks failed; delivered byte count covers successful files only")
    return CollectionReport(
        request_id=request_id,
        plan_id=plan_id,
        generated_at=generated_at if generated_at is not None else _utc_now(),
        tool_version=tool_version if tool_version is not None else __version__,
        request_count=request_count,
        discovered_tasks=len(tasks),
        completed_tasks=completed,
        failed_tasks=failed,
        skipped_tasks=skipped,
        quarantined_tasks=quarantined_tasks,
        unfinished_tasks=unfinished,
        total_bytes=total_bytes,
        integrity=tally,
        failures=tuple(failure_records),
        quarantined=tuple(quarantined_records),
        files=tuple(file_summaries),
        sources=tuple(source_summaries),
        dataset_id=dataset_id,
        delivery_manifest_path=(
            str(delivery_manifest_path) if delivery_manifest_path is not None else None
        ),
        warnings=tuple(warnings),
    )


def _tally(files: Sequence[FileRecord], latest: Mapping[str, IntegrityReport]) -> IntegrityTally:
    passed = failed = skipped = unknown = missing = 0
    for record in files:
        report = latest.get(record.file_id)
        if report is None:
            missing += 1
        elif report.status is IntegrityStatus.PASSED:
            passed += 1
        elif report.status is IntegrityStatus.FAILED:
            failed += 1
        elif report.status is IntegrityStatus.SKIPPED:
            skipped += 1
        else:
            unknown += 1
    return IntegrityTally(
        passed=passed, failed=failed, skipped=skipped, unknown=unknown, missing=missing
    )


def write_collection_report(report: CollectionReport, path: Path) -> Path:
    """把报告原子写到 ``path``（同目录临时文件 + ``os.replace``），返回该路径。"""
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report.as_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        raise ModelValidationError(
            "cannot write collection report",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def render_collection_report(
    report: CollectionReport,
    *,
    width: int = 100,
    color: bool = False,
) -> str:
    """用 ``rich`` 渲染终端表格，返回字符串（``color=False`` 时无 ANSI 转义）。"""
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=color,
        no_color=not color,
        highlight=False,
        color_system="auto" if color else None,
    )
    console.print(f"[bold]采集报告[/bold]  request={report.request_id}  plan={report.plan_id}")
    console.print(f"生成时间 {report.generated_at.isoformat()}  工具版本 {report.tool_version}")
    overview = Table(box=box.SIMPLE_HEAVY, show_header=True, title="采集概览")
    overview.add_column("指标", style="cyan", no_wrap=True)
    overview.add_column("数值", justify="right")
    overview_rows: tuple[tuple[str, str], ...] = (
        ("请求量", str(report.request_count)),
        ("发现任务数", str(report.discovered_tasks)),
        ("成功", str(report.completed_tasks)),
        ("失败", str(report.failed_tasks)),
        ("跳过", str(report.skipped_tasks)),
        ("隔离", str(report.quarantined_tasks)),
        ("未完成", str(report.unfinished_tasks)),
        ("总下载量", format_bytes(report.total_bytes)),
        (
            "校验结果",
            (
                f"通过 {report.integrity.passed} / 失败 {report.integrity.failed} / "
                f"跳过 {report.integrity.skipped} / 未知 {report.integrity.unknown} / "
                f"无报告 {report.integrity.missing}"
            ),
        ),
        ("数据源数", str(len(report.sources))),
        ("交付清单", report.delivery_manifest_path if report.delivery_manifest_path else "未生成"),
    )
    for label, value in overview_rows:
        overview.add_row(label, value)
    console.print(overview)
    if report.sources:
        sources_table = Table(box=box.SIMPLE, title="数据源")
        sources_table.add_column("source_id")
        sources_table.add_column("提供方")
        sources_table.add_column("访问路径")
        for source in report.sources:
            sources_table.add_row(source.source_id, source.provider, source.access_route)
        console.print(sources_table)
    if report.failures:
        failures_table = Table(box=box.SIMPLE, title="失败原因")
        failures_table.add_column("task_id")
        failures_table.add_column("source_id")
        failures_table.add_column("错误码")
        failures_table.add_column("原因", overflow="fold")
        for failure in report.failures:
            failures_table.add_row(
                failure.task_id,
                failure.source_id if failure.source_id is not None else "-",
                failure.code,
                failure.message,
            )
        console.print(failures_table)
        console.print(
            "错误码聚合: "
            + ", ".join(f"{code}={count}" for code, count in report.failure_reasons())
        )
    if report.quarantined:
        quarantined_table = Table(box=box.SIMPLE, title="隔离文件")
        quarantined_table.add_column("task_id")
        quarantined_table.add_column("source_id")
        quarantined_table.add_column("位置")
        quarantined_table.add_column("原因", overflow="fold")
        for item in report.quarantined:
            quarantined_table.add_row(
                item.task_id,
                item.source_id if item.source_id is not None else "-",
                item.path if item.path else "-",
                item.reason,
            )
        console.print(quarantined_table)
    if report.files:
        files_table = Table(box=box.SIMPLE, title="文件")
        files_table.add_column("file_id")
        files_table.add_column("source_id")
        files_table.add_column("大小", justify="right")
        files_table.add_column("校验")
        files_table.add_column("位置", overflow="fold")
        for file_item in report.files:
            status = (
                file_item.integrity_status.value
                if file_item.integrity_status is not None
                else "missing"
            )
            files_table.add_row(
                file_item.file_id,
                file_item.source_id,
                format_bytes(file_item.size_bytes),
                status,
                file_item.path,
            )
        console.print(files_table)
    for warning in report.warnings:
        console.print(f"[yellow]警告[/yellow] {warning}")
    return stream.getvalue()
