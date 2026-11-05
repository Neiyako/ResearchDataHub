"""错误信封与错误码表的一致性测试。

这批测试守护外部契约：错误码是对 AI Agent 写的 API，**不能**因为一次重构悄悄改名、
也不能让未登记的 Core 错误码溜进 ``internal_error`` 而没人知道。
"""

from __future__ import annotations

import pytest

from researchdatahub.core.errors import (
    ConfigError,
    IntegrityError,
    ManifestError,
    MetadataError,
    ModelValidationError,
    NotImplementedFeatureError,
    RdhError,
    RetryableTransportError,
    ScheduleError,
    SourceNotFoundError,
    SourceUnavailableError,
    StateTransitionError,
    StorageError,
    TransportError,
)
from researchdatahub.interfaces.mcp.errors import (
    APPROVAL_INVALID_CODE,
    CORE_CODE_TO_MCP_CODE,
    ENVELOPE_SCHEMA_VERSION,
    ERROR_CODES,
    ERROR_HINTS,
    RETRYABLE_CODES,
    MCPError,
    NotAvailableError,
    approval_required_error,
    conflict_error,
    envelope_error,
    envelope_success,
    error_from_exception,
    invalid_arguments,
    invalid_state_error,
    not_found_error,
)

_REQUIRED_KEYS = {"code", "message", "hint", "retryable"}


def _core_error_types() -> list[type[RdhError]]:
    return [
        ConfigError,
        IntegrityError,
        ManifestError,
        MetadataError,
        ModelValidationError,
        NotImplementedFeatureError,
        ScheduleError,
        SourceNotFoundError,
        SourceUnavailableError,
        StateTransitionError,
        StorageError,
        TransportError,
    ]


def test_envelope_success_shape() -> None:
    envelope = envelope_success({"answer": 42}, tool="demo", duration_ms=1.23456)
    assert envelope["ok"] is True
    assert envelope["data"] == {"answer": 42}
    assert envelope["error"] is None
    assert envelope["meta"] == {
        "tool": "demo",
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "duration_ms": 1.235,
    }


def test_envelope_error_shape_and_null_data() -> None:
    envelope = envelope_error(
        {"code": "not_found", "message": "x", "hint": "y", "retryable": False},
        tool="demo",
        duration_ms=0.5,
    )
    assert envelope["ok"] is False
    assert envelope["data"] == {}
    assert envelope["error"]["code"] == "not_found"
    assert set(envelope["error"]) == _REQUIRED_KEYS


def test_every_error_code_has_a_hint() -> None:
    missing = [code for code in ERROR_CODES if not ERROR_HINTS.get(code)]
    assert missing == []


def test_retryable_codes_are_declared() -> None:
    assert RETRYABLE_CODES.issubset(set(ERROR_CODES))


@pytest.mark.parametrize("error_type", _core_error_types())
def test_every_core_error_maps_to_a_declared_code(error_type: type[RdhError]) -> None:
    mapped = CORE_CODE_TO_MCP_CODE.get(error_type.default_code)
    assert mapped is not None, f"{error_type.__name__} has no MCP mapping"
    assert mapped in ERROR_CODES


def test_all_declared_mappings_target_known_codes() -> None:
    assert set(CORE_CODE_TO_MCP_CODE.values()).issubset(set(ERROR_CODES))


def test_retryable_transport_error_keeps_retryable_flag() -> None:
    info = error_from_exception(RetryableTransportError("boom", retry_after_seconds=5))
    assert info["code"] == "transport_error"
    assert info["retryable"] is True


def test_permanent_transport_error_is_not_retryable() -> None:
    from researchdatahub.core.errors import PermanentTransportError

    info = error_from_exception(PermanentTransportError("nope"))
    assert info["code"] == "transport_error"
    assert info["retryable"] is False


def test_unexpected_exception_becomes_internal_error_without_message() -> None:
    secret = "sk-live-DEADBEEF"
    info = error_from_exception(RuntimeError(f"connection failed token={secret}"))
    assert info["code"] == "internal_error"
    assert secret not in info["message"]
    assert "RuntimeError" in info["message"]


def test_mcp_error_keeps_hint_and_detail() -> None:
    info = error_from_exception(NotAvailableError("missing pkg", detail={"capability": "network"}))
    assert info["code"] == "not_available"
    assert info["detail"] == {"capability": "network"}
    assert info["retryable"] is False


def test_secret_like_detail_keys_are_redacted() -> None:
    exc = ManifestError(
        "write failed",
        detail={
            "path": "/tmp/x",
            "approval_token": "v1.plan.1.f.sig",
            "nested": {"api_key": "hunter2", "note": "ok"},
        },
    )
    info = error_from_exception(exc)
    assert info["detail"]["path"] == "/tmp/x"
    assert info["detail"]["approval_token"] == "[redacted]"
    assert info["detail"]["nested"]["api_key"] == "[redacted]"
    assert info["detail"]["nested"]["note"] == "ok"


def test_helper_codes() -> None:
    assert invalid_arguments("bad").code == "invalid_arguments"
    assert not_found_error("plan", "p1").code == "not_found"
    assert conflict_error("exists").code == "conflict"
    assert approval_required_error("need token").code == "approval_required"
    assert invalid_state_error("bad state").code == "invalid_state"
    assert MCPError("x", code="conflict").code == "conflict"
    assert APPROVAL_INVALID_CODE == "approval_invalid"


def test_not_found_error_carries_identifier() -> None:
    info = error_from_exception(not_found_error("plan", "plan-42"))
    assert info["detail"] == {"kind": "plan", "id": "plan-42"}
    assert "plan-42" in info["message"]


__all__ = ["_core_error_types"]
