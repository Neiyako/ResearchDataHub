"""``CredentialChecker`` 的测试：只验证存在性，**绝不泄漏值**。

安全用例（本文件的核心价值）
---------------------------
* ``test_report_json_never_contains_secret_sentinel``：用哨兵字符串做 env / 文件 / keyring
  三种来源，断言报告 JSON 与 guidance 里都不含该值；
* ``test_guidance_is_redacted_when_exception_contains_secret``：模拟第三方库把 secret 拼进
  异常消息，断言打码生效；
* ``test_absent_credential_does_not_leak_neighbouring_value``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sentinels import SECRET_SENTINEL, SECRET_SENTINEL_2

from researchdatahub.environment.credentials import (
    CredentialChecker,
    load_credential_references,
    parse_credential_reference,
)
from researchdatahub.environment.models import (
    CheckKind,
    CredentialKind,
    EnvironmentReport,
    OverallStatus,
    RequirementStatus,
    assert_report_has_no_secret,
)


# --------------------------------------------------------------------------- #
# 解析引用
# --------------------------------------------------------------------------- #
def test_parse_env_reference() -> None:
    reference = parse_credential_reference("env:CDSAPI_KEY")
    assert reference.kind is CredentialKind.ENV
    assert reference.name == "CDSAPI_KEY"
    assert reference.describe() == "环境变量 CDSAPI_KEY"


def test_parse_file_reference() -> None:
    reference = parse_credential_reference("  file:~/.netrc ")
    assert reference.kind is CredentialKind.FILE
    assert reference.name == "~/.netrc"


def test_parse_keyring_reference() -> None:
    reference = parse_credential_reference("keyring:researchdatahub/alice")
    assert reference.kind is CredentialKind.KEYRING
    assert reference.service == "researchdatahub"
    assert reference.name == "alice"


@pytest.mark.parametrize("text", ["", "CDSAPI_KEY", "env:", "keyring:only-service", "http://x"])
def test_parse_invalid_reference_raises(text: str) -> None:
    with pytest.raises(ValueError, match=r"(credential reference|keyring reference)"):
        parse_credential_reference(text)


def test_parse_literal_secret_rejected_with_explanation() -> None:
    """裸字面量（把密钥直接写进配置）必须被拒绝并说明原因。"""
    with pytest.raises(ValueError, match="must never be written"):
        parse_credential_reference("abc123literal")


# --------------------------------------------------------------------------- #
# env 引用
# --------------------------------------------------------------------------- #
def test_env_present() -> None:
    checker = CredentialChecker(environ={"CDSAPI_KEY": SECRET_SENTINEL})
    result = checker.check_reference(
        "key", parse_credential_reference("env:CDSAPI_KEY"), required=True
    )
    assert result.status is RequirementStatus.PRESENT
    assert result.kind is CheckKind.CREDENTIAL
    assert result.observed["variable"] == "CDSAPI_KEY"
    assert result.observed["present"] is True


def test_env_absent_is_missing_with_actionable_guidance() -> None:
    checker = CredentialChecker(environ={})
    result = checker.check_reference(
        "key", parse_credential_reference("env:CDSAPI_KEY"), required=True, purpose="请求 ERA5"
    )
    assert result.status is RequirementStatus.MISSING
    assert result.blocking is True
    assert "export CDSAPI_KEY" in result.guidance
    assert "不要" in result.guidance
    assert "聊天窗口" in result.guidance


def test_env_blank_is_treated_as_absent() -> None:
    checker = CredentialChecker(environ={"CDSAPI_KEY": "   "})
    result = checker.check_reference(
        "key", parse_credential_reference("env:CDSAPI_KEY"), required=True
    )
    assert result.status is RequirementStatus.MISSING


def test_env_absent_optional_is_optional() -> None:
    checker = CredentialChecker(environ={})
    result = checker.check_reference(
        "url", parse_credential_reference("env:CDSAPI_URL"), required=False
    )
    assert result.status is RequirementStatus.OPTIONAL
    assert result.required is False


# --------------------------------------------------------------------------- #
# 安全：只返回存在性
# --------------------------------------------------------------------------- #
def test_check_result_never_carries_secret_value() -> None:
    checker = CredentialChecker(environ={"CDSAPI_KEY": SECRET_SENTINEL})
    result = checker.check_reference(
        "key", parse_credential_reference("env:CDSAPI_KEY"), required=True
    )
    dumped = result.model_dump_json()
    assert SECRET_SENTINEL not in dumped
    assert SECRET_SENTINEL not in result.guidance


def test_report_json_never_contains_secret_sentinel(tmp_path: Path) -> None:
    """三种来源同时给 secret，报告 JSON 里一个都不能出现。"""
    netrc = tmp_path / "netrc"
    netrc.write_text(f"machine urs.earthdata.nasa.gov\n  password {SECRET_SENTINEL_2}\n")
    from researchdatahub.environment.models import (
        CredentialReference,
        CredentialRequirement,
        RequirementResult,
    )

    requirements = [
        CredentialRequirement(
            key="key",
            reference=CredentialReference(kind=CredentialKind.ENV, name="CDSAPI_KEY"),
            purpose="认证",
        ),
        CredentialRequirement(
            key="netrc",
            reference=CredentialReference(kind=CredentialKind.FILE, name=str(netrc)),
            purpose="netrc 认证",
        ),
        CredentialRequirement(
            key="kr",
            reference=CredentialReference(
                kind=CredentialKind.KEYRING, name="alice", service="researchdatahub"
            ),
            purpose="钥匙串认证",
        ),
    ]
    checker = CredentialChecker(
        environ={"CDSAPI_KEY": SECRET_SENTINEL},
        keyring_getter=lambda service, user: SECRET_SENTINEL_2,
        redact_values=[SECRET_SENTINEL, SECRET_SENTINEL_2],
    )
    results: list[RequirementResult] = checker.check_credentials(requirements)
    assert all(item.status is RequirementStatus.PRESENT for item in results)
    report = EnvironmentReport(
        provider="nasa_earthdata",
        status=OverallStatus.OK,
        requirements=results,
    )
    payload = repr(report.to_json_dict())
    assert SECRET_SENTINEL not in payload
    assert SECRET_SENTINEL_2 not in payload
    assert_report_has_no_secret(report, [SECRET_SENTINEL, SECRET_SENTINEL_2])


def test_guidance_is_redacted_when_exception_contains_secret() -> None:
    """模拟「第三方异常消息里带出了 secret」——guidance 必须被打码。"""

    class _ExplodingMapping(dict[str, str]):
        def get(self, key: str, default: object = None) -> str:  # type: ignore[override]
            raise RuntimeError(f"boom while reading {SECRET_SENTINEL}")

    checker = CredentialChecker(environ=_ExplodingMapping(), redact_values=[SECRET_SENTINEL])
    result = checker.check_reference(
        "key", parse_credential_reference("env:CDSAPI_KEY"), required=True
    )
    assert result.status is RequirementStatus.MISSING
    assert SECRET_SENTINEL not in result.guidance
    assert "***REDACTED***" in result.guidance


# --------------------------------------------------------------------------- #
# file 引用
# --------------------------------------------------------------------------- #
def test_file_missing(tmp_path: Path) -> None:
    checker = CredentialChecker(environ={})
    reference = parse_credential_reference(f"file:{tmp_path / 'missing-rc'}")
    result = checker.check_reference("key", reference, required=True)
    assert result.status is RequirementStatus.MISSING
    assert result.observed["present"] is False
    assert "文件不存在" in result.guidance.replace(f"{tmp_path}", "文件")


def test_file_empty_is_absent(tmp_path: Path) -> None:
    rc = tmp_path / "cdsapirc"
    rc.write_text("")
    checker = CredentialChecker(environ={})
    result = checker.check_reference("key", parse_credential_reference(f"file:{rc}"), required=True)
    assert result.status is RequirementStatus.MISSING
    assert result.observed["size_bytes"] == 0


def test_netrc_without_marker_is_absent(tmp_path: Path) -> None:
    """存在但不是 netrc 结构（如空模板）不能算已配置：按文件名识别必须含 machine 行。"""
    netrc = tmp_path / ".netrc"
    netrc.write_text("# nothing here\n\n")
    checker = CredentialChecker(environ={})
    result = checker.check_reference(
        "api_key", parse_credential_reference(f"file:{netrc}"), required=True
    )
    assert result.status is RequirementStatus.MISSING
    assert result.observed["has_marker"] is False


def test_plain_file_without_marker_rule_is_present_when_non_empty(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text(SECRET_SENTINEL)
    checker = CredentialChecker(environ={}, redact_values=[SECRET_SENTINEL])
    result = checker.check_reference(
        "token", parse_credential_reference(f"file:{token_file}"), required=True
    )
    assert result.status is RequirementStatus.PRESENT
    assert SECRET_SENTINEL not in result.model_dump_json()


def test_cdsapirc_with_key_line_is_present(tmp_path: Path) -> None:
    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: placeholder\n")
    checker = CredentialChecker(environ={})
    result = checker.check_reference("key", parse_credential_reference(f"file:{rc}"), required=True)
    assert result.status is RequirementStatus.PRESENT
    assert result.observed["has_marker"] is True


def test_cdsapirc_without_key_line_is_absent(tmp_path: Path) -> None:
    """同名字文件里没有 key 行 → 只是空模板，不能算已配置（且不回显文件内容）。"""
    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\n")
    checker = CredentialChecker(environ={})
    result = checker.check_reference("key", parse_credential_reference(f"file:{rc}"), required=True)
    assert result.status is RequirementStatus.MISSING
    assert result.observed["has_marker"] is False
    assert "https://cds.climate.copernicus.eu/api" not in result.guidance


def test_literal_home_path_uses_marker_rule(tmp_path: Path) -> None:
    """``~/.cdsapirc`` 字面写法走字面规则：用 monkeypatch 把 home 指到 tmp_path 后验证。"""
    import os

    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\n")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
        checker = CredentialChecker(environ={})
        result = checker.check_reference(
            "key", parse_credential_reference("file:~/.cdsapirc"), required=True
        )
        assert result.observed["path"] == str(rc)
        assert result.status is RequirementStatus.MISSING
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# keyring 引用
# --------------------------------------------------------------------------- #
def test_keyring_present_via_injected_getter() -> None:
    checker = CredentialChecker(environ={}, keyring_getter=lambda service, user: SECRET_SENTINEL)
    reference = parse_credential_reference("keyring:researchdatahub/alice")
    result = checker.check_reference("kr", reference, required=True)
    assert result.status is RequirementStatus.PRESENT
    assert result.observed["service"] == "researchdatahub"
    assert SECRET_SENTINEL not in result.model_dump_json()


def test_keyring_blank_is_absent() -> None:
    checker = CredentialChecker(environ={}, keyring_getter=lambda service, user: "")
    result = checker.check_reference(
        "kr", parse_credential_reference("keyring:researchdatahub/alice"), required=True
    )
    assert result.status is RequirementStatus.MISSING


def test_keyring_library_missing_reports_reason() -> None:
    checker = CredentialChecker(environ={}, keyring_available=False)
    result = checker.check_reference(
        "kr", parse_credential_reference("keyring:researchdatahub/alice"), required=True
    )
    assert result.status is RequirementStatus.MISSING
    assert result.observed["library_available"] is False
    assert "keyring" in result.guidance


def test_keyring_backend_error_does_not_crash() -> None:
    def explode(service: str, user: str) -> str:
        raise RuntimeError(f"keychain locked ({SECRET_SENTINEL})")

    checker = CredentialChecker(environ={}, keyring_getter=explode, redact_values=[SECRET_SENTINEL])
    result = checker.check_reference(
        "kr", parse_credential_reference("keyring:researchdatahub/alice"), required=True
    )
    assert result.status is RequirementStatus.MISSING
    assert "钥匙串读取失败" in result.guidance
    assert SECRET_SENTINEL not in result.guidance


# --------------------------------------------------------------------------- #
# 批量与聚合
# --------------------------------------------------------------------------- #
def test_any_present_across_multiple_sources() -> None:
    from researchdatahub.environment.models import CredentialRequirement

    requirements = [
        CredentialRequirement(
            key="key",
            reference=parse_credential_reference("env:CDSAPI_KEY"),
            purpose="a",
        ),
        CredentialRequirement(
            key="key",
            reference=parse_credential_reference("file:/nonexistent/rc"),
            purpose="b",
        ),
    ]
    assert CredentialChecker(environ={"CDSAPI_KEY": "x"}).any_present(requirements) is True
    assert CredentialChecker(environ={}).any_present(requirements) is False


# --------------------------------------------------------------------------- #
# 读取 configs/sources*.yaml
# --------------------------------------------------------------------------- #
def test_load_credential_references_from_repo_sources_yaml() -> None:
    project_root = Path(__file__).resolve().parents[3]
    mapping = load_credential_references(project_root / "configs" / "sources.example.yaml")
    assert "cds" in mapping
    cds = mapping["cds"]
    assert cds["key"].kind is CredentialKind.ENV
    assert cds["key"].name == "CDSAPI_KEY"
    # 公开的 url 字面量不是凭据引用，应被跳过
    assert "url" not in cds


def test_load_credential_references_does_not_expand_env() -> None:
    """读取引用时不得展开 env（否则真实值会进入内存/日志）。"""
    project_root = Path(__file__).resolve().parents[3]
    mapping = load_credential_references(project_root / "configs" / "sources.example.yaml")
    for refs in mapping.values():
        for reference in refs.values():
            assert not reference.name.startswith("env:")


def test_load_credential_references_handles_keyring_and_missing_file(tmp_path: Path) -> None:
    yaml_text = (
        "sources:\n"
        "  demo:\n"
        "    credentials:\n"
        "      token: keyring:researchdatahub/alice\n"
        "      other: literal-value\n"
    )
    path = tmp_path / "sources.yaml"
    path.write_text(yaml_text)
    mapping = load_credential_references(path)
    assert mapping["demo"]["token"].kind is CredentialKind.KEYRING
    assert "other" not in mapping["demo"]
    with pytest.raises(FileNotFoundError):
        load_credential_references(tmp_path / "nope.yaml")


def test_load_credential_references_ignores_malformed_document(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("- just\n- a\n- list\n")
    assert load_credential_references(path) == {}
