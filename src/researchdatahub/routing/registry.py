"""Route experience registry：JSON 持久化，不引入数据库。

注册表刻意保持小：一个 route 的最近探活、基准、成功率和连续失败次数足以完成第一版
过期/降级决策。写入使用同目录临时文件 + ``os.replace``，避免半写文件；默认路径由上层
组合根决定，库本身不偷偷写入用户主目录。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from researchdatahub.core.errors import ConfigError
from researchdatahub.core.models import RouteQualification
from researchdatahub.routing.models import (
    BenchmarkResult,
    CheckStatus,
    ProbeResult,
    Route,
    RouteExperience,
    RouteStatus,
)

__all__ = ["RouteExperienceRegistry"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RouteExperienceRegistry:
    """线程安全的 route 经验注册表。

    Parameters
    ----------
    path:
        JSON 文件路径；``None`` 时只保存在内存中（测试与纯规划场景）。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path).expanduser() if path is not None else None
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        self._records: dict[str, RouteExperience] = {}
        self._load()

    @property
    def path(self) -> Path | None:
        """持久化路径（只读）。"""
        return self._path

    def get(self, route_id: str) -> RouteExperience | None:
        """读取一条 route 经验；不存在返回 ``None``。"""
        with self._lock:
            return self._records.get(route_id)

    def list_records(self) -> tuple[RouteExperience, ...]:
        """按 route ID 稳定排序返回全部记录。"""
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    def upsert(self, route: Route) -> RouteExperience:
        """写入/更新 route 定义，不改动已有历史（除非 route 自带更新历史）。"""
        now = self._clock()
        history = route.history
        if history.updated_at is None:
            history = history.model_copy(update={"updated_at": now})
        updated_route = route.model_copy(update={"history": history})
        record = RouteExperience(route=updated_route, history=history, updated_at=now)
        with self._lock:
            previous = self._records.get(route.id)
            if previous is not None:
                record = record.model_copy(update={"history": previous.history})
                record = record.model_copy(
                    update={"route": updated_route.model_copy(update={"history": previous.history})}
                )
            self._records[route.id] = record
            self._persist_locked()
        return record

    def record_probe(
        self,
        route: Route,
        result: ProbeResult,
        *,
        qualify: bool = False,
    ) -> RouteExperience:
        """记录一次探活结果，并按结果更新状态与 qualification。"""
        now = self._clock()
        if result.status is CheckStatus.PASS:
            route_status = RouteStatus.AVAILABLE
        elif result.status is CheckStatus.FAIL:
            route_status = RouteStatus.UNAVAILABLE
        else:
            route_status = RouteStatus.UNKNOWN

        if route_status is RouteStatus.UNAVAILABLE:
            qualification = RouteQualification.FAILED
        elif qualify and result.is_success:
            qualification = RouteQualification.QUALIFIED
        elif route.qualification is RouteQualification.QUALIFIED:
            qualification = route.qualification
        else:
            qualification = RouteQualification.DISCOVERED

        last_verified = now if result.is_success else route.last_verified
        history = route.history.model_copy(
            update={
                "last_verified": last_verified,
                "benchmark": result.benchmark or route.history.benchmark,
                "updated_at": now,
            }
        )
        updated = route.model_copy(
            update={
                "status": route_status,
                "qualification": qualification,
                "capabilities": result.capabilities or route.capabilities,
                "benchmark": result.benchmark or route.benchmark,
                "last_verified": last_verified,
                "history": history,
            }
        )
        return self._save_updated(updated, now)

    def record_transfer(
        self,
        route: Route,
        *,
        success: bool,
        benchmark: BenchmarkResult | None = None,
        failure_reason: str | None = None,
    ) -> RouteExperience:
        """记录真实传输结果；连续失败降级，成功传输可恢复 qualification。"""
        now = self._clock()
        history = route.history
        attempts = history.attempts + 1
        successes = history.successes + (1 if success else 0)
        failures = history.failures + (0 if success else 1)
        recent_failures = 0 if success else history.recent_failures + 1
        success_rate = successes / attempts if attempts else None

        last_verified: datetime | None
        if success:
            qualification = (
                RouteQualification.QUALIFIED
                if route.qualification
                in (
                    RouteQualification.QUALIFIED,
                    RouteQualification.DEGRADED,
                    RouteQualification.EXPIRED,
                )
                else route.qualification
            )
            route_status = RouteStatus.AVAILABLE
            last_verified = now
        elif recent_failures >= 3:
            qualification = RouteQualification.FAILED
            route_status = RouteStatus.UNAVAILABLE
            last_verified = route.last_verified
        else:
            qualification = RouteQualification.DEGRADED
            route_status = RouteStatus.UNKNOWN
            last_verified = route.last_verified

        updated_history = history.model_copy(
            update={
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "recent_failures": recent_failures,
                "success_rate": success_rate,
                "benchmark": benchmark or history.benchmark,
                "last_verified": last_verified,
                "updated_at": now,
            }
        )
        updated = route.model_copy(
            update={
                "status": route_status,
                "qualification": qualification,
                "benchmark": benchmark or route.benchmark,
                "last_verified": last_verified,
                "history": updated_history,
                "notes": (
                    [*route.notes, f"last transfer failed: {failure_reason}"]
                    if failure_reason
                    else route.notes
                )[-32:],
            }
        )
        return self._save_updated(updated, now)

    def expire(self, route_id: str) -> RouteExperience | None:
        """把 route 标记为过期；下一次使用前必须重新 probe。"""
        with self._lock:
            record = self._records.get(route_id)
            if record is None:
                return None
            now = self._clock()
            route = record.route.model_copy(
                update={"qualification": RouteQualification.EXPIRED, "history": record.history}
            )
            updated = record.model_copy(update={"route": route, "updated_at": now})
            self._records[route_id] = updated
            self._persist_locked()
            return updated

    def clear(self) -> None:
        """删除全部经验（测试/校准用途）。"""
        with self._lock:
            self._records.clear()
            self._persist_locked()

    def _save_updated(self, route: Route, now: datetime) -> RouteExperience:
        history = route.history.model_copy(update={"updated_at": now})
        updated_route = route.model_copy(update={"history": history})
        record = RouteExperience(route=updated_route, history=history, updated_at=now)
        with self._lock:
            self._records[route.id] = record
            self._persist_locked()
        return record

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"cannot read route experience registry: {self._path}",
                detail={"path": str(self._path), "error_type": type(exc).__name__},
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
            raise ConfigError(
                "route experience registry must be a JSON object with schema_version '1.0'",
                detail={"path": str(self._path)},
            )
        records = raw.get("records")
        if not isinstance(records, dict):
            raise ConfigError(
                "route experience registry missing records object",
                detail={"path": str(self._path)},
            )
        loaded: dict[str, RouteExperience] = {}
        for key, payload in records.items():
            try:
                record = RouteExperience.model_validate(payload)
            except ValidationError as exc:
                raise ConfigError(
                    f"invalid route experience record {key!r}",
                    detail={"path": str(self._path), "errors": exc.errors(include_url=False)},
                ) from exc
            loaded[record.route.id] = record
        self._records = loaded

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized: dict[str, object] = {}
        for key, value in sorted(self._records.items()):
            record_payload = value.model_dump(mode="json")
            route_payload = record_payload.get("route")
            if isinstance(route_payload, dict):
                # capability_summary 是 computed field，读回时由 capabilities 重新计算。
                route_payload.pop("capability_summary", None)
            serialized[key] = record_payload
        payload = {"schema_version": "1.0", "records": serialized}
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        except OSError as exc:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
            raise ConfigError(
                f"cannot persist route experience registry: {self._path}",
                detail={"path": str(self._path), "error_type": type(exc).__name__},
            ) from exc

    def merge_many(self, routes: Iterable[Route]) -> tuple[RouteExperience, ...]:
        """批量合并 route 定义（保留已有历史），返回合并后的记录。"""
        return tuple(self.upsert(route) for route in routes)
