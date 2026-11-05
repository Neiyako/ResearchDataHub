"""Worker 注册表与链路统计的 SQLite 持久化（独立库文件 ``~/.rdh/workers.db``）。

为什么单独一个库
----------------
* ``manifest.db`` 是"任务/文件/证据"的事实来源，由 :mod:`researchdatahub.manifest` 独占
  管理（schema 迁移、单写者、WAL 风格）。节点注册与链路性能是**控制面**数据，两者写入
  频率与生命周期都不同；混进同一库会让控制面的高频心跳写放大 Manifest 的写锁争用。
* 本模块自带迁移表（``worker_schema_version``），不依赖也不修改 ``manifest/schema.py``。

连接与事务风格（与 ``manifest/connection.py`` 一致）
---------------------------------------------------
* 每条连接**只属于创建它的线程**（``check_same_thread=True``）；注册表内部用锁串行化
  写操作，读操作各自开短连接；
* 写操作统一 ``BEGIN IMMEDIATE`` → 操作 → ``COMMIT``，``SQLITE_BUSY`` 指数退避；
* ``journal_mode=WAL`` + ``foreign_keys=ON`` + ``busy_timeout``，与 Manifest 库同口径。

表结构
------
``workers``
    ``worker_id`` 主键；能力与健康以 JSON 列存储（能力是可变长度的白名单，拆表会使
    "空列表 = 不限制"的语义变复杂）；``last_heartbeat_at`` 单独成列以便按时间索引。
``worker_events``
    追加式审计：``registered`` / ``heartbeat`` / ``marked_offline`` / ``deregistered`` /
    ``transferred`` / ``transport_failed``，一条事件一行，永不更新。
``route_stats``
    按 ``(route_id, source_id)`` 聚合的**实测**链路样本（成功/失败/重试次数、字节数、
    耗时、延迟），以及每条样本的 ``sample_latency_ms`` / ``sample_duration_s`` 滑动累计
    所需的分母列。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeVar

from researchdatahub.core.errors import ManifestError

__all__ = [
    "WORKERS_SCHEMA_VERSION",
    "WorkerStore",
    "WorkerStoreError",
    "connect_workers_db",
    "json_dumps",
    "utc_iso",
]

T = TypeVar("T")

WORKERS_SCHEMA_VERSION: Final[int] = 1
"""当前控制面库的 schema 版本；新增迁移时递增并追加到 ``_MIGRATIONS``。"""

_BUSY_MARKERS: Final[tuple[str, ...]] = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)
_BACKOFF_BASE_SECONDS: Final[float] = 0.02
_BACKOFF_MAX_SECONDS: Final[float] = 0.5
_WRITE_ATTEMPTS: Final[int] = 5


class WorkerStoreError(ManifestError):
    """控制面库（workers.db）读写失败。继承 :class:`ManifestError` 以复用统一失败语义。"""

    default_code = "RDH_WORKER_STORE"


_MIGRATIONS: Final[tuple[tuple[int, str, tuple[str, ...]], ...]] = (
    (
        1,
        "worker registry, audit events and measured route statistics",
        (
            """
            CREATE TABLE IF NOT EXISTS worker_schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT    NOT NULL,
                applied_at  TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_id          TEXT PRIMARY KEY,
                kind               TEXT NOT NULL,
                region             TEXT,
                endpoint           TEXT,
                version            TEXT NOT NULL,
                capabilities_json  TEXT NOT NULL,
                status             TEXT NOT NULL,
                last_heartbeat_at  TEXT,
                registered_at      TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS worker_events (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_worker_events_worker "
            "ON worker_events (worker_id, event_id)",
            """
            CREATE TABLE IF NOT EXISTS route_stats (
                route_id              TEXT NOT NULL,
                source_id             TEXT NOT NULL,
                samples               INTEGER NOT NULL DEFAULT 0,
                successes             INTEGER NOT NULL DEFAULT 0,
                failures              INTEGER NOT NULL DEFAULT 0,
                retry_count           INTEGER NOT NULL DEFAULT 0,
                transferred_bytes     INTEGER NOT NULL DEFAULT 0,
                latency_ms_sum        REAL    NOT NULL DEFAULT 0,
                transfer_duration_s   REAL    NOT NULL DEFAULT 0,
                last_outcome          TEXT,
                last_success_at       TEXT,
                last_failure_at       TEXT,
                updated_at            TEXT    NOT NULL,
                PRIMARY KEY (route_id, source_id)
            )
            """,
        ),
    ),
)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(UTC).isoformat()


def utc_iso(moment: datetime) -> str:
    """把任意 tz-aware 时间归一成 UTC ISO 字符串（库内时间列的**唯一**写法）。"""
    return moment.astimezone(UTC).isoformat()


def connect_workers_db(path: Path, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    """为**当前线程**打开一条已配置好的控制面库连接（父目录自动创建）。

    Raises
    ------
    WorkerStoreError
        目录无法创建、无法打开文件，或 ``journal_mode`` 不是 WAL。
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkerStoreError(
            f"cannot create workers database directory {target.parent}",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    try:
        connection = sqlite3.connect(
            target, timeout=timeout_seconds, isolation_level=None, check_same_thread=True
        )
    except sqlite3.Error as exc:  # pragma: no cover - 依赖文件系统状态
        raise WorkerStoreError(
            f"cannot open workers database {target}",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        mode = "" if row is None else str(row[0]).lower()
        if mode != "wal":
            raise WorkerStoreError(
                f"workers database is not in WAL mode (got {mode!r})",
                detail={"path": str(target)},
            )
        connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:  # pragma: no cover - 依赖底层构建
        connection.close()
        raise WorkerStoreError(
            f"cannot configure workers database {target}",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    except BaseException:
        connection.close()
        raise
    return connection


class WorkerStore:
    """控制面库句柄：建库迁移、写事务、按线程连接。

    Parameters
    ----------
    db_path:
        SQLite 文件路径（必须是具体文件，不允许 ``:memory:``：进程重启后必须能恢复）。
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._write_lock = threading.Lock()

    # -- 属性 -------------------------------------------------------------- #
    @property
    def db_path(self) -> Path:
        """数据库文件路径。"""
        return self._db_path

    # -- 迁移 -------------------------------------------------------------- #
    def initialize(self) -> int:
        """创建/升级 schema（幂等），返回迁移后的版本号。"""
        with self._write_connection() as connection:
            applied = 0
            for version, description, statements in _MIGRATIONS:
                current = self._version(connection)
                if version <= current:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR REPLACE INTO worker_schema_version "
                    "(version, description, applied_at) VALUES (?, ?, ?)",
                    (version, description, _now_iso()),
                )
                applied = version
            current_version = self._version(connection)
        expected = WORKERS_SCHEMA_VERSION
        if current_version < expected:  # pragma: no cover - 迁移表定义自检
            raise WorkerStoreError(
                f"workers schema ended at v{current_version}, expected v{expected}",
                detail={"applied": applied},
            )
        return current_version

    @staticmethod
    def _version(connection: sqlite3.Connection) -> int:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_schema_version'"
        ).fetchone()
        if table is None:
            return 0
        row = connection.execute("SELECT MAX(version) FROM worker_schema_version").fetchone()
        if row is None:  # pragma: no cover - 聚合查询在空表上也返回一行
            return 0
        raw = row[0]
        return 0 if raw is None else int(raw)

    # -- 事务 -------------------------------------------------------------- #
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """在 ``BEGIN IMMEDIATE`` 写事务内执行操作；异常回滚后原样抛出。"""
        connection = connect_workers_db(self._db_path)
        try:
            self._begin(connection)
            try:
                yield connection
            except BaseException:
                self._rollback(connection)
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """序列化写事务（进程内多线程心跳与注册不会互相抢写锁）。"""
        with self._write_lock, self.write() as connection:
            yield connection

    def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """用一条短连接执行只读操作（连接用完即关，不跨线程复用）。"""
        connection = connect_workers_db(self._db_path)
        try:
            return operation(connection)
        finally:
            connection.close()

    def _begin(self, connection: sqlite3.Connection) -> None:
        for attempt in range(_WRITE_ATTEMPTS):
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if self._is_busy(exc) and attempt + 1 < _WRITE_ATTEMPTS:
                    time.sleep(min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS))
                    continue
                raise WorkerStoreError(
                    f"could not start workers write transaction: {exc}",
                    detail={"db_path": str(self._db_path)},
                ) from exc

    def _rollback(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 连接损坏时才会发生
            return

    @staticmethod
    def _is_busy(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in _BUSY_MARKERS)

    # -- 便捷读取（注册表/统计实现共用） ------------------------------------ #
    def all_worker_rows(self) -> Sequence[sqlite3.Row]:
        """读取全部 worker 行（按 ``worker_id`` 升序）。"""
        return self.read(
            lambda connection: connection.execute(
                "SELECT * FROM workers ORDER BY worker_id"
            ).fetchall()
        )

    def worker_row(self, worker_id: str) -> sqlite3.Row | None:
        """读取单个 worker 行；不存在返回 ``None``。"""
        row: sqlite3.Row | None = self.read(
            lambda connection: connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        )
        return row

    def events(self, worker_id: str, *, limit: int | None = None) -> Sequence[tuple[str, str]]:
        """读取某个 worker 的审计事件 ``(event_type, occurred_at)``，按写入顺序。"""
        sql = (
            "SELECT event_type, occurred_at FROM worker_events "
            "WHERE worker_id = ? ORDER BY event_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params: tuple[object, ...] = (worker_id, int(limit))
        else:
            params = (worker_id,)
        return self.read(
            lambda connection: [
                (str(row["event_type"]), str(row["occurred_at"]))
                for row in connection.execute(sql, params).fetchall()
            ]
        )


def json_dumps(payload: object) -> str:
    """稳定的 JSON 序列化（``sort_keys`` 保证同一语义只有一个字节串）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
