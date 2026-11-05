"""交付到用户指定目录（ADR-0015）。

问题
----
执行链路把校验通过的文件提交到存储根（``~/.rdh/raw/<dataset_id>/``），这是**权威副本**。
但用户说的往往是"保存到当前项目的 ``data/raw``"。两者不是一回事：

* 权威副本：Manifest 的事实来源，可复现、只读、由本项目管理；
* 用户副本：用户项目里的数据，用户可修改/删除。

本模块把权威副本**交付**到用户指定的目录，并且**默认不覆盖已有文件**（指令 §13）。

行为约定
--------
* 目标不存在 → ``CREATE``；
* 目标存在且大小 + sha256 完全一致 → ``SKIP_IDENTICAL``（幂等，不重写、不改 mtime）；
* 目标存在但内容不同 → ``CONFLICT``（默认**不覆盖**，交由调用方决定）；
* 任何写操作**之前**先检查空间，不足立即失败（不产生半截文件）；
* 写入用 ``copy → fsync → 同目录原子 rename``，中途崩溃不会留下"看起来完整"的文件。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from researchdatahub.core.errors import StorageError
from researchdatahub.core.models import FileRecord

__all__ = [
    "SHA256_ALGORITHM",
    "ConflictPolicy",
    "DeliveryAction",
    "DeliveryConflict",
    "DeliveryFile",
    "DeliveryOutcome",
    "DeliveryPlan",
    "DeliveryResult",
    "apply_delivery",
    "plan_delivery",
    "resolve_target_dir",
]

SHA256_ALGORITHM: Final[str] = "sha256"
_CHUNK: Final[int] = 1024 * 1024
_TEMP_PREFIX: Final[str] = ".rdh-tmp-"


class DeliveryAction(StrEnum):
    """单个交付文件的处置动作。"""

    CREATE = "create"
    SKIP_IDENTICAL = "skip_identical"
    CONFLICT = "conflict"
    FAILED = "failed"


class ConflictPolicy(StrEnum):
    """冲突策略。默认 ``RAISE``：不替用户做决定。"""

    RAISE = "raise"
    """遇到冲突立即抛 :class:`DeliveryConflictError`（调用方据此请用户选择）。"""

    SKIP = "skip"
    """跳过冲突文件，继续交付其它文件。"""

    OVERWRITE = "overwrite"
    """覆盖目标。**只允许在用户显式选择时使用。**"""

    SUFFIX = "suffix"
    """另存为 ``name.ext.1`` / ``name.ext.2``……，原文件不动。"""


class DeliveryConflictError(StorageError):
    """交付目标已存在且内容不同，默认策略拒绝覆盖。"""

    default_code = "RDH_DELIVERY_CONFLICT"


@dataclass(frozen=True)
class DeliveryConflict:
    """一个冲突文件的结构化描述（供 Agent 向用户提问）。"""

    file_id: str
    source_path: str
    target_path: str
    expected_size: int
    target_size: int
    resolution_options: tuple[str, ...] = ("overwrite", "skip", "suffix")

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示。"""
        return {
            "file_id": self.file_id,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "expected_size": self.expected_size,
            "target_size": self.target_size,
            "resolution_options": list(self.resolution_options),
        }


@dataclass(frozen=True)
class DeliveryFile:
    """计划中的一个交付项。"""

    file_id: str
    source_path: Path
    target_path: Path
    size_bytes: int
    checksum: str | None
    action: DeliveryAction


@dataclass(frozen=True)
class DeliveryPlan:
    """交付计划：只做判定，不写任何文件。"""

    target_dir: Path
    files: tuple[DeliveryFile, ...] = ()
    conflicts: tuple[DeliveryConflict, ...] = ()
    free_bytes: int = 0
    required_bytes: int = 0

    @property
    def has_conflicts(self) -> bool:
        """是否存在冲突项（需要用户决定）。"""
        return bool(self.conflicts)

    @property
    def has_enough_space(self) -> bool:
        """目标盘空间是否足够（含 10% 余量）。"""
        return self.free_bytes >= int(self.required_bytes * 1.1)

    def to_create(self) -> tuple[DeliveryFile, ...]:
        """需要真正写入的文件（``CREATE`` 与 ``SUFFIX`` 之外的纯新建）。"""
        return tuple(item for item in self.files if item.action is DeliveryAction.CREATE)


@dataclass
class DeliveryOutcome:
    """单个文件的交付结果。"""

    file_id: str
    action: DeliveryAction
    target_path: str
    bytes_written: int = 0
    error: str | None = None


@dataclass
class DeliveryResult:
    """一次交付的整体结果。"""

    target_dir: str
    outcomes: list[DeliveryOutcome] = field(default_factory=list)
    conflicts: list[DeliveryConflict] = field(default_factory=list)

    @property
    def delivered(self) -> tuple[DeliveryOutcome, ...]:
        """真正写入到目标目录的文件。"""
        return tuple(
            item
            for item in self.outcomes
            if item.action in (DeliveryAction.CREATE,) and item.error is None
        )

    @property
    def skipped(self) -> tuple[DeliveryOutcome, ...]:
        """因内容相同而跳过的文件。"""
        return tuple(item for item in self.outcomes if item.action is DeliveryAction.SKIP_IDENTICAL)

    @property
    def failed(self) -> tuple[DeliveryOutcome, ...]:
        """交付失败的文件。"""
        return tuple(item for item in self.outcomes if item.error is not None)

    @property
    def complete(self) -> bool:
        """全部文件是否都已就位（本次写入成功，或此前已存在且内容一致）。

        注意：``conflicts`` 记录的是**判定阶段**发现的冲突，若调用方用
        ``SKIP``/``OVERWRITE``/``SUFFIX`` 策略处理了它们，``complete`` 仍应为真——
        真正决定"是否完整"的是逐文件的执行结果，而不是判定阶段的快照。
        因此这里只看 :attr:`failed`（同目录下另存 ``SUFFIX`` 会新增文件，同样算成功）。
        """
        return not self.failed

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示（供 MCP ``get_manifest`` 使用）。"""
        return {
            "target_dir": self.target_dir,
            "complete": self.complete,
            "delivered": [
                {"file_id": item.file_id, "path": item.target_path, "bytes": item.bytes_written}
                for item in self.delivered
            ],
            "skipped_identical": [
                {"file_id": item.file_id, "path": item.target_path} for item in self.skipped
            ],
            "conflicts": [item.as_dict() for item in self.conflicts],
            "failed": [
                {"file_id": item.file_id, "path": item.target_path, "error": item.error}
                for item in self.failed
            ],
        }


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def resolve_target_dir(output_path: str, *, cwd: Path | None = None) -> Path:
    """把 ``output.path`` 解析为绝对目录。

    Parameters
    ----------
    output_path:
        用户给出的目录（相对或绝对）。
    cwd:
        解析相对路径的基准目录；``None`` 时用当前进程工作目录。

    Returns
    -------
    Path
        规范化后的绝对路径（不创建目录）。

    Raises
    ------
    StorageError
        路径为空、含 NUL 字节，或指向已存在的**普通文件**。
    """
    text = output_path.strip()
    if not text:
        raise StorageError("delivery path must not be empty")
    if "\x00" in text:
        raise StorageError("delivery path must not contain NUL bytes")
    base = Path(cwd) if cwd is not None else Path.cwd()
    candidate = Path(text).expanduser()
    resolved = candidate if candidate.is_absolute() else base / candidate
    normalized = Path(os.path.normpath(str(resolved)))
    if normalized.exists() and not normalized.is_dir():
        raise StorageError(
            f"delivery path {normalized} exists and is not a directory",
            detail={"path": str(normalized)},
        )
    return normalized


# --------------------------------------------------------------------------- #
# 判定（无副作用）
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    """流式计算文件 sha256（不把文件读入内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum_hex(record: FileRecord) -> str | None:
    """取记录里的 sha256 十六进制摘要（其它算法无法用于判等）。"""
    if record.checksum.algorithm != SHA256_ALGORITHM:
        return None
    return record.checksum.hexdigest


def _target_name(record: FileRecord) -> str:
    """交付文件名：优先用记录里的文件名，退化为权威副本的文件名。"""
    return Path(record.local_path).name


def plan_delivery(
    records: Sequence[FileRecord],
    target_dir: Path,
) -> DeliveryPlan:
    """判定每个文件应 ``CREATE`` / ``SKIP_IDENTICAL`` / ``CONFLICT``。

    **不写任何文件**。判定"内容相同"必须同时满足大小相等与 sha256 相等；
    记录里若不是 sha256 校验和，则无法判等，一律按冲突处理（宁可问用户，不可误判相同）。

    Parameters
    ----------
    records:
        要交付的文件记录（来自 Manifest）。
    target_dir:
        目标目录（可不存在）。

    Returns
    -------
    DeliveryPlan
        判定结果 + 空间需求。
    """
    files: list[DeliveryFile] = []
    conflicts: list[DeliveryConflict] = []
    required = 0
    for record in records:
        source = Path(record.local_path)
        target = target_dir / _target_name(record)
        expected_hex = _checksum_hex(record)
        if not target.exists():
            action = DeliveryAction.CREATE
            required += record.size_bytes
        elif not target.is_file() or target.stat().st_size != record.size_bytes:
            action = DeliveryAction.CONFLICT
        elif expected_hex is not None and _sha256(target) == expected_hex:
            action = DeliveryAction.SKIP_IDENTICAL
        else:
            # 大小相同但不是 sha256 / 摘要不同 → 无法证明相同 → 冲突。
            action = DeliveryAction.CONFLICT
        entry = DeliveryFile(
            file_id=record.file_id,
            source_path=source,
            target_path=target,
            size_bytes=record.size_bytes,
            checksum=record.checksum.value,
            action=action,
        )
        files.append(entry)
        if action is DeliveryAction.CONFLICT:
            conflicts.append(
                DeliveryConflict(
                    file_id=record.file_id,
                    source_path=str(source),
                    target_path=str(target),
                    expected_size=record.size_bytes,
                    target_size=target.stat().st_size if target.is_file() else -1,
                )
            )
    free = _free_bytes(target_dir)
    return DeliveryPlan(
        target_dir=target_dir,
        files=tuple(files),
        conflicts=tuple(conflicts),
        free_bytes=free,
        required_bytes=required,
    )


def _free_bytes(path: Path) -> int:
    """目标盘可用字节数（目录不存在时沿父级查找直到存在的祖先）。"""
    probe = path
    while True:
        try:
            return int(shutil.disk_usage(probe).free)
        except OSError:
            parent = probe.parent
            if parent == probe:
                return 0
            probe = parent


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _atomic_copy(source: Path, target: Path) -> int:
    """``copy → fsync → 同目录原子 rename``，返回写入字节数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f"{_TEMP_PREFIX}{uuid.uuid4().hex}{target.suffix}"
    written = 0
    try:
        with source.open("rb") as src, temp.open("wb") as dst:
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                dst.write(chunk)
                written += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        # 同目录 rename 是原子的；先 fsync 目录项（尽力而为，Windows 上不支持目录句柄）。
        os.replace(temp, target)
        _fsync_dir(target.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return written


def _fsync_dir(directory: Path) -> None:
    """尽力 fsync 目录，确保 rename 落盘（不支持时静默跳过）。"""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - Windows 不支持
        pass
    finally:
        os.close(handle)


def _suffixed(target: Path) -> Path:
    """生成不冲突的另存名：``a.nc`` → ``a.nc.1`` → ``a.nc.2``……"""
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.name}.{index}")
        if not candidate.exists():
            return candidate
    raise StorageError(
        f"cannot find a free suffixed name for {target}",
        detail={"path": str(target)},
    )


def apply_delivery(
    plan: DeliveryPlan,
    *,
    on_conflict: ConflictPolicy = ConflictPolicy.RAISE,
    allow_overwrite: bool = False,
) -> DeliveryResult:
    """按计划把文件写到目标目录。

    Parameters
    ----------
    plan:
        :func:`plan_delivery` 的结果。
    on_conflict:
        冲突策略，默认 ``RAISE``（不覆盖、立即报错，交由调用方决策）。
    allow_overwrite:
        ``on_conflict=OVERWRITE`` 时的第二道闸门：必须显式置 ``True``。
        二者缺一即拒绝覆盖，防止一次误传参数就把用户数据冲掉。

    Returns
    -------
    DeliveryResult
        每个文件的交付结果。

    Raises
    ------
    DeliveryConflictError
        ``on_conflict=RAISE`` 且存在冲突。
    StorageError
        空间不足，或 ``OVERWRITE`` 未经 ``allow_overwrite`` 授权。
    """
    if on_conflict is ConflictPolicy.OVERWRITE and not allow_overwrite:
        raise StorageError(
            "refusing to overwrite existing files without explicit allow_overwrite=True "
            "(see ADR-0015)",
            detail={"policy": on_conflict.value},
        )
    if on_conflict is ConflictPolicy.RAISE and plan.has_conflicts:
        raise DeliveryConflictError(
            f"{len(plan.conflicts)} file(s) already exist at {plan.target_dir} with different "
            "content; ask the user whether to overwrite, skip, or save with a suffix",
            detail={"conflicts": [item.as_dict() for item in plan.conflicts]},
        )
    if not plan.has_enough_space:
        raise StorageError(
            "not enough free space at delivery target "
            f"(need {plan.required_bytes} bytes +10%, free {plan.free_bytes} bytes)",
            detail={
                "target_dir": str(plan.target_dir),
                "required_bytes": plan.required_bytes,
                "free_bytes": plan.free_bytes,
            },
        )

    result = DeliveryResult(target_dir=str(plan.target_dir), conflicts=list(plan.conflicts))
    for item in plan.files:
        if item.action is DeliveryAction.SKIP_IDENTICAL:
            result.outcomes.append(
                DeliveryOutcome(
                    file_id=item.file_id,
                    action=DeliveryAction.SKIP_IDENTICAL,
                    target_path=str(item.target_path),
                )
            )
            continue
        if item.action is DeliveryAction.CONFLICT:
            if on_conflict is ConflictPolicy.SKIP:
                result.outcomes.append(
                    DeliveryOutcome(
                        file_id=item.file_id,
                        action=DeliveryAction.CONFLICT,
                        target_path=str(item.target_path),
                        error="skipped by conflict policy",
                    )
                )
                continue
            if on_conflict is ConflictPolicy.OVERWRITE:
                target = item.target_path
            else:  # SUFFIX
                target = _suffixed(item.target_path)
        else:
            target = item.target_path
        try:
            written = _atomic_copy(item.source_path, target)
        except OSError as exc:
            result.outcomes.append(
                DeliveryOutcome(
                    file_id=item.file_id,
                    action=DeliveryAction.FAILED,
                    target_path=str(target),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        result.outcomes.append(
            DeliveryOutcome(
                file_id=item.file_id,
                action=DeliveryAction.CREATE,
                target_path=str(target),
                bytes_written=written,
            )
        )
    return result


def delivery_extensions(
    result: DeliveryResult,
) -> dict[str, dict[str, str]]:
    """把交付结果转成可写进 ``FileRecord.extensions`` 的命名空间片段。

    Returns
    -------
    dict[str, dict[str, str]]
        ``{file_id: {"rdh.delivery_path": ..., "rdh.delivery_status": ...}}``，
        供调用方更新 Manifest（键已命名空间化，符合契约约束）。
    """
    out: dict[str, dict[str, str]] = {}
    for outcome in result.outcomes:
        status = (
            "delivered"
            if outcome.action is DeliveryAction.CREATE and outcome.error is None
            else "skipped_identical"
            if outcome.action is DeliveryAction.SKIP_IDENTICAL
            else "conflict"
            if outcome.action is DeliveryAction.CONFLICT
            else "failed"
        )
        out[outcome.file_id] = {
            "rdh.delivery_path": outcome.target_path,
            "rdh.delivery_status": status,
        }
    return out


def iter_sources(plan: DeliveryPlan) -> Iterable[Path]:
    """遍历计划涉及的源文件路径（便于调用方预检存在性）。"""
    for item in plan.files:
        yield item.source_path
