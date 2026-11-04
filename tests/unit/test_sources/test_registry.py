"""SourceRegistry 测试：注册/工厂/构建/启停/健康检查，以及"禁止 if/else 堆源"。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.conftest import make_request
from tests.unit.test_sources.conftest import (
    make_source_config,
    make_sources_config,
)

from researchdatahub.config.models import SourceConfig
from researchdatahub.core.errors import (
    ConfigError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import AccessRoute
from researchdatahub.sources.base import SourceAdapter, build_asset, build_source_metadata, utcnow
from researchdatahub.sources.registry import DEFAULT_FACTORIES, SourceRegistry


class _RegistryAdapter(SourceAdapter):
    """测试用适配器：可配置健康状态与资产，不访问网络。"""

    adapter_id = "test"

    def __init__(self, *, source_id: str, config: SourceConfig | None = None, healthy=True):
        super().__init__(source_id=source_id, config=config)
        self.healthy = healthy
        self.closed = False

    def _describe(self):
        return build_source_metadata(
            source_id=self.source_id,
            display_name="Registry Test Source",
            provider="Test",
            access_route=AccessRoute.HTTPS,
            dataset_id="ds-test",
            origin_uri="https://example.invalid/ds-test/",
            retrieved_at=utcnow(),
        )

    def _discover_assets(self, request):
        return (
            build_asset(
                source_id=self.source_id,
                dataset_id=request.dataset_id,
                access_route=AccessRoute.HTTPS,
                origin_uri="https://example.invalid/file.nc",
                file_name="file.nc",
                size_bytes=1,
                metadata=build_source_metadata(
                    source_id=self.source_id,
                    display_name="Registry Test Source",
                    provider="Test",
                    access_route=AccessRoute.HTTPS,
                    dataset_id="ds-test",
                    origin_uri="https://example.invalid/file.nc",
                    retrieved_at=utcnow(),
                ),
                discovered_at=utcnow(),
            ),
        )

    def _health_check(self):
        return self.healthy

    def close(self):
        self.closed = True
        super().close()


def _factory(source_id: str, config: SourceConfig) -> SourceAdapter:
    healthy = config.extensions.get("test.healthy", True)
    return _RegistryAdapter(source_id=source_id, config=config, healthy=bool(healthy))


def _test_config(source_id: str = "src_a", *, healthy: bool = True) -> SourceConfig:
    return make_source_config(
        adapter="test",
        datasets=("era5-pressure-levels",),
        extensions={"test.healthy": healthy},
    )


def test_default_factories_cover_known_adapters():
    """内建工厂表是唯一源类型清单（cds + rda/ncar 别名）。"""
    assert set(DEFAULT_FACTORIES) == {"cds", "rda", "ncar"}
    registry = SourceRegistry.with_defaults()
    assert registry.adapter_types == ("cds", "ncar", "rda")


def test_registration_rules():
    """重复登记必须显式 replace；未知类型注销报错。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    with pytest.raises(ConfigError):
        registry.register("test", _factory)
    registry.register("test", _factory, replace=True)
    assert registry.adapter_types == ("test",)
    registry.unregister("test")
    assert registry.adapter_types == ()
    with pytest.raises(ConfigError):
        registry.unregister("test")
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("  ", _factory)


def test_create_rejects_unknown_adapter_type():
    """配置引用未登记适配器类型时给出可操作错误（列出已登记类型）。"""
    registry = SourceRegistry()
    config = make_source_config(adapter="nope")
    with pytest.raises(ConfigError) as caught:
        registry.create("src_a", config)
    assert "unknown adapter type" in caught.value.message
    assert caught.value.detail["registered"] == []


def test_build_from_sources_config():
    """从 SourcesConfig 构建：适配器类型识别、配置带着走、按 source_id 排序。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    configs = make_sources_config(
        {
            "src_b": make_source_config(adapter="test", endpoint="https://b.invalid"),
            "src_a": make_source_config(adapter="test", endpoint="https://a.invalid"),
        }
    )
    built = registry.build(configs)
    assert len(built) == 2
    assert [entry.source_id for entry in built.entries] == ["src_a", "src_b"]
    assert built.adapter_type("src_a") == "test"
    assert built.get("src_a").source_id == "src_a"
    assert built.enabled_sources == ("src_a", "src_b")
    assert built.describe("src_a").source_id == "src_a"
    assert len(built.discover("src_a", make_request())) == 1


def test_build_skips_disabled_and_can_include_them():
    """enabled=false 的源默认不构建；include_disabled 时构建但保持禁用。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    configs = make_sources_config(
        {
            "src_a": make_source_config(adapter="test"),
            "src_b": make_source_config(adapter="test", enabled=False),
        }
    )
    built = registry.build(configs)
    assert len(built) == 1
    assert "src_b" not in built
    assert built.get_or_none("src_b") is None

    with_disabled = registry.build(configs, include_disabled=True)
    assert len(with_disabled) == 2
    assert with_disabled.is_enabled("src_b") is False
    with pytest.raises(SourceUnavailableError):
        with_disabled.get("src_b")
    assert with_disabled.entry("src_b").adapter.source_id == "src_b"


def test_build_reports_unknown_adapter_types_aggregated():
    """多个源配置错误时聚合报告，而不是在第一个源上静默停止。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    configs = make_sources_config(
        {
            "src_a": make_source_config(adapter="nope-1"),
            "src_b": make_source_config(adapter="nope-2"),
            "src_c": make_source_config(adapter="test"),
        }
    )
    with pytest.raises(ConfigError) as caught:
        registry.build(configs)
    failures = caught.value.detail["failures"]
    assert {item["source_id"] for item in failures} == {"src_a", "src_b"}


def test_build_reuses_existing_adapters_by_default():
    """重复 build 时默认复用已构造实例（避免重建 HTTP 连接池/重复探测）。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    configs = make_sources_config({"src_a": make_source_config(adapter="test")})
    first = registry.build(configs)
    second = first.build(configs)
    assert second.get("src_a") is first.get("src_a")


def test_enable_disable_roundtrip():
    """启停只影响运行期可用性，不销毁适配器实例。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    built = registry.build(make_sources_config({"src_a": make_source_config(adapter="test")}))
    adapter = built.get("src_a")
    assert built.disable("src_a") is True
    assert built.disable("src_a") is False
    with pytest.raises(SourceUnavailableError):
        built.get("src_a")
    assert built.get_or_none("src_a") is None
    assert built.health_check("src_a") is False
    assert built.enable("src_a") is True
    assert built.get("src_a") is adapter
    with pytest.raises(SourceNotFoundError):
        built.enable("missing")
    with pytest.raises(SourceNotFoundError):
        built.get("missing")


def test_health_check_all_and_capabilities():
    """批量健康检查按启用源返回；capabilities 含禁用源的能力边界。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    built = registry.build(
        make_sources_config(
            {
                "src_a": _test_config("src_a", healthy=True),
                "src_b": _test_config("src_b", healthy=False),
            }
        )
    )
    assert built.health_check_all() == {"src_a": True, "src_b": False}
    capabilities = built.capabilities()
    assert set(capabilities) == {"src_a", "src_b"}
    assert capabilities["src_a"].available is True


def test_source_ids_for_dataset_uses_config_declarations():
    """计划器需要知道哪些源声明了某数据集；这里只查配置，不做科学推断。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    built = registry.build(
        make_sources_config(
            {
                "src_a": make_source_config(adapter="test", datasets=("era5-pressure-levels",)),
                "src_b": make_source_config(adapter="test", datasets=("cmip6",)),
            }
        )
    )
    assert built.source_ids_for_dataset("era5-pressure-levels") == ("src_a",)
    assert built.source_ids_for_dataset("unknown") == ()

    built.disable("src_a")
    assert built.source_ids_for_dataset("era5-pressure-levels") == ()


def test_close_closes_adapters_and_clears_registry():
    """close 释放所有适配器；之后注册表为空。"""
    registry = SourceRegistry()
    registry.register("test", _factory)
    built = registry.build(make_sources_config({"src_a": make_source_config(adapter="test")}))
    adapter = built.get("src_a")
    built.close()
    assert isinstance(adapter, _RegistryAdapter)
    assert adapter.closed is True
    assert len(built) == 0


def test_registry_has_no_per_source_if_branches():
    """禁止 if/else 堆源：模块源码里不得出现与源 ID 字面量的比较。"""
    source = Path("src/researchdatahub/sources/registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        constants = [
            item.value
            for item in [node.left, *node.comparators]
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        offenders.extend(value for value in constants if value in {"ncar", "cds", "rda"})
    assert offenders == []
