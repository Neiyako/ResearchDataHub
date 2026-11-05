"""审批令牌的安全属性测试：HMAC 绑定、过期、一次性、密钥权限、dirty-state 拒绝。

这批测试对应任务书 §4 的每一条要求，逐条给出证据；也覆盖 ``docs/mcp.md`` 里承诺
"失败不产生副作用"的部分（入队数、任务状态、审批记录都不变）。
"""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_mcp.conftest import Harness

from researchdatahub.interfaces.mcp.auth import (
    APPROVAL_TABLE,
    ApprovalKey,
    ApprovalStatus,
    ApprovalStore,
    PlanApprovalAuthorizer,
    default_approval_key_path,
    plan_fingerprint,
    token_digest,
)
from researchdatahub.interfaces.mcp.errors import ApprovalInvalidError

# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #


def test_key_is_created_with_0600_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "state" / "approval.key"
    ApprovalKey(key_path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    directory_mode = stat.S_IMODE(key_path.parent.stat().st_mode)
    assert directory_mode == 0o700, f"expected 0700, got {oct(directory_mode)}"


def test_key_is_stable_across_instances(tmp_path: Path) -> None:
    key_path = tmp_path / "approval.key"
    first = ApprovalKey(key_path)
    token = first.issue(plan_id="p1", expires_at=10, fingerprint="fp")
    second = ApprovalKey(key_path)
    assert second.verify_signature(token, plan_id="p1", fingerprint="fp") == 10


def test_loose_permissions_are_tightened(tmp_path: Path) -> None:
    key_path = tmp_path / "approval.key"
    ApprovalKey(key_path)
    os.chmod(key_path, 0o644)
    ApprovalKey(key_path)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_short_key_is_rejected(tmp_path: Path) -> None:
    from researchdatahub.core.errors import ManifestError

    key_path = tmp_path / "approval.key"
    key_path.write_bytes(b"tooshort\n")
    with pytest.raises(ManifestError, match="too short"):
        ApprovalKey(key_path)


def test_default_key_path_follows_root(tmp_path: Path) -> None:
    assert default_approval_key_path(tmp_path) == tmp_path / "approval.key"


def test_key_secret_never_appears_in_repr(tmp_path: Path) -> None:
    key = ApprovalKey(tmp_path / "approval.key")
    secret = key._secret  # 测试需要确认密钥本身不会外泄
    token = key.issue(plan_id="p1", expires_at=10, fingerprint="fp")
    assert secret.decode() not in token
    rendered = repr(key)
    assert secret.decode() not in rendered
    assert "secret" not in rendered.lower()


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #


def test_signature_rejects_tampered_expiry(tmp_path: Path) -> None:
    key = ApprovalKey(tmp_path / "approval.key")
    token = key.issue(plan_id="p1", expires_at=100, fingerprint="fp")
    parts = token.split(".")
    parts[2] = "999999"
    assert key.verify_signature(".".join(parts), plan_id="p1", fingerprint="fp") is None


def test_signature_rejects_tampered_fingerprint(tmp_path: Path) -> None:
    key = ApprovalKey(tmp_path / "approval.key")
    token = key.issue(plan_id="p1", expires_at=100, fingerprint="fp")
    parts = token.split(".")
    parts[3] = "other-fingerprint"
    assert key.verify_signature(".".join(parts), plan_id="p1", fingerprint="fp") is None


def test_signature_rejects_other_plan(tmp_path: Path) -> None:
    key = ApprovalKey(tmp_path / "approval.key")
    token = key.issue(plan_id="p1", expires_at=100, fingerprint="fp")
    assert key.verify_signature(token, plan_id="p2", fingerprint="fp") is None


def test_signature_rejects_other_key(tmp_path: Path) -> None:
    key_a = ApprovalKey(tmp_path / "a.key")
    key_b = ApprovalKey(tmp_path / "b.key")
    token = key_a.issue(plan_id="p1", expires_at=100, fingerprint="fp")
    assert key_b.verify_signature(token, plan_id="p1", fingerprint="fp") is None


@pytest.mark.parametrize("token", ["", "v1", "v1.p1", "v1.p1.x.fp.sig", "v2.p1.1.fp.sig", "a.b.c"])
def test_signature_rejects_malformed_tokens(tmp_path: Path, token: str) -> None:
    key = ApprovalKey(tmp_path / "approval.key")
    assert key.verify_signature(token, plan_id="p1", fingerprint="fp") is None


def test_plan_id_with_dot_is_rejected(tmp_path: Path) -> None:
    from researchdatahub.core.errors import ManifestError

    key = ApprovalKey(tmp_path / "approval.key")
    with pytest.raises(ManifestError):
        key.issue(plan_id="a.b", expires_at=1, fingerprint="fp")


# --------------------------------------------------------------------------- #
# 记录表
# --------------------------------------------------------------------------- #


def _store(tmp_path: Path) -> ApprovalStore:
    connection = sqlite3.connect(tmp_path / "approvals.db")
    connection.row_factory = sqlite3.Row
    store = ApprovalStore(connection)
    store.ensure_schema()
    return store


def test_store_roundtrip_and_consume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key", clock=clock)
    token, record = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    stored = store.get("p1")
    assert stored is not None
    assert stored.status is ApprovalStatus.APPROVED
    assert stored.token_digest == record.token_digest == token_digest(token)
    assert token not in str(stored.as_dict())  # 记录里没有令牌本身

    decision = authorizer.verify(store, plan_id="p1", token=token, fingerprint="fp")
    assert decision.ok
    assert authorizer.consume(store, plan_id="p1", token=token) is True
    after = store.get("p1")
    assert after is not None
    assert after.status is ApprovalStatus.CONSUMED
    assert after.consumed_at is not None
    # 第二次消费必须失败（一次性）
    assert authorizer.consume(store, plan_id="p1", token=token) is False


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key", clock=lambda: now)
    token, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=60)
    later = PlanApprovalAuthorizer(
        key_path=tmp_path / "k.key", clock=lambda: now + timedelta(seconds=61)
    )
    decision = later.verify(store, plan_id="p1", token=token, fingerprint="fp")
    assert not decision.ok
    assert "expired" in decision.reason


def test_ttl_outside_bounds_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key")
    with pytest.raises(Exception, match="ttl"):
        authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=10)
    with pytest.raises(Exception, match="ttl"):
        authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=10**6)


def test_fingerprint_change_invalidates_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key")
    token, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp-a", ttl_seconds=600)
    decision = authorizer.verify(store, plan_id="p1", token=token, fingerprint="fp-b")
    assert not decision.ok
    assert "changed" in decision.reason


def test_reenrol_overwrites_previous_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key")
    old_token, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    new_token, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    assert authorizer.verify(store, plan_id="p1", token=old_token, fingerprint="fp").ok is False
    assert authorizer.verify(store, plan_id="p1", token=new_token, fingerprint="fp").ok is True


def test_never_issued_plan_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key")
    token, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    decision = authorizer.verify(store, plan_id="p2", token=token, fingerprint="fp")
    assert not decision.ok
    assert "no approval record" in decision.reason


def test_expired_record_marked_expired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key", clock=lambda: now)
    authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=60)
    store.mark_expired("p1", now=now + timedelta(seconds=120))
    record = store.get("p1")
    assert record is not None
    assert record.status is ApprovalStatus.EXPIRED


def test_approval_table_name_is_stable() -> None:
    assert APPROVAL_TABLE == "mcp_approvals"


def test_attach_task_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key")
    authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    store.attach_task_group("p1", "task-group-9")
    record = store.get("p1")
    assert record is not None
    assert record.task_group_id == "task-group-9"


# --------------------------------------------------------------------------- #
# 端到端：令牌被拒绝时不得产生任何副作用
# --------------------------------------------------------------------------- #


def _plan(harness: Harness, body: bytes) -> tuple[str, str]:
    """建一个可执行的计划并返回 ``(plan_id, token)``（真实 planner + 真实落库）。"""
    from tests.unit.test_mcp.conftest import REQUEST_DOCUMENT

    from researchdatahub.interfaces.mcp.server import call_tool

    harness.add_source([harness.make_asset(http_path="/data/a.nc", body=body, file_name="a.nc")])
    context = harness.context()
    envelope = call_tool(
        context, "create_execution_plan", {"request": REQUEST_DOCUMENT, "approval_ttl_seconds": 600}
    )
    assert envelope["ok"] is True, envelope
    return envelope["data"]["plan_id"], envelope["data"]["approval_token"]


def _task_states(context: object) -> dict[str, str]:
    from researchdatahub.interfaces.mcp.context import McpContext

    assert isinstance(context, McpContext)
    with context.repository() as repo:
        return {
            task.task_id: task.state.value
            for task in repo.list_tasks()  # type: ignore[union-attr]
        }


def test_invalid_token_does_not_queue_anything(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_id, token = _plan(harness, body)
    context = harness.context()
    envelope = call_tool(
        context, "submit_acquisition", {"plan_id": plan_id, "approval_token": token + "x"}
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "approval_invalid"
    assert envelope["error"]["detail"]["side_effects_performed"] is False
    assert set(_task_states(context).values()) == {"PLANNED"}
    # 令牌没有被消费：正确令牌随后仍然可用
    retry = call_tool(context, "submit_acquisition", {"plan_id": plan_id, "approval_token": token})
    assert retry["ok"] is True
    assert retry["data"]["queued_tasks"] == 1


def test_missing_token_is_rejected_by_schema(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_id, _ = _plan(harness, body)
    context = harness.context()
    envelope = call_tool(context, "submit_acquisition", {"plan_id": plan_id})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"
    assert set(_task_states(context).values()) == {"PLANNED"}


def test_unknown_plan_is_not_found(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    _plan(harness, body)
    context = harness.context()
    envelope = call_tool(
        context,
        "submit_acquisition",
        {"plan_id": "plan-does-not-exist", "approval_token": "x" * 32},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"


def test_token_from_other_plan_is_rejected(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_a, token_a = _plan(harness, body)
    context = harness.context()
    # 第二个计划（不同请求名 → 不同 plan_id）
    from tests.unit.test_mcp.conftest import REQUEST_DOCUMENT

    document = dict(REQUEST_DOCUMENT)
    document["request"] = {"name": "era5_mcp_test_2", "priority": "normal", "max_sources": 1}
    created = call_tool(context, "create_execution_plan", {"request": document})
    assert created["ok"] is True
    plan_b = created["data"]["plan_id"]
    assert plan_b != plan_a
    envelope = call_tool(
        context, "submit_acquisition", {"plan_id": plan_b, "approval_token": token_a}
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "approval_invalid"


def test_replay_after_success_is_idempotent(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_id, token = _plan(harness, body)
    context = harness.context()
    first = call_tool(context, "submit_acquisition", {"plan_id": plan_id, "approval_token": token})
    assert first["ok"] is True
    assert first["data"]["queued_tasks"] == 1
    second = call_tool(context, "submit_acquisition", {"plan_id": plan_id, "approval_token": token})
    assert second["ok"] is True
    assert second["data"]["duplicate"] is True
    assert second["data"]["queued_tasks"] == 0
    assert set(_task_states(context).values()) == {"QUEUED"}


def test_expired_token_rejected_end_to_end(harness: Harness, body: bytes) -> None:
    """用可注入时钟让令牌过期，确认服务器按 ``approval_invalid`` 拒绝。"""
    from datetime import timedelta as _td

    from tests.unit.test_mcp.conftest import REQUEST_DOCUMENT

    from researchdatahub.interfaces.mcp.auth import PlanApprovalAuthorizer
    from researchdatahub.interfaces.mcp.server import call_tool

    harness.add_source([harness.make_asset(http_path="/data/b.nc", body=body, file_name="b.nc")])
    issued_at = datetime(2026, 3, 1, tzinfo=UTC)
    issuer = harness.context()
    issuer.approval_authorizer = PlanApprovalAuthorizer(
        key_path=harness.key_path, clock=lambda: issued_at
    )
    created = call_tool(
        issuer, "create_execution_plan", {"request": REQUEST_DOCUMENT, "approval_ttl_seconds": 60}
    )
    assert created["ok"] is True, created
    plan_id = created["data"]["plan_id"]
    token = created["data"]["approval_token"]

    later = harness.context()
    later.approval_authorizer = PlanApprovalAuthorizer(
        key_path=harness.key_path, clock=lambda: issued_at + _td(seconds=120)
    )
    envelope = call_tool(later, "submit_acquisition", {"plan_id": plan_id, "approval_token": token})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "approval_invalid"
    assert "expired" in envelope["error"]["detail"]["reason"]
    assert set(_task_states(later).values()) == {"PLANNED"}


def test_tampered_token_rejected_end_to_end(harness: Harness, body: bytes) -> None:
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_id, token = _plan(harness, body)
    parts = token.split(".")
    parts[2] = str(int(parts[2]) + 86_400)
    context = harness.context()
    envelope = call_tool(
        context, "submit_acquisition", {"plan_id": plan_id, "approval_token": ".".join(parts)}
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "approval_invalid"


def test_token_survives_in_sqlite_across_contexts(harness: Harness, body: bytes) -> None:
    """审批记录与令牌都落在 SQLite 里：换一个 context 实例（等价于重启进程）仍然有效。"""
    from researchdatahub.interfaces.mcp.server import call_tool

    plan_id, token = _plan(harness, body)
    fresh_context = harness.context()
    envelope = call_tool(
        fresh_context, "submit_acquisition", {"plan_id": plan_id, "approval_token": token}
    )
    assert envelope["ok"] is True
    assert envelope["data"]["queued_tasks"] == 1


def test_approval_invalid_is_never_retryable() -> None:
    from researchdatahub.interfaces.mcp.errors import error_from_exception

    info = error_from_exception(ApprovalInvalidError("bad token"))
    assert info["code"] == "approval_invalid"
    assert info["retryable"] is False


def test_fingerprint_changes_when_tasks_change() -> None:
    from tests.unit.test_mcp.conftest import DATASET_ID

    from researchdatahub.core.models import (
        AcquisitionPlan,
        AcquisitionTask,
        SourceAsset,
        SourceMetadata,
    )

    metadata = SourceMetadata(
        source_id="ncar",
        display_name="x",
        provider="x",
        access_route="https",
        dataset_id=DATASET_ID,
        origin_uri="https://example.invalid/a.nc",
        retrieved_at=datetime.now(UTC),
    )
    asset = SourceAsset(
        asset_id="asset-1",
        source_id="ncar",
        dataset_id=DATASET_ID,
        access_route="https",
        origin_uri="https://example.invalid/a.nc",
        file_name="a.nc",
        size_bytes=10,
        metadata=metadata,
        discovered_at=datetime.now(UTC),
    )
    task = AcquisitionTask(task_id="t1", plan_id="p1", request_id="r1", asset=asset)
    plan = AcquisitionPlan(
        plan_id="p1", request_id="r1", tasks=[task], generated_at=datetime.now(UTC)
    )
    modified = plan.model_copy(update={"tasks": [task.model_copy(update={"attempt": 1})]})
    assert plan_fingerprint(plan) != plan_fingerprint(modified)
    # 键序不同但内容相同 → 指纹相同（这正是 SQLite 往返后仍能验签的前提）
    reordered = plan.model_copy(
        update={
            "extensions": dict(reversed(list({"a": 1, "b": 2}.items()))),
        }
    )
    regular = plan.model_copy(update={"extensions": {"a": 1, "b": 2}})
    assert plan_fingerprint(reordered) == plan_fingerprint(regular)
    # 内容不同 → 指纹不同
    value_changed = plan.model_copy(update={"extensions": {"a": 2, "b": 2}})
    assert plan_fingerprint(regular) != plan_fingerprint(value_changed)


def test_tokens_are_unique_per_issue(tmp_path: Path) -> None:
    """同一计划、同一指纹、同一秒内重复签发也必须得到不同令牌（nonce 保证）。"""
    store = _store(tmp_path)
    fixed = datetime(2026, 5, 1, tzinfo=UTC)
    authorizer = PlanApprovalAuthorizer(key_path=tmp_path / "k.key", clock=lambda: fixed)
    first, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    second, _ = authorizer.issue(store, plan_id="p1", fingerprint="fp", ttl_seconds=600)
    assert first != second
    assert len(first.split(".")) == 6
    assert len(second.split(".")) == 6
