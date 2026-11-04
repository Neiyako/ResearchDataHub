"""数据源注册表：注册、构建、查询、启停与健康检查。

为什么是注册表而不是 ``if source_id == ...``
-------------------------------------------
ADR-0002 要求"新增数据源 = 新增一个 ``DataSourceAdapter`` + 一条配置"，传输层一行不动。
如果适配器选择写成 ``if/else`` 堆叠，那么：

* 每加一个源都要改调度路径上的公共函数；
* 无法在运行期禁用/替换某个源做对比实验；
* 单测必须 import 所有源实现，任何一源坏了全挂。

本模块把"适配器类型 → 构造工厂"做成注册表，``SourceRegistry`` 只做查表与生命周期管理；
:data:`DEFAULT_FACTORIES` 是唯一的源类型清单（**没有**按 source_id 的分支）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from researchdatahub.config.models import SourceConfig, SourcesConfig
from researchdatahub.core.errors import ConfigError, SourceNotFoundError, SourceUnavailableError
from researchdatahub.core.models import DatasetRequest, SourceAsset, SourceMetadata
from researchdatahub.sources.base import AdapterCapabilities, SourceAdapter
from researchdatahub.sources.cds import cds_adapter_from_config
from researchdatahub.sources.ncar import ncar_adapter_from_config

__all__ = [
    "DEFAULT_FACTORIES",
    "AdapterFactory",
    "SourceEntry",
    "SourceRegistry",
]

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[str, SourceConfig], SourceAdapter]
"""适配器工厂：``(source_id, source_config) -> SourceAdapter``。"""

DEFAULT_FACTORIES: Final[dict[str, AdapterFactory]] = {
    # 配置里的 ``sources.<id>.adapter`` 取值 → 构造工厂。
    # ``rda`` 是配置示例使用的名称；``ncar`` 作为同一实现的别名保留，避免配置拼写差异
    # 变成两套代码。
    "cds": cds_adapter_from_config,
    "rda": ncar_adapter_from_config,
    "ncar": ncar_adapter_from_config,
}
"""内建适配器类型清单；新增数据源只需在此登记一个工厂。"""


@dataclass(frozen=True)
class SourceEntry:
    """注册表中的一个数据源条目（配置 + 已构造的适配器 + 运行期启停状态）。"""

    source_id: str
    config: SourceConfig
    adapter: SourceAdapter
    enabled: bool

    @property
    def adapter_type(self) -> str:
        """配置声明的适配器类型（``sources.<id>.adapter``）。"""
        return self.config.adapter

    @property
    def implemented(self) -> bool:
        """配置是否声明该源已实现（``implemented`` 字段，M0 契约为 false）。"""
        return self.config.implemented

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        """配置声明的逻辑数据集 ID。"""
        return tuple(self.config.datasets)


class SourceRegistry:
    """数据源注册表（线程安全：注册/启停加锁，查询返回快照）。

    使用方式
    --------
    >>> registry = SourceRegistry.with_defaults().build(sources_config)
    >>> registry.get("cds").source_id
    'cds'
    """

    def __init__(self, *, factories: Mapping[str, AdapterFactory] | None = None) -> None:
        self._factories: dict[str, AdapterFactory] = dict(factories or {})
        self._entries: dict[str, SourceEntry] = {}
        self._enabled: set[str] = set()
        self._lock = threading.RLock()

    # -- 注册表维护 -------------------------------------------------------- #
    @classmethod
    def with_defaults(cls) -> SourceRegistry:
        """返回注册了全部内建适配器类型的空注册表。"""
        return cls(factories=DEFAULT_FACTORIES)

    @property
    def adapter_types(self) -> tuple[str, ...]:
        """已注册的适配器类型（升序）。"""
        with self._lock:
            return tuple(sorted(self._factories))

    def register(
        self, adapter_type: str, factory: AdapterFactory, *, replace: bool = False
    ) -> None:
        """登记一个适配器类型。

        Raises
        ------
        ValueError
            类型名为空。
        ConfigError
            重复登记且未声明 ``replace=True``（静默覆盖会让实现悄悄漂移）。
        """
        name = adapter_type.strip().lower()
        if not name:
            raise ValueError("adapter_type must be a non-empty string")
        with self._lock:
            if name in self._factories and not replace:
                raise ConfigError(
                    f"adapter type {name!r} is already registered; pass replace=True to override",
                    detail={"adapter_type": name},
                )
            self._factories[name] = factory

    def unregister(self, adapter_type: str) -> None:
        """注销一个适配器类型；已构建的条目不受影响。

        Raises
        ------
        ConfigError
            类型未登记。
        """
        name = adapter_type.strip().lower()
        with self._lock:
            if name not in self._factories:
                raise ConfigError(
                    f"adapter type {name!r} is not registered",
                    detail={"adapter_type": name},
                )
            del self._factories[name]

    # -- 构建 -------------------------------------------------------------- #
    def create(self, source_id: str, config: SourceConfig) -> SourceAdapter:
        """按配置的 ``adapter`` 字段构造适配器（不加入注册表）。

        Raises
        ------
        ConfigError
            配置引用了未登记的适配器类型，或工厂构造失败。
        """
        adapter_type = config.adapter.strip().lower()
        with self._lock:
            factory = self._factories.get(adapter_type)
            registered = tuple(sorted(self._factories))
        if factory is None:
            raise ConfigError(
                f"source {source_id!r} references unknown adapter type {adapter_type!r}",
                detail={
                    "source_id": source_id,
                    "adapter_type": adapter_type,
                    "registered": list(registered),
                },
            )
        try:
            return factory(source_id, config)
        except (ConfigError, SourceNotFoundError, SourceUnavailableError):
            raise
        except Exception as exc:  # pragma: no cover - 工厂缺陷诊断出口
            raise ConfigError(
                f"failed to construct adapter {adapter_type!r} for source {source_id!r}",
                detail={
                    "source_id": source_id,
                    "adapter_type": adapter_type,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def build(
        self,
        sources: SourcesConfig,
        *,
        include_disabled: bool = False,
        adopt_existing: bool = True,
    ) -> SourceRegistry:
        """从 :class:`SourcesConfig` 构建注册表（返回新实例，不回滚旧实例）。

        Parameters
        ----------
        sources:
            已由 loader 加载并展开 ``env:`` 的配置对象。
        include_disabled:
            是否连 ``enabled=False`` 的源一起构建（默认跳过）。
        adopt_existing:
            是否沿用当前注册表中已登记且 source_id 相同的条目的适配器实例
            （避免重建 HTTP 客户端；默认开启）。

        Raises
        ------
        ConfigError
            任一源引用未知适配器类型或构造失败（聚合报告全部失败项）。
        """
        rebuilt = SourceRegistry(factories=dict(self._factories))
        failures: list[dict[str, object]] = []
        for source_id, config in sources.sources.items():
            if not config.enabled and not include_disabled:
                continue
            adapter: SourceAdapter | None = None
            if adopt_existing:
                with self._lock:
                    existing = self._entries.get(source_id)
                if existing is not None and existing.adapter_type == config.adapter:
                    adapter = existing.adapter
            if adapter is None:
                try:
                    adapter = rebuilt.create(source_id, config)
                except ConfigError as exc:
                    failures.append({"source_id": source_id, "error": exc.message})
                    continue
            rebuilt._entries[source_id] = SourceEntry(
                source_id=source_id,
                config=config,
                adapter=adapter,
                enabled=config.enabled,
            )
            if config.enabled:
                rebuilt._enabled.add(source_id)
        if failures:
            raise ConfigError(
                "one or more data sources could not be built",
                detail={"failures": failures},
            )
        return rebuilt

    # -- 查询 -------------------------------------------------------------- #
    def __len__(self) -> int:
        """条目数量。"""
        with self._lock:
            return len(self._entries)

    def __contains__(self, source_id: object) -> bool:
        """``source_id in registry``：是否已构建该源。"""
        with self._lock:
            return isinstance(source_id, str) and source_id in self._entries

    def __iter__(self) -> Iterator[SourceEntry]:
        """按 source_id 升序迭代条目快照。"""
        return iter(self.entries)

    @property
    def entries(self) -> tuple[SourceEntry, ...]:
        """全部条目（按 source_id 升序的快照）。"""
        with self._lock:
            return tuple(self._entries[key] for key in sorted(self._entries))

    @property
    def enabled_sources(self) -> tuple[str, ...]:
        """启用中的 source_id（升序）。"""
        with self._lock:
            return tuple(sorted(self._enabled))

    def entry(self, source_id: str) -> SourceEntry:
        """取条目（不检查启用状态）。

        Raises
        ------
        SourceNotFoundError
            注册表里没有该 source_id。
        """
        with self._lock:
            found = self._entries.get(source_id)
        if found is None:
            with self._lock:
                registered = sorted(self._entries)
            raise SourceNotFoundError(
                f"source {source_id!r} is not registered",
                source_id=source_id,
                detail={"registered": registered},
            )
        return found

    def get(self, source_id: str) -> SourceAdapter:
        """取已启用的适配器。

        Raises
        ------
        SourceNotFoundError
            未注册。
        SourceUnavailableError
            已注册但当前被禁用。
        """
        found = self.entry(source_id)
        with self._lock:
            enabled = source_id in self._enabled
        if not enabled:
            raise SourceUnavailableError(
                f"source {source_id!r} is disabled in this registry",
                source_id=source_id,
            )
        return found.adapter

    def get_or_none(self, source_id: str) -> SourceAdapter | None:
        """取启用中的适配器；未注册或已禁用返回 ``None``（供可选路径使用）。"""
        try:
            return self.get(source_id)
        except (SourceNotFoundError, SourceUnavailableError):
            return None

    def adapter_type(self, source_id: str) -> str:
        """返回该源的适配器类型 ID（供报告/CLI 展示，不触发 I/O）。"""
        return self.entry(source_id).adapter_type

    def source_ids_for_dataset(self, dataset_id: str) -> tuple[str, ...]:
        """哪些已启用数据源声明了该逻辑数据集（按注册顺序的确定性排序）。"""
        with self._lock:
            return tuple(
                sorted(
                    entry.source_id
                    for entry in self._entries.values()
                    if entry.source_id in self._enabled and dataset_id in entry.dataset_ids
                )
            )

    # -- 启停 -------------------------------------------------------------- #
    def enable(self, source_id: str) -> bool:
        """启用一个源；返回是否发生变化。

        Raises
        ------
        SourceNotFoundError
            未注册。
        """
        self.entry(source_id)
        with self._lock:
            if source_id in self._enabled:
                return False
            self._enabled.add(source_id)
            return True

    def disable(self, source_id: str) -> bool:
        """禁用一个源（适配器实例保留，可再启用）；返回是否发生变化。

        Raises
        ------
        SourceNotFoundError
            未注册。
        """
        self.entry(source_id)
        with self._lock:
            if source_id not in self._enabled:
                return False
            self._enabled.discard(source_id)
            return True

    def is_enabled(self, source_id: str) -> bool:
        """该源是否启用（未注册返回 ``False``）。"""
        with self._lock:
            return source_id in self._enabled

    # -- 能力 / 元数据 / 发现 / 健康检查 ---------------------------------- #
    def capabilities(self) -> dict[str, AdapterCapabilities]:
        """所有条目（含禁用）的能力边界，按 source_id 升序。"""
        return {entry.source_id: entry.adapter.capabilities() for entry in self.entries}

    def describe(self, source_id: str) -> SourceMetadata:
        """返回数据源元数据（委托给适配器）。"""
        return self.get(source_id).describe()

    def discover(self, source_id: str, request: DatasetRequest) -> Sequence[SourceAsset]:
        """在指定源上发现资产（委托给适配器；不下载字节）。"""
        return self.get(source_id).discover_assets(request)

    def health_check(self, source_id: str) -> bool:
        """单源健康检查；未注册/被禁用返回 ``False``（不抛异常）。"""
        adapter = self.get_or_none(source_id)
        if adapter is None:
            return False
        return adapter.health_check()

    def health_check_all(self) -> dict[str, bool]:
        """所有启用源的健康检查结果（按 source_id 升序）。"""
        return {source_id: self.health_check(source_id) for source_id in self.enabled_sources}

    # -- 生命周期 ---------------------------------------------------------- #
    def close(self) -> None:
        """关闭所有条目的适配器（幂等，异常不阻断其余关闭）。"""
        for entry in self.entries:
            try:
                entry.adapter.close()
            except Exception as exc:  # pragma: no cover - 资源释放诊断出口
                logger.warning(
                    "failed to close adapter: source_id=%s error_type=%s",
                    entry.source_id,
                    type(exc).__name__,
                )
        with self._lock:
            self._entries.clear()
            self._enabled.clear()

    def __enter__(self) -> SourceRegistry:
        """进入上下文（退出时关闭全部适配器）。"""
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """退出上下文并关闭全部适配器。"""
        self.close()
