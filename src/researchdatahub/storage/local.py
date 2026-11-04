"""本地存储后端：staging → raw 的原子提交、隔离、归档与崩溃恢复。

设计要点
--------
* **目录布局完全来自配置**（``StorageConfig`` 的 ``root`` 与五个子目录名），本模块
  不硬编码任何路径。构造时校验子目录互不重叠且都在 ``root`` 之内。
* **正式文件禁止覆盖**：目标已存在时比较 ``size + sha256``；完全相同返回
  ``SKIPPED_IDENTICAL``（不写、不删、也不改目标），不同则抛 ``StorageError``。
  人工删除/替换正式数据必须走显式流程，而不是让一次下载静默覆盖历史交付物。
* **同文件系统**：``os.replace``（同一文件系统内的 rename 是原子的——目标要么不存在，
  要么是完整文件）。
* **跨文件系统（EXDEV）**：``rename`` 不跨越挂载点，且**不原子**。实现走
  ``staging 分块复制 → fsync → 同名目录内 rename → 大小/哈希复核 → 删除源``。
  可见性窗口：复制期间 raw 目录里只有 ``.rdh-partial-*`` 临时文件，最终 rename 在同一
  目录内完成，因此读者永远看不到「半个正式文件」。崩溃后由
  :meth:`LocalStorageBackend.recover_pending_commits` 依据提交日志继续或回滚。
* **提交日志**：每个正式目标旁边写 ``<file>.commit.json``（只由 ``publish_original``
  创建，``raw`` 目录内其余文件即交付数据）。日志先以 ``O_EXCL`` 创建——它同时充当
  「同一目标串行提交」的锁；崩溃后 ``pending`` 状态让恢复流程可以判断文件已移动但
  数据库未更新，从而完成一致性修复。
* **只读保护**：提交完成后默认 ``chmod 0444``（可用 ``read_only=False`` 关闭）。
* **并发**：不同任务的 ``allocate_temp`` / ``allocate_staging`` 互不共享路径；
  ``publish_original`` 对**同一目标**的并发通过提交日志的 ``O_EXCL`` 串行化。不同目标
  可并发提交。
* **8GB 内存约束**：哈希与复制全部 1 MiB 流式分块，内存占用恒定。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import uuid
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from researchdatahub.config.models import StorageConfig
from researchdatahub.core.errors import StorageError
from researchdatahub.core.models import Checksum, JsonValue
from researchdatahub.storage.naming import (
    HASH_CHUNK_SIZE,
    ensure_within,
    sanitize_file_name,
    sha256_file,
    validate_component,
)

__all__ = [
    "COMMIT_JOURNAL_SUFFIX",
    "CommitJournal",
    "CommitRecovery",
    "CommitStatus",
    "LocalStorageBackend",
    "OrphanScan",
    "PublishResult",
    "PublishStatus",
    "RecoveryAction",
]

COMMIT_JOURNAL_SUFFIX: Final[str] = ".commit.json"
"""提交日志侧车文件后缀；``raw`` 目录内以此结尾的文件不是交付数据。"""

_PARTIAL_PREFIX: Final[str] = ".rdh-partial-"
_JOURNAL_TMP_PREFIX: Final[str] = ".rdh-journal-"
_JOURNAL_SCHEMA_VERSION: Final[int] = 1


class CommitStatus(StrEnum):
    """提交日志状态机：``PENDING`` → ``COMMITTED`` / ``ABORTED`` / ``NEEDS_ATTENTION``。"""

    PENDING = "pending"
    COMMITTED = "committed"
    ABORTED = "aborted"
    NEEDS_ATTENTION = "needs_attention"


class PublishStatus(StrEnum):
    """一次提交的结果：真正写入，或因目标内容完全相同而跳过。"""

    COMMITTED = "committed"
    SKIPPED_IDENTICAL = "skipped_identical"


class RecoveryAction(StrEnum):
    """一条挂起提交的恢复结论。"""

    FINALIZED = "finalized"
    RETRIED = "retried"
    ABORTED = "aborted_commit"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class PublishResult:
    """``commit_staging`` 的结构化结果。

    ``SKIPPED_IDENTICAL`` 时源文件**仍然留在原地**（不写、不删），调用方如需清理
    staging 应由 ``scan_orphans`` 判定后显式处理。
    """

    status: PublishStatus
    path: Path
    size_bytes: int
    sha256: str

    @property
    def committed(self) -> bool:
        """本次调用是否真正把文件移入了正式区。"""
        return self.status is PublishStatus.COMMITTED

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好的表示（写入事件/日志）。"""
        return {
            "status": self.status.value,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CommitJournal:
    """一份提交日志。持久化为目标旁的 ``<file>.commit.json``。

    ``sha256`` 是**移动前**在 staging 源文件上计算的值；恢复流程用它复核目标文件。
    """

    journal_path: Path
    target_path: Path
    source_path: Path
    dataset_id: str
    file_name: str
    size_bytes: int
    sha256: str
    status: CommitStatus
    created_at: datetime
    updated_at: datetime
    attempts: int

    def as_json(self) -> dict[str, JsonValue]:
        """序列化为 JSON 字典（稳定键集，便于人工排查）。"""
        return {
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "target_path": str(self.target_path),
            "source_path": str(self.source_path),
            "dataset_id": self.dataset_id,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "attempts": self.attempts,
        }

    @classmethod
    def from_json(cls, journal_path: Path, payload: dict[str, JsonValue]) -> CommitJournal:
        """从 JSON 重建日志；结构不符时抛 ``StorageError``（恢复流程据此判 NEEDS_ATTENTION）。"""
        schema = payload.get("schema_version")
        if schema != _JOURNAL_SCHEMA_VERSION:
            raise StorageError(
                "unsupported commit journal schema",
                detail={"journal": str(journal_path), "schema_version": str(schema)},
            )
        try:
            return cls(
                journal_path=journal_path,
                target_path=Path(_require_str(payload, "target_path")),
                source_path=Path(_require_str(payload, "source_path")),
                dataset_id=_require_str(payload, "dataset_id"),
                file_name=_require_str(payload, "file_name"),
                size_bytes=_require_int(payload, "size_bytes"),
                sha256=_require_str(payload, "sha256"),
                status=CommitStatus(_require_str(payload, "status")),
                created_at=datetime.fromisoformat(_require_str(payload, "created_at")),
                updated_at=datetime.fromisoformat(_require_str(payload, "updated_at")),
                attempts=_require_int(payload, "attempts"),
            )
        except ValueError as exc:
            raise StorageError(
                "corrupt commit journal",
                detail={"journal": str(journal_path), "reason": str(exc)},
            ) from exc


@dataclass(frozen=True)
class CommitRecovery:
    """``recover_pending_commits`` 对一条挂起提交的处理结果。"""

    journal_path: Path
    action: RecoveryAction
    target_path: Path | None
    detail: str


@dataclass(frozen=True)
class OrphanScan:
    """``scan_orphans`` 的只读体检结果。

    * ``raw_orphans``：正式区里既不在数据库已知集合、也没有挂起提交日志的文件；
    * ``pending_commit_targets``：有挂起日志的目标——它们是「文件已移动但数据库未更新」
      的候选，应先跑 :meth:`LocalStorageBackend.recover_pending_commits`；
    * ``staging_orphans``：没有被任何挂起日志引用的 staging 文件；
    * ``temp_files``：超过年龄阈值仍未清理的进程级临时文件；
    * ``partial_files``：跨文件系统复制崩溃遗留的 ``.rdh-partial-*``。
    """

    raw_orphans: tuple[Path, ...] = ()
    pending_commit_targets: tuple[Path, ...] = ()
    staging_orphans: tuple[Path, ...] = ()
    temp_files: tuple[Path, ...] = ()
    partial_files: tuple[Path, ...] = ()

    @property
    def total(self) -> int:
        """所有类别的总数（终端报告用）。"""
        return (
            len(self.raw_orphans)
            + len(self.pending_commit_targets)
            + len(self.staging_orphans)
            + len(self.temp_files)
            + len(self.partial_files)
        )


# --------------------------------------------------------------------------- #
# JSON 辅助
# --------------------------------------------------------------------------- #
def _require_str(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise StorageError("corrupt commit journal: missing string field", detail={"field": key})
    return value


def _require_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("corrupt commit journal: missing integer field", detail={"field": key})
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _portable_name(name: str, *, fallback: str = "file") -> str:
    """把任意名字变成可安全落盘的一段（用于隔离区/归档区，**不用于正式区**）。

    正式区文件名必须走 :func:`~researchdatahub.storage.naming.sanitize_file_name`
    的严格校验；隔离区则相反——即使源文件名非法也必须能留下来，因此这里做替换而非拒绝。
    """
    cleaned = "".join(char if (char.isalnum() or char in "._-+,") else "_" for char in name)
    cleaned = cleaned.strip("._") or fallback
    return cleaned[:120]


class LocalStorageBackend:
    """基于本地文件系统的 :class:`~researchdatahub.core.ports.StorageBackend` 实现。

    Parameters
    ----------
    config:
        存储布局配置（``root`` 与 ``raw_dir`` / ``staging_dir`` / ``quarantine_dir`` /
        ``temp_dir`` / ``archive_dir``）。本类不读取 YAML，也不猜测默认路径。
    read_only:
        提交成功后是否 ``chmod 0444``。默认开启；只读保护是**降低误写风险**，不是安全边界。
    create_dirs:
        是否在构造时创建目录。``None`` 时取 ``config.create_dirs_on_init``。

    Raises
    ------
    StorageError
        ``root`` 是文件、子目录逃逸出 ``root``、子目录互相重叠或目录创建失败。
    """

    def __init__(
        self,
        config: StorageConfig,
        *,
        read_only: bool = True,
        create_dirs: bool | None = None,
    ) -> None:
        self._config = config
        # ``resolve(strict=False)`` 解析已存在祖先的符号链接（例如 macOS 的 /var →
        # /private/var），否则子目录 resolve 后与 root 基准不一致会误判为逃逸。
        root = Path(config.root).expanduser().resolve(strict=False)
        if root.exists() and not root.is_dir():
            raise StorageError(
                "storage root exists but is not a directory",
                detail={"root": str(root)},
            )
        self._root = root
        self._raw_dir = self._resolve_subdir(config.raw_dir, field="raw_dir")
        self._staging_dir = self._resolve_subdir(config.staging_dir, field="staging_dir")
        self._quarantine_dir = self._resolve_subdir(config.quarantine_dir, field="quarantine_dir")
        self._temp_dir = self._resolve_subdir(config.temp_dir, field="temp_dir")
        self._archive_dir = self._resolve_subdir(config.archive_dir, field="archive_dir")
        managed = {
            self._raw_dir,
            self._staging_dir,
            self._quarantine_dir,
            self._temp_dir,
            self._archive_dir,
        }
        if len(managed) != 5:
            raise StorageError(
                "storage subdirectories overlap after path normalisation",
                detail={"dirs": sorted(str(path) for path in managed)},
            )
        self._read_only = read_only
        should_create = config.create_dirs_on_init if create_dirs is None else create_dirs
        if should_create:
            self._ensure_layout()

    # -- 目录布局 -------------------------------------------------------- #
    def _resolve_subdir(self, name: str, *, field: str) -> Path:
        candidate = Path(name)
        if candidate.is_absolute() or name.startswith(("~", "env:")):
            raise StorageError(
                f"{field} must be relative to storage.root",
                detail={"field": field, "value": name},
            )
        try:
            resolved = (self._root / candidate).resolve()
        except OSError as exc:  # pragma: no cover - 仅在损坏的符号链接下发生
            raise StorageError(
                f"cannot resolve {field}", detail={"field": field, "reason": str(exc)}
            ) from exc
        if resolved == self._root or not resolved.is_relative_to(self._root):
            raise StorageError(
                f"{field} escapes storage.root",
                detail={"field": field, "value": name, "root": str(self._root)},
            )
        return resolved

    def _ensure_layout(self) -> None:
        self._ensure_dir(self._root, field="root")
        self._ensure_dir(self._raw_dir, field="raw_dir")
        self._ensure_dir(self._staging_dir, field="staging_dir")
        self._ensure_dir(self._quarantine_dir, field="quarantine_dir")
        self._ensure_dir(self._temp_dir, field="temp_dir")
        self._ensure_dir(self._archive_dir, field="archive_dir")

    @staticmethod
    def _ensure_dir(path: Path, *, field: str) -> Path:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"cannot create {field}",
                detail={"field": field, "path": str(path), "reason": str(exc)},
            ) from exc
        if not path.is_dir():
            raise StorageError(
                f"{field} is not a directory", detail={"field": field, "path": str(path)}
            )
        return path

    @property
    def root(self) -> Path:
        """存储根目录（已解析为绝对路径）。"""
        return self._root

    @property
    def raw_dir(self) -> Path:
        """正式数据区。"""
        return self._raw_dir

    @property
    def staging_dir(self) -> Path:
        """下载中转区。"""
        return self._staging_dir

    @property
    def quarantine_dir(self) -> Path:
        """隔离区。"""
        return self._quarantine_dir

    @property
    def temp_dir(self) -> Path:
        """进程级临时区。"""
        return self._temp_dir

    @property
    def archive_dir(self) -> Path:
        """归档区。"""
        return self._archive_dir

    # -- 容量 ----------------------------------------------------------- #
    def free_bytes(self) -> int:
        """存储根所在卷的可用字节数。"""
        try:
            usage = shutil.disk_usage(self._root)
        except OSError as exc:
            raise StorageError(
                "cannot query free space",
                detail={"root": str(self._root), "reason": str(exc)},
            ) from exc
        return int(usage.free)

    def has_free_space(self, required_bytes: int = 0) -> bool:
        """在 ``free_space_floor_gb`` 之上是否还能放下 ``required_bytes``。"""
        if required_bytes < 0:
            raise StorageError(
                "required_bytes must be >= 0", detail={"required_bytes": required_bytes}
            )
        try:
            available = self.free_bytes()
        except StorageError:
            return False
        return available - required_bytes >= self._floor_bytes()

    def ensure_free_space(self, required_bytes: int = 0) -> None:
        """校验剩余空间；不足时抛 ``StorageError``（调度器据此暂停/失败任务）。

        ``free_space_floor_gb`` 的含义是「写入后仍需保留的余量」：复制 ``required_bytes``
        之后剩余必须不低于阈值，否则拒绝开始，避免把卷写满导致 SQLite 与日志也写不进去。
        """
        if required_bytes < 0:
            raise StorageError(
                "required_bytes must be >= 0", detail={"required_bytes": required_bytes}
            )
        available = self.free_bytes()
        floor = self._floor_bytes()
        if available - required_bytes < floor:
            raise StorageError(
                "insufficient free space",
                detail={
                    "available_bytes": available,
                    "required_bytes": required_bytes,
                    "floor_bytes": floor,
                },
            )

    def _floor_bytes(self) -> int:
        return int(self._config.free_space_floor_gb * 1024**3)

    # -- 分配路径 ------------------------------------------------------- #
    def allocate_temp(self, task_id: str, *, file_name: str | None = None) -> Path:
        """为任务分配进程级临时文件路径（父目录已创建）。

        ``file_name`` 为空时生成随机 ``<uuid>.part``；提供 ``file_name`` 时按科学文件名
        规则清洗后使用，并**允许已存在**（断点续传场景由调用方决定是否复用）。
        """
        component = validate_component(task_id, field="task_id")
        directory = self._ensure_dir(self._temp_dir / component, field="temp_dir")
        name = (
            sanitize_file_name(file_name) if file_name is not None else f"{uuid.uuid4().hex}.part"
        )
        return directory / name

    def allocate_staging(self, task_id: str, file_name: str) -> Path:
        """为任务分配 staging 路径（父目录已创建）。

        与 ``allocate_temp`` 的区别：staging 是**跨进程可恢复**的中间区（大文件下载、
        重试、跨文件系统提交的源），temp 只是本进程内的一次性文件。
        返回的路径可能已存在（断点续传），本方法不创建也不截断文件。
        """
        component = validate_component(task_id, field="task_id")
        name = sanitize_file_name(file_name)
        directory = self._ensure_dir(self._staging_dir / component, field="staging_dir")
        return directory / name

    def expected_original_path(self, *, dataset_id: str, file_name: str) -> Path:
        """计算正式区目标路径（不创建目录、不移动文件），供调度器提前展示/规划。"""
        dataset = validate_component(dataset_id, field="dataset_id")
        name = sanitize_file_name(file_name)
        return self._raw_dir / dataset / name

    # -- 提交 ----------------------------------------------------------- #
    def publish_original(
        self,
        temp_path: Path,
        *,
        dataset_id: str,
        file_name: str,
        expected_size: int | None = None,
        expected_checksum: Checksum | None = None,
    ) -> Path:
        """把校验通过的文件原子移入正式区，返回最终路径。

        这是 :class:`~researchdatahub.core.ports.StorageBackend` 协议方法；需要区分
        「真正提交」与「内容相同而跳过」时请用 :meth:`commit_staging`。
        """
        return self.commit_staging(
            temp_path,
            dataset_id=dataset_id,
            file_name=file_name,
            expected_size=expected_size,
            expected_checksum=expected_checksum,
        ).path

    def commit_staging(
        self,
        temp_path: Path,
        *,
        dataset_id: str,
        file_name: str,
        expected_size: int | None = None,
        expected_checksum: Checksum | None = None,
    ) -> PublishResult:
        """以提交日志保护的方式把 staging/temp 文件提交到正式区。

        Parameters
        ----------
        temp_path:
            源文件；**必须**位于 ``staging_dir`` 或 ``temp_dir`` 之内（解析符号链接后）。
        dataset_id:
            正式区子目录名，小写标识符。
        file_name:
            正式文件名，经 :func:`sanitize_file_name` 清洗与校验。
        expected_size:
            期望字节数；与源文件不符立即失败（防止把半截文件提交成正式数据）。
        expected_checksum:
            期望校验和；算法支持 ``sha256`` / ``sha256-16`` / ``md5`` / ``crc32``。

        Returns
        -------
        PublishResult
            ``COMMITTED`` 或 ``SKIPPED_IDENTICAL``（目标已存在且内容相同）。

        Raises
        ------
        StorageError
            源文件非法/缺失、大小或校验和不符、目标已存在且内容不同、存在挂起提交、
            磁盘不足或跨文件系统复制失败。失败时源文件保留，提交日志标记为可恢复状态。
        """
        dataset = validate_component(dataset_id, field="dataset_id")
        name = sanitize_file_name(file_name)
        source = self._validated_source(temp_path)
        size_bytes = self._stat_size(source)
        if expected_size is not None and expected_size != size_bytes:
            raise StorageError(
                "staging file size does not match expected size",
                detail={
                    "path": str(source),
                    "expected_size": expected_size,
                    "actual_size": size_bytes,
                },
            )
        digest = sha256_file(source)
        if expected_checksum is not None:
            self._verify_expected_checksum(source, digest, expected_checksum)
        target = self._raw_dir / dataset / name
        self._ensure_dir(target.parent, field="raw_dir")
        if target.exists() or target.is_symlink():
            return self._handle_existing_target(source, target, size_bytes, digest)

        journal_path = self._journal_path(target)
        if journal_path.exists():
            self._clear_aborted_journal(journal_path)
        now = _utc_now()
        journal = CommitJournal(
            journal_path=journal_path,
            target_path=target,
            source_path=source,
            dataset_id=dataset,
            file_name=name,
            size_bytes=size_bytes,
            sha256=digest,
            status=CommitStatus.PENDING,
            created_at=now,
            updated_at=now,
            attempts=0,
        )
        self._write_journal(journal, create_exclusive=True)
        self._move_into_raw(source, target, journal)
        committed = self._finalize_commit(journal)
        return PublishResult(
            status=PublishStatus.COMMITTED,
            path=target,
            size_bytes=committed.size_bytes,
            sha256=committed.sha256,
        )

    def _journal_path(self, target: Path) -> Path:
        return target.with_name(target.name + COMMIT_JOURNAL_SUFFIX)

    def _validated_source(self, temp_path: Path) -> Path:
        candidate = Path(temp_path).expanduser()
        if candidate.is_symlink():
            raise StorageError(
                "staging source must not be a symlink", detail={"path": str(candidate)}
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise StorageError(
                "staging source does not exist",
                detail={"path": str(candidate), "reason": str(exc)},
            ) from exc
        if not resolved.is_file():
            raise StorageError(
                "staging source must be a regular file", detail={"path": str(resolved)}
            )
        if not (
            resolved.is_relative_to(self._staging_dir) or resolved.is_relative_to(self._temp_dir)
        ):
            raise StorageError(
                "staging source must live under staging_dir or temp_dir",
                detail={
                    "path": str(resolved),
                    "staging_dir": str(self._staging_dir),
                    "temp_dir": str(self._temp_dir),
                },
            )
        return resolved

    @staticmethod
    def _stat_size(path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError as exc:
            raise StorageError(
                "cannot stat file", detail={"path": str(path), "reason": str(exc)}
            ) from exc

    def _verify_expected_checksum(self, path: Path, sha256_digest: str, expected: Checksum) -> None:
        algorithm = expected.algorithm
        if algorithm == "sha256":
            actual = sha256_digest
        elif algorithm == "sha256-16":
            actual = sha256_digest[:16]
        elif algorithm == "md5":
            actual = _hash_file(path, "md5")
        elif algorithm == "crc32":
            crc = 0
            try:
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(HASH_CHUNK_SIZE)
                        if not chunk:
                            break
                        crc = zlib.crc32(chunk, crc)
            except OSError as exc:
                raise StorageError(
                    "cannot hash file",
                    detail={"path": str(path), "reason": str(exc)},
                ) from exc
            actual = f"{crc & 0xFFFFFFFF:08x}"
        else:  # pragma: no cover - Checksum 正则已限制算法集合
            raise StorageError("unsupported checksum algorithm", detail={"algorithm": algorithm})
        if actual != expected.hexdigest:
            raise StorageError(
                "staging file does not match expected checksum",
                detail={
                    "path": str(path),
                    "algorithm": algorithm,
                    "expected": expected.hexdigest,
                    "actual": actual,
                },
            )

    def _handle_existing_target(
        self, source: Path, target: Path, size_bytes: int, digest: str
    ) -> PublishResult:
        if target.is_symlink():
            raise StorageError("raw target must not be a symlink", detail={"target": str(target)})
        if not target.is_file():
            raise StorageError(
                "raw target exists but is not a regular file", detail={"target": str(target)}
            )
        target_size = self._stat_size(target)
        if target_size == size_bytes:
            target_digest = sha256_file(target)
            if target_digest == digest:
                return PublishResult(
                    status=PublishStatus.SKIPPED_IDENTICAL,
                    path=target,
                    size_bytes=size_bytes,
                    sha256=digest,
                )
        raise StorageError(
            "raw target already exists with different content; refusing to overwrite",
            detail={
                "target": str(target),
                "source": str(source),
                "target_size": target_size,
                "source_size": size_bytes,
            },
        )

    def _clear_aborted_journal(self, journal_path: Path) -> None:
        try:
            journal = self._read_journal(journal_path)
        except StorageError as exc:
            raise StorageError(
                "existing commit journal is corrupt; run recover_pending_commits()",
                detail={"journal": str(journal_path), "reason": exc.message},
            ) from exc
        if journal.status is CommitStatus.ABORTED:
            try:
                journal_path.unlink()
            except OSError as exc:  # pragma: no cover - 仅权限异常
                raise StorageError(
                    "cannot remove aborted commit journal",
                    detail={"journal": str(journal_path), "reason": str(exc)},
                ) from exc
            return
        if journal.status is CommitStatus.COMMITTED and journal.target_path.exists():
            raise StorageError(
                "raw target already committed but re-publish was requested; refusing to overwrite",
                detail={"journal": str(journal_path), "target": str(journal.target_path)},
            )
        raise StorageError(
            "a pending commit journal exists; run recover_pending_commits() first",
            detail={"journal": str(journal_path), "status": journal.status.value},
        )

    def _write_journal(self, journal: CommitJournal, *, create_exclusive: bool) -> None:
        payload = json.dumps(
            journal.as_json(), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        data = payload + b"\n"
        path = journal.journal_path
        if create_exclusive:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise StorageError(
                    "a commit journal already exists for this target", detail={"journal": str(path)}
                ) from exc
            except OSError as exc:  # pragma: no cover - 仅权限/磁盘异常
                raise StorageError(
                    "cannot create commit journal",
                    detail={"journal": str(path), "reason": str(exc)},
                ) from exc
            try:
                os.write(descriptor, data)
                os.fsync(descriptor)
            except OSError as exc:  # pragma: no cover - 仅磁盘异常
                raise StorageError(
                    "cannot write commit journal",
                    detail={"journal": str(path), "reason": str(exc)},
                ) from exc
            finally:
                os.close(descriptor)
            return
        tmp = path.with_name(f"{_JOURNAL_TMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise StorageError(
                "cannot update commit journal",
                detail={"journal": str(path), "reason": str(exc)},
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _update_journal(
        self, journal: CommitJournal, status: CommitStatus, *, attempts: int | None = None
    ) -> CommitJournal:
        updated = replace(
            journal,
            status=status,
            updated_at=_utc_now(),
            attempts=journal.attempts + 1 if attempts is None else attempts,
        )
        self._write_journal(updated, create_exclusive=False)
        return updated

    def _move_into_raw(self, source: Path, target: Path, journal: CommitJournal) -> None:
        """同文件系统直接 ``os.replace``；EXDEV 时走复制 → fsync → rename → 复核。"""
        try:
            os.replace(source, target)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                self._copy_across_devices(source, target, journal)
                return
            raise StorageError(
                "atomic move into raw failed",
                detail={"source": str(source), "target": str(target), "reason": str(exc)},
            ) from exc

    def _copy_across_devices(self, source: Path, target: Path, journal: CommitJournal) -> None:
        """跨挂载点提交：复制到同目录 ``.rdh-partial-*``，fsync 后 rename。

        rename **只在同一目录内**发生，因此正式路径的可见性仍然原子；跨文件系统
        没有原子性的是「复制」这一步，它由提交日志 + 恢复流程兜底。
        """
        self.ensure_free_space(journal.size_bytes)
        partial = target.with_name(f"{_PARTIAL_PREFIX}{uuid.uuid4().hex}-{target.name}")
        digest = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as source_handle, partial.open("xb") as target_handle:
                while True:
                    chunk = source_handle.read(HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target_handle.write(chunk)
                    copied += len(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if copied != journal.size_bytes:
                raise StorageError(
                    "cross-device copy size mismatch",
                    detail={
                        "source": str(source),
                        "expected_size": journal.size_bytes,
                        "copied_bytes": copied,
                    },
                )
            if digest.hexdigest() != journal.sha256:
                raise StorageError(
                    "cross-device copy checksum mismatch",
                    detail={"source": str(source), "expected": journal.sha256},
                )
            os.replace(partial, target)
        except OSError as exc:
            raise StorageError(
                "cross-device copy into raw failed",
                detail={"source": str(source), "target": str(target), "reason": str(exc)},
            ) from exc
        finally:
            partial.unlink(missing_ok=True)
        try:
            source.unlink()
        except OSError as exc:
            raise StorageError(
                "target committed but staging source cleanup failed",
                detail={"source": str(source), "reason": str(exc)},
            ) from exc

    def _finalize_commit(self, journal: CommitJournal) -> CommitJournal:
        target = journal.target_path
        if self._read_only:
            try:
                target.chmod(0o444)
            except OSError as exc:
                raise StorageError(
                    "file moved into raw but read-only protection failed",
                    detail={"target": str(target), "reason": str(exc)},
                ) from exc
        return self._update_journal(journal, CommitStatus.COMMITTED)

    # -- 隔离与归档 ----------------------------------------------------- #
    def quarantine(self, path: Path, *, reason: str) -> Path:
        """把文件移入隔离区并写审计侧车，**不删除**。

        命名：``<UTC 时间戳>-<可移植文件名>``；重名时追加 ``-1``、``-2``……
        侧车 ``<隔离文件名>.reason.json`` 记录原始路径、原因、大小与 sha256。
        已在隔离区的文件幂等返回原路径。
        """
        if not isinstance(reason, str) or reason.strip() == "":
            raise StorageError("quarantine reason must be a non-empty string")
        source = self._existing_file(path, field="path")
        if source.is_relative_to(self._quarantine_dir):
            return source
        size_bytes = self._stat_size(source)
        digest = sha256_file(source)
        stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        base = _portable_name(source.name, fallback="quarantined-file")
        target = self._unique_path(self._quarantine_dir / f"{stamp}-{base}")
        self._ensure_dir(self._quarantine_dir, field="quarantine_dir")
        self._move_verified(source, target, size_bytes=size_bytes, sha256_expected=digest)
        payload: dict[str, JsonValue] = {
            "original_path": str(source),
            "reason": reason[:2048],
            "quarantined_at": _utc_now().isoformat(),
            "size_bytes": size_bytes,
            "sha256": digest,
        }
        sidecar = target.with_name(target.name + ".reason.json")
        try:
            sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise StorageError(
                "file quarantined but reason sidecar could not be written",
                detail={"target": str(target), "reason": str(exc)},
            ) from exc
        return target

    def archive(self, path: Path, *, archive_name: str | None = None) -> Path:
        """把文件移入归档区（打包/长期保存产物）。同名且内容相同 → 跳过（保留源）。"""
        source = self._existing_file(path, field="path")
        name = sanitize_file_name(archive_name if archive_name is not None else source.name)
        self._ensure_dir(self._archive_dir, field="archive_dir")
        target = self._archive_dir / name
        size_bytes = self._stat_size(source)
        digest = sha256_file(source)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise StorageError("archive target exists and is not a regular file")
            if self._stat_size(target) == size_bytes and sha256_file(target) == digest:
                return target
            raise StorageError(
                "archive target already exists with different content; refusing to overwrite",
                detail={"target": str(target), "source": str(source)},
            )
        self._move_verified(source, target, size_bytes=size_bytes, sha256_expected=digest)
        return target

    def _existing_file(self, path: Path, *, field: str) -> Path:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            raise StorageError(
                f"{field} must not be a symlink", detail={"field": field, "path": str(candidate)}
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise StorageError(
                f"{field} does not exist",
                detail={"field": field, "path": str(candidate), "reason": str(exc)},
            ) from exc
        if not resolved.is_file():
            raise StorageError(
                f"{field} must be a regular file", detail={"field": field, "path": str(resolved)}
            )
        ensure_within(resolved, self._root, field=field)
        return resolved

    @staticmethod
    def _unique_path(candidate: Path) -> Path:
        path = candidate
        counter = 0
        while path.exists() or path.is_symlink():
            counter += 1
            path = candidate.with_name(f"{candidate.name}-{counter}")
        return path

    def _move_verified(
        self,
        source: Path,
        target: Path,
        *,
        size_bytes: int,
        sha256_expected: str,
    ) -> None:
        """移动并复核（EXDEV 时复制 → fsync → rename → 大小/哈希复核 → 删源）。"""
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise StorageError(
                    "atomic move failed",
                    detail={"source": str(source), "target": str(target), "reason": str(exc)},
                ) from exc
        partial = target.with_name(f"{_PARTIAL_PREFIX}{uuid.uuid4().hex}-{target.name}")
        digest = hashlib.sha256()
        copied = 0
        try:
            with source.open("rb") as source_handle, partial.open("xb") as target_handle:
                while True:
                    chunk = source_handle.read(HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target_handle.write(chunk)
                    copied += len(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if copied != size_bytes or digest.hexdigest() != sha256_expected:
                raise StorageError(
                    "cross-device move verification failed",
                    detail={"source": str(source), "target": str(target)},
                )
            os.replace(partial, target)
        except OSError as exc:
            raise StorageError(
                "cross-device move failed",
                detail={"source": str(source), "target": str(target), "reason": str(exc)},
            ) from exc
        finally:
            partial.unlink(missing_ok=True)
        try:
            source.unlink()
        except OSError as exc:
            raise StorageError(
                "target written but source cleanup failed",
                detail={"source": str(source), "reason": str(exc)},
            ) from exc

    # -- 崩溃恢复与体检 ------------------------------------------------- #
    def journal_path_for(self, raw_path: Path) -> Path:
        """给定正式区路径，返回其提交日志路径（不要求文件存在）。"""
        return self._journal_path(Path(raw_path))

    def _iter_journals(self) -> Iterator[CommitJournal]:
        for journal_path in sorted(self._raw_dir.rglob(f"*{COMMIT_JOURNAL_SUFFIX}")):
            if journal_path.name.startswith(_JOURNAL_TMP_PREFIX):
                continue
            try:
                yield self._read_journal(journal_path)
            except StorageError:
                continue

    def _read_journal(self, journal_path: Path) -> CommitJournal:
        try:
            raw = journal_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(
                "cannot read commit journal",
                detail={"journal": str(journal_path), "reason": str(exc)},
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(
                "commit journal is not valid JSON",
                detail={"journal": str(journal_path), "reason": str(exc)},
            ) from exc
        if not isinstance(payload, dict):
            raise StorageError(
                "commit journal root must be an object", detail={"journal": str(journal_path)}
            )
        converted: dict[str, JsonValue] = {}
        for key, value in payload.items():
            if isinstance(key, str):
                converted[key] = _json_value(value)
        return CommitJournal.from_json(journal_path, converted)

    def pending_commits(self) -> tuple[CommitJournal, ...]:
        """列出所有状态为 ``PENDING`` 的提交日志（供调度器启动时对账）。"""
        return tuple(
            journal for journal in self._iter_journals() if journal.status is CommitStatus.PENDING
        )

    def scan_orphans(
        self,
        *,
        known_raw_paths: Iterable[Path] = (),
        max_temp_age_seconds: float = 86_400.0,
    ) -> OrphanScan:
        """扫描正式区/staging/temp 里的孤儿与残留，只读、不删除任何文件。"""
        known = {Path(path).resolve() for path in known_raw_paths}
        pending = {journal.target_path.resolve(): journal for journal in self.pending_commits()}
        raw_orphans: list[Path] = []
        pending_targets: list[Path] = []
        if self._raw_dir.is_dir():
            for candidate in sorted(self._raw_dir.rglob("*")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if candidate.name.endswith(COMMIT_JOURNAL_SUFFIX) or candidate.name.startswith(
                    (".rdh-journal-", _PARTIAL_PREFIX)
                ):
                    continue
                resolved = candidate.resolve()
                if resolved in known:
                    continue
                if resolved in pending:
                    pending_targets.append(resolved)
                else:
                    raw_orphans.append(resolved)
        referenced_sources = {journal.source_path.resolve() for journal in pending.values()}
        staging_orphans: list[Path] = []
        if self._staging_dir.is_dir():
            for candidate in sorted(self._staging_dir.rglob("*")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                resolved = candidate.resolve()
                if resolved not in referenced_sources:
                    staging_orphans.append(resolved)
        temp_files: list[Path] = []
        if self._temp_dir.is_dir():
            now = _utc_now().timestamp()
            for candidate in sorted(self._temp_dir.rglob("*")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                try:
                    age = now - candidate.stat().st_mtime
                except OSError:
                    continue
                if age >= max_temp_age_seconds:
                    temp_files.append(candidate)
        partial_files: list[Path] = []
        for directory in (self._raw_dir, self._archive_dir, self._quarantine_dir):
            if not directory.is_dir():
                continue
            for candidate in sorted(directory.rglob(f"{_PARTIAL_PREFIX}*")):
                if candidate.is_file():
                    partial_files.append(candidate)
        return OrphanScan(
            raw_orphans=tuple(raw_orphans),
            pending_commit_targets=tuple(pending_targets),
            staging_orphans=tuple(staging_orphans),
            temp_files=tuple(temp_files),
            partial_files=tuple(partial_files),
        )

    def recover_pending_commits(
        self, *, verify_checksum: bool = True
    ) -> tuple[CommitRecovery, ...]:
        """处理 ``raw`` 下的提交日志，修复「文件已移动但数据库未更新」的中间状态。

        决策表（目标 = 正式区文件，源 = 日志记录的 staging 文件）：

        ================  ================  =======================================
        目标存在           源存在            动作
        ================  ================  =======================================
        是（大小/哈希一致） 任意             清理源 + ``FINALIZED``
        是（不一致）        任意             ``NEEDS_ATTENTION``（绝不删除目标）
        否                是               重做移动 + ``RETRIED``
        否                否               ``ABORTED``（无数据可救）
        ================  ================  =======================================

        已 ``COMMITTED`` 的日志不重复处理，但目标缺失会报 ``NEEDS_ATTENTION``——这说明
        正式文件在提交后被外部删除，必须人工介入而不是静默重建。
        """
        results: list[CommitRecovery] = []
        for journal_path in sorted(self._raw_dir.rglob(f"*{COMMIT_JOURNAL_SUFFIX}")):
            if journal_path.name.startswith(_JOURNAL_TMP_PREFIX):
                continue
            try:
                journal = self._read_journal(journal_path)
            except StorageError as exc:
                results.append(
                    CommitRecovery(
                        journal_path=journal_path,
                        action=RecoveryAction.NEEDS_ATTENTION,
                        target_path=None,
                        detail=f"corrupt journal: {exc.message}",
                    )
                )
                continue
            action = self._recover_one(journal, verify_checksum=verify_checksum)
            if action is not None:
                results.append(action)
        return tuple(results)

    def _recover_one(
        self, journal: CommitJournal, *, verify_checksum: bool
    ) -> CommitRecovery | None:
        if journal.status is CommitStatus.COMMITTED:
            if not journal.target_path.exists():
                return CommitRecovery(
                    journal_path=journal.journal_path,
                    action=RecoveryAction.NEEDS_ATTENTION,
                    target_path=journal.target_path,
                    detail=(
                        "journal says committed but the raw target is missing (external deletion?)"
                    ),
                )
            return None
        if journal.status is CommitStatus.ABORTED:
            return None
        if journal.status is CommitStatus.NEEDS_ATTENTION:
            return None
        target = journal.target_path
        source = journal.source_path
        if target.is_symlink():
            return CommitRecovery(
                journal_path=journal.journal_path,
                action=RecoveryAction.NEEDS_ATTENTION,
                target_path=target,
                detail="raw target is a symlink; refusing to touch it",
            )
        if target.exists():
            problem = self._verify_target(target, journal, verify_checksum=verify_checksum)
            if problem is not None:
                return CommitRecovery(
                    journal_path=journal.journal_path,
                    action=RecoveryAction.NEEDS_ATTENTION,
                    target_path=target,
                    detail=problem,
                )
            if source.exists():
                try:
                    source.unlink()
                except OSError as exc:
                    return CommitRecovery(
                        journal_path=journal.journal_path,
                        action=RecoveryAction.NEEDS_ATTENTION,
                        target_path=target,
                        detail=f"target valid but staging source cleanup failed: {exc}",
                    )
            try:
                self._harden(target)
            except StorageError as exc:
                return CommitRecovery(
                    journal_path=journal.journal_path,
                    action=RecoveryAction.NEEDS_ATTENTION,
                    target_path=target,
                    detail=exc.message,
                )
            self._update_journal(journal, CommitStatus.COMMITTED)
            return CommitRecovery(
                journal_path=journal.journal_path,
                action=RecoveryAction.FINALIZED,
                target_path=target,
                detail="target already present and verified; journal finalized",
            )
        if source.exists():
            try:
                validated = self._validated_source(source)
            except StorageError as exc:
                return CommitRecovery(
                    journal_path=journal.journal_path,
                    action=RecoveryAction.NEEDS_ATTENTION,
                    target_path=target,
                    detail=f"staging source rejected: {exc.message}",
                )
            try:
                self._ensure_dir(target.parent, field="raw_dir")
                self._move_into_raw(validated, target, journal)
                self._harden(target)
            except StorageError as exc:
                return CommitRecovery(
                    journal_path=journal.journal_path,
                    action=RecoveryAction.NEEDS_ATTENTION,
                    target_path=target,
                    detail=f"retry failed: {exc.message}",
                )
            self._update_journal(journal, CommitStatus.COMMITTED)
            return CommitRecovery(
                journal_path=journal.journal_path,
                action=RecoveryAction.RETRIED,
                target_path=target,
                detail="moved staging file into raw again",
            )
        self._update_journal(journal, CommitStatus.ABORTED)
        return CommitRecovery(
            journal_path=journal.journal_path,
            action=RecoveryAction.ABORTED,
            target_path=None,
            detail="neither raw target nor staging source exists",
        )

    def _verify_target(
        self, target: Path, journal: CommitJournal, *, verify_checksum: bool
    ) -> str | None:
        if not target.is_file():
            return "raw target exists but is not a regular file"
        try:
            actual_size = self._stat_size(target)
        except StorageError as exc:
            return exc.message
        if actual_size != journal.size_bytes:
            return (
                f"raw target size {actual_size} != journal size {journal.size_bytes}; "
                "refusing to modify it"
            )
        if verify_checksum:
            try:
                actual_digest = sha256_file(target)
            except StorageError as exc:
                return exc.message
            if actual_digest != journal.sha256:
                return "raw target checksum != journal checksum; refusing to modify it"
        return None

    def _harden(self, target: Path) -> None:
        if not self._read_only or not target.exists():
            return
        try:
            target.chmod(0o444)
        except OSError as exc:
            raise StorageError(
                "cannot set read-only protection",
                detail={"target": str(target), "reason": str(exc)},
            ) from exc


# --------------------------------------------------------------------------- #
# 私有工具
# --------------------------------------------------------------------------- #
def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StorageError(
            "cannot hash file", detail={"path": str(path), "reason": str(exc)}
        ) from exc
    return digest.hexdigest()


def _json_value(value: object) -> JsonValue:
    """把 ``json.loads`` 得到的任意值收敛成 :data:`JsonValue`，非 JSON 类型直接拒绝。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        mapping: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StorageError("commit journal keys must be strings")
            mapping[key] = _json_value(item)
        return mapping
    raise StorageError(
        "commit journal contains a non-JSON value", detail={"type": type(value).__name__}
    )
