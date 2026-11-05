r"""副作用授权：审批令牌（HMAC）与 ``mcp_approvals`` 记录表。

设计目标与威胁模型
------------------
MCP 是外部 AI Agent 的接口，``submit_acquisition`` 会**真正消耗带宽与磁盘**。因此设计成
两阶段确认：``create_execution_plan`` 出具审批令牌 → 调用方显式提交令牌才允许执行。
这不是防恶意攻击（同机进程本来就能读文件），而是保证**Agent 的误操作不会直接变成下载**。

令牌格式（单行、可放进 JSON）::

    v1.<plan_id>.<expires_at_epoch>.<fingerprint_hex>.<nonce_hex>.<hmac_hex>

各部分
------
* ``v1``：令牌版本。未来换算法时旧令牌整体失效，不需要猜。
* ``plan_id``：被审批的计划。
* ``expires_at_epoch``：过期时刻（UTC epoch 秒），**参与签名**，所以无法被改大。
* ``fingerprint_hex``：计划指纹（``AcquisitionPlan.fingerprint()``，sha256 前 16 hex）。
* ``nonce_hex``：每次签发新生成的 16 字节随机数（参与签名）。它的作用是让**同一计划**
  重复签发得到的令牌互不相同，因此"重新生成计划"会立刻让旧令牌失效，而不是靠
  ``expires_at`` 恰好不同。随机性也顺带消除了"同一秒内签发 → 令牌相同"的隐患。
* ``hmac_hex``：``HMAC-SHA256(key, "v1\n<plan_id>\n<expires>\n<fingerprint>\n<nonce>")``。

签名密钥
--------
* 位置：``~/.rdh/approval.key``（可用 ``--approval-key`` / ``RDH_APPROVAL_KEY_FILE`` 覆盖）。
* 权限：文件 **0600**、父目录 **0700**；启动时若权限比这更宽，**自动收窄**并记日志。
* 生成：缺失时用 ``secrets.token_bytes(32)`` 生成并**以 O_EXCL + 0600 原子落盘**
  （``O_EXCL`` 避免并发首次启动互相覆盖对方的密钥）。
* 永不进入返回值/日志：本模块**没有任何**函数返回密钥内容。

重放与重复提交
--------------
1. **令牌一次性**：``mcp_approvals`` 表记录 ``consumed_at``；校验通过后立即用
   ``UPDATE ... WHERE consumed_at IS NULL`` 原子消费，并发提交只有一个能成功。
2. **计划状态门槛**：计划必须先由 ``create_execution_plan`` 落到 Manifest，
   且在库里的审批记录必须是 ``approved``（存在、未过期、未消费）。
3. **指纹绑定**：令牌里的 fingerprint 必须等于库中该计划当前任务的指纹，
   计划被任何方式改动后旧令牌自动失效。
4. **幂等返回**：同一 plan 已存在任务时为"重复提交"，直接返回既有任务组（不再次消费令牌、
   不重复入队），这既满足"重试无害"，也保证"重放令牌"拿不到第二次副作用。

为什么不写进 Manifest 的业务表
------------------------------
``core/models.py`` 的契约是只读 SSOT，计划本身没有"审批状态"字段，M5 也无权改它。
审批是 **MCP 接口层的概念**（别的接口可能用别的授权方式），因此独立成表；
表建在**同一个 SQLite 文件**里，与任务状态共享事务与 WAL 语义，但**不参与** Manifest 迁移
编号（``mcp_approvals`` 由本模块 ``CREATE TABLE IF NOT EXISTS`` 自建）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from researchdatahub.core.errors import ManifestError, RdhError
from researchdatahub.core.models import AcquisitionPlan

__all__ = [
    "APPROVAL_KEY_ENV_VAR",
    "APPROVAL_KEY_FILENAME",
    "APPROVAL_TABLE",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "MAX_APPROVAL_TTL_SECONDS",
    "MIN_APPROVAL_TTL_SECONDS",
    "ApprovalKey",
    "ApprovalRecord",
    "ApprovalStatus",
    "ApprovalStore",
    "PlanApprovalAuthorizer",
    "TokenMismatch",
    "default_approval_key_path",
    "plan_fingerprint",
]

logger = logging.getLogger(__name__)

TOKEN_VERSION: Final[str] = "v1"
TOKEN_SEPARATOR: Final[str] = "."
KEY_BYTES: Final[int] = 32
_TOKEN_NONCE_BYTES: Final[int] = 16
APPROVAL_TABLE: Final[str] = "mcp_approvals"
APPROVAL_KEY_FILENAME: Final[str] = "approval.key"
APPROVAL_KEY_ENV_VAR: Final[str] = "RDH_APPROVAL_KEY_FILE"
DEFAULT_APPROVAL_TTL_SECONDS: Final[int] = 3600
MIN_APPROVAL_TTL_SECONDS: Final[int] = 60
MAX_APPROVAL_TTL_SECONDS: Final[int] = 86_400
"""有效期上下界：低于 60 秒几乎不可能完成一次人机确认；超过一天则"审批"形同虚设。"""

_KEY_FILE_MODE: Final[int] = 0o600
_KEY_DIR_MODE: Final[int] = 0o700


def default_approval_key_path(root: Path) -> Path:
    """默认密钥路径：``<状态根>/approval.key``（默认状态根即 ``~/.rdh``）。"""
    return Path(root).expanduser() / APPROVAL_KEY_FILENAME


# --------------------------------------------------------------------------- #
# 计划指纹
# --------------------------------------------------------------------------- #
def plan_fingerprint(plan: AcquisitionPlan) -> str:
    """计划指纹：对**规范化 JSON**（键排序、紧凑分隔符）取 sha256 前 16 hex。

    为什么不用 ``AcquisitionPlan.fingerprint()``
    -------------------------------------------
    契约自带的 ``fingerprint()`` 是对 ``model_dump_json()`` 的原始字节做哈希，而 JSON 里
    ``dict`` 的**键顺序**来自插入顺序：同一个计划经过 SQLite 往返（``extensions`` 等列
    按存储顺序重建）后键序会变，字节不同但语义完全一致，于是"刚签发的令牌立刻被判为
    计划已改动"。改用排序键后，指纹只对**内容**敏感。

    这不削弱安全属性：计划任何字段（含 extensions 的值）变化都会改变指纹；我们只消除
    "键序"这一与语义无关的自由度。
    """
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256-16:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:  # pragma: no cover - 只在异常文件系统上触发
        logger.warning("cannot chmod %s to %o: %s", path, mode, exc)


class ApprovalKey:
    """HMAC 签名密钥（从磁盘加载或原子生成；内存中只保留字节，不暴露给调用方）。"""

    def __init__(self, path: Path, *, secret: bytes | None = None) -> None:
        self._path = Path(path).expanduser()
        self._secret = secret if secret is not None else self._load_or_create()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """密钥文件路径（可以跑在日志里，它不是秘密）。"""
        return self._path

    def _load_or_create(self) -> bytes:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(directory, _KEY_DIR_MODE)
        if self._path.exists():
            return self._read_existing()
        return self._create()

    def _read_existing(self) -> bytes:
        self._enforce_permissions()
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise ManifestError(
                "cannot read approval key", detail={"path": str(self._path), "reason": str(exc)}
            ) from exc
        secret = raw.strip()
        if len(secret) < KEY_BYTES:
            raise ManifestError(
                "approval key is too short (expected >= 32 bytes of hex/raw material)",
                detail={
                    "path": str(self._path),
                    "length": len(secret),
                    "hint": "delete the file to regenerate, all outstanding tokens become invalid",
                },
            )
        return secret

    def _enforce_permissions(self) -> None:
        """把宽于 0600 的文件权限收窄；无法确认权限时按失败处理。"""
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except OSError as exc:
            raise ManifestError(
                "cannot stat approval key", detail={"path": str(self._path), "reason": str(exc)}
            ) from exc
        if mode & ~_KEY_FILE_MODE:
            logger.warning(
                "approval key %s had mode %o; restricting to %o",
                self._path,
                mode,
                _KEY_FILE_MODE,
            )
            _chmod_quiet(self._path, _KEY_FILE_MODE)
            mode = stat.S_IMODE(self._path.stat().st_mode)
            if mode & ~_KEY_FILE_MODE:  # pragma: no cover - 只在异常文件系统上触发
                raise ManifestError(
                    "approval key permissions are too permissive and could not be fixed",
                    detail={"path": str(self._path), "mode": oct(mode)},
                )

    def _create(self) -> bytes:
        material = secrets.token_hex(KEY_BYTES).encode("ascii")
        try:
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _KEY_FILE_MODE,
            )
        except FileExistsError:
            # 并发的首个启动：对方已经建好了，改用它的密钥（不覆盖，否则令牌互相失效）。
            return self._read_existing()
        except OSError as exc:
            raise ManifestError(
                "cannot create approval key", detail={"path": str(self._path), "reason": str(exc)}
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(material + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            _chmod_quiet(self._path, _KEY_FILE_MODE)
        logger.info("generated new approval key at %s (mode 0600)", self._path)
        return material

    def _sign(self, plan_id: str, expires_at: int, fingerprint: str, nonce: str) -> str:
        message = f"{TOKEN_VERSION}\n{plan_id}\n{expires_at}\n{fingerprint}\n{nonce}".encode()
        with self._lock:
            digest = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return digest

    def issue(self, *, plan_id: str, expires_at: int, fingerprint: str) -> str:
        """签发令牌。返回的字符串**可以**交给调用方（它就是授权凭证本身）。"""
        if TOKEN_SEPARATOR in plan_id:
            raise ManifestError("plan id must not contain '.'", detail={"plan_id": plan_id})
        nonce = secrets.token_hex(_TOKEN_NONCE_BYTES)
        signature = self._sign(plan_id, expires_at, fingerprint, nonce)
        return TOKEN_SEPARATOR.join(
            [TOKEN_VERSION, plan_id, str(expires_at), fingerprint, nonce, signature]
        )

    def verify_signature(self, token: str, *, plan_id: str, fingerprint: str) -> int | None:
        """校验签名与嵌入的 plan_id/指纹，返回令牌里的过期时刻；格式错误返回 ``None``。

        用 :func:`hmac.compare_digest` 做常数时间比较，避免计时侧信道。
        """
        parts = token.split(TOKEN_SEPARATOR)
        if len(parts) != 6:
            return None
        version, token_plan, expires_raw, embedded, nonce, signature = parts
        if version != TOKEN_VERSION or token_plan != plan_id or embedded != fingerprint:
            return None
        if len(nonce) != _TOKEN_NONCE_BYTES * 2:
            return None
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return None
        expected = self._sign(plan_id, expires_at, fingerprint, nonce)
        if not hmac.compare_digest(expected, signature):
            return None
        return expires_at


# --------------------------------------------------------------------------- #
# 记录表
# --------------------------------------------------------------------------- #
class ApprovalStatus(StrEnum):
    """审批记录的生命周期状态。"""

    APPROVED = "approved"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRecord:
    """``mcp_approvals`` 中的一行（**不含令牌本身**：令牌只交给调用方一次）。"""

    plan_id: str
    fingerprint: str
    status: ApprovalStatus
    issued_at: str
    expires_at: str
    remaining_uses: int
    token_digest: str
    consumed_at: str | None = None
    task_group_id: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, object]:
        """JSON 友好表示（可安全写进返回值：``token_digest`` 是 sha256，不是令牌）。"""
        return {
            "plan_id": self.plan_id,
            "status": self.status.value,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "remaining_uses": self.remaining_uses,
            "consumed_at": self.consumed_at,
            "task_group_id": self.task_group_id,
            "fingerprint": self.fingerprint,
            "token_digest": self.token_digest,
        }


_CREATE_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {APPROVAL_TABLE} (
    plan_id         TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    status          TEXT NOT NULL,
    issued_at       TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    remaining_uses  INTEGER NOT NULL DEFAULT 1,
    token_digest    TEXT NOT NULL,
    consumed_at     TEXT,
    task_group_id   TEXT,
    note            TEXT
)
"""


def token_digest(token: str) -> str:
    """令牌的 sha256 摘要（用于审计关联，无法反推令牌）。"""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApprovalStore:
    """``mcp_approvals`` 表读写（每条连接一个实例；sqlite3 连接不可跨线程共享）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def ensure_schema(self) -> None:
        """建表（幂等）。在 Manifest 数据库初始化后调用。"""
        self._connection.execute(_CREATE_TABLE_SQL)
        self._connection.commit()

    def upsert(self, record: ApprovalRecord) -> None:
        """写入/覆盖某计划的审批记录（新签发时覆盖旧的，旧令牌随之作废）。"""
        self._connection.execute(
            f"""
            INSERT INTO {APPROVAL_TABLE} (
                plan_id, fingerprint, status, issued_at, expires_at,
                remaining_uses, token_digest, consumed_at, task_group_id, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                status=excluded.status,
                issued_at=excluded.issued_at,
                expires_at=excluded.expires_at,
                remaining_uses=excluded.remaining_uses,
                token_digest=excluded.token_digest,
                consumed_at=excluded.consumed_at,
                task_group_id=excluded.task_group_id,
                note=excluded.note
            """,
            (
                record.plan_id,
                record.fingerprint,
                record.status.value,
                record.issued_at,
                record.expires_at,
                record.remaining_uses,
                record.token_digest,
                record.consumed_at,
                record.task_group_id,
                record.note,
            ),
        )
        self._connection.commit()

    def get(self, plan_id: str) -> ApprovalRecord | None:
        """读取某计划的审批记录；不存在返回 ``None``。"""
        row = self._connection.execute(
            f"SELECT * FROM {APPROVAL_TABLE} WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def consume(self, plan_id: str, *, digest: str, consumed_at: str) -> bool:
        """原子消费：只有 ``approved`` 且摘要匹配的行会被改成 ``consumed``。

        返回 ``True`` 表示本次调用成功消费（唯一一个），``False`` 表示已被消费/状态不符。
        """
        cursor = self._connection.execute(
            f"""
            UPDATE {APPROVAL_TABLE}
               SET status = ?, consumed_at = ?
             WHERE plan_id = ? AND status = ? AND remaining_uses > 0 AND token_digest = ?
            """,
            (
                ApprovalStatus.CONSUMED.value,
                consumed_at,
                plan_id,
                ApprovalStatus.APPROVED.value,
                digest,
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def mark_expired(self, plan_id: str, *, now: datetime) -> None:
        """把已过期的 ``approved`` 记录标记为 ``expired``（可审计，不删除）。"""
        self._connection.execute(
            f"""
            UPDATE {APPROVAL_TABLE}
               SET status = ?, note = COALESCE(note, 'expired before use')
             WHERE plan_id = ? AND status = ? AND expires_at < ?
            """,
            (
                ApprovalStatus.EXPIRED.value,
                plan_id,
                ApprovalStatus.APPROVED.value,
                _iso(now),
            ),
        )
        self._connection.commit()

    def attach_task_group(self, plan_id: str, task_group_id: str) -> None:
        """记录该计划对应的任务组 ID（便于回溯"哪次提交产生了这组任务"）。"""
        self._connection.execute(
            f"UPDATE {APPROVAL_TABLE} SET task_group_id = ? WHERE plan_id = ?",
            (task_group_id, plan_id),
        )
        self._connection.commit()


def _record_from_row(row: sqlite3.Row | tuple[object, ...]) -> ApprovalRecord:
    keys = (
        "plan_id",
        "fingerprint",
        "status",
        "issued_at",
        "expires_at",
        "remaining_uses",
        "token_digest",
        "consumed_at",
        "task_group_id",
        "note",
    )
    data = dict(zip(keys, row, strict=False))
    return ApprovalRecord(
        plan_id=str(data["plan_id"]),
        fingerprint=str(data["fingerprint"]),
        status=ApprovalStatus(str(data["status"])),
        issued_at=str(data["issued_at"]),
        expires_at=str(data["expires_at"]),
        remaining_uses=int(str(data["remaining_uses"])),
        token_digest=str(data["token_digest"]),
        consumed_at=None if data["consumed_at"] is None else str(data["consumed_at"]),
        task_group_id=None if data["task_group_id"] is None else str(data["task_group_id"]),
        note=None if data["note"] is None else str(data["note"]),
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TokenMismatch(RdhError):
    """令牌校验失败（内部用；对外由 tools 层转成 ``approval_invalid``）。"""

    default_code = "RDH_APPROVAL_INVALID"


# --------------------------------------------------------------------------- #
# 高层授权对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApprovalDecision:
    """一次令牌校验的结论。``ok=False`` 时 ``reason`` 是给 Agent 的具体原因。"""

    ok: bool
    reason: str = ""
    record: ApprovalRecord | None = None


class PlanApprovalAuthorizer:
    """把"签发 / 校验 / 消费"三件事收拢到一个对象里（tools 层只调这里）。

    Parameters
    ----------
    key:
        HMAC 密钥（``None`` 时按 ``key_path`` 加载/生成）。
    key_path:
        密钥文件路径。
    clock:
        可注入时钟（测试用）。
    """

    def __init__(
        self,
        *,
        key_path: Path,
        clock: Callable[[], datetime] | None = None,
        key: ApprovalKey | None = None,
    ) -> None:
        self._key = key if key is not None else ApprovalKey(key_path)
        self._clock = clock or _utc_now

    @property
    def key_path(self) -> Path:
        """密钥文件路径。"""
        return self._key.path

    def issue(
        self,
        store: ApprovalStore,
        *,
        plan_id: str,
        fingerprint: str,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> tuple[str, ApprovalRecord]:
        """签发令牌并落审批记录。

        Returns
        -------
        tuple[str, ApprovalRecord]
            令牌（**只在这里出现一次**）与可审计的记录（不含令牌）。
        """
        if not MIN_APPROVAL_TTL_SECONDS <= ttl_seconds <= MAX_APPROVAL_TTL_SECONDS:
            raise TokenMismatch(
                f"approval ttl must be within [{MIN_APPROVAL_TTL_SECONDS}, "
                f"{MAX_APPROVAL_TTL_SECONDS}] seconds",
                detail={"ttl_seconds": ttl_seconds},
            )
        now = self._clock()
        expires_at = int(now.timestamp()) + int(ttl_seconds)
        token = self._key.issue(plan_id=plan_id, expires_at=expires_at, fingerprint=fingerprint)
        record = ApprovalRecord(
            plan_id=plan_id,
            fingerprint=fingerprint,
            status=ApprovalStatus.APPROVED,
            issued_at=_iso(now),
            expires_at=_iso(datetime.fromtimestamp(expires_at, UTC)),
            remaining_uses=1,
            token_digest=token_digest(token),
        )
        stored = ApprovalRecord(
            plan_id=record.plan_id,
            fingerprint=record.fingerprint,
            status=record.status,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            remaining_uses=record.remaining_uses,
            token_digest=record.token_digest,
        )
        store.upsert(stored)
        return token, stored

    def verify(
        self,
        store: ApprovalStore,
        *,
        plan_id: str,
        token: str,
        fingerprint: str,
    ) -> ApprovalDecision:
        """校验令牌与记录；**不改任何状态**（消费是单独一步，见 :meth:`consume`）。"""
        record = store.get(plan_id)
        if record is None:
            return ApprovalDecision(False, "no approval record for this plan; create a plan first")
        if record.status is ApprovalStatus.CONSUMED:
            return ApprovalDecision(False, "approval token has already been used (one-shot)")
        if record.status is not ApprovalStatus.APPROVED:
            return ApprovalDecision(False, f"approval record status is {record.status.value}")
        if record.fingerprint != fingerprint:
            return ApprovalDecision(
                False, "plan content changed after the token was issued; re-plan for a new token"
            )
        expires_at = self._key.verify_signature(token, plan_id=plan_id, fingerprint=fingerprint)
        if expires_at is None:
            return ApprovalDecision(False, "approval token signature or plan binding is invalid")
        if expires_at <= int(self._clock().timestamp()):
            return ApprovalDecision(False, "approval token has expired")
        if not hmac.compare_digest(record.token_digest, token_digest(token)):
            return ApprovalDecision(False, "approval token does not match the issued record")
        return ApprovalDecision(True, record=record)

    def consume(self, store: ApprovalStore, *, plan_id: str, token: str) -> bool:
        """原子消费令牌；返回 ``False`` 表示并发/重复提交输了竞争。"""
        return store.consume(plan_id, digest=token_digest(token), consumed_at=_iso(self._clock()))
