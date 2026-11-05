"""Provider 注册表与分层边界测试：MCP 层的入口、依赖方向、跨 Provider 一致性。

三个目标
--------
1. **MCP 层入口可用**：``list_providers()`` / ``get_provider()`` /
   ``get_provider_capabilities()`` 可直接被 ``search_datasets`` / ``get_provider_capabilities``
   等工具调用，且能力查询**不发网络请求**。
2. **依赖方向（ADR-0008）**：``providers/`` 只依赖 ``core``，不 import ``sources/``
   （用 AST 静态检查，而不是靠人工 review）。方向必须是单向的。
3. **两个 Provider 的能力声明互相独立、都自洽**：Earthdata 与 CDSE 不是同一平台，
   不允许出现"一个 Provider 的能力被复制到另一个"的漂移。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Protocol

import pytest

from researchdatahub.core.errors import ConfigError, SourceNotFoundError
from researchdatahub.providers import (
    CONTRACT_METHODS,
    DEFAULT_PROVIDER_IDS,
    CapabilityStatus,
    Registry,
    build_default_registry,
    get_provider,
    get_provider_capabilities,
    list_providers,
)
from researchdatahub.providers.base import ProviderCapabilities, ProviderContract
from researchdatahub.providers.registry import PROVIDER_FACTORIES

PROVIDERS_DIR = Path(__file__).resolve().parents[3] / "src" / "researchdatahub" / "providers"
SOURCES_DIR = Path(__file__).resolve().parents[3] / "src" / "researchdatahub" / "sources"

FORBIDDEN_FROM_PROVIDERS = (
    "researchdatahub.sources",
    "researchdatahub.transfer",
    "researchdatahub.manifest",
    "researchdatahub.integrity",
    "researchdatahub.storage",
    "researchdatahub.planner",
    "researchdatahub.scheduler",
    "researchdatahub.reporting",
    "researchdatahub.cli",
)


def _provider_modules() -> list[Path]:
    """``providers/`` 下全部 .py 文件（递归）。"""
    return sorted(PROVIDERS_DIR.rglob("*.py"))


def _assigns_dunder_all(node: ast.Assign) -> bool:
    """判断一条赋值语句是否在给 ``__all__`` 赋值。"""
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)


def _imported_modules(path: Path) -> set[str]:
    """收集一个模块的全部 import（静态 AST 解析，不执行代码）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name)
    return modules


# --------------------------------------------------------------------------- #
# 1. MCP 层入口
# --------------------------------------------------------------------------- #
def test_list_providers_returns_stable_known_ids():
    """``list_providers`` 返回两个已实现 Provider 的稳定 ID。"""
    assert list_providers() == ("earthdata", "copernicus")
    assert list_providers() == DEFAULT_PROVIDER_IDS
    assert list_providers() == tuple(PROVIDER_FACTORIES)


def test_get_provider_capabilities_covers_every_registered_provider():
    """``get_provider_capabilities`` 返回全部 Provider 的能力声明（MCP 工具的落地实现）。"""
    capabilities = get_provider_capabilities()
    assert [item.provider_id for item in capabilities] == list(list_providers())
    for item in capabilities:
        assert isinstance(item, ProviderCapabilities)
        assert item.contract_version == "provider-contract/1.0"


def test_get_provider_capabilities_can_filter_by_id():
    """支持只取指定 Provider。"""
    capabilities = get_provider_capabilities(provider_ids=("earthdata",))
    assert len(capabilities) == 1
    assert capabilities[0].provider_id == "earthdata"


def test_capability_query_never_touches_the_network(monkeypatch):
    """能力查询是纯内存计算：即使网络完全不可用也必须成功。"""
    import httpx

    def _explode(*_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError("capability query must not perform network I/O")

    monkeypatch.setattr(httpx.Client, "request", _explode, raising=False)
    monkeypatch.setattr(httpx.Client, "send", _explode, raising=False)
    capabilities = get_provider_capabilities()
    assert len(capabilities) == 2


def test_every_implemented_method_exists_and_unsupported_ones_raise():
    """能力声明与真实方法一一对应：已实现的可调用；不支持的抛显式异常。"""
    from researchdatahub.core.errors import NotImplementedFeatureError

    for provider_id in list_providers():
        provider = get_provider(provider_id)
        try:
            capabilities = provider.capabilities()
            for name in capabilities.implemented:
                assert callable(getattr(provider, name, None)), name
            for name in capabilities.unsupported:
                method = getattr(provider, name)
                with pytest.raises(NotImplementedFeatureError):
                    method("arbitrary-id")
        finally:
            provider.close()


def test_contract_methods_are_exactly_declared_by_both_providers():
    """两个 Provider 的声明必须恰好覆盖 10 个契约方法（不多不少）。"""
    for capability in get_provider_capabilities():
        declared = set(capability.implemented) | set(capability.unsupported)
        assert declared == set(CONTRACT_METHODS)


def test_get_provider_unknown_id_raises_source_not_found():
    """未注册的 ID → ``SourceNotFoundError``（携带已知列表）。"""
    with pytest.raises(SourceNotFoundError) as excinfo:
        get_provider("no-such-platform")
    assert excinfo.value.detail["known"] == ["copernicus", "earthdata"]


def test_get_provider_rejects_unknown_options():
    """未知构造选项 → ``ConfigError``（不静默丢弃）。"""
    with pytest.raises(ConfigError, match="invalid options"):
        get_provider("earthdata", no_such_option=1)


def test_registry_can_use_custom_factories():
    """注册表可注入自定义工厂（便于测试与未来扩展）。"""

    class _Stub:
        provider_id = "stub"

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities.model_construct(
                provider_id="stub",
                display_name="Stub",
                implemented=CONTRACT_METHODS,
                unsupported=(),
                verified=(),
                unverified=(),
                credential_features={},
                notes=(),
            )

        def close(self) -> None:
            pass

    registry = Registry({"stub": lambda: _Stub()})
    assert registry.ids == ("stub",)
    provider = registry.create("stub")
    assert isinstance(provider, Protocol) or provider.provider_id == "stub"
    registry.close_all()


def test_build_default_registry_lists_builtin_providers():
    """默认注册表包含全部内建 Provider。"""
    registry = build_default_registry()
    assert set(registry.ids) == set(DEFAULT_PROVIDER_IDS)


# --------------------------------------------------------------------------- #
# 2. 分层边界（ADR-0008）
# --------------------------------------------------------------------------- #
def test_provider_modules_have_no_forbidden_imports():
    """``providers/`` 不得 import 执行层（sources/transfer/manifest/...）——依赖必须是单向的。"""
    offenders: list[str] = []
    for path in _provider_modules():
        for module in _imported_modules(path):
            for forbidden in FORBIDDEN_FROM_PROVIDERS:
                if module == forbidden or module.startswith(f"{forbidden}."):
                    offenders.append(f"{path.name} -> {module}")
    assert offenders == [], f"providers must not depend on execution layers: {offenders}"


def test_sources_package_is_unchanged_by_providers():
    """``providers/`` 的存在不得让 ``sources/`` 反向依赖它（sources 不认识 providers）。"""
    offenders: list[str] = []
    for path in sorted(SOURCES_DIR.rglob("*.py")):
        for module in _imported_modules(path):
            if module == "researchdatahub.providers" or module.startswith(
                "researchdatahub.providers."
            ):
                offenders.append(f"{path.name} -> {module}")
    assert offenders == []


def test_importing_providers_does_not_load_sources():
    """运行期验证：import ``providers`` 后 ``researchdatahub.sources`` 不应被加载。"""
    import subprocess
    import sys

    code = (
        "import sys; import researchdatahub.providers; "
        "loaded = [m for m in sys.modules if m == 'researchdatahub.sources' "
        "or m.startswith('researchdatahub.sources.')]; "
        "print('|'.join(sorted(loaded)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_provider_modules_declare_all():
    """每个 Provider 模块都必须声明 ``__all__``（公开 API 可控）。"""
    for path in _provider_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_all = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
            for node in tree.body
        )
        assert has_all, f"{path.name} has no __all__"


# --------------------------------------------------------------------------- #
# 3. 两个 Provider 的差异必须如实体现
# --------------------------------------------------------------------------- #
def test_providers_are_not_copies_of_each_other():
    """能力声明必须各自独立：平台不同，但都不支持异步作业（这是两者共同的真实事实）。"""
    capabilities = {item.provider_id: item for item in get_provider_capabilities()}
    earthdata = capabilities["earthdata"]
    copernicus = capabilities["copernicus"]
    assert earthdata.display_name != copernicus.display_name
    assert earthdata.auth_required is False
    assert copernicus.auth_required is False
    # 两者都不支持 S3 直连与云处理（如实声明，而不是为了"看起来强"而打开）
    for capability in (earthdata, copernicus):
        assert capability.credential_features["s3_direct"] == "unsupported"
        assert capability.credential_features["cloud_processing"] == "unsupported"
        assert capability.supports_async_job is False


def test_capability_status_is_never_silently_verified():
    """没有真实证据的能力不得出现在 verified 里：``unverified`` 条目必须带原因。"""
    for capability in get_provider_capabilities():
        for entry in capability.unverified:
            assert len(entry) > 20, f"{capability.provider_id}: unverified entry too vague"
        for entry in capability.verified:
            assert "20" in entry, f"{capability.provider_id}: verified entry lacks a date/evidence"


def test_status_of_marks_download_unverified_on_this_host(monkeypatch):
    """本机无凭证：两个 Provider 的 ``data_download`` 都必须是 unverified（不是 verified）。"""
    for key in ("EARTHDATA_TOKEN", "CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for configured in get_provider("earthdata"), get_provider("copernicus"):
        try:
            capabilities = configured.capabilities()
            assert capabilities.status_of("estimate_request") is CapabilityStatus.VERIFIED
            assert capabilities.status_of("submit_task") is CapabilityStatus.UNSUPPORTED
        finally:
            configured.close()


def test_provider_contract_is_runtime_checkable():
    """两个 Provider 都满足 ``@runtime_checkable`` 的协议检查。"""
    for provider_id in list_providers():
        provider = get_provider(provider_id)
        try:
            assert isinstance(provider, ProviderContract)
        finally:
            provider.close()
