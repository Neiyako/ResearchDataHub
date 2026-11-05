"""``EnvironmentReport`` / ``RequirementResult`` 等 Pydantic 契约的单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchdatahub.environment.models import (
    ENVIRONMENT_SCHEMA_VERSION,
    REDACTED_PLACEHOLDER,
    CheckKind,
    CredentialKind,
    CredentialReference,
    EnvironmentReport,
    GuidanceStep,
    OverallStatus,
    RequirementResult,
    RequirementStatus,
    assert_report_has_no_secret,
    redact_secrets,
)


def _result(
    name: str = "httpx",
    status: RequirementStatus = RequirementStatus.PRESENT,
    *,
    required: bool = True,
    kind: CheckKind = CheckKind.DEPENDENCY,
) -> RequirementResult:
    return RequirementResult(name=name, kind=kind, status=status, required=required)


def _report(
    *results: RequirementResult, status: OverallStatus = OverallStatus.OK
) -> EnvironmentReport:
    return EnvironmentReport(
        provider="cds",
        display_name="Copernicus Climate Data Store (CDS)",
        status=status,
        requirements=list(results),
    )


def test_schema_version_is_literal_1_0() -> None:
    assert ENVIRONMENT_SCHEMA_VERSION == "1.0"
    report = _report()
    assert report.schema_version == "1.0"
    assert report.to_json_dict()["schema_version"] == "1.0"


def test_schema_version_rejects_other_versions() -> None:
    with pytest.raises(ValidationError):
        EnvironmentReport(provider="cds", status=OverallStatus.OK, schema_version="2.0")


def test_credential_reference_has_no_value_field() -> None:
    """凭据引用模型**不存在**能装 secret 的字段（安全不变式的第一道防线）。"""
    fields = set(CredentialReference.model_fields)
    assert fields == {"kind", "name", "service"}
    for forbidden in ("value", "secret", "token", "password"):
        assert forbidden not in fields


def test_credential_reference_keyring_requires_service() -> None:
    with pytest.raises(ValidationError, match="service"):
        CredentialReference(kind=CredentialKind.KEYRING, name="alice")
    ok = CredentialReference(kind=CredentialKind.KEYRING, name="alice", service="rdh")
    assert ok.describe() == "系统钥匙串（service=rdh, user=alice）"


def test_credential_reference_service_only_for_keyring() -> None:
    with pytest.raises(ValidationError, match="service"):
        CredentialReference(kind=CredentialKind.ENV, name="X", service="rdh")


def test_requirement_result_optional_status_requires_not_required() -> None:
    with pytest.raises(ValidationError, match="optional"):
        RequirementResult(
            name="boto3",
            kind=CheckKind.DEPENDENCY,
            status=RequirementStatus.OPTIONAL,
            required=True,
        )


def test_requirement_result_missing_requires_required_true() -> None:
    with pytest.raises(ValidationError, match="optional"):
        RequirementResult(
            name="boto3",
            kind=CheckKind.DEPENDENCY,
            status=RequirementStatus.MISSING,
            required=False,
        )


@pytest.mark.parametrize(
    ("status", "required", "blocking", "unresolved"),
    [
        (RequirementStatus.PRESENT, True, False, False),
        (RequirementStatus.OPTIONAL, False, False, False),
        (RequirementStatus.MISSING, True, True, True),
        (RequirementStatus.UNKNOWN, True, False, True),
    ],
)
def test_requirement_result_flags(
    status: RequirementStatus, required: bool, blocking: bool, unresolved: bool
) -> None:
    result = _result(status=status, required=required)
    assert result.blocking is blocking
    assert result.unresolved is unresolved


def test_report_ok_contradicts_blocking_requirement() -> None:
    """``status=ok`` 与「存在阻塞项」不能同时成立（模型级不变式）。"""
    with pytest.raises(ValidationError, match="blocking"):
        _report(_result(status=RequirementStatus.MISSING), status=OverallStatus.OK)


def test_report_ok_when_no_requirement() -> None:
    report = _report()
    assert report.ok is True
    assert report.blocking_requirements == []


def test_report_by_kind_groups_in_kind_order() -> None:
    report = EnvironmentReport(
        provider="cds",
        status=OverallStatus.NEEDS_CONFIGURATION,
        requirements=[
            _result("httpx"),
            _result("key@env", status=RequirementStatus.MISSING, kind=CheckKind.CREDENTIAL),
            _result("netcdf4", kind=CheckKind.DEPENDENCY),
        ],
    )
    grouped = report.by_kind
    assert list(grouped) == [CheckKind.DEPENDENCY, CheckKind.CREDENTIAL]
    assert [item.name for item in grouped[CheckKind.DEPENDENCY]] == ["httpx", "netcdf4"]


def test_report_json_excludes_none_and_keeps_schema_version_first() -> None:
    payload = _report(_result()).to_json_dict()
    assert next(iter(payload)) == "schema_version"
    assert payload["provider"] == "cds"
    assert "help_url" not in payload["requirements"][0]


def test_redact_secrets_replaces_all_occurrences() -> None:
    text = "token=abc123 and again abc123"
    assert (
        redact_secrets(text, "abc123")
        == f"token={REDACTED_PLACEHOLDER} and again {REDACTED_PLACEHOLDER}"
    )


def test_redact_secrets_longest_first() -> None:
    assert redact_secrets("abcdef", ["abc", "abcdef"]) == REDACTED_PLACEHOLDER


def test_redact_secrets_ignores_empty_and_collects_nested() -> None:
    secrets = {"a": ["", "s1"], "b": ("s2", None), "c": 42}
    assert (
        redact_secrets("s1-s2-42", secrets) == f"{REDACTED_PLACEHOLDER}-{REDACTED_PLACEHOLDER}-42"
    )


def test_assert_report_has_no_secret_passes_for_clean_report() -> None:
    assert_report_has_no_secret(_report(_result()), "not-in-report")


def test_assert_report_has_no_secret_detects_leak() -> None:
    leaky = _result()
    leaky = leaky.model_copy(update={"observed": {"oops": "SENTINEL-LEAK"}})
    with pytest.raises(AssertionError, match="contains a secret"):
        assert_report_has_no_secret(_report(leaky), ["SENTINEL-LEAK"])


def test_guidance_step_requires_all_four_fields() -> None:
    step = GuidanceStep(action="a", why="b", where="c", verify="d")
    assert step.where == "c"
    with pytest.raises(ValidationError):
        GuidanceStep(action="a", why="b", where="c")  # type: ignore[call-arg]
