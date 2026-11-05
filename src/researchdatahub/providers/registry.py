"""Provider 注册表：按 ID 查找、构造与列出 Provider（**没有按 ID 的 if/else**）。

设计原则
--------
* 注册表只做"Provider ID → 构造工厂"的查表，与 ``sources.registry`` 的思路一致
  （ADR-0002：新增数据源 = 新增一个实现 + 注册一行）。
* 工厂签名固定为 ``(**options) -> ProviderContract``；``options`` 只允许显式声明的字段，
  未知字段直接报 :class:`~researchdatahub.core.errors.ConfigError`（不静默丢弃）。
* :func:`get_provider_capabilities` 是 MCP 层 ``get_provider_capabilities`` 工具的落地点：
  只读能力声明，可离线调用（不发起网络请求）。

MCP 层如何调用（示例）
----------------------
.. code-block:: python

    from researchdatahub.providers import Registry, get_provider_capabilities

    registry = Registry()
    earthdata = registry.create("earthdata")
    candidates = earthdata.search("precipitation", DatasetFilter(provider="GES_DISC"))

    for cap in get_provider_capabilities(registry):
        print(cap.provider_id, cap.implemented, cap.unsupported)
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from researchdatahub.core.errors import ConfigError, SourceNotFoundError
from researchdatahub.providers.base import ProviderCapabilities, ProviderContract
from researchdatahub.providers.copernicus.provider import (
    CDSE_CATALOGUE_ROOT,
    CDSE_ODATA_ROOT,
    CopernicusProvider,
)
from researchdatahub.providers.earthdata.provider import CMR_ROOT, EarthdataProvider

__all__ = [
    "DEFAULT_PROVIDER_IDS",
    "PROVIDER_FACTORIES",
    "ProviderFactory",
    "Registry",
    "build_default_registry",
    "get_provider_capabilities",
]

ProviderFactory = Callable[..., ProviderContract]
"""Provider 构造工厂：``(**options) -> ProviderContract``。"""


def _make_earthdata(
    *,
    base_url: str = CMR_ROOT,
    timeout: float = 60.0,
) -> ProviderContract:
    """构造 Earthdata Provider（CMR 根可覆盖，便于离线测试）。"""
    return EarthdataProvider(base_url=base_url, timeout=timeout)


def _make_copernicus(
    *,
    stac_root: str = CDSE_CATALOGUE_ROOT,
    odata_root: str = CDSE_ODATA_ROOT,
    timeout: float = 60.0,
) -> ProviderContract:
    """构造 Copernicus Data Space Provider（STAC/OData 根可覆盖）。"""
    return CopernicusProvider(stac_root=stac_root, odata_root=odata_root, timeout=timeout)


PROVIDER_FACTORIES: Final[Mapping[str, ProviderFactory]] = {
    "earthdata": _make_earthdata,
    "copernicus": _make_copernicus,
}
"""内建 Provider 清单（唯一来源；新增 Provider 只需在此登记）。"""

DEFAULT_PROVIDER_IDS: Final[tuple[str, ...]] = tuple(PROVIDER_FACTORIES)
"""默认 Provider ID 顺序（用于 ``get_provider_capabilities`` 的稳定输出）。"""


@dataclass
class Registry:
    """Provider 注册表：创建、查询与统一关闭。

    ``Registry`` 持有已创建的 Provider 实例并负责 :meth:`close_all`；
    同一个 ID 的多次 ``create`` 会返回**不同实例**（调用方负责各自关闭或统一交给注册表）。
    """

    factories: Mapping[str, ProviderFactory]
    _created: list[ProviderContract]

    def __init__(self, factories: Mapping[str, ProviderFactory] | None = None) -> None:
        """构造注册表；``factories`` 默认使用 :data:`PROVIDER_FACTORIES`。"""
        self.factories = dict(PROVIDER_FACTORIES if factories is None else factories)
        self._created = []

    def __iter__(self) -> Iterator[str]:
        """迭代已知 Provider ID（稳定顺序）。"""
        return iter(self.factories)

    @property
    def ids(self) -> tuple[str, ...]:
        """已知 Provider ID 元组。"""
        return tuple(self.factories)

    def create(self, provider_id: str, **options: Any) -> ProviderContract:
        """构造指定 Provider。

        Raises
        ------
        SourceNotFoundError
            ``provider_id`` 未注册。
        ConfigError
            传入了工厂不接受的 ``options`` 字段（不静默忽略）。
        """
        factory = self.factories.get(provider_id)
        if factory is None:
            raise SourceNotFoundError(
                f"provider {provider_id!r} is not registered",
                detail={"provider_id": provider_id, "known": sorted(self.factories)},
            )
        try:
            provider = factory(**options)
        except TypeError as exc:
            raise ConfigError(
                f"invalid options for provider {provider_id!r}",
                detail={"provider_id": provider_id, "options": sorted(options)},
            ) from exc
        self._created.append(provider)
        return provider

    def create_all(self, **options: Any) -> dict[str, ProviderContract]:
        """按默认顺序构造全部 Provider（同一组 ``options`` 传给每个工厂）。"""
        return {provider_id: self.create(provider_id, **options) for provider_id in self.ids}

    def capabilities(self, provider_id: str) -> ProviderCapabilities:
        """构造并读取某个 Provider 的能力声明（调用方需自行关闭返回的实例）。

        为避免资源泄漏，这里创建后立即关闭：能力声明是纯内存计算，不需要连接。
        """
        provider = self.create(provider_id)
        try:
            return provider.capabilities()
        finally:
            provider.close()

    def close_all(self) -> None:
        """关闭所有已创建的 Provider（幂等；重复 close 由各实现保证）。"""
        for provider in self._created:
            provider.close()
        self._created.clear()


def build_default_registry() -> Registry:
    """构造使用内建 Provider 的默认注册表。"""
    return Registry()


def get_provider_capabilities(
    registry: Registry | None = None,
    *,
    provider_ids: tuple[str, ...] | None = None,
) -> tuple[ProviderCapabilities, ...]:
    """MCP ``get_provider_capabilities`` 的实现：返回能力声明元组（**不发网络请求**）。

    Parameters
    ----------
    registry:
        注册表；``None`` 时使用默认注册表。
    provider_ids:
        只取指定的 Provider；``None`` 表示全部（按 :data:`DEFAULT_PROVIDER_IDS` 顺序）。
    """
    active = registry if registry is not None else build_default_registry()
    ids = provider_ids if provider_ids is not None else active.ids
    return tuple(active.capabilities(provider_id) for provider_id in ids)
