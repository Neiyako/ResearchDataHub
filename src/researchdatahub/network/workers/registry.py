"""Worker 注册表：注册、心跳、健康判定、能力匹配与重启恢复。

健康判定规则（**事实驱动，不看配置**）
-------------------------------------
1. 注册时 ``status=healthy``，``last_heartbeat`` 置为注册时刻（注册本身就是一次真实
   请求，把它记成心跳不算编造）；
2. 每次 :meth:`WorkerRegistry.heartbeat` 更新 ``last_heartbeat`` 与 ``updated_at``；
3. 读路径（:meth:`WorkerRegistry.list_workers` 等）用**当前时间**重算：

   * 距上次心跳 ≤ ``heartbeat_timeout_s`` → 保持库中记录的 ``healthy``；
   * ``heartbeat_timeout_s`` < 距上次心跳 ≤ ``offline_after_s`` → ``degraded``；
   * 距上次心跳 > ``offline_after_s`` → ``offline``（并**写回**库、追加 ``marked_offline``
     事件，保证重启后看到的仍是同一个判定）；
   * 连续失败次数 ≥ ``degraded_after_failures`` → ``degraded``。

4. 进程重启恢复：``workers`` 表是持久事实，重启后 :meth:`WorkerRegistry.list_workers`
   直接读库并按当前时间重算健康度——"把配置里的节点重新当成健康节点"是不可能的。

并发
----
写操作（注册/心跳/下线/失效）用库级写锁串行化；读操作各开短连接，可并发。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Final

from researchdatahub.core.errors import RdhError
from researchdatahub.network.workers.models import (
    WorkerCapabilities,
    WorkerHealth,
    WorkerKind,
    WorkerRecord,
    WorkerStatus,
)
from researchdatahub.network.workers.store import WorkerStore, json_dumps, utc_iso

__all__ = [
    "DEFAULT_DEGRADED_AFTER_FAILURES",
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_OFFLINE_AFTER_SECONDS",
    "WorkerRegistrationError",
    "WorkerRegistry",
    "WorkerUnknownError",
]

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: Final[float] = 30.0
"""超过该时长没有心跳 → ``degraded``（仍可派活，但排序靠后）。"""

DEFAULT_OFFLINE_AFTER_SECONDS: Final[float] = 120.0
"""超过该时长没有心跳 → ``offline``（不再派活，直到下一次心跳把它唤醒）。"""

DEFAULT_DEGRADED_AFTER_FAILURES: Final[int] = 3
"""连续失败达到该次数 → ``degraded``（成功一次即清零）。"""


class WorkerRegistrationError(RdhError):
    """注册被拒绝（重复注册且内容不一致、或节点类型与端点自相矛盾）。"""

    default_code = "RDH_WORKER_REGISTRATION"


class WorkerUnknownError(RdhError):
    """引用了未注册的 worker。"""

    default_code = "RDH_WORKER_UNKNOWN"


def _now() -> datetime:
    return datetime.now(UTC)


def _as_int(value: object) -> int:
    """把库列值安全转成 ``int``（SQLite 的 ``INTEGER`` 列可能返回 ``None``）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        return int(value)
    return 0


class WorkerRegistry:
    """节点注册表（SQLite 持久化，进程重启后恢复）。

    Parameters
    ----------
    store:
        控制面库句柄；构造后自动 :meth:`WorkerStore.initialize`（幂等）。
    clock:
        可注入时钟（测试固定健康判定）。
    heartbeat_timeout_s, offline_after_s, degraded_after_failures:
        健康判定阈值；``offline_after_s`` 必须严格大于 ``heartbeat_timeout_s``。
    """

    def __init__(
        self,
        store: WorkerStore,
        *,
        clock: Callable[[], datetime] | None = None,
        heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        offline_after_s: float = DEFAULT_OFFLINE_AFTER_SECONDS,
        degraded_after_failures: int = DEFAULT_DEGRADED_AFTER_FAILURES,
    ) -> None:
        if offline_after_s <= heartbeat_timeout_s:
            raise ValueError("offline_after_s must be greater than heartbeat_timeout_s")
        if degraded_after_failures < 1:
            raise ValueError("degraded_after_failures must be >= 1")
        self._store = store
        self._clock = clock or _now
        self._heartbeat_timeout = float(heartbeat_timeout_s)
        self._offline_after = float(offline_after_s)
        self._degraded_after = int(degraded_after_failures)
        self._store.initialize()

    # -- 属性 -------------------------------------------------------------- #
    @property
    def store(self) -> WorkerStore:
        """底层控制面库。"""
        return self._store

    @property
    def heartbeat_timeout_s(self) -> float:
        """判定 ``degraded`` 的心跳超时（秒）。"""
        return self._heartbeat_timeout

    @property
    def offline_after_s(self) -> float:
        """判定 ``offline`` 的心跳超时（秒）。"""
        return self._offline_after

    # -- 注册 -------------------------------------------------------------- #
    def register(self, record: WorkerRecord, *, replace: bool = True) -> WorkerRecord:
        """登记（或更新）一个节点，返回落到库里的健康快照。

        Parameters
        ----------
        record:
            节点声明。``last_heartbeat`` / ``uptime_s`` 由注册表接管（注册即一次真实
            心跳），调用方传入的值会被覆盖。
        replace:
            ``True`` 时同名 ``worker_id`` 视为重新注册（更新能力与端点）；``False`` 时
            直接拒绝重复注册。

        Raises
        ------
        WorkerRegistrationError
            重复注册且 ``replace=False``。
        """
        now = self._clock()
        existing = self._store.worker_row(record.worker_id)
        if existing is not None and not replace:
            raise WorkerRegistrationError(
                f"worker {record.worker_id!r} is already registered",
                detail={"worker_id": record.worker_id},
            )
        registered_at = (
            now if existing is None else datetime.fromisoformat(str(existing["registered_at"]))
        )
        # 注册即一次真实心跳：即使声明里写成 offline，也因刚验证可达而恢复 healthy。
        status = WorkerStatus.HEALTHY
        stored = record.model_copy(
            update={
                "health": WorkerHealth(
                    status=status,
                    last_heartbeat=now,
                    uptime_s=max(0.0, (now - registered_at).total_seconds()),
                    consecutive_failures=0,
                ),
                "registered_at": registered_at,
                "updated_at": now,
            }
        )
        with self._store.write() as connection:
            connection.execute(
                """
                INSERT INTO workers (
                    worker_id, kind, region, endpoint, version, capabilities_json, status,
                    last_heartbeat_at, registered_at, updated_at, consecutive_failures
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    kind=excluded.kind,
                    region=excluded.region,
                    endpoint=excluded.endpoint,
                    version=excluded.version,
                    capabilities_json=excluded.capabilities_json,
                    status=excluded.status,
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    updated_at=excluded.updated_at,
                    consecutive_failures=0
                """,
                (
                    stored.worker_id,
                    stored.kind.value,
                    stored.region,
                    stored.endpoint,
                    stored.version,
                    json_dumps(stored.capabilities.model_dump(mode="json")),
                    stored.health.status.value,
                    utc_iso(now),
                    utc_iso(registered_at),
                    utc_iso(now),
                    0,
                ),
            )
            self._append_event(
                connection,
                stored.worker_id,
                "registered",
                now,
                {"kind": stored.kind.value, "endpoint": stored.endpoint},
            )
        return stored

    # -- 心跳 -------------------------------------------------------------- #
    def heartbeat(self, worker_id: str, *, at: datetime | None = None) -> WorkerRecord:
        """记录一次真实心跳，返回刷新后的记录。

        Raises
        ------
        WorkerUnknownError
            该 ``worker_id`` 未注册。
        """
        now = at or self._clock()
        existing = self._store.worker_row(worker_id)
        if existing is None:
            raise WorkerUnknownError(
                f"unknown worker {worker_id!r}", detail={"worker_id": worker_id}
            )
        registered_at = datetime.fromisoformat(str(existing["registered_at"]))
        with self._store.write() as connection:
            connection.execute(
                """
                UPDATE workers
                   SET status = ?, last_heartbeat_at = ?, updated_at = ?,
                       consecutive_failures = 0
                 WHERE worker_id = ?
                """,
                (WorkerStatus.HEALTHY.value, utc_iso(now), utc_iso(now), worker_id),
            )
            self._append_event(connection, worker_id, "heartbeat", now, {})
        return self._record(
            dict(existing)
            | {
                "status": WorkerStatus.HEALTHY.value,
                "last_heartbeat_at": utc_iso(now),
                "updated_at": utc_iso(now),
                "consecutive_failures": 0,
                "uptime_s": max(0.0, (now - registered_at).total_seconds()),
            },
            now,
        )

    def record_outcome(self, worker_id: str, *, ok: bool, detail: str | None = None) -> None:
        """记录一次真实任务结果（成功清零失败计数，失败累加）。

        Raises
        ------
        WorkerUnknownError
            该 ``worker_id`` 未注册。
        """
        now = self._clock()
        existing = self._store.worker_row(worker_id)
        if existing is None:
            raise WorkerUnknownError(
                f"unknown worker {worker_id!r}", detail={"worker_id": worker_id}
            )
        failures = 0 if ok else _as_int(existing["consecutive_failures"]) + 1
        status = (
            WorkerStatus.DEGRADED
            if failures >= self._degraded_after
            else WorkerStatus(existing["status"] if existing["status"] != "offline" else "healthy")
        )
        with self._store.write() as connection:
            connection.execute(
                "UPDATE workers SET consecutive_failures = ?, status = ?, updated_at = ? "
                "WHERE worker_id = ?",
                (failures, status.value, utc_iso(now), worker_id),
            )
            self._append_event(
                connection,
                worker_id,
                "transferred" if ok else "transport_failed",
                now,
                {"consecutive_failures": failures, "detail": detail},
            )

    # -- 读路径 ------------------------------------------------------------ #
    def get(self, worker_id: str) -> WorkerRecord:
        """读取单个节点（已按当前时间重算健康度）。

        Raises
        ------
        WorkerUnknownError
            未注册。
        """
        row = self._store.worker_row(worker_id)
        if row is None:
            raise WorkerUnknownError(
                f"unknown worker {worker_id!r}", detail={"worker_id": worker_id}
            )
        return self._with_health(dict(row))

    def list_workers(self, *, kind: WorkerKind | None = None) -> Sequence[WorkerRecord]:
        """列出全部节点（按 ``worker_id`` 升序，健康度已重算）。"""
        records = [self._with_health(dict(row)) for row in self._store.all_worker_rows()]
        if kind is not None:
            records = [record for record in records if record.kind is kind]
        return records

    def candidates(
        self,
        *,
        dataset_id: str,
        provider_id: str,
        protocol: str,
        size_bytes: int,
        include_degraded: bool = True,
        kind: WorkerKind | None = None,
    ) -> Sequence[WorkerRecord]:
        """按能力匹配筛出可承接该请求的节点。

        排序：``healthy`` 先于 ``degraded``；同健康度按 ``worker_id`` 升序（稳定可测）。
        ``offline`` 节点永不返回。
        """
        matched = []
        for record in self.list_workers(kind=kind):
            if record.health.status is WorkerStatus.OFFLINE:
                continue
            if record.health.status is WorkerStatus.DEGRADED and not include_degraded:
                continue
            ok, _reason = record.matches(
                dataset_id=dataset_id,
                provider_id=provider_id,
                protocol=protocol,
                size_bytes=size_bytes,
                require_available=False,
            )
            if ok:
                matched.append(record)
        matched.sort(
            key=lambda item: (
                0 if item.health.status is WorkerStatus.HEALTHY else 1,
                item.worker_id,
            )
        )
        return matched

    def mark_offline(self, worker_id: str, *, reason: str) -> WorkerRecord:
        """显式把节点标记为 ``offline``（失联检测/运维下线）。

        Raises
        ------
        WorkerUnknownError
            未注册。
        """
        now = self._clock()
        if self._store.worker_row(worker_id) is None:
            raise WorkerUnknownError(
                f"unknown worker {worker_id!r}", detail={"worker_id": worker_id}
            )
        with self._store.write() as connection:
            connection.execute(
                "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ?",
                (WorkerStatus.OFFLINE.value, utc_iso(now), worker_id),
            )
            self._append_event(connection, worker_id, "marked_offline", now, {"reason": reason})
        return self.get(worker_id)

    def deregister(self, worker_id: str) -> bool:
        """删除节点记录（返回是否真的删除）。"""
        now = self._clock()
        if self._store.worker_row(worker_id) is None:
            return False
        with self._store.write() as connection:
            connection.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,))
            self._append_event(connection, worker_id, "deregistered", now, {"worker_id": worker_id})
        return True

    def events(self, worker_id: str, *, limit: int | None = None) -> Sequence[tuple[str, str]]:
        """读取节点的审计事件 ``(event_type, occurred_at)``。"""
        return self._store.events(worker_id, limit=limit)

    # -- 内部 -------------------------------------------------------------- #
    def _append_event(
        self,
        connection: sqlite3.Connection,
        worker_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO worker_events (worker_id, event_type, occurred_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (worker_id, event_type, utc_iso(occurred_at), json_dumps(payload)),
        )

    @staticmethod
    def _record(row: dict[str, object], now: datetime) -> WorkerRecord:
        """把库行转成 :class:`WorkerRecord`（不重算健康状态）。

        ``uptime`` 由 ``last_heartbeat_at - registered_at`` **实测**得出；``now`` 只在
        ``last_heartbeat_at`` 缺失时用于"从未心跳"的保守处理。
        """
        last_raw = row.get("last_heartbeat_at")
        last = None if last_raw in (None, "") else datetime.fromisoformat(str(last_raw))
        registered_at = row.get("registered_at")
        registered = (
            None if registered_at in (None, "") else datetime.fromisoformat(str(registered_at))
        )
        uptime: float | None = None
        if last is not None and registered is not None:
            uptime = max(0.0, (last - registered).total_seconds())
        capabilities = row.get("capabilities_json")
        return WorkerRecord(
            worker_id=str(row["worker_id"]),
            kind=WorkerKind(str(row["kind"])),
            region=None if row.get("region") is None else str(row["region"]),
            endpoint=None if row.get("endpoint") is None else str(row["endpoint"]),
            version=str(row["version"]),
            capabilities=(
                WorkerCapabilities.model_validate_json("{}")
                if capabilities in (None, "")
                else WorkerCapabilities.model_validate(json.loads(str(capabilities)))
            ),
            health=WorkerHealth(
                status=WorkerStatus(str(row["status"])),
                last_heartbeat=last,
                uptime_s=uptime,
                consecutive_failures=_as_int(row.get("consecutive_failures")),
            ),
            registered_at=registered,
            updated_at=(
                None
                if row.get("updated_at") in (None, "")
                else datetime.fromisoformat(str(row["updated_at"]))
            ),
        )

    def _with_health(self, row: dict[str, object]) -> WorkerRecord:
        """按当前时间重算健康度；转 ``offline`` 时写回库并记审计事件。"""
        now = self._clock()
        record = self._record(row, now)
        last = record.health.last_heartbeat
        status = record.health.status
        if last is None:
            effective = status
        else:
            age = (now - last).total_seconds()
            if age > self._offline_after:
                effective = WorkerStatus.OFFLINE
            elif age > self._heartbeat_timeout:
                effective = (
                    WorkerStatus.OFFLINE
                    if status is WorkerStatus.OFFLINE
                    else WorkerStatus.DEGRADED
                )
            else:
                # 心跳新鲜：库中记录的状态直接生效（degraded 只能由心跳恢复）。
                effective = status
        if record.health.consecutive_failures >= self._degraded_after and (
            effective is WorkerStatus.HEALTHY
        ):
            effective = WorkerStatus.DEGRADED
        if effective is status:
            return record
        updated = record.model_copy(
            update={
                "health": record.health.model_copy(update={"status": effective}),
                "updated_at": now,
            }
        )
        if effective is WorkerStatus.OFFLINE:
            self._persist_offline(record.worker_id, now, last)
        return updated

    def _persist_offline(self, worker_id: str, now: datetime, last: datetime | None) -> None:
        """把推导出的 ``offline`` 写回库（幂等；已是 offline 时不重复写事件）。"""
        with self._store.write() as connection:
            cursor = connection.execute(
                "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ? AND status != ?",
                (WorkerStatus.OFFLINE.value, utc_iso(now), worker_id, WorkerStatus.OFFLINE.value),
            )
            if cursor.rowcount:
                self._append_event(
                    connection,
                    worker_id,
                    "marked_offline",
                    now,
                    {
                        "reason": "heartbeat timeout",
                        "last_heartbeat": None if last is None else utc_iso(last),
                    },
                )
