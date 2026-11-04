"""完整性校验：大小、校验和、格式嗅探与 NetCDF 打开性检查（Integrity Before Delivery）。

公开 API
--------
* :class:`~researchdatahub.integrity.checker.IntegrityChecker`：实现
  :class:`~researchdatahub.core.ports.IntegrityChecker`；失败返回 ``FAILED`` 报告，
  只有“检查无法进行”才抛 :class:`~researchdatahub.core.errors.IntegrityError`。
* :class:`~researchdatahub.integrity.checker.CheckResult` / ``CheckKind`` / ``CheckLevel``：
  报告里的逐条检查证据与层级（existence / size / checksum / protocol / format / scientific）。
* :class:`~researchdatahub.integrity.checker.NetcdfSummary` 与 ``probe_netcdf``：懒加载
  ``netCDF4`` / ``xarray``，依赖缺席时检查记 **SKIPPED** 而非 PASSED。
* :data:`~researchdatahub.integrity.checker.DEFAULT_CHECK_CHAIN`：默认完整检查链。
"""

from researchdatahub.integrity.checker import (
    DEFAULT_CHECK_CHAIN,
    CheckKind,
    CheckLevel,
    CheckResult,
    CheckStatus,
    ChecksumOrigin,
    IntegrityChecker,
    NetcdfProbe,
    NetcdfSummary,
    netcdf_backend_available,
    probe_netcdf,
)

__all__ = [
    "DEFAULT_CHECK_CHAIN",
    "CheckKind",
    "CheckLevel",
    "CheckResult",
    "CheckStatus",
    "ChecksumOrigin",
    "IntegrityChecker",
    "NetcdfProbe",
    "NetcdfSummary",
    "netcdf_backend_available",
    "probe_netcdf",
]
