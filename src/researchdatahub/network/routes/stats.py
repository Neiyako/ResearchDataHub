"""Route 性能记录（真实测量写入 SQLite ``route_stats``）与选路打分。

记录什么、为什么
----------------
每次**真实传输结束**（无论成败）都写一条样本：首字节延迟、总耗时、字节数、是否重试、
是否成功。调度决策只相信这些数字；没有样本时如实标注冷启动
（:data:`~researchdatahub.network.routes.models.NO_MEASUREMENT`），**绝不按国家/地区猜速度**。

打分公式（可解释、可测试）
--------------------------
::

    scale = 1 - exp(-samples / SAMPLE_CONFIDENCE)
    score = scale * (5.0 * success_rate + 2.0 * min(throughput_bps / P50_BPS, 1.0)
                     + 1.0 * (1 - latency_penalty)
                     + 1.0 * (1 - size_penalty) + 0.5 * resume_bonus)

* ``scale``：样本越少越接近 0，避免"一条样本定终身"；
* ``success_rate`` 权重最高：失败即重下，代价远大于慢；
* ``throughput`` 按 50 MiB/s 归一（家用/院系出口量级），只体现"更快"而不鼓励极慢链路；
* ``latency_penalty``：首字节延迟 2s 以上记满惩罚；
* ``size_penalty``：单文件超过 4 GiB 时对不支持续传的链路额外惩罚；
* 冷启动（``samples == 0``）返回 ``None``，排序退化为"可用性 + 稳定字典序"。
"""

from __future__ import annotations

import math
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from researchdatahub.network.routes.models import Route, RouteStats
from researchdatahub.network.workers.store import WorkerStore, json_dumps, utc_iso

__all__ = [
    "LATENCY_PENALTY_MS",
    "P50_BYTES_PER_SECOND",
    "SAMPLE_CONFIDENCE",
    "SIZE_PENALTY_BYTES",
    "RouteStatsStore",
    "score_route",
]

#: 样本置信度标尺：约 5 条样本后 scale 达到 0.63，10 条达到 0.86。
SAMPLE_CONFIDENCE: Final[float] = 5.0
#: 吞吐归一分母（字节/秒）：约 50 MiB/s。
P50_BYTES_PER_SECOND: Final[float] = 50.0 * 1024 * 1024
#: 首字节延迟惩罚上限（毫秒）：≥2s 记满惩罚。
LATENCY_PENALTY_MS: Final[float] = 2000.0
#: 大文件阈值（字节）：4 GiB；超过后不支持续传的链路额外扣分。
SIZE_PENALTY_BYTES: Final[int] = 4 * 1024**3


def _now() -> datetime:
    return datetime.now(UTC)


def score_route(
    stats: RouteStats,
    *,
    supports_resume: bool,
    size_bytes: int,
) -> tuple[float | None, str]:
    """计算一条链路的实测得分与人类可读依据。

    Returns
    -------
    tuple[float | None, str]
        ``(score, rationale)``；``score is None`` 表示该链路**没有任何实测样本**。
    """
    if stats.samples == 0:
        return None, "no measured samples; ranked by availability only"
    scale = 1.0 - math.exp(-stats.samples / SAMPLE_CONFIDENCE)
    success = stats.success_rate or 0.0
    throughput = stats.throughput_bps or 0.0
    latency = stats.latency_ms or LATENCY_PENALTY_MS
    throughput_ratio = min(throughput / P50_BYTES_PER_SECOND, 1.0)
    latency_penalty = min(latency / LATENCY_PENALTY_MS, 1.0)
    size_penalty = 0.0
    if size_bytes > SIZE_PENALTY_BYTES and not supports_resume:
        size_penalty = 1.0
    resume_bonus = 1.0 if supports_resume else 0.0
    score = scale * (
        5.0 * success
        + 2.0 * throughput_ratio
        + 1.0 * (1.0 - latency_penalty)
        + 1.0 * (1.0 - size_penalty)
        + 0.5 * resume_bonus
    )
    rationale = (
        f"measured samples={stats.samples} success_rate={success:.2f} "
        f"throughput_bps={throughput:.0f} latency_ms={latency:.0f} "
        f"resume={'yes' if supports_resume else 'no'}"
    )
    return score, rationale


class RouteStatsStore:
    """``route_stats`` 表的读写（与 worker 注册共用同一个控制面库）。

    Parameters
    ----------
    store:
        控制面库句柄（:class:`~researchdatahub.network.workers.store.WorkerStore`）。
    """

    def __init__(self, store: WorkerStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._store.initialize()

    def record(
        self,
        route: Route,
        *,
        success: bool,
        latency_ms: float,
        duration_s: float,
        bytes_transferred: int,
        retried: bool,
        outcome: str,
        provider_id: str | None = None,
        at: datetime | None = None,
    ) -> RouteStats:
        """写入一条**实测**样本（成功与失败都写；失败样本同样计入样本数）。

        Parameters
        ----------
        route:
            产生该样本的链路。
        success:
            是否成功（部分成功按失败计：可重试失败也要计入失败率）。
        latency_ms:
            首字节延迟（毫秒）；未知时传 0 会被当作"极快"，因此调用方应尽量测真实值。
        duration_s:
            传输总耗时（秒，含失败重试的耗时）。
        bytes_transferred:
            实际落盘/接收的字节数。
        retried:
            本次传输是否发生过内部重试。
        outcome:
            结果标签（``succeeded`` / ``failed`` / ``skipped`` 等）。
        provider_id:
            统计维度；缺省使用 ``route.provider_id``。
        at:
            样本时间（默认当前 UTC 时间）。
        """
        stamp = at or _now()
        provider = provider_id or route.provider_id
        latency = max(0.0, float(latency_ms))
        duration = max(0.0, float(duration_s))
        transferred = max(0, int(bytes_transferred))
        with self._lock, self._store.write() as connection:
            row = connection.execute(
                "SELECT * FROM route_stats WHERE route_id = ? AND source_id = ?",
                (route.route_id, provider),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO route_stats (
                        route_id, source_id, samples, successes, failures, retry_count,
                        transferred_bytes, latency_ms_sum, transfer_duration_s,
                        last_outcome, last_success_at, last_failure_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route.route_id,
                        provider,
                        1 if success else 0,
                        0 if success else 1,
                        1 if retried else 0,
                        transferred,
                        latency,
                        duration,
                        outcome,
                        utc_iso(stamp) if success else None,
                        None if success else utc_iso(stamp),
                        utc_iso(stamp),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE route_stats
                       SET samples = samples + 1,
                           successes = successes + ?,
                           failures = failures + ?,
                           retry_count = retry_count + ?,
                           transferred_bytes = transferred_bytes + ?,
                           latency_ms_sum = latency_ms_sum + ?,
                           transfer_duration_s = transfer_duration_s + ?,
                           last_outcome = ?,
                           last_success_at = COALESCE(?, last_success_at),
                           last_failure_at = COALESCE(?, last_failure_at),
                           updated_at = ?
                     WHERE route_id = ? AND source_id = ?
                    """,
                    (
                        1 if success else 0,
                        0 if success else 1,
                        1 if retried else 0,
                        transferred,
                        latency,
                        duration,
                        outcome,
                        utc_iso(stamp) if success else None,
                        None if success else utc_iso(stamp),
                        utc_iso(stamp),
                        route.route_id,
                        provider,
                    ),
                )
        return self.get(route.route_id, provider_id=provider) or RouteStats(
            route_id=route.route_id, provider_id=provider
        )

    def get(self, route_id: str, *, provider_id: str | None = None) -> RouteStats | None:
        """读取一条链路的统计；``provider_id`` 为空时按全部提供方聚合。"""
        rows = self._store.read(
            lambda connection: connection.execute(
                "SELECT * FROM route_stats WHERE route_id = ?", (route_id,)
            ).fetchall()
        )
        if not rows:
            return None
        if provider_id is None:
            return _aggregate(route_id, rows)
        for row in rows:
            if str(row["source_id"]) == provider_id:
                return _row_to_stats(row, provider_id)
        return None

    def list_stats(self) -> Sequence[RouteStats]:
        """列出所有 ``(route, provider)`` 统计（按 route_id 升序）。"""
        rows = self._store.read(
            lambda connection: connection.execute(
                "SELECT * FROM route_stats ORDER BY route_id, source_id"
            ).fetchall()
        )
        return [_row_to_stats(row, str(row["source_id"])) for row in rows]

    def reset(self, route_id: str | None = None) -> int:
        """清空统计（``route_id`` 为空时清空全部），返回删除行数（用于重新校准）。"""
        with self._lock, self._store.write() as connection:
            if route_id is None:
                cursor = connection.execute("DELETE FROM route_stats")
            else:
                cursor = connection.execute(
                    "DELETE FROM route_stats WHERE route_id = ?", (route_id,)
                )
            return int(cursor.rowcount)

    def audit_event(self, worker_id: str, event_type: str, payload: dict[str, object]) -> None:
        """把链路事件写进 ``worker_events``（与节点审计同一条时间线）。"""
        with self._lock, self._store.write() as connection:
            connection.execute(
                "INSERT INTO worker_events (worker_id, event_type, occurred_at, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (worker_id, event_type, utc_iso(_now()), json_dumps(payload)),
            )


def _row_to_stats(row: sqlite3.Row, provider_id: str) -> RouteStats:
    """把 ``route_stats`` 行转成 :class:`RouteStats`。"""

    def _optional_iso(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        return datetime.fromisoformat(str(value))

    return RouteStats(
        route_id=str(row["route_id"]),
        provider_id=provider_id,
        samples=int(row["samples"]),
        successes=int(row["successes"]),
        failures=int(row["failures"]),
        retry_count=int(row["retry_count"]),
        transferred_bytes=int(row["transferred_bytes"]),
        latency_ms_total=float(row["latency_ms_sum"]),
        transfer_duration_s=float(row["transfer_duration_s"]),
        last_outcome=None if row["last_outcome"] is None else str(row["last_outcome"]),
        last_outcome_at=_optional_iso(row["updated_at"]),
        last_success_at=_optional_iso(row["last_success_at"]),
        last_failure_at=_optional_iso(row["last_failure_at"]),
        updated_at=_optional_iso(row["updated_at"]),
    )


def _aggregate(route_id: str, rows: Sequence[sqlite3.Row]) -> RouteStats:
    """跨提供方聚合（用于只关心"这条链路整体表现"的展示）。"""
    return RouteStats(
        route_id=route_id,
        provider_id="",
        samples=sum(int(row["samples"]) for row in rows),
        successes=sum(int(row["successes"]) for row in rows),
        failures=sum(int(row["failures"]) for row in rows),
        retry_count=sum(int(row["retry_count"]) for row in rows),
        transferred_bytes=sum(int(row["transferred_bytes"]) for row in rows),
        latency_ms_total=sum(float(row["latency_ms_sum"]) for row in rows),
        transfer_duration_s=sum(float(row["transfer_duration_s"]) for row in rows),
        last_outcome=str(rows[-1]["last_outcome"]) if rows[-1]["last_outcome"] else None,
        updated_at=(
            None
            if rows[-1]["updated_at"] in (None, "")
            else datetime.fromisoformat(str(rows[-1]["updated_at"]))
        ),
    )
