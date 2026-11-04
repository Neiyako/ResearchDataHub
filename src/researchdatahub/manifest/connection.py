"""SQLite 连接工厂与写事务协议（ADR-0003 规则 1/2）。

为什么需要工厂
--------------
:class:`sqlite3.Connection` **不可跨线程共享**（``check_same_thread=True`` 是默认值，
这里刻意保留）。调度器在 ``ThreadPoolExecutor`` 里跑传输，每个 worker 线程必须
用 :meth:`ManifestConnectionFactory.connect` 为自己新建一条连接，任务结束再关闭。

写事务
------
写操作统一走 :meth:`ManifestConnectionFactory.run_write`：

1. ``BEGIN IMMEDIATE``：立刻拿写锁，避免“先读后写”被其它连接插队
   （deferred 事务读到旧快照后再升级写锁会直接 ``SQLITE_BUSY``）；
2. 执行调用方提供的操作；
3. ``COMMIT``；任何异常先 ``ROLLBACK`` 再向上抛。

遇到 ``SQLITE_BUSY`` / ``database is locked`` 时按指数退避重试
（次数来自 ``config.defaults.manifest.write_retry_attempts``），其它 SQL 错误立即
转成 :class:`~researchdatahub.core.errors.ManifestError`，绝不吞掉。

PRAGMA 预算
-----------
每条连接创建时都会设置并**校验**：

* ``journal_mode=WAL``：读不阻塞写（ADR-0003 §6），M1 的硬性要求；不是 WAL 就报错。
* ``foreign_keys=ON``：外键约束是数据库级的事实保证，不能靠 Python 自觉。
* ``busy_timeout=...``：与重试策略配合，先让 SQLite 自己等，再让我们退避。
* ``synchronous=NORMAL|FULL``：由 ``ManifestConfig`` 决定，WAL 下 NORMAL 是常用折中。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Final, TypeVar

from researchdatahub.config.models import ManifestConfig
from researchdatahub.core.errors import ManifestError
from researchdatahub.manifest.schema import MIGRATIONS, SCHEMA_VERSION, Migration, latest_version

__all__ = [
    "ManifestConnectionFactory",
    "MigrationReport",
    "read_schema_version",
    "rollback_if_needed",
]

T = TypeVar("T")

#: 退避基数与上限（秒）：次数由配置控制，单次等待有上界，避免测试与 CLI 卡死。
_BACKOFF_BASE_SECONDS: Final[float] = 0.02
_BACKOFF_MAX_SECONDS: Final[float] = 0.5

_BUSY_MARKERS: Final[tuple[str, ...]] = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


@dataclass(frozen=True)
class MigrationReport:
    """一次 ``initialize`` / ``migrate`` 的结果（可打印、可审计）。

    Parameters
    ----------
    db_path:
        被迁移的数据库文件。
    previous_version:
        迁移前版本（0 表示全新库）。
    schema_version:
        迁移后版本。
    applied:
        本次实际应用的迁移（已是最新时为空元组）。
    """

    db_path: Path
    previous_version: int
    schema_version: int
    applied: tuple[Migration, ...] = ()


def read_schema_version(connection: sqlite3.Connection) -> int:
    """读取当前 schema 版本；``schema_version`` 表不存在时返回 0。

    Parameters
    ----------
    connection:
        已完成 PRAGMA 设置的连接。

    Returns
    -------
    int
        已应用的最高迁移版本号，未初始化时为 ``0``。
    """
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row is None:  # pragma: no cover - 聚合查询在空表上也返回一行
        return 0
    raw = row[0]
    return 0 if raw is None else int(raw)


def rollback_if_needed(connection: sqlite3.Connection) -> None:
    """回滚当前事务（若处于事务中）；连接不可用时转成 :class:`ManifestError`。

    失败时不能静默：回滚失败意味着连接状态未知，继续读写会把不确定性带进事实库。
    """
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error as exc:  # pragma: no cover - 连接损坏时才会发生
        raise ManifestError(f"rollback failed: {exc}") from exc


def _is_busy(error: sqlite3.OperationalError) -> bool:
    """判断一个 ``OperationalError`` 是否属于可重试的锁冲突。"""
    message = str(error).lower()
    return any(marker in message for marker in _BUSY_MARKERS)


class ManifestConnectionFactory:
    """为当前线程创建已配置好的 SQLite 连接，并负责 schema 迁移。

    Parameters
    ----------
    db_path:
        SQLite 文件路径（必须是具体文件，不允许 ``:memory:``：Manifest 的事实来源
        必须能在进程重启后存活）。
    config:
        ``config.defaults.manifest`` 段；缺省时使用 ``ManifestConfig`` 默认值。
    """

    def __init__(self, db_path: Path, *, config: ManifestConfig | None = None) -> None:
        if not Path(db_path).name:
            raise ManifestError(f"invalid manifest db path: {db_path!r}")
        self._db_path = Path(db_path)
        self._config = config if config is not None else ManifestConfig()

    # -- 只读属性 ---------------------------------------------------------- #
    @property
    def db_path(self) -> Path:
        """数据库文件路径。"""
        return self._db_path

    @property
    def config(self) -> ManifestConfig:
        """本工厂使用的 Manifest 配置。"""
        return self._config

    # -- 连接 -------------------------------------------------------------- #
    def connect(self) -> sqlite3.Connection:
        """为**当前线程**新建一条连接，并应用/校验全部 PRAGMA。

        Returns
        -------
        sqlite3.Connection
            新连接；调用方负责 ``close()``。``check_same_thread=True``（默认值）意味着
            它只能在创建它的线程里使用。

        Raises
        ------
        ManifestError
            父目录不存在、无法打开文件，或 ``journal_mode`` 不是 WAL。
        """
        parent = self._db_path.parent
        if not parent.exists():
            raise ManifestError(
                f"manifest database directory does not exist: {parent}",
                detail={"db_path": str(self._db_path), "hint": "run 'rdh db init'"},
            )
        try:
            connection = sqlite3.connect(
                self._db_path,
                timeout=self._config.busy_timeout_ms / 1000.0,
                isolation_level=None,  # 显式事务：BEGIN IMMEDIATE / COMMIT 由 run_write 控制
                check_same_thread=True,
            )
        except sqlite3.Error as exc:  # pragma: no cover - 依赖文件系统状态
            raise ManifestError(
                f"cannot open manifest database {self._db_path}: {exc}",
                detail={"db_path": str(self._db_path)},
            ) from exc
        try:
            connection.row_factory = sqlite3.Row
            # PRAGMA 不支持参数占位符；这里只拼接经过校验的整数/字面量，不拼接用户输入。
            connection.execute(f"PRAGMA busy_timeout = {int(self._config.busy_timeout_ms)}")
            connection.execute(f"PRAGMA synchronous = {self._config.synchronous.upper()}")
            journal_row = connection.execute(
                f"PRAGMA journal_mode = {self._config.journal_mode.upper()}"
            ).fetchone()
            journal_mode = "" if journal_row is None else str(journal_row[0]).lower()
            if journal_mode != "wal":
                raise ManifestError(
                    f"manifest database is not in WAL mode (got {journal_mode!r})",
                    detail={"db_path": str(self._db_path), "journal_mode": journal_mode},
                )
            connection.execute("PRAGMA foreign_keys = ON")
            fk_row = connection.execute("PRAGMA foreign_keys").fetchone()
            foreign_keys = bool(fk_row is not None and int(fk_row[0]) == 1)
            if not foreign_keys:  # pragma: no cover - 只有极端构建才会失败
                raise ManifestError(
                    "SQLite build does not support PRAGMA foreign_keys=ON",
                    detail={"db_path": str(self._db_path)},
                )
        except sqlite3.Error as exc:  # pragma: no cover - PRAGMA 失败依赖底层构建
            connection.close()
            raise ManifestError(
                f"cannot configure manifest database {self._db_path}: {exc}",
                detail={"db_path": str(self._db_path)},
            ) from exc
        except BaseException:
            connection.close()
            raise
        return connection

    # -- 迁移 -------------------------------------------------------------- #
    def migrate(self) -> MigrationReport:
        """应用所有待执行迁移（幂等），返回可打印的迁移报告。

        Raises
        ------
        ManifestError
            迁移版本不连续、迁移语句失败（整体回滚）。
        """
        if latest_version() != SCHEMA_VERSION:  # pragma: no cover - 定义期自检
            raise ManifestError("migration table is inconsistent with SCHEMA_VERSION")
        connection = self.connect()
        try:
            previous = read_schema_version(connection)
            applied: list[Migration] = []
            for migration in MIGRATIONS:
                applied_here = self.run_write(
                    partial(self._apply_migration, migration=migration),
                    connection=connection,
                )
                if applied_here:
                    applied.append(migration)
            current = read_schema_version(connection)
            if current != SCHEMA_VERSION:
                raise ManifestError(
                    f"schema migration ended at v{current}, expected v{SCHEMA_VERSION}",
                    detail={"db_path": str(self._db_path)},
                )
            return MigrationReport(
                db_path=self._db_path,
                previous_version=previous,
                schema_version=current,
                applied=tuple(applied),
            )
        finally:
            connection.close()

    def initialize(self) -> None:
        """创建/升级 schema（幂等，契约 :meth:`ManifestRepository.initialize`）。"""
        self.migrate()

    def _apply_migration(self, connection: sqlite3.Connection, migration: Migration) -> bool:
        """在调用方已开启的写事务内应用一个迁移；已应用过则跳过并返回 ``False``。"""
        current = read_schema_version(connection)
        if migration.version <= current:
            return False
        if migration.version != current + 1:
            raise ManifestError(
                f"cannot apply migration v{migration.version} on top of v{current}: "
                "migrations must be applied in order",
                detail={"db_path": str(self._db_path), "current": current},
            )
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.description, _now_iso()),
        )
        return True

    # -- 写事务 ------------------------------------------------------------ #
    def run_write(
        self,
        operation: Callable[[sqlite3.Connection], T],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> T:
        """在 ``BEGIN IMMEDIATE`` 写事务内执行 ``operation``，冲突时退避重试。

        Parameters
        ----------
        operation:
            接收已开启写事务的连接，返回值原样返回。**重试会重新执行该函数**，
            因此它必须只做数据库写操作，不能在成功前产生外部副作用。
        connection:
            使用的连接；``None`` 时为本线程新建一条并在结束后关闭。

        Returns
        -------
        T
            ``operation`` 的返回值。

        Raises
        ------
        ManifestError
            重试耗尽（锁冲突）或任何非可重试的 SQL 错误。
        """
        owned = connection is None
        target = connection if connection is not None else self.connect()
        attempts = int(self._config.write_retry_attempts)
        try:
            for attempt in range(attempts):
                try:
                    target.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if _is_busy(exc) and attempt + 1 < attempts:
                        self._sleep_before_retry(attempt)
                        continue
                    raise ManifestError(
                        f"could not start write transaction: {exc}",
                        detail={"db_path": str(self._db_path), "attempts": attempt + 1},
                    ) from exc
                except sqlite3.Error as exc:
                    raise ManifestError(
                        f"could not start write transaction: {exc}",
                        detail={"db_path": str(self._db_path)},
                    ) from exc
                try:
                    result = operation(target)
                except BaseException:
                    rollback_if_needed(target)
                    raise
                try:
                    target.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    rollback_if_needed(target)
                    if _is_busy(exc) and attempt + 1 < attempts:
                        self._sleep_before_retry(attempt)
                        continue
                    raise ManifestError(
                        f"could not commit write transaction: {exc}",
                        detail={"db_path": str(self._db_path), "attempts": attempt + 1},
                    ) from exc
                except sqlite3.Error as exc:
                    rollback_if_needed(target)
                    raise ManifestError(
                        f"could not commit write transaction: {exc}",
                        detail={"db_path": str(self._db_path)},
                    ) from exc
                return result
            raise ManifestError(  # pragma: no cover - 循环内已抛，仅类型收窄
                "write transaction retry budget exhausted",
                detail={"db_path": str(self._db_path)},
            )
        finally:
            if owned:
                target.close()

    @staticmethod
    def _sleep_before_retry(attempt: int) -> None:
        """指数退避：0.02s, 0.04s, ... 上限 0.5s。"""
        delay = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
        time.sleep(delay)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（迁移账本用）。"""
    return datetime.now(UTC).isoformat()
