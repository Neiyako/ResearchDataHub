"""``ConfigurationValidator`` 的测试：整体状态推导、组合、再验证与安全。

覆盖
----
* 整体状态推导的**全部分支**（ok / needs_configuration / unavailable / unknown）；
* 未知 provider 返回报告而不是抛异常；
* ``validate_environment`` 与 ``validate`` 共用同一实现；
* 报告 JSON 不含 secret 哨兵（sentinel 断言）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sentinels import SECRET_SENTINEL

from researchdatahub.environment.credentials import CredentialChecker
from researchdatahub.environment.dependencies import DependencyChecker
from researchdatahub.environment.models import (
    CheckKind,
    EnvironmentReport,
    OverallStatus,
    RequirementResult,
    RequirementStatus,
    assert_report_has_no_secret,
)
from researchdatahub.environment.network import NetworkChecker
from researchdatahub.environment.registry import _ENTRIES
from researchdatahub.environment.validation import (
    ConfigurationValidator,
    derive_overall_status,
    render_guide,
    validate_environment,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _result(
    name: str,
    status: RequirementStatus,
    *,
    required: bool = True,
    kind: CheckKind = CheckKind.DEPENDENCY,
    observed: dict[str, str | int | float | bool | None] | None = None,
) -> RequirementResult:
    return RequirementResult(
        name=name,
        kind=kind,
        status=status,
        required=required,
        observed=observed or {},
    )


# --------------------------------------------------------------------------- #
# 整体状态推导（纯函数，全部分支）
# --------------------------------------------------------------------------- #
def test_derive_ok_when_all_present() -> None:
    results = [_result("httpx", RequirementStatus.PRESENT)]
    assert derive_overall_status(results) is OverallStatus.OK


def test_derive_ok_when_only_optional_missing() -> None:
    results = [
        _result("httpx", RequirementStatus.PRESENT),
        _result("boto3", RequirementStatus.OPTIONAL, required=False),
    ]
    assert derive_overall_status(results) is OverallStatus.OK


def test_derive_needs_configuration_on_blocking_missing() -> None:
    results = [
        _result("httpx", RequirementStatus.PRESENT),
        _result("cdsapi", RequirementStatus.MISSING),
    ]
    assert derive_overall_status(results) is OverallStatus.NEEDS_CONFIGURATION


def test_derive_needs_configuration_on_unknown() -> None:
    """UNKNOWN 绝不等于通过。"""
    assert derive_overall_status([_result("x", RequirementStatus.UNKNOWN)]) is (
        OverallStatus.NEEDS_CONFIGURATION
    )


def test_derive_ignores_optional_unknown() -> None:
    """非必需项的 ``unknown`` 不拖垮整体状态（与 blocking 定义一致：只有 required 才阻塞）。"""
    results = [_result("x", RequirementStatus.UNKNOWN, required=False)]
    assert derive_overall_status(results) is OverallStatus.OK


def test_derive_unavailable_on_structural_network_failure() -> None:
    results = [
        _result(
            "cds-api",
            RequirementStatus.MISSING,
            kind=CheckKind.NETWORK,
            observed={"status_code": 503},
        )
    ]
    assert derive_overall_status(results) is OverallStatus.UNAVAILABLE


def test_derive_needs_configuration_on_network_connect_failure() -> None:
    """连接失败不算结构性（可能只是本机网络没开）。"""
    results = [
        _result(
            "cds-api",
            RequirementStatus.MISSING,
            kind=CheckKind.NETWORK,
            observed={"error": "ConnectError"},
        )
    ]
    assert derive_overall_status(results) is OverallStatus.NEEDS_CONFIGURATION


def test_derive_empty_is_ok() -> None:
    assert derive_overall_status([]) is OverallStatus.OK


# --------------------------------------------------------------------------- #
# 构造 validator
# --------------------------------------------------------------------------- #
def _validator(
    *,
    present: bool = True,
    environ: dict[str, str] | None = None,
    transport_handler: object | None = None,
    redirect_probe_failure: bool = False,
    sources_path: Path | None = None,
) -> ConfigurationValidator:
    client = (
        httpx.Client(transport=httpx.MockTransport(transport_handler))  # type: ignore[arg-type]
        if transport_handler is not None
        else None
    )
    return ConfigurationValidator(
        dependency_checker=DependencyChecker(probe=lambda module: present),
        credential_checker=CredentialChecker(environ=environ or {}),
        network_checker=NetworkChecker(client=client, timeout=1.0),
        redirect_probe_failure=redirect_probe_failure,
        sources_path=sources_path,
        clock=lambda: datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """让 ``~/.cdsapirc`` 等文件指向空的临时 home，结果与本机真实配置无关。"""
    import os

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home)))


# --------------------------------------------------------------------------- #
# 组合
# --------------------------------------------------------------------------- #
def test_validate_ok_when_everything_present(tmp_path: Path) -> None:
    rc_dir = tmp_path / "home"
    (rc_dir / ".cdsapirc").write_text("url: https://cds.climate.copernicus.eu/api\nkey: x\n")
    validator = _validator(
        environ={"CDSAPI_KEY": SECRET_SENTINEL},
        transport_handler=lambda request: httpx.Response(200),
    )
    report = validator.validate("cds", storage_root=tmp_path, check_network=True)
    assert report.status is OverallStatus.OK
    assert report.ok is True
    assert report.summary == ""
    assert set(report.checks_run) == {"dependencies", "credentials", "network", "storage"}
    assert report.checked_at == "2026-11-05T12:00:00+00:00"
    assert report.duration_ms >= 0.0


def test_validate_needs_configuration_when_credential_missing(tmp_path: Path) -> None:
    validator = _validator(environ={})
    report = validator.validate("cds", storage_root=None, check_network=False)
    assert report.status is OverallStatus.NEEDS_CONFIGURATION
    assert report.blocking_requirements
    assert "缺少" in report.summary
    assert "check_network=True" in report.summary


def test_validate_unavailable_on_503(tmp_path: Path) -> None:
    rc_dir = tmp_path / "home"
    (rc_dir / ".cdsapirc").write_text("url: x\nkey: y\n")
    validator = _validator(
        environ={"CDSAPI_KEY": "x"},
        transport_handler=lambda request: httpx.Response(503),
    )
    report = validator.validate("cds", check_network=True)
    assert report.status is OverallStatus.UNAVAILABLE
    assert "结构性故障" in report.summary


def test_validate_skips_network_by_default() -> None:
    validator = _validator(environ={"CDSAPI_KEY": "x"})
    report = validator.validate("cds")
    assert "network" not in report.checks_run
    assert "未做网络探测" in report.summary


def test_validate_unknown_provider_returns_report_not_exception() -> None:
    report = _validator().validate("era5-direct")
    assert report.status is OverallStatus.UNAVAILABLE
    assert report.requirements == []
    assert any("unknown provider" in error for error in report.errors)
    assert "已登记" in report.summary


def test_validate_ncar_has_no_required_credential(tmp_path: Path) -> None:
    """NCAR 公有数据：凭据全可选，缺了也不该是 needs_configuration。"""
    validator = _validator(environ={})
    report = validator.validate("ncar", check_network=False)
    assert report.status is OverallStatus.OK
    assert any(item.kind is CheckKind.CREDENTIAL for item in report.requirements)


def test_validate_many_covers_all_providers() -> None:
    reports = _validator().validate_many(check_network=False)
    assert [report.provider for report in reports] == [
        "cds",
        "ncar",
        "nasa_earthdata",
        "copernicus_datasphere",
        "gee",
    ]


def test_validate_many_accepts_subset() -> None:
    reports = _validator().validate_many(["cds", "gee"], check_network=False)
    assert [report.provider for report in reports] == ["cds", "gee"]


def test_validate_storage_failure_is_blocking(tmp_path: Path) -> None:
    validator = _validator(environ={"CDSAPI_KEY": "x"})
    report = validator.validate("cds", storage_root=tmp_path / "missing", check_network=False)
    storage = [item for item in report.requirements if item.kind is CheckKind.STORAGE]
    assert storage
    assert storage[0].status is RequirementStatus.MISSING
    assert report.status is OverallStatus.NEEDS_CONFIGURATION


def test_redirect_probe_failure_softens_connect_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked", request=request)

    validator = _validator(
        environ={"CDSAPI_KEY": "x"},
        transport_handler=handler,
        redirect_probe_failure=True,
    )
    report = validator.validate("cds", check_network=True)
    network = [item for item in report.requirements if item.kind is CheckKind.NETWORK]
    assert network
    assert network[0].status is RequirementStatus.UNKNOWN
    assert report.status is OverallStatus.NEEDS_CONFIGURATION


# --------------------------------------------------------------------------- #
# sources.yaml 凭据引用
# --------------------------------------------------------------------------- #
def test_sources_yaml_credentials_checked(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n"
        "  cds:\n"
        "    credentials:\n"
        "      key: env:CDSAPI_KEY\n"
        "      url: https://cds.climate.copernicus.eu/api\n"
    )
    validator = _validator(environ={"CDSAPI_KEY": "x"}, sources_path=sources)
    report = validator.validate("cds", check_network=False)
    assert "sources-credentials" in report.checks_run
    entry = next(i for i in report.requirements if i.name.startswith("sources.yaml:"))
    assert entry.status is RequirementStatus.PRESENT
    assert entry.observed["references"] == 1


def test_sources_yaml_missing_file_is_unknown_not_fatal(tmp_path: Path) -> None:
    validator = _validator(sources_path=tmp_path / "nope.yaml")
    report = validator.validate("cds", check_network=False)
    entry = next(i for i in report.requirements if i.name.startswith("sources.yaml:"))
    assert entry.status is RequirementStatus.UNKNOWN
    assert entry.required is False


def test_sources_yaml_with_bad_yaml_is_unknown(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources: [unclosed\n")
    validator = _validator(sources_path=sources)
    report = validator.validate("cds", check_network=False)
    entry = next(i for i in report.requirements if i.name.startswith("sources.yaml:"))
    assert entry.status is RequirementStatus.UNKNOWN


def test_real_repo_sources_example_is_parseable() -> None:
    """仓库自带的 sources.example.yaml 必须能被本包读取（防止格式漂移）。"""
    from researchdatahub.environment.credentials import load_credential_references

    mapping = load_credential_references(PROJECT_ROOT / "configs" / "sources.example.yaml")
    assert mapping["cds"]["key"].name == "CDSAPI_KEY"


# --------------------------------------------------------------------------- #
# validate_environment（再验证入口）
# --------------------------------------------------------------------------- #
def test_validate_environment_defaults_network_on(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    validator = ConfigurationValidator(
        dependency_checker=DependencyChecker(probe=lambda module: True),
        credential_checker=CredentialChecker(environ={"CDSAPI_KEY": "x"}),
        network_checker=NetworkChecker(
            client=httpx.Client(transport=httpx.MockTransport(handler)), timeout=1.0
        ),
    )
    report = validate_environment("cds", storage_root=None, validator=validator)
    assert "network" in report.checks_run
    assert calls, "默认应执行网络探测"


def test_validate_environment_without_validator_uses_real_checkers(tmp_path: Path) -> None:
    report = validate_environment("cds", check_network=False, storage_root=tmp_path, environ={})
    assert report.provider == "cds"
    assert report.status in (
        OverallStatus.OK,
        OverallStatus.NEEDS_CONFIGURATION,
        OverallStatus.UNAVAILABLE,
    )


def test_validate_environment_unknown_provider() -> None:
    report = validate_environment("bogus", check_network=False, storage_root=None)
    assert report.status is OverallStatus.UNAVAILABLE


# --------------------------------------------------------------------------- #
# 安全：报告不含 secret
# --------------------------------------------------------------------------- #
def test_report_json_never_contains_secret_sentinel(tmp_path: Path) -> None:
    rc_dir = tmp_path / "home"
    (rc_dir / ".cdsapirc").write_text(f"url: https://x/api\nkey: {SECRET_SENTINEL}\n")
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  cds:\n    credentials:\n      key: env:CDSAPI_KEY\n")
    validator = ConfigurationValidator(
        dependency_checker=DependencyChecker(probe=lambda module: True),
        credential_checker=CredentialChecker(
            environ={"CDSAPI_KEY": SECRET_SENTINEL}, redact_values=[SECRET_SENTINEL]
        ),
        network_checker=NetworkChecker(
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        ),
        sources_path=sources,
    )
    report = validator.validate("cds", storage_root=tmp_path, check_network=True)
    payload = json.dumps(report.to_json_dict(), ensure_ascii=False)
    assert SECRET_SENTINEL not in payload
    assert SECRET_SENTINEL not in report.summary
    assert SECRET_SENTINEL not in json.dumps(report.to_json_dict(), ensure_ascii=False)
    assert_report_has_no_secret(report, SECRET_SENTINEL)


def test_report_json_for_every_provider_has_no_secret(tmp_path: Path) -> None:
    validator = ConfigurationValidator(
        dependency_checker=DependencyChecker(probe=lambda module: True),
        credential_checker=CredentialChecker(
            environ={
                "CDSAPI_KEY": SECRET_SENTINEL,
                "RDA_API_KEY": SECRET_SENTINEL,
                "EARTHDATA_TOKEN": SECRET_SENTINEL,
                "CDSE_TOKEN": SECRET_SENTINEL,
            },
            redact_values=[SECRET_SENTINEL],
        ),
        network_checker=NetworkChecker(
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        ),
    )
    for report in validator.validate_many(check_network=True, storage_root=None):
        assert_serialized_without(report, SECRET_SENTINEL)


def assert_serialized_without(report: EnvironmentReport, secret: str) -> None:
    """辅助断言：任何序列化形式都不含 secret。"""
    assert secret not in json.dumps(report.to_json_dict(), ensure_ascii=False)
    assert secret not in report.model_dump_json()
    assert secret not in repr(report)
    assert_report_has_no_secret(report, secret)


# --------------------------------------------------------------------------- #
# 引导渲染
# --------------------------------------------------------------------------- #
def test_render_guide_returns_markdown() -> None:
    text = render_guide("gee")
    assert text.startswith("### ")
    assert "earthengine" in text.lower()


def test_render_guide_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        render_guide("nope")


def test_registry_entries_are_immutable_mapping() -> None:
    with pytest.raises(TypeError):
        _ENTRIES["hacked"] = None  # type: ignore[index]
