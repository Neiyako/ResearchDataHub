"""``ManifestRepository`` 的 SQLite 实现（ADR-0003 的事实来源）。

职责
----
* 任务/租约：原子领取、心跳续约、过期回收、乐观并发状态推进；
* 目录数据：数据源、数据集、请求、资产、计划、文件、完整性报告、审计事件；
* 交付：从**已完成的文件记录**生成 :class:`DeliveryManifest`（绝不扫描目录）；
* 观测：``status()`` 给 ``rdh db status`` 提供可观测事实。

并发模型
--------
* 一个仓库实例 = 一条连接 = 一个线程（``check_same_thread=True``）。跨线程共享会
  由 ``sqlite3`` 直接抛 ``ProgrammingError``，本模块刻意不屏蔽它。
* 所有写操作走 :meth:`ManifestConnectionFactory.run_write`（``BEGIN IMMEDIATE`` +
  退避重试）。因此 ``claim_tasks`` 里“查候选 → 逐条推进”的读-改-写是**串行**的：
  拿到写锁后其它连接不可能插进来提交，两个 worker 不可能领到同一个任务。
* 状态推进只调用 :func:`researchdatahub.core.state.transition`；仓库层额外用
  :func:`~researchdatahub.core.state.can_transition` 复核“数据库里当前状态 → 新状态”
  的合法性，绕过状态机直接改 ``state`` 会被 :class:`StateTransitionError` 拦住。

退化行为（明确记录，不静默）
----------------------------
* ``release_expired`` 对“租约过期但心跳未超时”的任务**续租而不接管**，返回的
  :class:`RecoveryResult` 满足 ``previous_state == new_state``（reason 以
  ``lease expired but heartbeat fresh`` 开头）；心跳超时的任务按
  :func:`~researchdatahub.core.state.recover_stale` 处理并写审计事件。
* 恢复为 ``RETRY_WAIT`` 的任务会把 ``scheduled_at`` 置为当前时间，使其**立即可领取**；
  退避策略由调度器在推进状态时覆盖，不由恢复流程猜测。
* ``save_plan`` 是幂等重放：已存在的任务**不会被降级**（不覆盖已推进的状态），
  只有不存在的任务按计划内容插入。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Final

import researchdatahub
from researchdatahub.core.errors import ManifestError, StateTransitionError
from researchdatahub.core.models import (
    AcquisitionPlan,
    AcquisitionTask,
    DatabaseStatus,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
    FileRecord,
    IntegrityReport,
    IntegrityStatus,
    RecoveryResult,
    SourceAsset,
    SourceMetadata,
    TransferLogEvent,
)
from researchdatahub.core.state import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    TaskState,
    can_transition,
    is_stale,
    lease_expired,
    recover_stale,
    renew_lease,
    transition,
)
from researchdatahub.manifest.connection import (
    ManifestConnectionFactory,
    MigrationReport,
    read_schema_version,
    rollback_if_needed,
)
from researchdatahub.manifest.rows import (
    TASK_SELECT,
    asset_from_row,
    event_from_row,
    file_record_from_row,
    from_json_dict,
    from_json_text,
    integrity_report_from_row,
    normalize_now,
    source_from_row,
    task_from_row,
    to_bool,
    to_iso,
    to_json_text,
    to_optional_iso,
    utc_now,
    validated,
)

__all__ = ["SqliteManifestRepository"]

#: 恢复为 RETRY_WAIT 时使用的排程基准：'1970...' 让无 scheduled_at 的 QUEUED 任务排在
#: 有时间戳的任务之后（SQLite 里 TEXT 按字典序比较，ISO 8601 正好可比）。
_EPOCH_ISO: Final[str] = "1970-01-01T00:00:00+00:00"

#: 单条任务的 UPDATE 语句。列顺序与 :func:`_task_params` 严格一致。
_UPDATE_TASK_SQL: Final[str] = """
UPDATE tasks SET
    plan_id = ?, request_id = ?, asset_id = ?, state = ?, priority = ?, attempt = ?,
    max_attempts = ?, scheduled_at = ?, started_at = ?, finished_at = ?,
    last_transition_at = ?, last_error = ?, worker_id = ?, heartbeat_at = ?,
    lease_expires_at = ?, notes = ?, updated_at = ?
WHERE task_id = ?
"""

#: 单条任务的 INSERT 语句（含 ``created_at``，重放时保持首次写入时间）。
_INSERT_TASK_SQL: Final[str] = """
INSERT INTO tasks (
    task_id, plan_id, request_id, asset_id, state, priority, attempt, max_attempts,
    scheduled_at, started_at, finished_at, last_transition_at, last_error,
    worker_id, heartbeat_at, lease_expires_at, notes, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_TASK_SQL_BY_STATE: Final[str] = _UPDATE_TASK_SQL + " AND state = ?"


def _tool_version() -> str:
    """当前工具版本（写进交付清单，用于复现）。"""
    return researchdatahub.__version__


def _delivery_id(request_id: str) -> str:
    """由请求 ID 派生稳定的交付 ID（确定性，重跑不产生新 ID）。"""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    return f"dlv-{digest}"


class SqliteManifestRepository:
    """SQLite 版 :class:`~researchdatahub.core.ports.ManifestRepository`。

    Parameters
    ----------
    factory:
        连接工厂（持有路径与 ``ManifestConfig``）。本实例会立刻为**当前线程**打开一条连接。
    connection:
        可选的既有连接（测试/上层复用）；为 ``None`` 时新建。

    Raises
    ------
    ManifestError
        无法打开数据库或 PRAGMA 设置失败。
    """

    def __init__(
        self,
        factory: ManifestConnectionFactory,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._factory = factory
        self._connection: sqlite3.Connection | None = (
            connection if connection is not None else factory.connect()
        )

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def factory(self) -> ManifestConnectionFactory:
        """本仓库使用的连接工厂（每个 worker 线程据此新建自己的连接）。"""
        return self._factory

    @property
    def db_path(self) -> str:
        """数据库文件路径（字符串形式，便于 CLI 展示）。"""
        return str(self._factory.db_path)

    def __enter__(self) -> SqliteManifestRepository:
        """上下文管理器入口：返回自身。"""
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """上下文管理器出口：关闭连接（幂等）。"""
        self.close()

    def close(self) -> None:
        """关闭连接；幂等（重复调用不报错）。"""
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def initialize(self) -> None:
        """创建/升级 schema（幂等）。

        迁移使用工厂自己的短生命周期连接，因此即使本实例的连接是旧快照，
        迁移完成后所有连接都能看到新表（SQLite schema cookie 会推进）。
        """
        self._require_open()
        self._factory.initialize()

    def migrate(self) -> MigrationReport:
        """执行待应用迁移并返回报告（``rdh db migrate`` 的数据来源）。"""
        self._require_open()
        return self._factory.migrate()

    @property
    def schema_version(self) -> int:
        """当前库的 schema 版本；未初始化（无 ``schema_version`` 表）时返回 0。"""
        with self._guard() as connection:
            return read_schema_version(connection)

    def status(self) -> DatabaseStatus:
        """返回 :class:`DatabaseStatus` 快照（``rdh db status`` 的数据来源）。

        Raises
        ------
        ManifestError
            数据库尚未初始化。
        """
        with self._snapshot() as connection:
            version = read_schema_version(connection)
            if version < 1:
                raise ManifestError(
                    f"manifest database {self._factory.db_path} is not initialized",
                    detail={"db_path": str(self._factory.db_path), "hint": "run 'rdh db init'"},
                )
            journal_row = connection.execute("PRAGMA journal_mode").fetchone()
            foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
            counts = {state.value: 0 for state in TaskState}
            for row in connection.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state"):
                counts[str(row[0])] = int(row[1])
            file_row = connection.execute("SELECT COUNT(*) FROM files").fetchone()
            failed_row = connection.execute(
                "SELECT COUNT(*) FROM integrity_reports WHERE status = 'failed'"
            ).fetchone()
            return DatabaseStatus(
                db_path=str(self._factory.db_path),
                db_schema_version=version,
                journal_mode="" if journal_row is None else str(journal_row[0]).lower(),
                foreign_keys=bool(foreign_keys_row is not None and int(foreign_keys_row[0]) == 1),
                task_counts=counts,
                file_count=0 if file_row is None else int(file_row[0]),
                integrity_failures=0 if failed_row is None else int(failed_row[0]),
                delivery_ready_requests=self._delivery_ready_requests(connection),
            )

    # -- 任务与租约 -------------------------------------------------------- #
    def save_task(self, task: AcquisitionTask, *, expected_state: TaskState | None = None) -> None:
        """写入任务当前状态；``expected_state`` 提供乐观并发控制。

        Raises
        ------
        ManifestError
            任务不存在（指定 ``expected_state`` 时）、状态冲突或事务失败。
        StateTransitionError
            数据库中的当前状态无法合法推进到传入状态（绕过状态机直接改 state）。
        """
        with self._guard():
            self._factory.run_write(
                lambda connection: self._write_task(
                    connection, task, expected_state=expected_state
                ),
                connection=self._connection_or_raise(),
            )

    def save_task_and_event(
        self,
        task: AcquisitionTask,
        *,
        expected_state: TaskState | None = None,
        event: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """**同一事务**内写入任务新状态与一条审计事件。

        Parameters
        ----------
        task:
            已通过状态机推进的新任务对象。
        expected_state:
            乐观并发期望的数据库当前状态。
        event:
            机器可读事件类型。
        payload:
            事件附加信息；必须可 JSON 序列化，不能序列化会抛 :class:`ManifestError`。
        """

        def operation(connection: sqlite3.Connection) -> None:
            self._write_task(connection, task, expected_state=expected_state)
            self._append_event_conn(connection, task.task_id, event, payload)

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def load_task(self, task_id: str) -> AcquisitionTask:
        """读取任务快照。

        Raises
        ------
        ManifestError
            任务不存在。
        """
        with self._guard() as connection:
            return self._load_task_by_id(connection, task_id)

    def list_tasks(
        self,
        *,
        state: TaskState | None = None,
        request_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Iterator[AcquisitionTask]:
        """惰性流式列出任务（查询一次、逐行解码，不一次性载入内存）。

        Raises
        ------
        ManifestError
            ``limit`` / ``offset`` 非法。
        """
        if limit is not None and limit < 0:
            raise ManifestError(f"list_tasks limit must be >= 0, got {limit}")
        if offset < 0:
            raise ManifestError(f"list_tasks offset must be >= 0, got {offset}")
        sql = TASK_SELECT
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("t.state = ?")
            params.append(state.value)
        if request_id is not None:
            clauses.append("t.request_id = ?")
            params.append(request_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.task_id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        with self._guard() as connection:
            cursor = connection.execute(sql, tuple(params))
            for row in cursor:
                yield task_from_row(row)

    def claim_tasks(
        self,
        *,
        owner: str,
        limit: int,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        request_id: str | None = None,
    ) -> Sequence[AcquisitionTask]:
        """原子领取至多 ``limit`` 个可执行任务（``QUEUED`` 或到期的 ``RETRY_WAIT``）。

        实现方式：单个 ``BEGIN IMMEDIATE`` 事务内“查候选 → :func:`transition` →
        ``UPDATE ... WHERE task_id=? AND state=?``”。写锁保证读到的候选不会被其它
        连接抢先提交，因此同一个任务不会被两个 worker 领取。

        ``RETRY_WAIT`` 候选会先按状态机回到 ``QUEUED``（``RETRY_WAIT -> RUNNING``
        不是合法边），再推进到 ``RUNNING``；两步都在同一事务内，``attempt`` 只自增一次。

        Raises
        ------
        ManifestError
            ``owner`` 为空、``limit``/``lease_seconds`` 非法或事务失败。
        """
        if not owner.strip():
            raise ManifestError("claim_tasks requires a non-empty owner")
        if len(owner) > 64:
            raise ManifestError(f"owner must be at most 64 characters, got {len(owner)}")
        if limit < 1:
            raise ManifestError(f"claim_tasks limit must be >= 1, got {limit}")
        if lease_seconds < 1:
            raise ManifestError(f"lease_seconds must be >= 1, got {lease_seconds}")
        stamp = normalize_now(now)

        def operation(connection: sqlite3.Connection) -> Sequence[AcquisitionTask]:
            sql = (
                "SELECT t.task_id FROM tasks AS t WHERE "
                "(t.state = ? OR (t.state = ? AND t.scheduled_at IS NOT NULL "
                "AND t.scheduled_at <= ?))"
            )
            params: list[object] = [
                TaskState.QUEUED.value,
                TaskState.RETRY_WAIT.value,
                to_iso(stamp),
            ]
            if request_id is not None:
                sql += " AND t.request_id = ?"
                params.append(request_id)
            sql += (
                " ORDER BY t.priority DESC, COALESCE(t.scheduled_at, ?) ASC, t.task_id ASC LIMIT ?"
            )
            params.extend([_EPOCH_ISO, limit])
            candidate_ids = [
                str(row[0]) for row in connection.execute(sql, tuple(params)).fetchall()
            ]
            claimed: list[AcquisitionTask] = []
            for task_id in candidate_ids:
                task = self._load_task_by_id(connection, task_id)
                if task.state is TaskState.RETRY_WAIT:
                    # 状态机里 RETRY_WAIT 只能先回 QUEUED（RETRY_WAIT -> RUNNING 不是合法边）。
                    # 两次 transition 都发生在同一个写事务内，attempt 只由 RUNNING 转换自增一次。
                    requeued = transition(
                        task,
                        TaskState.QUEUED,
                        reason="retry delay elapsed; re-queued for claim",
                        now=stamp,
                    )
                    self._write_task(connection, requeued, expected_state=TaskState.RETRY_WAIT)
                    task = requeued
                claimed_task = transition(
                    task,
                    TaskState.RUNNING,
                    reason=f"claimed by worker {owner!r}",
                    now=stamp,
                    worker_id=owner,
                    lease_seconds=lease_seconds,
                )
                self._write_task(connection, claimed_task, expected_state=task.state)
                self._append_event_conn(
                    connection,
                    task_id,
                    "task_claimed",
                    {
                        "owner": owner,
                        "lease_expires_at": to_optional_iso(claimed_task.lease_expires_at),
                        "attempt": claimed_task.attempt,
                    },
                )
                claimed.append(claimed_task)
            return claimed

        with self._guard():
            return self._factory.run_write(operation, connection=self._connection_or_raise())

    def heartbeat(
        self,
        task_id: str,
        *,
        owner: str,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """续约；``False`` 表示调用方已失去执行权，必须停止执行。

        ``False`` 的三种情况：任务不存在、任务不在 ``RUNNING``/``VERIFYING``、
        租约持有者不是 ``owner``、或租约已经过期（另一个 worker 可能已经接管）。
        """
        if lease_seconds < 1:
            raise ManifestError(f"lease_seconds must be >= 1, got {lease_seconds}")
        stamp = normalize_now(now)

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(TASK_SELECT + " WHERE t.task_id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            task = task_from_row(row)
            if task.state not in (TaskState.RUNNING, TaskState.VERIFYING):
                return False
            if task.worker_id != owner:
                return False
            if lease_expired(task, now=stamp):
                return False
            renewed = renew_lease(task, now=stamp, lease_seconds=lease_seconds)
            self._write_task(connection, renewed, expected_state=task.state)
            return True

        with self._guard():
            return self._factory.run_write(operation, connection=self._connection_or_raise())

    def release_expired(
        self,
        *,
        now: datetime | None = None,
        stale_timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Sequence[RecoveryResult]:
        """回收“租约已失效”的在途任务，区分续租与接管两种情况。

        判定顺序（先短周期、后长周期）：

        1. ``lease_expired(task, now)`` 为 ``False`` → 租约仍有效，跳过；
        2. 租约失效但 ``is_stale == False``（心跳仍新）→ **续租不接管**，返回
           ``previous_state == new_state`` 的 :class:`RecoveryResult`；
        3. 心跳也超时 → :func:`~researchdatahub.core.state.recover_stale` 重新排队
           （``RETRY_WAIT``，``scheduled_at`` 置为当前时间）或判 ``FAILED``，
           并写 ``transfer_logs`` 审计事件。

        Raises
        ------
        ManifestError
            ``stale_timeout_seconds`` / ``lease_seconds`` 非法或事务失败。
        """
        if stale_timeout_seconds < 1:
            raise ManifestError(f"stale_timeout_seconds must be >= 1, got {stale_timeout_seconds}")
        if lease_seconds < 1:
            raise ManifestError(f"lease_seconds must be >= 1, got {lease_seconds}")
        stamp = normalize_now(now)

        def operation(connection: sqlite3.Connection) -> Sequence[RecoveryResult]:
            rows = connection.execute(
                TASK_SELECT + " WHERE t.state IN (?, ?) ORDER BY t.task_id ASC",
                (TaskState.RUNNING.value, TaskState.VERIFYING.value),
            ).fetchall()
            results: list[RecoveryResult] = []
            for row in rows:
                task = task_from_row(row)
                if not lease_expired(task, now=stamp):
                    continue
                if is_stale(task, now=stamp, timeout_seconds=stale_timeout_seconds):
                    recovered = recover_stale(
                        task, now=stamp, timeout_seconds=stale_timeout_seconds
                    )
                    if recovered.state is TaskState.RETRY_WAIT:
                        # 立即可领取；退避由调度器负责，恢复流程不猜退避时长。
                        # 租约同时清空：恢复意味着已经没有 worker 持有它，留着会误导审计。
                        recovered = recovered.model_copy(
                            update={"scheduled_at": stamp, "lease_expires_at": None}
                        )
                    self._write_task(connection, recovered, expected_state=task.state)
                    reason = (
                        f"heartbeat older than {stale_timeout_seconds}s; recovered "
                        f"{task.state.value}->{recovered.state.value}"
                    )
                    results.append(
                        RecoveryResult(
                            task_id=task.task_id,
                            previous_state=task.state.value,
                            new_state=recovered.state.value,
                            lease_owner=task.worker_id,
                            reason=reason,
                        )
                    )
                    self._append_event_conn(
                        connection,
                        task.task_id,
                        "lease_recovered",
                        {
                            "message": reason,
                            "previous_state": task.state.value,
                            "new_state": recovered.state.value,
                            "lease_owner": task.worker_id,
                        },
                    )
                else:
                    renewed = renew_lease(task, now=stamp, lease_seconds=lease_seconds)
                    self._write_task(connection, renewed, expected_state=task.state)
                    reason = (
                        f"lease expired but heartbeat fresh; renewed until "
                        f"{to_iso(renewed.lease_expires_at)} without takeover"
                        if renewed.lease_expires_at is not None
                        else "lease expired but heartbeat fresh; renewed without takeover"
                    )
                    results.append(
                        RecoveryResult(
                            task_id=task.task_id,
                            previous_state=task.state.value,
                            new_state=task.state.value,
                            lease_owner=task.worker_id,
                            reason=reason,
                        )
                    )
                    self._append_event_conn(
                        connection,
                        task.task_id,
                        "lease_renewed",
                        {"message": reason, "lease_owner": task.worker_id},
                    )
            return results

        with self._guard():
            return self._factory.run_write(operation, connection=self._connection_or_raise())

    def append_event(self, task_id: str, event: str, payload: dict[str, object]) -> None:
        """追加一条不可变审计事件（任务必须已存在，外键保证）。"""
        with self._guard():
            self._factory.run_write(
                lambda connection: self._append_event_conn(connection, task_id, event, payload),
                connection=self._connection_or_raise(),
            )

    def list_events(self, task_id: str, *, limit: int | None = None) -> Sequence[TransferLogEvent]:
        """按写入顺序读取任务事件（``event_id`` 升序）。

        Raises
        ------
        ManifestError
            ``limit`` 为负数。
        """
        if limit is not None and limit < 0:
            raise ManifestError(f"list_events limit must be >= 0, got {limit}")
        sql = (
            "SELECT event_id, task_id, event_type, message, bytes_transferred, "
            "metadata, timestamp FROM transfer_logs WHERE task_id = ? ORDER BY event_id ASC"
        )
        params: list[object] = [task_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._guard() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [event_from_row(row) for row in rows]

    # -- 目录数据 ---------------------------------------------------------- #
    def upsert_source(self, source: SourceMetadata, *, adapter_id: str) -> None:
        """登记/更新数据源（按 ``source_id`` 幂等 upsert）。"""
        with self._guard():
            self._factory.run_write(
                lambda connection: self._upsert_source_conn(
                    connection, source, adapter_id=adapter_id
                ),
                connection=self._connection_or_raise(),
            )

    def list_sources(self) -> Sequence[SourceMetadata]:
        """列出已登记数据源（按 ``source_id`` 升序）。"""
        with self._guard() as connection:
            rows = connection.execute("SELECT * FROM sources ORDER BY source_id ASC").fetchall()
            return [source_from_row(row) for row in rows]

    def get_source(self, source_id: str) -> SourceMetadata:
        """读取单个数据源。

        Raises
        ------
        ManifestError
            数据源未登记。
        """
        with self._guard() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if row is None:
                raise ManifestError(f"source {source_id!r} is not registered", source_id=source_id)
            return source_from_row(row)

    def save_dataset(self, dataset_id: str, metadata: DatasetMetadata) -> None:
        """登记数据集科学元数据（按 ``dataset_id`` 幂等 upsert）。

        ``datasets.name`` 取 ``metadata.product``：契约里的 :class:`DatasetMetadata`
        只有 ``product``（产品族）能充当人类可读名称，不额外发明字段。
        """
        stamp = to_iso(utc_now())

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO datasets (dataset_id, name, metadata, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(dataset_id) DO UPDATE SET "
                "name = excluded.name, metadata = excluded.metadata, "
                "updated_at = excluded.updated_at",
                (
                    dataset_id,
                    metadata.product,
                    to_json_text(metadata.model_dump(mode="json")),
                    stamp,
                ),
            )

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def get_dataset(self, dataset_id: str) -> tuple[str, DatasetMetadata]:
        """返回 ``(dataset_name, metadata)``。

        Raises
        ------
        ManifestError
            数据集未登记或元数据不符合契约。
        """
        with self._guard() as connection:
            row = connection.execute(
                "SELECT name, metadata FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise ManifestError(f"dataset {dataset_id!r} is not registered")
            metadata = validated(
                DatasetMetadata,
                from_json_text(row["metadata"], column="datasets.metadata"),
                context=f"dataset {dataset_id!r}",
            )
            return str(row["name"]), metadata

    def save_request(self, request: DatasetRequest) -> None:
        """登记用户请求（幂等；同 ID 重复提交以最新内容为准并刷新 ``updated_at``）。"""
        stamp = to_iso(utc_now())

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO requests (request_id, dataset_id, priority, payload, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET dataset_id = excluded.dataset_id, "
                "priority = excluded.priority, payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    request.request_id,
                    request.dataset_id,
                    request.priority.rank,
                    to_json_text(request.model_dump(mode="json")),
                    stamp,
                    stamp,
                ),
            )

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def get_request(self, request_id: str) -> DatasetRequest:
        """读取请求。

        Raises
        ------
        ManifestError
            请求未登记。
        """
        with self._guard() as connection:
            row = connection.execute(
                "SELECT payload FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ManifestError(f"request {request_id!r} is not registered")
            return validated(
                DatasetRequest,
                from_json_text(row["payload"], column="requests.payload"),
                context=f"request {request_id!r}",
            )

    def save_plan(self, plan: AcquisitionPlan) -> None:
        """登记计划与其任务（同一事务；重复 ``plan_id`` 视为幂等重放）。

        重放语义：计划行被更新，**已存在的任务保持其当前状态**（不回退），
        不存在的任务按计划内容插入；``plan_tasks`` 顺序被重写为计划声明顺序。
        """
        stamp = to_iso(utc_now())

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO plans (plan_id, request_id, policy, unmatched_variables, "
                "extensions, generated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET request_id = excluded.request_id, "
                "policy = excluded.policy, unmatched_variables = excluded.unmatched_variables, "
                "extensions = excluded.extensions, generated_at = excluded.generated_at",
                (
                    plan.plan_id,
                    plan.request_id,
                    plan.policy,
                    to_json_text(plan.unmatched_variables),
                    to_json_text(plan.extensions),
                    to_iso(plan.generated_at),
                    stamp,
                ),
            )
            for task in plan.tasks:
                if task.plan_id != plan.plan_id:
                    raise ManifestError(
                        f"task {task.task_id!r} belongs to plan {task.plan_id!r}, "
                        f"not {plan.plan_id!r}",
                        task_id=task.task_id,
                    )
                self._insert_task_if_absent(connection, task, stamp=stamp)
            connection.execute("DELETE FROM plan_tasks WHERE plan_id = ?", (plan.plan_id,))
            for ordinal, task in enumerate(plan.tasks):
                connection.execute(
                    "INSERT INTO plan_tasks (plan_id, task_id, ordinal) VALUES (?, ?, ?)",
                    (plan.plan_id, task.task_id, ordinal),
                )

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def load_plan(self, plan_id: str) -> AcquisitionPlan:
        """读取计划（任务按计划声明顺序）。

        Raises
        ------
        ManifestError
            计划不存在，或计划引用的任务缺失。
        """
        with self._guard() as connection:
            row = connection.execute(
                "SELECT plan_id, request_id, policy, unmatched_variables, extensions, "
                "generated_at FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ManifestError(f"plan {plan_id!r} is not registered")
            task_ids = [
                str(item[0])
                for item in connection.execute(
                    "SELECT task_id FROM plan_tasks WHERE plan_id = ? ORDER BY ordinal ASC",
                    (plan_id,),
                ).fetchall()
            ]
            tasks = [self._load_task_by_id(connection, task_id) for task_id in task_ids]
            return validated(
                AcquisitionPlan,
                {
                    "plan_id": row["plan_id"],
                    "request_id": row["request_id"],
                    "policy": row["policy"],
                    "tasks": tasks,
                    "unmatched_variables": from_json_text(
                        row["unmatched_variables"], column="plans.unmatched_variables"
                    ),
                    "generated_at": row["generated_at"],
                    "extensions": from_json_dict(row["extensions"], column="plans.extensions"),
                },
                context=f"plan {plan_id!r}",
            )

    def save_asset(self, asset: SourceAsset) -> None:
        """登记远端资产（按 ``asset_id`` 幂等 upsert）。

        资产内嵌的 ``SourceMetadata`` 也会确保 ``sources`` 行存在（外键前提），
        但**不会覆盖**已由 :meth:`upsert_source` 登记的权威数据源条目。
        """
        with self._guard():
            self._factory.run_write(
                lambda connection: self._upsert_asset_conn(connection, asset),
                connection=self._connection_or_raise(),
            )

    def list_assets(
        self, *, dataset_id: str | None = None, source_id: str | None = None
    ) -> Iterator[SourceAsset]:
        """惰性流式列出资产（可按数据集/数据源过滤）。"""
        sql = "SELECT * FROM assets"
        clauses: list[str] = []
        params: list[object] = []
        if dataset_id is not None:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY asset_id ASC"
        with self._guard() as connection:
            for row in connection.execute(sql, tuple(params)):
                yield asset_from_row(row)

    # -- 文件与校验 -------------------------------------------------------- #
    def save_file_record(self, record: FileRecord) -> None:
        """登记一个已落盘的文件（按 ``file_id`` 幂等 upsert；``task_id`` 外键必须存在）。"""
        stamp = to_iso(utc_now())
        payload = (
            record.task_id,
            record.dataset_id,
            record.source_id,
            record.access_route.value,
            record.origin_uri,
            record.local_path,
            record.size_bytes,
            record.checksum.value,
            record.content_type,
            record.remote_etag,
            to_optional_iso(record.remote_last_modified),
            to_iso(record.retrieved_at),
            to_bool(record.is_read_only),
            to_json_text(record.extensions),
            stamp,
            record.file_id,
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO files (task_id, dataset_id, source_id, access_route, origin_uri, "
                "local_path, size_bytes, checksum, content_type, remote_etag, "
                "remote_last_modified, retrieved_at, is_read_only, extensions, created_at, "
                "file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET task_id = excluded.task_id, "
                "dataset_id = excluded.dataset_id, source_id = excluded.source_id, "
                "access_route = excluded.access_route, origin_uri = excluded.origin_uri, "
                "local_path = excluded.local_path, size_bytes = excluded.size_bytes, "
                "checksum = excluded.checksum, content_type = excluded.content_type, "
                "remote_etag = excluded.remote_etag, "
                "remote_last_modified = excluded.remote_last_modified, "
                "retrieved_at = excluded.retrieved_at, "
                "is_read_only = excluded.is_read_only, extensions = excluded.extensions",
                payload,
            )

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def load_file_record(self, file_id: str) -> FileRecord:
        """读取文件记录。

        Raises
        ------
        ManifestError
            文件记录不存在。
        """
        with self._guard() as connection:
            row = connection.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
            if row is None:
                raise ManifestError(f"file record {file_id!r} is not registered")
            return file_record_from_row(row)

    def list_file_records(
        self, *, task_id: str | None = None, request_id: str | None = None
    ) -> Iterator[FileRecord]:
        """惰性流式列出文件记录（可按任务/请求过滤；请求过滤经由 ``tasks.request_id``）。"""
        sql = "SELECT f.* FROM files AS f JOIN tasks AS t ON t.task_id = f.task_id"
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("f.task_id = ?")
            params.append(task_id)
        if request_id is not None:
            clauses.append("t.request_id = ?")
            params.append(request_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY f.file_id ASC"
        with self._guard() as connection:
            for row in connection.execute(sql, tuple(params)):
                yield file_record_from_row(row)

    def save_integrity_report(self, report: IntegrityReport) -> None:
        """追加一条完整性报告（不可变，只追加；重复 ``report_id`` 抛 ManifestError）。"""
        payload = (
            report.report_id,
            report.task_id,
            report.file_id,
            report.status.value,
            to_json_text(report.checks),
            to_json_text(report.failures),
            to_json_text(report.expected),
            to_json_text(report.observed),
            to_iso(report.checked_at),
            report.checker_id,
        )

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO integrity_reports (report_id, task_id, file_id, status, checks, "
                "failures, expected, observed, checked_at, checker_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )

        with self._guard():
            self._factory.run_write(operation, connection=self._connection_or_raise())

    def list_integrity_reports(
        self, *, task_id: str | None = None, file_id: str | None = None
    ) -> Sequence[IntegrityReport]:
        """读取完整性报告（按 ``checked_at`` 升序；可按任务/文件过滤）。"""
        sql = "SELECT * FROM integrity_reports WHERE 1 = 1"
        params: list[object] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if file_id is not None:
            sql += " AND file_id = ?"
            params.append(file_id)
        sql += " ORDER BY checked_at ASC, report_id ASC"
        with self._guard() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [integrity_report_from_row(row) for row in rows]

    # -- 交付 -------------------------------------------------------------- #
    def build_delivery_manifest(self, request_id: str) -> DeliveryManifest:
        """从**已完成的文件记录**生成交付清单。

        证据链要求（ADR-0003 §2）：
        * 请求下至少一条文件记录；
        * 请求下**不存在**非 ``PASSED`` 的完整性报告；
        * 每条文件记录至少有一条 ``PASSED`` 报告，且每条报告 ``permits_completion``。

        Raises
        ------
        ManifestError
            没有文件、存在非 PASSED 报告、文件缺少 PASSED 报告、请求或数据源未登记。
        """
        with self._snapshot() as connection:
            request_row = connection.execute(
                "SELECT payload FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request_row is None:
                raise ManifestError(f"request {request_id!r} is not registered")
            request = validated(
                DatasetRequest,
                from_json_text(request_row["payload"], column="requests.payload"),
                context=f"request {request_id!r}",
            )
            files = [
                file_record_from_row(row)
                for row in connection.execute(
                    "SELECT f.* FROM files AS f JOIN tasks AS t ON t.task_id = f.task_id "
                    "WHERE t.request_id = ? ORDER BY f.file_id ASC",
                    (request_id,),
                ).fetchall()
            ]
            if not files:
                raise ManifestError(f"no file records for request {request_id!r}")
            reports = [
                integrity_report_from_row(row)
                for row in connection.execute(
                    "SELECT r.* FROM integrity_reports AS r "
                    "JOIN tasks AS t ON t.task_id = r.task_id "
                    "WHERE t.request_id = ? ORDER BY r.checked_at ASC, r.report_id ASC",
                    (request_id,),
                ).fetchall()
            ]
            non_passed = [report for report in reports if not report.permits_completion]
            if non_passed:
                raise ManifestError(
                    f"request {request_id!r} has {len(non_passed)} non-PASSED integrity "
                    f"report(s): {', '.join(sorted(report.report_id for report in non_passed))}",
                    detail={"request_id": request_id},
                )
            passed_file_ids = {report.file_id for report in reports if report.file_id is not None}
            missing = sorted(
                record.file_id for record in files if record.file_id not in passed_file_ids
            )
            if missing:
                raise ManifestError(
                    f"file(s) without a PASSED integrity report: {', '.join(missing)}",
                    detail={"request_id": request_id},
                )
            plan_row = connection.execute(
                "SELECT plan_id FROM plans WHERE request_id = ? "
                "ORDER BY generated_at DESC, plan_id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            if plan_row is not None:
                plan_id = str(plan_row["plan_id"])
            else:
                task_plan_row = connection.execute(
                    "SELECT t.plan_id FROM tasks AS t JOIN files AS f ON f.task_id = t.task_id "
                    "WHERE t.request_id = ? ORDER BY t.plan_id ASC LIMIT 1",
                    (request_id,),
                ).fetchone()
                if task_plan_row is None:  # pragma: no cover - 有文件必有任务
                    raise ManifestError(f"cannot determine plan for request {request_id!r}")
                plan_id = str(task_plan_row["plan_id"])
            sources: list[SourceMetadata] = []
            for source_id in sorted({record.source_id for record in files}):
                source_row = connection.execute(
                    "SELECT * FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
                if source_row is None:
                    raise ManifestError(
                        f"file record references unregistered source {source_id!r}",
                        source_id=source_id,
                    )
                sources.append(source_from_row(source_row))
            total_bytes = sum(record.size_bytes for record in files)
            return DeliveryManifest(
                delivery_id=_delivery_id(request_id),
                request_id=request_id,
                plan_id=plan_id,
                dataset_metadata=request.metadata,
                files=files,
                integrity=reports,
                sources=sources,
                created_at=utc_now(),
                tool_version=_tool_version(),
                total_bytes=total_bytes,
                complete=total_bytes > 0,
            )

    async def aiter_tasks(self) -> AsyncIterator[AcquisitionTask]:
        """异步迭代任务；语义与 :meth:`list_tasks` 相同（底层仍是同步 SQLite 调用）。"""
        for task in self.list_tasks():
            yield task

    # -- 内部：连接与快照 -------------------------------------------------- #
    def _require_open(self) -> None:
        """确认连接仍可用；关闭后调用任何方法都抛 ``RuntimeError``。"""
        if self._connection is None:
            raise RuntimeError("manifest repository is closed")

    def _connection_or_raise(self) -> sqlite3.Connection:
        """返回当前连接；已关闭时抛 ``RuntimeError``。"""
        self._require_open()
        assert self._connection is not None
        return self._connection

    @contextmanager
    def _guard(self) -> Iterator[sqlite3.Connection]:
        """连接守卫：关闭后 ``RuntimeError``；SQL 错误转 ``ManifestError``。

        ``sqlite3.ProgrammingError``（跨线程使用连接等编程错误）**原样抛出**，
        因为那是缺陷而不是数据错误，必须带着原始类型暴露出来。
        """
        connection = self._connection
        if connection is None:
            raise RuntimeError("manifest repository is closed")
        try:
            yield connection
        except ManifestError:
            raise
        except sqlite3.ProgrammingError:
            raise
        except sqlite3.Error as exc:
            raise ManifestError(
                f"manifest database operation failed: {exc}",
                detail={"db_path": str(self._factory.db_path)},
            ) from exc

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """只读事务快照：多条查询看到同一个数据库状态，避免交付清单被并发写撕开。"""
        with self._guard() as connection:
            if connection.in_transaction:
                yield connection
                return
            connection.execute("BEGIN")
            try:
                yield connection
            except BaseException:
                # BEGIN 已经执行过；此时事务必然处于开启状态（异常来自本次快照内）。
                rollback_if_needed(connection)
                raise
            connection.execute("COMMIT")

    # -- 内部：任务行 ------------------------------------------------------ #
    def _load_task_by_id(self, connection: sqlite3.Connection, task_id: str) -> AcquisitionTask:
        """按 ID 读取任务；不存在时抛 :class:`ManifestError`。"""
        row = connection.execute(TASK_SELECT + " WHERE t.task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise ManifestError(f"task {task_id!r} is not registered", task_id=task_id)
        return task_from_row(row)

    def _task_params(self, task: AcquisitionTask, *, stamp: str) -> tuple[object, ...]:
        """把任务模型摊平成 UPDATE 参数（顺序必须与 :data:`_UPDATE_TASK_SQL` 一致）。"""
        return (
            task.plan_id,
            task.request_id,
            task.asset.asset_id,
            task.state.value,
            task.priority.rank,
            task.attempt,
            task.max_attempts,
            to_optional_iso(task.scheduled_at),
            to_optional_iso(task.started_at),
            to_optional_iso(task.finished_at),
            to_optional_iso(task.last_transition_at),
            task.last_error,
            task.worker_id,
            to_optional_iso(task.heartbeat_at),
            to_optional_iso(task.lease_expires_at),
            to_json_text(task.notes),
            stamp,
            task.task_id,
        )

    def _task_insert_params(self, task: AcquisitionTask, *, stamp: str) -> tuple[object, ...]:
        """INSERT 参数（顺序与 :data:`_INSERT_TASK_SQL` 一致；``created_at`` 取首次写入时间）。"""
        return (
            task.task_id,
            task.plan_id,
            task.request_id,
            task.asset.asset_id,
            task.state.value,
            task.priority.rank,
            task.attempt,
            task.max_attempts,
            to_optional_iso(task.scheduled_at),
            to_optional_iso(task.started_at),
            to_optional_iso(task.finished_at),
            to_optional_iso(task.last_transition_at),
            task.last_error,
            task.worker_id,
            to_optional_iso(task.heartbeat_at),
            to_optional_iso(task.lease_expires_at),
            to_json_text(task.notes),
            stamp,
            stamp,
        )

    def _check_transition(self, current: TaskState, target: TaskState, *, task_id: str) -> None:
        """复核状态机：只允许 ``TRANSITIONS`` 中的边（含事后隔离例外）。"""
        if current is target:
            return
        if can_transition(current, target) or can_transition(current, target, after_integrity=True):
            return
        raise StateTransitionError(
            f"illegal transition {current.value} -> {target.value}",
            from_state=current.value,
            to_state=target.value,
            task_id=task_id,
            detail={"hint": "use researchdatahub.core.state.transition"},
        )

    def _write_task(
        self,
        connection: sqlite3.Connection,
        task: AcquisitionTask,
        *,
        expected_state: TaskState | None = None,
    ) -> None:
        """写入任务（存在则 UPDATE，不存在则 INSERT），并校验状态机与乐观并发。"""
        self._upsert_asset_conn(connection, task.asset)
        stamp = to_iso(utc_now())
        params = self._task_params(task, stamp=stamp)
        existing = connection.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()
        if expected_state is not None:
            self._check_transition(expected_state, task.state, task_id=task.task_id)
            if existing is None:
                raise ManifestError(
                    f"task {task.task_id!r} does not exist; cannot apply expected_state",
                    task_id=task.task_id,
                )
            cursor = connection.execute(_UPDATE_TASK_SQL_BY_STATE, (*params, expected_state.value))
            if cursor.rowcount != 1:
                actual = None if existing is None else str(existing["state"])
                raise ManifestError(
                    f"optimistic concurrency conflict on task {task.task_id!r}: "
                    f"expected state {expected_state.value}, found {actual}",
                    task_id=task.task_id,
                    detail={"expected_state": expected_state.value, "actual_state": actual},
                )
            return
        if existing is None:
            connection.execute(_INSERT_TASK_SQL, self._task_insert_params(task, stamp=stamp))
            return
        current = TaskState(str(existing["state"]))
        self._check_transition(current, task.state, task_id=task.task_id)
        connection.execute(_UPDATE_TASK_SQL, params)

    def _insert_task_if_absent(
        self, connection: sqlite3.Connection, task: AcquisitionTask, *, stamp: str
    ) -> None:
        """计划登记专用：任务不存在时插入，已存在时保持原状（幂等重放）。"""
        self._upsert_asset_conn(connection, task.asset)
        connection.execute(
            _INSERT_TASK_SQL + " ON CONFLICT(task_id) DO NOTHING",
            self._task_insert_params(task, stamp=stamp),
        )

    # -- 内部：目录行 ------------------------------------------------------ #
    def _upsert_source_conn(
        self,
        connection: sqlite3.Connection,
        source: SourceMetadata,
        *,
        adapter_id: str | None,
    ) -> None:
        """插入/更新数据源行（含 ``adapter_id`` 绑定）。"""
        stamp = to_iso(utc_now())
        connection.execute(
            "INSERT INTO sources (source_id, display_name, provider, access_route, dataset_id, "
            "origin_uri, protocol_version, license, citation, retrieved_at, extensions, "
            "adapter_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET display_name = excluded.display_name, "
            "provider = excluded.provider, access_route = excluded.access_route, "
            "dataset_id = excluded.dataset_id, origin_uri = excluded.origin_uri, "
            "protocol_version = excluded.protocol_version, license = excluded.license, "
            "citation = excluded.citation, retrieved_at = excluded.retrieved_at, "
            "extensions = excluded.extensions, adapter_id = excluded.adapter_id, "
            "updated_at = excluded.updated_at",
            (
                source.source_id,
                source.display_name,
                source.provider,
                source.access_route.value,
                source.dataset_id,
                source.origin_uri,
                source.protocol_version,
                source.license,
                source.citation,
                to_iso(source.retrieved_at),
                to_json_text(source.extensions),
                adapter_id,
                stamp,
            ),
        )

    def _ensure_source_conn(self, connection: sqlite3.Connection, source: SourceMetadata) -> None:
        """仅当数据源不存在时插入（不覆盖 :meth:`upsert_source` 的权威登记）。"""
        stamp = to_iso(utc_now())
        connection.execute(
            "INSERT INTO sources (source_id, display_name, provider, access_route, dataset_id, "
            "origin_uri, protocol_version, license, citation, retrieved_at, extensions, "
            "adapter_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(source_id) DO NOTHING",
            (
                source.source_id,
                source.display_name,
                source.provider,
                source.access_route.value,
                source.dataset_id,
                source.origin_uri,
                source.protocol_version,
                source.license,
                source.citation,
                to_iso(source.retrieved_at),
                to_json_text(source.extensions),
                stamp,
            ),
        )

    def _upsert_asset_conn(self, connection: sqlite3.Connection, asset: SourceAsset) -> None:
        """插入/更新资产行，并确保其数据源行存在（外键前提）。"""
        self._ensure_source_conn(connection, asset.metadata)
        stamp = to_iso(utc_now())
        connection.execute(
            "INSERT INTO assets (asset_id, source_id, dataset_id, access_route, origin_uri, "
            "file_name, size_bytes, checksum, remote_etag, remote_last_modified, content_type, "
            "time_slice, level_slice, metadata, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET source_id = excluded.source_id, "
            "dataset_id = excluded.dataset_id, access_route = excluded.access_route, "
            "origin_uri = excluded.origin_uri, file_name = excluded.file_name, "
            "size_bytes = excluded.size_bytes, checksum = excluded.checksum, "
            "remote_etag = excluded.remote_etag, "
            "remote_last_modified = excluded.remote_last_modified, "
            "content_type = excluded.content_type, time_slice = excluded.time_slice, "
            "level_slice = excluded.level_slice, metadata = excluded.metadata, "
            "discovered_at = excluded.discovered_at, updated_at = excluded.updated_at",
            (
                asset.asset_id,
                asset.source_id,
                asset.dataset_id,
                asset.access_route.value,
                asset.origin_uri,
                asset.file_name,
                asset.size_bytes,
                None if asset.checksum is None else asset.checksum.value,
                asset.remote_etag,
                to_optional_iso(asset.remote_last_modified),
                asset.content_type,
                None
                if asset.time_slice is None
                else to_json_text(asset.time_slice.model_dump(mode="json")),
                None if asset.level_slice is None else to_json_text(asset.level_slice),
                to_json_text(asset.metadata.model_dump(mode="json")),
                to_iso(asset.discovered_at),
                stamp,
            ),
        )

    # -- 内部：审计事件 ---------------------------------------------------- #
    def _append_event_conn(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        event: str,
        payload: dict[str, object] | None,
    ) -> None:
        """在调用方事务内插入一条事件（message/bytes 从 payload 约定键提取）。"""
        if not event.strip():
            raise ManifestError("event type must not be empty", task_id=task_id)
        raw = payload if payload is not None else {}
        message_value = raw.get("message")
        if isinstance(message_value, str) and message_value.strip():
            message = message_value
        else:
            message = f"event {event}"
        raw_bytes = raw.get("bytes_transferred", 0)
        if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int) or raw_bytes < 0:
            raise ManifestError(
                f"event payload bytes_transferred must be a non-negative integer, "
                f"got {raw_bytes!r}",
                task_id=task_id,
            )
        metadata = from_json_dict(to_json_text(raw), column="event payload")
        connection.execute(
            "INSERT INTO transfer_logs (task_id, event_type, message, bytes_transferred, "
            "metadata, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                event,
                message,
                raw_bytes,
                to_json_text(metadata),
                to_iso(utc_now()),
            ),
        )

    # -- 内部：交付就绪 ---------------------------------------------------- #
    def _delivery_ready_requests(self, connection: sqlite3.Connection) -> list[str]:
        """返回“交付清单可以生成”的请求 ID（每条文件都有 PASSED 报告且无失败报告）。"""
        rows = connection.execute(
            "SELECT DISTINCT t.request_id FROM files AS f "
            "JOIN tasks AS t ON t.task_id = f.task_id ORDER BY t.request_id ASC"
        ).fetchall()
        ready: list[str] = []
        for row in rows:
            request_id = str(row["request_id"])
            files = connection.execute(
                "SELECT f.file_id FROM files AS f JOIN tasks AS t ON t.task_id = f.task_id "
                "WHERE t.request_id = ?",
                (request_id,),
            ).fetchall()
            reports = connection.execute(
                "SELECT r.file_id, r.status FROM integrity_reports AS r "
                "JOIN tasks AS t ON t.task_id = r.task_id WHERE t.request_id = ?",
                (request_id,),
            ).fetchall()
            if any(str(report["status"]) != IntegrityStatus.PASSED.value for report in reports):
                continue
            passed = {
                str(report["file_id"])
                for report in reports
                if report["file_id"] is not None
                and str(report["status"]) == IntegrityStatus.PASSED.value
            }
            if all(str(item["file_id"]) in passed for item in files):
                ready.append(request_id)
        return ready
