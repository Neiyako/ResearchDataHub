"""报告生成：交付清单（JSON）与采集报告（JSON + 终端表格）。

公开 API
--------
* 交付清单：:func:`~researchdatahub.reporting.delivery.build_delivery_manifest`、
  :func:`~researchdatahub.reporting.delivery.verify_delivery_manifest`、
  :func:`~researchdatahub.reporting.delivery.delivery_manifest_to_json`、
  :func:`~researchdatahub.reporting.delivery.write_delivery_manifest`；
  失败/跳过/隔离文件通过 :class:`~researchdatahub.reporting.delivery.ExcludedFile` 保留。
* 采集报告：:func:`~researchdatahub.reporting.report.build_collection_report`、
  :func:`~researchdatahub.reporting.report.render_collection_report`（rich 终端表格）、
  :func:`~researchdatahub.reporting.report.write_collection_report`（JSON）。
"""

from researchdatahub.reporting.delivery import (
    DeliveryIssue,
    DeliveryVerification,
    ExcludedDisposition,
    ExcludedFile,
    build_delivery_manifest,
    delivery_manifest_to_json,
    latest_reports_by_file,
    verify_delivery_manifest,
    write_delivery_manifest,
)
from researchdatahub.reporting.report import (
    CollectionReport,
    FailureRecord,
    FileSummary,
    IntegrityTally,
    QuarantinedRecord,
    SourceSummary,
    build_collection_report,
    format_bytes,
    render_collection_report,
    write_collection_report,
)

__all__ = [
    "CollectionReport",
    "DeliveryIssue",
    "DeliveryVerification",
    "ExcludedDisposition",
    "ExcludedFile",
    "FailureRecord",
    "FileSummary",
    "IntegrityTally",
    "QuarantinedRecord",
    "SourceSummary",
    "build_collection_report",
    "build_delivery_manifest",
    "delivery_manifest_to_json",
    "format_bytes",
    "latest_reports_by_file",
    "render_collection_report",
    "verify_delivery_manifest",
    "write_collection_report",
    "write_delivery_manifest",
]
