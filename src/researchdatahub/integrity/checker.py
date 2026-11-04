"""完整性检查链：存在性 → 可读 → 非空 → 大小 → 截断 → 校验和 → 格式 → NetCDF → 元数据。

分层（报告里逐条标明 ``level``，不允许把所有检查混成一句“校验通过”）
----------------------------------------------------------------------
* ``existence``：文件存在、是常规文件、可读——没有这些前提谈不上校验；
* ``size``：非空、大小匹配、截断判定（实测小于声明大小 = 很可能是断点续传/断流产物）；
* ``checksum``：按 ``FileRecord.checksum`` 声明的算法本地重算并比较；
* ``protocol``：校验和**来源标记**的可信度（例如把 HTTP ETag 当 MD5 必须拒绝）；
* ``format``：魔数嗅探 + NetCDF 打开性（可打开 = 容器结构完整，不代表科学内容正确）；
* ``scientific``：与调用方给出的期望元数据（变量/维度/格式）比较。

校验和策略
----------
* 远端声明的 checksum 只作为**期望值**；本地必须重新计算后才能声称 “已验证”。
* 报告 ``observed`` 中明确区分：
  - ``checksum.remote_checksum_verified``：远端声明值被本地重算证实；
  - ``checksum.local_sha256_recorded``：本地计算的 SHA-256 被记录（哪怕远端只给了 MD5）。
* ETag **绝不**直接当 MD5。来源可用 ``FileRecord.extensions["integrity.checksum_origin"]``
  显式标注（``remote_trusted`` / ``local_computed`` / ``remote_etag``）；未标注时按
  ``remote_etag == checksum.hexdigest`` 推断，MD5 命中即判定为协议级失败。

可选依赖
--------
NetCDF 检查通过 ``netCDF4`` 优先、``xarray`` 兜底；两者都缺席时该检查记为 **SKIPPED**
并写明原因，绝不记为 PASSED。测试可通过构造函数注入 ``netcdf_probe`` 模拟依赖缺席。

fail-closed 规则
----------------
* 文件缺失/不是常规文件 → 抛 ``IntegrityError``（检查无法进行）；
* 任一检查 FAILED → 报告 ``FAILED``（必须列出 failures）；
* 没有失败但存在 SKIPPED → 报告 ``SKIPPED``（未完成的校验不等于通过）；
* 一个检查都没执行（``kinds=[]``）→ 报告 ``UNKNOWN``。
"""

from __future__ import annotations

import hashlib
import importlib
import uuid
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Final

from researchdatahub.config.models import IntegrityConfig
from researchdatahub.core.errors import IntegrityError
from researchdatahub.core.models import (
    AcquisitionTask,
    Checksum,
    FileRecord,
    IntegrityCheckKind,
    IntegrityReport,
    IntegrityStatus,
    JsonValue,
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

_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024
_HEADER_BYTES: Final[int] = 8
_CHECKSUM_ORIGIN_KEY: Final[str] = "integrity.checksum_origin"
_NETCDF_SUFFIXES: Final[frozenset[str]] = frozenset({".nc", ".nc4", ".cdf", ".netcdf"})
_GRIB_SUFFIXES: Final[frozenset[str]] = frozenset({".grib", ".grib2", ".grb", ".grb2"})


class CheckKind(StrEnum):
    """本实现实际执行的检查种类（含只存在于存储/字节层面的检查）。"""

    EXISTS = "exists"
    READABLE = "readable"
    NON_EMPTY = "non_empty"
    SIZE_MATCH = "size_match"
    TRUNCATION = "truncation"
    CHECKSUM_MATCH = "checksum_match"
    FORMAT_SNIFF = "format_sniff"
    NETCDF_OPENABLE = "netcdf_openable"
    VARIABLE_PRESENT = "variable_present"
    METADATA_CONSISTENT = "metadata_consistent"


class CheckLevel(StrEnum):
    """检查层级；报告消费方据此判断“哪一层通过/失败”。"""

    EXISTENCE = "existence"
    SIZE = "size"
    CHECKSUM = "checksum"
    PROTOCOL = "protocol"
    FORMAT = "format"
    SCIENTIFIC = "scientific"


class CheckStatus(StrEnum):
    """单条检查的三态。``SKIPPED`` 永远不会被折算成通过。"""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ChecksumOrigin(StrEnum):
    """校验和值的来源标记——决定“已验证”到底验证了什么。"""

    REMOTE_TRUSTED = "remote_trusted"
    LOCAL_COMPUTED = "local_computed"
    REMOTE_ETAG = "remote_etag"


DEFAULT_CHECK_CHAIN: Final[tuple[CheckKind, ...]] = (
    CheckKind.EXISTS,
    CheckKind.READABLE,
    CheckKind.NON_EMPTY,
    CheckKind.SIZE_MATCH,
    CheckKind.TRUNCATION,
    CheckKind.CHECKSUM_MATCH,
    CheckKind.FORMAT_SNIFF,
    CheckKind.NETCDF_OPENABLE,
    CheckKind.METADATA_CONSISTENT,
)
"""调度器未显式指定检查时的完整默认链（顺序即执行顺序）。"""

_EXECUTION_ORDER: Final[tuple[CheckKind, ...]] = (
    CheckKind.EXISTS,
    CheckKind.READABLE,
    CheckKind.NON_EMPTY,
    CheckKind.SIZE_MATCH,
    CheckKind.TRUNCATION,
    CheckKind.CHECKSUM_MATCH,
    CheckKind.FORMAT_SNIFF,
    CheckKind.NETCDF_OPENABLE,
    CheckKind.VARIABLE_PRESENT,
    CheckKind.METADATA_CONSISTENT,
)

_ALIASES: Final[dict[str, CheckKind]] = {
    "exists": CheckKind.EXISTS,
    "readable": CheckKind.READABLE,
    "non_empty": CheckKind.NON_EMPTY,
    "size": CheckKind.SIZE_MATCH,
    "size_match": CheckKind.SIZE_MATCH,
    "truncation": CheckKind.TRUNCATION,
    "checksum": CheckKind.CHECKSUM_MATCH,
    "checksum_match": CheckKind.CHECKSUM_MATCH,
    "sha256": CheckKind.CHECKSUM_MATCH,
    "sha256_match": CheckKind.CHECKSUM_MATCH,
    "format": CheckKind.FORMAT_SNIFF,
    "format_sniff": CheckKind.FORMAT_SNIFF,
    "netcdf": CheckKind.NETCDF_OPENABLE,
    "netcdf_openable": CheckKind.NETCDF_OPENABLE,
    "variable_present": CheckKind.VARIABLE_PRESENT,
    "metadata": CheckKind.METADATA_CONSISTENT,
    "metadata_consistent": CheckKind.METADATA_CONSISTENT,
}

_CORE_KIND: Final[dict[CheckKind, IntegrityCheckKind]] = {
    CheckKind.SIZE_MATCH: IntegrityCheckKind.SIZE_MATCH,
    CheckKind.CHECKSUM_MATCH: IntegrityCheckKind.CHECKSUM_MATCH,
    CheckKind.FORMAT_SNIFF: IntegrityCheckKind.FORMAT_SNIFF,
    CheckKind.NETCDF_OPENABLE: IntegrityCheckKind.NETCDF_OPENABLE,
    CheckKind.VARIABLE_PRESENT: IntegrityCheckKind.VARIABLE_PRESENT,
    CheckKind.METADATA_CONSISTENT: IntegrityCheckKind.METADATA_CONSISTENT,
}

_LEVEL: Final[dict[CheckKind, CheckLevel]] = {
    CheckKind.EXISTS: CheckLevel.EXISTENCE,
    CheckKind.READABLE: CheckLevel.EXISTENCE,
    CheckKind.NON_EMPTY: CheckLevel.SIZE,
    CheckKind.SIZE_MATCH: CheckLevel.SIZE,
    CheckKind.TRUNCATION: CheckLevel.SIZE,
    CheckKind.CHECKSUM_MATCH: CheckLevel.CHECKSUM,
    CheckKind.FORMAT_SNIFF: CheckLevel.FORMAT,
    CheckKind.NETCDF_OPENABLE: CheckLevel.FORMAT,
    CheckKind.VARIABLE_PRESENT: CheckLevel.SCIENTIFIC,
    CheckKind.METADATA_CONSISTENT: CheckLevel.SCIENTIFIC,
}

_UNSUPPORTED_DECLARED: Final[dict[IntegrityCheckKind, str]] = {
    IntegrityCheckKind.TIME_COVERAGE: (
        "time_coverage check needs a decoded time axis and is not implemented in M5; "
        "it is reported SKIPPED rather than PASSED"
    ),
}


@dataclass(frozen=True)
class NetcdfSummary:
    """NetCDF 打开检查的观测结果（只读 Open 得到的结构事实）。"""

    engine: str
    data_model: str
    variables: tuple[str, ...]
    dimensions: tuple[str, ...]

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "engine": self.engine,
            "data_model": self.data_model,
            "variables": list(self.variables),
            "dimensions": list(self.dimensions),
        }


NetcdfProbe = Callable[[Path], "NetcdfSummary | None"]
"""探针：返回 ``None`` 表示可选依赖缺席；抛 ``OSError`` 表示文件损坏。"""


@dataclass(frozen=True)
class CheckResult:
    """单条检查的完整结果；``expected`` / ``actual`` 会进入报告 ``observed.checks``。"""

    kind: CheckKind
    level: CheckLevel
    status: CheckStatus
    expected: JsonValue
    actual: JsonValue
    detail: str

    @property
    def passed(self) -> bool:
        """是否通过（``SKIPPED`` 不是通过）。"""
        return self.status is CheckStatus.PASSED

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示（``status`` 与布尔 ``passed`` 同时给出，避免消费方自行折算）。"""
        return {
            "kind": self.kind.value,
            "level": self.level.value,
            "status": self.status.value,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PathCheckResult:
    """对**任意路径**做体检的结果（不依赖 ``FileRecord`` / ``AcquisitionTask``）。

    为什么需要它
    ------------
    ``rdh file verify <path>`` 面对的是交付盘上的历史文件，既没有 task 也没有可信的
    ``dataset_id`` / ``source_id``。用占位值构造 ``FileRecord`` 等于伪造元数据，因此这里
    返回一个只描述**文件自身字节事实**的结构：``path`` / ``size_bytes`` / ``sha256`` /
    逐条检查结果 / 汇总状态 / failures。缺失的期望值（大小、校验和、元数据）不会被
    假装通过——对应检查记 ``SKIPPED`` 并在 ``detail`` 说明原因。
    """

    path: Path
    status: IntegrityStatus
    size_bytes: int
    sha256: str | None
    checks: tuple[CheckResult, ...]
    failures: tuple[str, ...]
    checked_at: datetime
    checker_id: str
    actual_format: str | None = None
    netcdf: NetcdfSummary | None = None
    expected_size: int | None = None
    expected_checksum: str | None = None

    @property
    def ok(self) -> bool:
        """是否完全通过（``SKIPPED`` / ``UNKNOWN`` 都不是通过）。"""
        return self.status is IntegrityStatus.PASSED

    def as_json(self) -> dict[str, JsonValue]:
        """JSON 友好表示（CLI 可直接用它输出 ``--json``）。"""
        return {
            "path": str(self.path),
            "status": self.status.value,
            "ok": self.ok,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "actual_format": self.actual_format,
            "netcdf": self.netcdf.as_json() if self.netcdf is not None else None,
            "expected_size": self.expected_size,
            "expected_checksum": self.expected_checksum,
            "checks": [check.as_json() for check in self.checks],
            "failures": list(self.failures),
            "checked_at": self.checked_at.isoformat(),
            "checker_id": self.checker_id,
        }


def _load_optional(module_name: str) -> ModuleType | None:
    """懒加载可选依赖；缺席时返回 ``None``（调用方记 SKIPPED，不吞其它异常）。"""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def netcdf_backend_available() -> bool:
    """当前环境是否具备 NetCDF 打开能力（``netCDF4`` 或 ``xarray``）。"""
    return _load_optional("netCDF4") is not None or _load_optional("xarray") is not None


def probe_netcdf(path: Path) -> NetcdfSummary | None:
    """默认 NetCDF 探针：优先 ``netCDF4``，缺席时用 ``xarray``；都缺席返回 ``None``。

    只读取容器结构（变量/维度清单），不载入数据数组，因此内存占用与文件大小无关。
    """
    netcdf4 = _load_optional("netCDF4")
    if netcdf4 is not None:
        with netcdf4.Dataset(str(path), "r") as dataset:  # anything: 第三方无类型标注
            variables = tuple(str(name) for name in dataset.variables)
            dimensions = tuple(str(name) for name in dataset.dimensions)
            data_model = str(getattr(dataset, "data_model", "unknown"))
        return NetcdfSummary(
            engine="netCDF4",
            data_model=data_model,
            variables=variables,
            dimensions=dimensions,
        )
    xarray = _load_optional("xarray")
    if xarray is None:
        return None
    with xarray.open_dataset(path) as dataset:
        variables = tuple(str(name) for name in dataset.variables)
        dimensions = tuple(str(name) for name in dataset.dims)
    return NetcdfSummary(
        engine="xarray",
        data_model="unknown",
        variables=variables,
        dimensions=dimensions,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IntegrityChecker:
    """:class:`~researchdatahub.core.ports.IntegrityChecker` 的实现。

    Parameters
    ----------
    config:
        完整性策略（``fail_fast`` 与默认检查链）。``None`` 时使用代码内默认值。
    netcdf_probe:
        可替换的 NetCDF 探针；测试用它模拟“可选依赖缺席”（返回 ``None``）。
    clock:
        可注入时钟，便于测试固定 ``checked_at``。
    checker_id:
        写入报告的检查器标识，用于区分不同实现/版本。
    """

    def __init__(
        self,
        config: IntegrityConfig | None = None,
        *,
        netcdf_probe: NetcdfProbe | None = None,
        clock: Callable[[], datetime] | None = None,
        checker_id: str = "local-integrity-v1",
    ) -> None:
        self._config = config if config is not None else IntegrityConfig()
        self._probe = netcdf_probe if netcdf_probe is not None else probe_netcdf
        self._clock = clock if clock is not None else _utc_now
        self._checker_id = checker_id

    @property
    def fail_fast(self) -> bool:
        """是否在首个 FAILED 后停止后续检查。"""
        return self._config.fail_fast

    def check(
        self,
        file_record: FileRecord,
        *,
        task: AcquisitionTask,
        kinds: Sequence[str] | None = None,
        expected_metadata: Mapping[str, object] | None = None,
    ) -> IntegrityReport:
        """执行完整性检查并返回报告。

        Parameters
        ----------
        file_record:
            本地文件的事实记录（``local_path`` / ``size_bytes`` / ``checksum`` / 来源追踪）。
        task:
            所属任务（提供远端声明的大小/校验和/内容类型用于交叉核对）。
        kinds:
            要执行的检查；``None`` 用 ``config.default_checks``，空序列表示“不检查任何项”
            （报告为 UNKNOWN，而**不是** PASSED）。
        expected_metadata:
            科学元数据期望值，支持键 ``format`` / ``size_bytes`` / ``content_type`` /
            ``variables`` / ``dimensions``。未知键判 FAILED，绝不静默忽略。

        Returns
        -------
        IntegrityReport
            ``PASSED`` 仅当所有被请求检查都通过且没有 SKIPPED。

        Raises
        ------
        IntegrityError
            文件不存在/不是常规文件、检查种类不受支持、``expected_metadata`` 含非 JSON 值。
        """
        requested = self._resolve_kinds(kinds)
        path = Path(file_record.local_path)
        if not path.exists():
            raise IntegrityError(
                "integrity check cannot proceed: file is missing",
                task_id=task.task_id,
                detail={"path": str(path), "file_id": file_record.file_id},
            )
        if path.is_dir():
            raise IntegrityError(
                "integrity check cannot proceed: path is a directory",
                task_id=task.task_id,
                detail={"path": str(path), "file_id": file_record.file_id},
            )

        origin, origin_problem = self._resolve_checksum_origin(file_record, task)
        state = _CheckState(
            path=path,
            expected_size=file_record.size_bytes,
            expected_checksum=file_record.checksum,
            expected_metadata=expected_metadata,
            expected_format=self._expected_format(file_record, task),
            content_type=file_record.content_type,
            asset_size=task.asset.size_bytes,
            checksum_origin=origin,
            checksum_origin_problem=origin_problem,
        )
        results = self._execute_checks(state, requested)
        status = self._aggregate(results)
        failures = [
            f"{result.kind.value}: {result.detail}"
            for result in results
            if result.status is CheckStatus.FAILED
        ]
        report_kinds: list[IntegrityCheckKind] = []
        for result in results:
            mapped = _CORE_KIND.get(result.kind)
            if mapped is not None and mapped not in report_kinds:
                report_kinds.append(mapped)
        observed: dict[str, JsonValue] = {
            "path": str(path),
            "size_bytes": state.actual_size if state.actual_size is not None else 0,
            "format": state.actual_format if state.actual_format is not None else "unknown",
            "sha256": state.sha256_hexdigest if state.sha256_hexdigest is not None else None,
            "checksum": state.checksum_observation,
            "netcdf": state.netcdf_summary.as_json() if state.netcdf_summary is not None else None,
            "checks": [result.as_json() for result in results],
            "requested_checks": [kind.value for kind in requested],
        }
        expected_payload: dict[str, JsonValue] = {
            "size_bytes": file_record.size_bytes,
            "checksum": file_record.checksum.value,
            "content_type": file_record.content_type,
            "format": state.expected_format,
        }
        if expected_metadata is not None:
            expected_payload["metadata"] = _json_mapping(expected_metadata)
        return IntegrityReport(
            report_id=f"report-{uuid.uuid4().hex[:16]}",
            task_id=task.task_id,
            file_id=file_record.file_id,
            status=status,
            checks=report_kinds,
            failures=failures,
            expected=expected_payload,
            observed=observed,
            checked_at=self._clock(),
            checker_id=self._checker_id,
        )

    def check_path(
        self,
        path: Path,
        *,
        kinds: Sequence[str] | None = None,
        expected_size: int | None = None,
        expected_checksum: Checksum | None = None,
        expected_metadata: Mapping[str, object] | None = None,
    ) -> PathCheckResult:
        """对任意路径做完整性体检（不需要 ``FileRecord`` / ``AcquisitionTask``）。

        语义与 :meth:`check` 完全一致，区别只在于**没有可信的上下文元数据**：

        * ``exists`` / ``readable`` / ``non_empty`` / ``format_sniff`` / ``netcdf_openable``
          照常执行；
        * ``size_match`` / ``truncation`` 只在给出 ``expected_size`` 时判定，否则记
          ``SKIPPED``（“没有期望值”不等于“大小正确”）；
        * ``checksum_match`` 只在给出 ``expected_checksum`` 时判定；否则仍计算本地
          SHA-256 并放进 ``sha256`` / ``observed``，但检查记 ``SKIPPED``——只记录，不判通过；
        * 不伪造 ``task_id`` / ``file_id`` / ``dataset_id``：需要它们的地方一律不填。

        Parameters
        ----------
        path:
            待体检文件路径。
        kinds:
            检查种类；``None`` 用 ``config.default_checks``，空序列表示不检查任何项。
        expected_size:
            期望字节数（来自外部记录，可能不存在）。
        expected_checksum:
            期望校验和（来自外部记录，可能不存在）。
        expected_metadata:
            支持的键同 :meth:`check`。

        Returns
        -------
        PathCheckResult
            ``status`` 为 ``FAILED`` / ``SKIPPED`` / ``UNKNOWN`` 时均不代表通过。

        Raises
        ------
        IntegrityError
            路径不存在/不是常规文件、``expected_size`` 为负、检查种类不受支持、
            文件读取过程中发生 I/O 错误。
        """
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise IntegrityError(
                "integrity check cannot proceed: file is missing",
                detail={"path": str(candidate)},
            )
        if candidate.is_dir():
            raise IntegrityError(
                "integrity check cannot proceed: path is a directory",
                detail={"path": str(candidate)},
            )
        if expected_size is not None and expected_size < 0:
            raise IntegrityError(
                "expected_size must be >= 0", detail={"expected_size": expected_size}
            )
        requested = self._resolve_kinds(kinds)
        state = _CheckState(
            path=candidate,
            expected_size=expected_size,
            expected_checksum=expected_checksum,
            expected_metadata=expected_metadata,
            expected_format=self._expected_format_from_path(candidate),
        )
        results = self._execute_checks(state, requested)
        if state.sha256_hexdigest is None and state.readable:
            # ``PathCheckResult.sha256`` 是 CLI ``file verify`` 的核心输出；即使调用方
            # 没请求 checksum 检查，可读文件也要记录本地 SHA-256（只记录，不代表通过）。
            try:
                state.ensure_sha256()
            except OSError as exc:
                raise IntegrityError(
                    "cannot compute file hash during path verification",
                    detail={"path": str(candidate), "reason": str(exc)},
                ) from exc
        status = self._aggregate(results)
        failures = tuple(
            f"{result.kind.value}: {result.detail}"
            for result in results
            if result.status is CheckStatus.FAILED
        )
        return PathCheckResult(
            path=candidate,
            status=status,
            size_bytes=state.size(),
            sha256=state.sha256_hexdigest,
            checks=tuple(results),
            failures=failures,
            checked_at=self._clock(),
            checker_id=self._checker_id,
            actual_format=state.actual_format,
            netcdf=state.netcdf_summary,
            expected_size=expected_size,
            expected_checksum=(expected_checksum.value if expected_checksum is not None else None),
        )

    def _execute_checks(
        self, state: _CheckState, requested: tuple[CheckKind, ...]
    ) -> list[CheckResult]:
        """按固定顺序执行被请求的检查（`check` 与 `check_path` 共用）。"""
        # 可读性是 checksum/format/netcdf 的前提；即使调用方没显式请求也要先探测，
        # 否则会出现「文件其实读不了，但因为没跑 readable 检查而被当成可读」的漏洞。
        self._probe_readable(state)
        results: list[CheckResult] = []
        for kind in _EXECUTION_ORDER:
            if kind not in requested:
                continue
            result = self._run_one(kind, state)
            if result is not None:
                results.append(result)
                if self._config.fail_fast and result.status is CheckStatus.FAILED:
                    break
        return results

    # -- 检查链 ---------------------------------------------------------- #
    def _resolve_kinds(self, kinds: Sequence[str] | None) -> tuple[CheckKind, ...]:
        raw: Sequence[str] = (
            [item.value for item in self._config.default_checks] if kinds is None else kinds
        )
        if len(raw) == 0:
            return ()
        resolved: list[CheckKind] = []
        for entry in raw:
            if not isinstance(entry, str) or entry == "":
                raise IntegrityError("integrity check kind must be a non-empty string")
            lowered = entry.strip().lower()
            kind = _ALIASES.get(lowered)
            if kind is None:
                declared = _declared_core_kind(lowered)
                if declared is not None:
                    raise IntegrityError(
                        _UNSUPPORTED_DECLARED.get(
                            declared,
                            f"declared check kind {lowered!r} is not implemented",
                        ),
                        detail={"kind": lowered},
                    )
                raise IntegrityError(
                    "unsupported integrity check kind",
                    detail={"kind": lowered, "supported": sorted(_ALIASES)},
                )
            if kind not in resolved:
                resolved.append(kind)
        return tuple(resolved)

    def _run_one(self, kind: CheckKind, state: _CheckState) -> CheckResult | None:
        if kind is CheckKind.EXISTS:
            return _result(kind, CheckStatus.PASSED, True, True, "file exists")
        if kind is CheckKind.READABLE:
            return self._check_readable(kind, state)
        if kind is CheckKind.NON_EMPTY:
            return self._check_non_empty(kind, state)
        if kind is CheckKind.SIZE_MATCH:
            return self._check_size(kind, state)
        if kind is CheckKind.TRUNCATION:
            return self._check_truncation(kind, state)
        if kind is CheckKind.CHECKSUM_MATCH:
            return self._check_checksum(kind, state)
        if kind is CheckKind.FORMAT_SNIFF:
            return self._check_format(kind, state)
        if kind is CheckKind.NETCDF_OPENABLE:
            return self._check_netcdf(kind, state)
        if kind is CheckKind.VARIABLE_PRESENT:
            return self._check_variables(kind, state)
        if kind is CheckKind.METADATA_CONSISTENT:
            return self._check_metadata(kind, state)
        raise IntegrityError(  # pragma: no cover - 枚举已穷举
            "unhandled integrity check kind", detail={"kind": kind.value}
        )

    @staticmethod
    def _probe_readable(state: _CheckState) -> None:
        """探测文件可读性并缓存结果（被 checksum/format/netcdf 复用）。"""
        try:
            with state.path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            state.readable = False
            state.readable_error = str(exc)
            return
        state.readable = True
        state.readable_error = None

    def _ensure_format(self, state: _CheckState) -> str:
        """嗅探并缓存文件格式魔数（metadata 检查也会复用，不要求显式请求 format_sniff）。"""
        if state.actual_format is not None:
            return state.actual_format
        if not state.readable:
            return "unknown"
        try:
            with state.path.open("rb") as handle:
                header = handle.read(_HEADER_BYTES)
        except OSError:
            return "unknown"
        state.actual_format = _sniff_format(header)
        return state.actual_format

    def _check_readable(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        if state.readable:
            return _result(kind, CheckStatus.PASSED, True, True, "file opened for reading")
        return _result(
            kind,
            CheckStatus.FAILED,
            True,
            False,
            f"file is not readable: {state.readable_error}",
            level=CheckLevel.EXISTENCE,
        )

    def _check_non_empty(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        size = state.size()
        return _result(
            kind,
            CheckStatus.PASSED if size > 0 else CheckStatus.FAILED,
            "> 0 bytes",
            size,
            "file is non-empty" if size > 0 else "file is empty",
        )

    def _check_size(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        actual = state.size()
        expected = state.expected_size
        if expected is None:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                None,
                actual,
                "no expected size supplied; size recorded only, not verified",
            )
        detail = f"size {actual} bytes matches expected {expected} bytes"
        if actual != expected:
            detail = f"size mismatch: expected {expected} bytes, observed {actual} bytes"
            if state.asset_size is not None and state.asset_size != expected:
                detail += f"; remote asset declared {state.asset_size} bytes"
        return _result(
            kind,
            CheckStatus.PASSED if actual == expected else CheckStatus.FAILED,
            expected,
            actual,
            detail,
        )

    def _check_truncation(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        actual = state.size()
        expected = state.expected_size
        if expected is None:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                None,
                actual,
                "no expected size supplied; truncation cannot be judged",
            )
        truncated = actual < expected
        return _result(
            kind,
            CheckStatus.FAILED if truncated else CheckStatus.PASSED,
            expected,
            actual,
            (
                f"file appears truncated: {expected - actual} bytes missing"
                if truncated
                else "file is not truncated relative to the expected size"
            ),
        )

    def _check_checksum(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        expected = state.expected_checksum
        if not state.readable:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                expected.value if expected is not None else None,
                None,
                "checksum not computed because file is not readable",
            )
        if expected is None:
            # 没有期望值：只记录本地 SHA-256，绝不把「算出了哈希」当成「校验通过」。
            try:
                local_sha256 = state.ensure_sha256()
            except OSError as exc:
                return _result(
                    kind,
                    CheckStatus.FAILED,
                    None,
                    None,
                    f"failed to compute local SHA-256: {exc}",
                )
            state.checksum_observation = {
                "algorithm": "sha256",
                "origin": None,
                "expected_hexdigest": None,
                "computed_hexdigest": local_sha256,
                "matched": None,
                "remote_checksum_verified": False,
                "local_sha256_recorded": True,
                "trusted": False,
            }
            return _result(
                kind,
                CheckStatus.SKIPPED,
                None,
                f"sha256:{local_sha256}",
                "no expected checksum supplied; local SHA-256 recorded but nothing verified",
            )
        origin = state.checksum_origin
        if state.checksum_origin_problem is not None:
            return _result(
                kind,
                CheckStatus.FAILED,
                expected.value,
                None,
                state.checksum_origin_problem,
                level=CheckLevel.PROTOCOL,
            )
        algorithm = expected.algorithm
        try:
            sha256_digest = state.ensure_sha256()
            actual = _digest_for(state.path, algorithm, sha256_digest)
        except OSError as exc:
            return _result(
                kind,
                CheckStatus.FAILED,
                expected.value,
                None,
                f"failed to compute checksum: {exc}",
            )
        matches = actual == expected.hexdigest
        trusted = origin in (
            None,
            ChecksumOrigin.REMOTE_TRUSTED,
            ChecksumOrigin.LOCAL_COMPUTED,
        )
        remote_verified = matches and origin is ChecksumOrigin.REMOTE_TRUSTED
        origin_value = origin.value if origin is not None else "caller_supplied"
        state.checksum_observation = {
            "algorithm": algorithm,
            "origin": origin_value,
            "expected_hexdigest": expected.hexdigest,
            "computed_hexdigest": actual,
            "matched": matches,
            "remote_checksum_verified": remote_verified,
            "local_sha256_recorded": True,
            "trusted": trusted,
        }
        return _result(
            kind,
            CheckStatus.PASSED if matches else CheckStatus.FAILED,
            expected.value,
            f"{algorithm}:{actual}",
            (
                f"{algorithm} checksum matches (origin={origin_value}, "
                f"remote_checksum_verified={remote_verified})"
                if matches
                else f"{algorithm} checksum mismatch (origin={origin_value})"
            ),
            level=CheckLevel.PROTOCOL if not trusted else CheckLevel.CHECKSUM,
        )

    def _check_format(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        if not state.readable:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                state.expected_format,
                None,
                "format sniff skipped because file is not readable",
            )
        actual = self._ensure_format(state)
        compatible = _format_compatible(state.expected_format, actual)
        return _result(
            kind,
            CheckStatus.PASSED if compatible else CheckStatus.FAILED,
            state.expected_format,
            actual,
            (
                f"format {actual} is compatible with expected {state.expected_format}"
                if compatible
                else f"format mismatch: expected {state.expected_format}, observed {actual}"
            ),
        )

    def _check_netcdf(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        if state.expected_format != "netcdf" and state.actual_format in (None, "unknown"):
            return _result(
                kind,
                CheckStatus.SKIPPED,
                "netcdf",
                state.actual_format,
                "file is not a NetCDF file; open check not applicable",
            )
        probe_result = self._probe_netcdf(state)
        if probe_result.failure is not None:
            return _result(kind, CheckStatus.FAILED, "openable", None, probe_result.failure)
        if probe_result.skipped is not None:
            return _result(kind, CheckStatus.SKIPPED, "openable", None, probe_result.skipped)
        if probe_result.summary is None:  # pragma: no cover - 契约由 _ProbeOutcome 保证
            raise IntegrityError("netcdf probe returned neither summary nor reason")
        return _result(
            kind,
            CheckStatus.PASSED,
            "openable",
            f"{probe_result.summary.engine}:{probe_result.summary.data_model}",
            (
                f"opened with {probe_result.summary.engine}: "
                f"{len(probe_result.summary.variables)} variables, "
                f"{len(probe_result.summary.dimensions)} dimensions"
            ),
        )

    def _check_variables(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        expected = _expected_string_list(state.expected_metadata, "variables")
        if expected is None:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                None,
                None,
                "no expected variables supplied; refusing to report PASSED",
            )
        probe_result = self._probe_netcdf(state)
        expected_json = _json_strings(expected)
        if probe_result.failure is not None:
            return _result(kind, CheckStatus.FAILED, expected_json, None, probe_result.failure)
        if probe_result.skipped is not None:
            return _result(kind, CheckStatus.SKIPPED, expected_json, None, probe_result.skipped)
        if probe_result.summary is None:  # pragma: no cover
            raise IntegrityError("netcdf probe returned neither summary nor reason")
        available = set(probe_result.summary.variables)
        missing = [name for name in expected if name not in available]
        return _result(
            kind,
            CheckStatus.FAILED if missing else CheckStatus.PASSED,
            expected_json,
            _json_strings(sorted(available)),
            (
                f"missing variables: {', '.join(missing)}"
                if missing
                else "all expected variables are present"
            ),
        )

    def _check_metadata(self, kind: CheckKind, state: _CheckState) -> CheckResult:
        if state.expected_metadata is None or len(state.expected_metadata) == 0:
            return _result(
                kind,
                CheckStatus.SKIPPED,
                None,
                None,
                "no expected metadata supplied; refusing to report PASSED",
            )
        observed: dict[str, JsonValue] = {
            "size_bytes": state.size(),
            "format": self._ensure_format(state),
        }
        if state.content_type is not None:
            observed["content_type"] = state.content_type
        summary = state.netcdf_summary
        if summary is None and _wants_netcdf_metadata(state.expected_metadata):
            probe_result = self._probe_netcdf(state)
            if probe_result.failure is not None:
                return _result(kind, CheckStatus.FAILED, None, observed, probe_result.failure)
            if probe_result.skipped is not None:
                return _result(kind, CheckStatus.SKIPPED, None, observed, probe_result.skipped)
            summary = probe_result.summary
        if summary is not None:
            observed["variables"] = _json_strings(sorted(summary.variables))
            observed["dimensions"] = _json_strings(sorted(summary.dimensions))
        comparison = _compare_metadata(state.expected_metadata, observed)
        if comparison.skipped is not None:
            return _result(kind, CheckStatus.SKIPPED, None, observed, comparison.skipped)
        return _result(
            kind,
            CheckStatus.FAILED if comparison.problems else CheckStatus.PASSED,
            _json_mapping(state.expected_metadata),
            observed,
            (
                "metadata mismatch: " + "; ".join(comparison.problems)
                if comparison.problems
                else "declared metadata agrees with the file"
            ),
        )

    # -- 辅助 ------------------------------------------------------------ #
    @staticmethod
    def _resolve_checksum_origin(
        record: FileRecord, task: AcquisitionTask
    ) -> tuple[ChecksumOrigin, str | None]:
        raw = record.extensions.get(_CHECKSUM_ORIGIN_KEY)
        if raw is not None:
            if not isinstance(raw, str):
                return (
                    ChecksumOrigin.REMOTE_ETAG,
                    f"invalid {_CHECKSUM_ORIGIN_KEY} marker: expected a string",
                )
            try:
                origin = ChecksumOrigin(raw)
            except ValueError:
                return (
                    ChecksumOrigin.REMOTE_ETAG,
                    f"invalid {_CHECKSUM_ORIGIN_KEY} marker {raw!r}; "
                    "trusted values are 'remote_trusted' / 'local_computed'",
                )
            if origin is ChecksumOrigin.REMOTE_ETAG:
                return (
                    origin,
                    "ETag must never be used as a checksum; recompute a local hash and "
                    "record integrity.checksum_origin=remote_trusted/local_computed",
                )
            return origin, None
        if _looks_like_etag_as_md5(record):
            return (
                ChecksumOrigin.REMOTE_ETAG,
                "MD5 checksum equals the remote ETag; ETag must never be used as MD5",
            )
        remote = task.asset.checksum
        if remote is not None and remote.value == record.checksum.value:
            return ChecksumOrigin.REMOTE_TRUSTED, None
        return ChecksumOrigin.LOCAL_COMPUTED, None

    def _probe_netcdf(self, state: _CheckState) -> _ProbeOutcome:
        if state.netcdf_probe_attempted:
            if state.netcdf_summary is None:
                return _ProbeOutcome(skipped=state.netcdf_skip_reason)
            return _ProbeOutcome(summary=state.netcdf_summary)
        state.netcdf_probe_attempted = True
        try:
            summary = self._probe(state.path)
        except (OSError, RuntimeError, ValueError) as exc:
            state.netcdf_skip_reason = f"NetCDF open failed: {exc}"
            return _ProbeOutcome(failure=state.netcdf_skip_reason)
        if summary is None:
            state.netcdf_skip_reason = (
                "optional NetCDF dependencies are not installed "
                "(netCDF4/xarray); check reported SKIPPED, not PASSED"
            )
            return _ProbeOutcome(skipped=state.netcdf_skip_reason)
        state.netcdf_summary = summary
        return _ProbeOutcome(summary=summary)

    @staticmethod
    def _expected_format(record: FileRecord, task: AcquisitionTask) -> str:
        content_types = [record.content_type, task.asset.content_type]
        for content_type in content_types:
            if content_type is None:
                continue
            lowered = content_type.lower()
            if "netcdf" in lowered or "x-netcdf" in lowered:
                return "netcdf"
            if "grib" in lowered:
                return "grib"
            if "zip" in lowered:
                return "zip"
        suffixes = [
            Path(record.local_path).suffix.lower(),
            Path(task.asset.file_name).suffix.lower(),
        ]
        for suffix in suffixes:
            if suffix in _NETCDF_SUFFIXES:
                return "netcdf"
            if suffix in _GRIB_SUFFIXES:
                return "grib"
            if suffix == ".zip":
                return "zip"
        return "unknown"

    @staticmethod
    def _expected_format_from_path(path: Path) -> str:
        """仅凭扩展名推断期望容器格式（``check_path`` 没有 content_type 可用）。"""
        suffix = path.suffix.lower()
        if suffix in _NETCDF_SUFFIXES:
            return "netcdf"
        if suffix in _GRIB_SUFFIXES:
            return "grib"
        if suffix == ".zip":
            return "zip"
        return "unknown"

    @staticmethod
    def _aggregate(results: Sequence[CheckResult]) -> IntegrityStatus:
        if any(result.status is CheckStatus.FAILED for result in results):
            return IntegrityStatus.FAILED
        if any(result.status is CheckStatus.SKIPPED for result in results):
            return IntegrityStatus.SKIPPED
        if results:
            return IntegrityStatus.PASSED
        return IntegrityStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# 检查状态与工具（模块内部）
# --------------------------------------------------------------------------- #
@dataclass
class _CheckState:
    """一次检查调用内的可变观测状态（不跨调用共享，无线程安全问题）。

    只保存**期望值**与观测值，不保存 ``FileRecord`` / ``AcquisitionTask``：这样
    :meth:`IntegrityChecker.check_path` 可以对任意路径复用同一条检查链，而不会为了
    满足内部结构去伪造 task/file/dataset 元数据。
    """

    path: Path
    expected_size: int | None
    expected_checksum: Checksum | None
    expected_metadata: Mapping[str, object] | None
    expected_format: str
    content_type: str | None = None
    asset_size: int | None = None
    checksum_origin: ChecksumOrigin | None = None
    checksum_origin_problem: str | None = None
    actual_size: int | None = None
    actual_format: str | None = None
    sha256_hexdigest: str | None = None
    readable: bool = False
    readable_error: str | None = None
    netcdf_summary: NetcdfSummary | None = None
    netcdf_probe_attempted: bool = False
    netcdf_skip_reason: str | None = None
    checksum_observation: dict[str, JsonValue] | None = None

    def size(self) -> int:
        """读取并缓存文件大小；I/O 失败抛 ``IntegrityError``（检查无法继续）。"""
        if self.actual_size is None:
            try:
                self.actual_size = int(self.path.stat().st_size)
            except OSError as exc:
                raise IntegrityError(
                    "cannot stat file during integrity check",
                    detail={"path": str(self.path), "reason": str(exc)},
                ) from exc
        return self.actual_size

    def ensure_sha256(self) -> str:
        """流式计算并缓存 SHA-256（1 MiB 分块，内存常量级）。"""
        if self.sha256_hexdigest is None:
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                    digest.update(chunk)
            self.sha256_hexdigest = digest.hexdigest()
        return self.sha256_hexdigest


@dataclass(frozen=True)
class _ProbeOutcome:
    summary: NetcdfSummary | None = None
    skipped: str | None = None
    failure: str | None = None


@dataclass(frozen=True)
class _MetadataComparison:
    problems: tuple[str, ...] = ()
    skipped: str | None = None


def _result(
    kind: CheckKind,
    status: CheckStatus,
    expected: JsonValue,
    actual: JsonValue,
    detail: str,
    *,
    level: CheckLevel | None = None,
) -> CheckResult:
    return CheckResult(
        kind=kind,
        level=level if level is not None else _LEVEL[kind],
        status=status,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def _declared_core_kind(value: str) -> IntegrityCheckKind | None:
    for candidate in IntegrityCheckKind:
        if candidate.value == value:
            return candidate
    return None


def _looks_like_etag_as_md5(record: FileRecord) -> bool:
    if record.remote_etag is None or record.checksum.algorithm != "md5":
        return False
    normalized = record.remote_etag.strip().strip('"').lower()
    return normalized == record.checksum.hexdigest.lower()


def _digest_for(path: Path, algorithm: str, sha256_digest: str) -> str:
    if algorithm == "sha256":
        return sha256_digest
    if algorithm == "sha256-16":
        return sha256_digest[:16]
    if algorithm == "crc32":
        crc = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        return f"{crc & 0xFFFFFFFF:08x}"
    if algorithm == "md5":
        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()
    raise IntegrityError("unsupported checksum algorithm", detail={"algorithm": algorithm})


def _sniff_format(header: bytes) -> str:
    if header.startswith((b"CDF\x01", b"CDF\x02")):
        return "netcdf-classic"
    if header.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5"
    if header.startswith(b"GRIB"):
        return "grib"
    if header.startswith(b"PK\x03\x04"):
        return "zip"
    if header.startswith(b"\x1f\x8b"):
        return "gzip"
    return "unknown"


def _format_compatible(expected: str, actual: str) -> bool:
    if actual == "unknown":
        return expected == "unknown"
    if expected == "unknown":
        return True
    if expected == "netcdf":
        return actual in {"netcdf-classic", "hdf5"}
    return expected == actual


def _expected_string_list(
    metadata: Mapping[str, object] | None, key: str
) -> tuple[str, ...] | None:
    if metadata is None:
        return None
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise IntegrityError(
            "expected metadata value must be a list of strings",
            detail={"key": key, "type": type(value).__name__},
        )
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise IntegrityError(
                "expected metadata list entries must be non-empty strings",
                detail={"key": key, "value": str(item)},
            )
        names.append(item)
    return tuple(names)


def _wants_netcdf_metadata(metadata: Mapping[str, object]) -> bool:
    return "variables" in metadata or "dimensions" in metadata


def _compare_metadata(
    expected: Mapping[str, object], observed: Mapping[str, JsonValue]
) -> _MetadataComparison:
    supported = {"format", "size_bytes", "content_type", "variables", "dimensions"}
    problems: list[str] = []
    for key in expected:
        if key not in supported:
            problems.append(f"unsupported expected metadata key {key!r}")
            continue
        if key not in observed:
            problems.append(f"cannot observe expected key {key!r} from this file")
            continue
        expected_value = expected[key]
        actual_value = observed[key]
        if key in {"variables", "dimensions"}:
            if key == "variables":
                expected_list = _expected_string_list(expected, "variables")
            else:
                raw = expected.get("dimensions")
                if not isinstance(raw, list | tuple) or not all(
                    isinstance(item, str) for item in raw
                ):
                    raise IntegrityError("expected metadata 'dimensions' must be a list of strings")
                expected_list = tuple(str(item) for item in raw)
            if expected_list is None:  # pragma: no cover - 上方已保证存在
                problems.append(f"expected key {key!r} is empty")
                continue
            actual_list = (
                sorted(str(item) for item in actual_value) if isinstance(actual_value, list) else []
            )
            if sorted(expected_list) != actual_list:
                problems.append(
                    f"{key} mismatch: expected {sorted(expected_list)}, observed {actual_list}"
                )
            continue
        if expected_value != actual_value:
            problems.append(
                f"{key} mismatch: expected {expected_value!r}, observed {actual_value!r}"
            )
    return _MetadataComparison(problems=tuple(problems))


def _json_strings(values: Sequence[str]) -> list[JsonValue]:
    """把字符串序列转成 JSON 值列表（``list[str]`` 不是 ``JsonValue``，需要显式转换）。"""
    payload: list[JsonValue] = []
    for value in values:
        payload.append(value)
    return payload


def _json_mapping(mapping: Mapping[str, object]) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    for key, value in mapping.items():
        payload[str(key)] = _to_json_value(value, field=str(key))
    return payload


def _to_json_value(value: object, *, field: str) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_to_json_value(item, field=field) for item in value]
    if isinstance(value, Mapping):
        nested: dict[str, JsonValue] = {}
        for key, item in value.items():
            nested[str(key)] = _to_json_value(item, field=f"{field}.{key}")
        return nested
    raise IntegrityError(
        "expected metadata contains a non-JSON value",
        detail={"field": field, "type": type(value).__name__},
    )
