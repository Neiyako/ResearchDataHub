"""Provider 要求登记表与配置引导的测试。

重点
----
1. 五个 Provider 的真实声明齐全（依赖 / 凭据 / 端点）；
2. 登记表里**没有 secret 字面量**（扫描全部字符串）；
3. 引导步骤四要素齐全（做什么 / 为什么 / 去哪 / 怎么验证）且含真实 URL；
4. 未知 provider 报错而不是返回空表。
"""

from __future__ import annotations

import re

import pytest

from researchdatahub.environment.models import CredentialKind, ProviderRequirements
from researchdatahub.environment.registry import (
    PROVIDER_IDS,
    PROVIDER_REQUIREMENTS_REGISTRY,
    ProviderRequirementsRegistry,
    get_environment_requirements,
    get_requirements,
    normalize_provider_id,
)

_SECRETISH = re.compile(
    r"(?i)(api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|"
    r"BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})"
)
"""疑似「值被写进了明文」的模式。登记表里只允许出现变量名/URL，不允许出现 `key: xxx`。"""

_WHERE_HINT = re.compile(r"(https://|~/|\.venv/|export |curl |test -f|mkdir)")
_VERIFY_HINT = re.compile(
    r"(curl |test -f|\.venv/|python -c|python3 -c|validate_environment|"
    r"浏览|页面|能打开|能运行|已复制|拿到|看到|不显示|不再|出现|True)"
)


def _all_texts(entry: ProviderRequirements) -> list[str]:
    """把一个 Provider 声明里所有字符串摊平（含 URL 与 purpose），用于安全扫描。"""
    texts: list[str] = [entry.provider, entry.display_name, *entry.notes]
    for requirement in entry.dependencies:
        texts.extend([requirement.module, requirement.package, requirement.purpose])
    for credential in entry.credentials:
        texts.extend([credential.key, credential.reference.name, credential.purpose])
        if credential.reference.service:
            texts.append(credential.reference.service)
        if credential.how_to_obtain:
            texts.append(credential.how_to_obtain)
    for endpoint in entry.endpoints:
        texts.extend([endpoint.name, endpoint.url, endpoint.purpose])
    return texts


def test_registry_has_exactly_the_five_required_providers() -> None:
    assert PROVIDER_IDS == ("cds", "ncar", "nasa_earthdata", "copernicus_datasphere", "gee")
    assert len(PROVIDER_REQUIREMENTS_REGISTRY) == 5
    assert [item.provider for item in PROVIDER_REQUIREMENTS_REGISTRY.all()] == list(PROVIDER_IDS)


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_each_provider_declares_real_requirements(provider: str) -> None:
    entry = get_requirements(provider)
    assert entry.dependencies, f"{provider} 必须声明依赖"
    assert entry.endpoints, f"{provider} 必须声明网络端点"
    assert entry.credentials, f"{provider} 必须声明凭据位置（哪怕是可选项）"


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_credential_references_have_no_literal_values(provider: str) -> None:
    entry = get_requirements(provider)
    for credential in entry.credentials:
        assert credential.reference.kind in {
            CredentialKind.ENV,
            CredentialKind.FILE,
            CredentialKind.KEYRING,
        }
        # Pydantic 模型只有 kind/name/service 三个字段，结构上就装不下 secret。
        assert set(type(credential.reference).model_fields) == {"kind", "name", "service"}


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_registry_contains_no_secret_like_literals(provider: str) -> None:
    entry = get_requirements(provider)
    for text in _all_texts(entry):
        assert not _SECRETISH.search(text), f"{provider} 声明里出现疑似 secret 明文：{text!r}"


def test_cds_matches_sources_yaml_variable_name() -> None:
    """CDS 的环境变量名必须与 configs/sources.yaml 的 ``key: env:CDSAPI_KEY`` 对齐。"""
    entry = get_requirements("cds")
    env_names = {
        item.reference.name
        for item in entry.credentials
        if item.reference.kind is CredentialKind.ENV
    }
    assert "CDSAPI_KEY" in env_names
    assert entry.source_id == "cds"


def test_ncar_public_data_needs_no_required_credential() -> None:
    """NCAR 公有数据集匿名可下：凭据全部是可选项，缺了不能算失败。"""
    entry = get_requirements("ncar")
    assert all(not item.required for item in entry.credentials)
    assert entry.required_credentials == []


def test_gee_import_name_is_ee_package_is_earthengine_api() -> None:
    entry = get_requirements("gee")
    ee = [item for item in entry.dependencies if item.module == "ee"]
    assert ee
    assert ee[0].package == "earthengine-api"


def test_boto3_is_optional_for_ncar() -> None:
    entry = get_requirements("ncar")
    boto3 = [item for item in entry.dependencies if item.module == "boto3"]
    assert boto3
    assert boto3[0].required is False


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_endpoints_are_https(provider: str) -> None:
    for endpoint in get_requirements(provider).endpoints:
        assert endpoint.url.startswith("https://"), endpoint.url


def test_normalize_provider_id() -> None:
    assert normalize_provider_id("NASA-Earthdata") == "nasa_earthdata"
    assert normalize_provider_id("  CDS  ") == "cds"


def test_registry_lookup_is_case_and_dash_insensitive() -> None:
    registry = ProviderRequirementsRegistry()
    assert registry.get("NASA-Earthdata") is not None
    assert registry.get("nope") is None
    assert "CDS" in registry


def test_registry_require_unknown_raises_with_known_ids() -> None:
    with pytest.raises(KeyError, match="known providers"):
        PROVIDER_REQUIREMENTS_REGISTRY.require("era5-direct")


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_guidance_steps_are_actionable(provider: str) -> None:
    """每步必须同时有「做什么 / 为什么 / 去哪 / 怎么验证」，且 where 里带 URL 或路径。"""
    guide = get_environment_requirements(provider)
    assert guide.provider == provider
    assert len(guide.steps) >= 3
    for step in guide.steps:
        assert step.action
        assert step.why
        assert step.where
        assert step.verify
        assert _WHERE_HINT.search(step.where), f"{provider} 步骤 where 缺少去处：{step.where!r}"
        assert _VERIFY_HINT.search(step.verify), f"{provider} 步骤 verify 不可执行：{step.verify!r}"
    assert "configure your credentials" not in guide.render_markdown().lower()


def test_guidance_mentions_real_obtain_url_for_cds() -> None:
    guide = get_environment_requirements("cds")
    where = " ".join(step.where for step in guide.steps)
    assert "https://cds.climate.copernicus.eu/how-to-api" in where
    assert "~/.cdsapirc" in where


def test_guidance_renders_markdown_with_numbered_steps() -> None:
    markdown = get_environment_requirements("ncar").render_markdown()
    assert markdown.startswith("### ")
    assert "1. **" in markdown
    assert "- 验证：" in markdown


def test_guidance_unknown_provider_raises() -> None:
    with pytest.raises(KeyError, match="unknown provider"):
        get_environment_requirements("not-a-provider")


def test_guidance_warns_against_pasting_tokens_into_chat() -> None:
    """引导必须**主动劝阻**把 token 贴到聊天窗口（安全硬约束）。"""
    text = get_environment_requirements("nasa_earthdata").render_markdown()
    assert "不要" in text
    assert "聊天窗口" in text
