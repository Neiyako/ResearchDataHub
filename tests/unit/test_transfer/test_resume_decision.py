"""续传决策的纯函数测试：每个条件单独证明，不依赖网络。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from researchdatahub.transfer import ResumeMode, ResumeProbe, decide_resume
from researchdatahub.transfer.staging import PartSidecar

LM = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
LM_LATER = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
ORIGIN = "https://example.test/data.bin"


def probe(
    *,
    accepts_ranges: bool = True,
    content_length: int | None = 1000,
    etag: str | None = '"v1"',
    last_modified: datetime | None = LM,
) -> ResumeProbe:
    """构造探测结果（默认：支持 Range、强 ETag、Last-Modified、长度 1000）。"""
    return ResumeProbe(
        origin_uri=ORIGIN,
        transport_id="https",
        accepts_ranges=accepts_ranges,
        content_length=content_length,
        etag=etag,
        last_modified=last_modified,
    )


def sidecar(
    *,
    origin_uri: str = ORIGIN,
    expected_size: int | None = 1000,
    etag: str | None = '"v1"',
    last_modified: datetime | None = LM,
    size_written: int = 400,
) -> PartSidecar:
    """构造上次留下的 sidecar。"""
    return PartSidecar(
        origin_uri=origin_uri,
        transport_id="https",
        task_id="task-0001",
        expected_size=expected_size,
        etag=etag,
        last_modified=last_modified,
        size_written=size_written,
        updated_at=LM,
    )


def decide(
    *,
    part_size: int = 400,
    side: PartSidecar | None = None,
    remote: ResumeProbe | None = None,
    resume_enabled: bool = True,
    policy: str = "restart",
) -> object:
    """带默认值的决策调用。"""
    return decide_resume(
        part_size=part_size,
        sidecar=side if side is not None else sidecar(),
        probe=remote if remote is not None else probe(),
        resume_enabled=resume_enabled,
        unverifiable_policy=policy,  # type: ignore[arg-type]
        expected_size=1000,
    )


def test_no_partial_starts_fresh() -> None:
    decision = decide(part_size=0)
    assert decision.mode is ResumeMode.FRESH  # type: ignore[attr-defined]
    assert decision.reason == "no_partial"  # type: ignore[attr-defined]
    assert decision.resumable is False  # type: ignore[attr-defined]


def test_valid_validators_and_range_allow_resume() -> None:
    decision = decide()
    assert decision.mode is ResumeMode.RESUME  # type: ignore[attr-defined]
    assert decision.offset == 400  # type: ignore[attr-defined]
    assert decision.reason == "validators_match"  # type: ignore[attr-defined]


def test_resume_disabled_forces_restart() -> None:
    decision = decide(resume_enabled=False)
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "resume_disabled"  # type: ignore[attr-defined]


def test_range_unsupported_forces_restart() -> None:
    decision = decide(remote=probe(accepts_ranges=False))
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "range_unsupported"  # type: ignore[attr-defined]


def test_missing_sidecar_forces_restart() -> None:
    decision = decide_resume(
        part_size=400,
        sidecar=None,
        probe=probe(),
        resume_enabled=True,
        unverifiable_policy="restart",
        expected_size=1000,
    )
    assert decision.mode is ResumeMode.RESTART
    assert decision.reason == "missing_sidecar"


def test_partial_larger_than_remote_forces_restart() -> None:
    decision = decide(part_size=1500)
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "partial_exceeds_remote"  # type: ignore[attr-defined]


def test_partial_equal_to_remote_is_not_trusted() -> None:
    decision = decide(part_size=1000)
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "partial_complete_unverified"  # type: ignore[attr-defined]


def test_etag_change_forces_restart() -> None:
    decision = decide(remote=probe(etag='"v2"'))
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "etag_changed"  # type: ignore[attr-defined]


def test_weak_etag_is_not_used_as_validator() -> None:
    # 弱 ETag 不能用于字节区间一致性判断；此时退回 Last-Modified，两者一致仍可续传。
    decision = decide(remote=probe(etag='W/"v1"'), side=sidecar(etag='W/"v1"'))
    assert decision.mode is ResumeMode.RESUME  # type: ignore[attr-defined]


def test_last_modified_change_forces_restart() -> None:
    decision = decide(
        remote=probe(etag=None, last_modified=LM_LATER),
        side=sidecar(etag=None, last_modified=LM),
    )
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "last_modified_changed"  # type: ignore[attr-defined]


def test_last_modified_match_allows_resume_without_etag() -> None:
    decision = decide(
        remote=probe(etag=None, last_modified=LM),
        side=sidecar(etag=None, last_modified=LM),
    )
    assert decision.mode is ResumeMode.RESUME  # type: ignore[attr-defined]


def test_identity_unknown_restart_policy() -> None:
    decision = decide(
        remote=probe(etag=None, last_modified=None), side=sidecar(etag=None, last_modified=None)
    )
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "identity_unknown"  # type: ignore[attr-defined]


def test_identity_unknown_manual_policy() -> None:
    decision = decide(
        remote=probe(etag=None, last_modified=None),
        side=sidecar(etag=None, last_modified=None),
        policy="manual",
    )
    assert decision.mode is ResumeMode.MANUAL  # type: ignore[attr-defined]
    assert decision.reason == "identity_unknown"  # type: ignore[attr-defined]


def test_size_change_forces_restart_even_if_etag_matches() -> None:
    decision = decide(remote=probe(content_length=1200), side=sidecar(expected_size=1000))
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "size_changed"  # type: ignore[attr-defined]


def test_origin_change_forces_restart() -> None:
    decision = decide(side=sidecar(origin_uri="https://other.test/elsewhere.bin"))
    assert decision.mode is ResumeMode.RESTART  # type: ignore[attr-defined]
    assert decision.reason == "origin_uri_changed"  # type: ignore[attr-defined]


def test_decision_model_is_frozen() -> None:
    from pydantic import ValidationError

    decision = decide()
    with pytest.raises(ValidationError, match="frozen"):
        decision.mode = ResumeMode.FRESH  # type: ignore[misc,attr-defined]
