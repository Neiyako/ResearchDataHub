"""MCP 服务容器的组合根：配置、数据库、适配器、可选包的装配与能力探测。

职责边界（与 CLI 的 ``cli/_services.py`` 同一原则）
--------------------------------------------------
这里只做**装配**：读配置、开库、构造适配器、探测可选包是否可用。任何业务规则都不写在这里——
业务规则在 ``planner`` / ``scheduler`` / ``manifest`` / ``integrity`` / ``reporting`` 各自的
service 层，``tools/`` 只调用它们。

三个并行包的可选注入
--------------------
``researchdatahub.environment``（M2）、``researchdatahub.providers``（M3）与
``researchdatahub.network``（M4）由其它成员并行开发，M5 **不依赖**它们的可用性：

* :func:`probe_optional_packages` 只做 ``importlib.import_module`` 的**能力探测**，
  不调用它们的任何函数（它们的 API 仍在变化，硬编码调用会在定型后立刻变成耦合）；
* 使用它们的工具在不可用时返回结构化 ``not_available``，**不会**假装成功，
  也**不会**自己实现一份替代品；
* 真实实现通过 :class:`OptionalServices` 显式注入（生产装配或集成测试）。

并发与连接
----------
``sqlite3`` 连接不可跨线程共享，而工具调用在线程池里执行。因此这里按**调用**取连接：
:meth:`McpContext.repository` / :meth:`McpContext.approval_store` 都是上下文管理器，
每次进出新建/关闭一条连接，不缓存任何连接在手。
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from researchdatahub.config.loader import load_defaults, load_sources
from researchdatahub.config.models import AppConfig, DefaultsConfig, SourcesConfig
from researchdatahub.core.errors import ConfigError
from researchdatahub.core.ports import DataSourceAdapter, ManifestRepository
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.interfaces.mcp.auth import (
    APPROVAL_KEY_ENV_VAR,
    ApprovalStore,
    PlanApprovalAuthorizer,
    default_approval_key_path,
)
from researchdatahub.manifest.cli_service import DbTarget, open_manifest
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository
from researchdatahub.scheduler.models import SchedulerStorage, TransferExecutor
from researchdatahub.sources.base import SourceAdapter
from researchdatahub.sources.registry import SourceEntry, SourceRegistry
from researchdatahub.storage.local import LocalStorageBackend
from researchdatahub.transfer.http import HttpTransportAdapter
from researchdatahub.transfer.retry import RetryPolicy
from researchdatahub.transfer.s3 import S3TransportAdapter

__all__ = [
    "DEFAULT_DEFAULTS_PATH",
    "DEFAULT_SOURCES_PATH",
    "LOCAL_SOURCES_PATH",
    "OPTIONAL_PACKAGES",
    "McpContext",
    "OptionalServices",
    "PackageProbe",
    "attach_transports",
    "build_context",
    "capability_report",
    "probe_optional_packages",
]

logger = logging.getLogger(__name__)

DEFAULT_DEFAULTS_PATH: Final[Path] = Path("configs/defaults.yaml")
DEFAULT_SOURCES_PATH: Final[Path] = Path("configs/sources.example.yaml")
LOCAL_SOURCES_PATH: Final[Path] = Path("configs/sources.yaml")

OPTIONAL_PACKAGES: Final[tuple[str, ...]] = ("environment", "providers", "network")
"""并行开发的三个可选包（M2 / M3 / M4）。M5 只探测它们是否可导入，不调用其 API。"""


@dataclass(frozen=True)
class PackageProbe:
    """一个可选包的探测结果。"""

    package: str
    available: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好表示（``available`` 如实反映探测结论）。"""
        payload: dict[str, Any] = {"available": self.available}
        if self.error is not None:
            payload["error"] = self.error
        return payload


def probe_optional_packages() -> dict[str, PackageProbe]:
    """探测可选包能否导入（**绝不抛异常**：探测失败只降级为不可用）。"""
    probes: dict[str, PackageProbe] = {}
    for package in OPTIONAL_PACKAGES:
        module_name = f"researchdatahub.{package}"
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            probes[package] = PackageProbe(package, available=False, error=str(exc))
        except Exception as exc:
            probes[package] = PackageProbe(package, available=False, error=type(exc).__name__)
        else:
            probes[package] = PackageProbe(package, available=True)
    return probes


def capability_report(probes: Mapping[str, PackageProbe]) -> dict[str, Any]:
    """把探测结果整理成对外的能力声明（供能力工具与文档使用）。"""
    return {
        "optional_packages": {name: probe.as_dict() for name, probe in probes.items()},
        "tools_requires_package": {
            "get_environment_requirements": "environment",
            "check_environment": "environment",
            "validate_environment": "environment",
            "list_workers": "network",
        },
    }


@dataclass(frozen=True)
class OptionalServices:
    """从并行包注入的真实实现（**全部可缺席**；缺席即 ``not_available``）。

    每个字段是一个**薄适配器 callable**，由装配方决定怎么接到具体实现上。这样 M5 既不
    依赖尚未定型的 API，又能在集成测试里注入真实实现验证链路。
    """

    environment_requirements: Callable[[str], Any] | None = None
    environment_check: Callable[..., Any] | None = None
    worker_listing: Callable[..., Any] | None = None
    provider_capabilities: Callable[[str], Any] | None = None

    def environment_available(self) -> bool:
        """环境检查能力是否可用（两个 callable 都在才算）。"""
        return self.environment_requirements is not None and self.environment_check is not None

    def network_available(self) -> bool:
        """Worker 列表能力是否可用。"""
        return self.worker_listing is not None


@dataclass
class McpContext:
    """一次 MCP 服务运行所需的全部装配结果（工具 handler 的唯一依赖）。

    ``transports`` 默认为空：只有 ``submit_acquisition`` 显式要求同步执行时才装配
    （见 :func:`attach_transports`），避免只查询状态的会话白白建立 HTTP 连接池。
    """

    app_config: AppConfig
    sources_config: SourcesConfig | None
    storage_root: Path
    db_target: DbTarget
    registry: SourceRegistry
    adapters: tuple[DataSourceAdapter, ...] = ()
    storage: SchedulerStorage | None = None
    integrity: IntegrityChecker | None = None
    transports: tuple[TransferExecutor, ...] = field(default_factory=tuple)
    approval_authorizer: PlanApprovalAuthorizer | None = None
    optional: OptionalServices = field(default_factory=OptionalServices)

    # -- 便捷属性 --------------------------------------------------------- #
    @property
    def defaults(self) -> DefaultsConfig:
        """``defaults.yaml`` 的 ``defaults:`` 段。"""
        return self.app_config.defaults

    @property
    def db_path(self) -> Path:
        """Manifest 数据库文件路径。"""
        return self.db_target.db_path

    # -- 连接 ------------------------------------------------------------- #
    @contextmanager
    def repository(self) -> Iterator[SqliteManifestRepository]:
        """每条调用开一条新连接（sqlite3 连接不可跨线程共享）。"""
        repository = open_manifest(self.db_target)
        try:
            yield repository
        finally:
            repository.close()

    @contextmanager
    def approval_store(self) -> Iterator[ApprovalStore]:
        """开一条短连接读写 ``mcp_approvals``（表在启动时创建）。"""
        factory = ManifestConnectionFactory(self.db_path, config=self.db_target.config)
        connection = factory.connect()
        try:
            yield ApprovalStore(connection)
        finally:
            connection.close()

    # -- 装配 ------------------------------------------------------------- #
    def scheduler_repository_factory(self) -> ManifestRepository:
        """调度器的连接工厂（每个 worker 线程各自一条连接）。"""
        return open_manifest(self.db_target)

    def adapter_entries(self) -> tuple[SourceEntry, ...]:
        """注册表里的数据源条目快照（供能力类工具展示）。"""
        return self.registry.entries

    def capability_probes(self) -> dict[str, PackageProbe]:
        """当前可选能力探测结果（显式注入优先于包探测）。"""
        probes = probe_optional_packages()
        if self.optional.environment_available():
            probes["environment"] = PackageProbe("environment", available=True)
        if self.optional.network_available():
            probes["network"] = PackageProbe("network", available=True)
        return probes

    def with_services(self, services: OptionalServices) -> McpContext:
        """返回注入了可选服务的新上下文（不修改自身，避免线程间互相污染）。"""
        return replace(self, optional=services)

    # -- 资源 ------------------------------------------------------------- #
    def close(self) -> None:
        """关闭服务持有的资源（传输适配器、源适配器的 HTTP 客户端）；失败只记日志。"""
        for transport in self.transports:
            _close_quietly(transport, "transport adapter")
        for entry in self.registry.entries:
            _close_quietly(entry.adapter, "source adapter")


def _close_quietly(resource: object, label: str) -> None:
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        logger.warning("failed to close %s", label, exc_info=True)


# --------------------------------------------------------------------------- #
# 装配函数
# --------------------------------------------------------------------------- #
def build_context(
    *,
    root: str | Path | None = None,
    config_path: str | Path | None = None,
    sources_path: str | Path | None = None,
    approval_key_path: str | Path | None = None,
    services: OptionalServices | None = None,
    clock: Callable[[], datetime] | None = None,
) -> McpContext:
    """按配置装配 :class:`McpContext`（``rdh-mcp`` 的启动路径）。

    Parameters
    ----------
    root:
        ``--root`` 指定的状态根；覆盖 ``defaults.storage.root`` 与数据库位置。
    config_path:
        ``defaults.yaml`` 路径；缺省为 ``configs/defaults.yaml``。
    sources_path:
        ``sources.yaml`` 路径；缺省优先 ``configs/sources.yaml``，回落到示例文件。
    approval_key_path:
        审批密钥路径；缺省读 ``RDH_APPROVAL_KEY_FILE``，再缺省 ``<root>/approval.key``。
    services:
        并行包的真实实现（可缺席）。
    clock:
        可注入时钟（测试用）。

    Raises
    ------
    ConfigError
        配置文件缺失/校验失败、Manifest 库不存在（提示先 ``rdh db init``）。
    """
    defaults_path = DEFAULT_DEFAULTS_PATH if config_path is None else Path(config_path)
    if not defaults_path.is_file():
        raise ConfigError(
            f"defaults 配置文件不存在：{defaults_path}",
            detail={"path": str(defaults_path), "hint": "在仓库根目录运行，或显式传 --config"},
        )
    app_config = load_defaults(defaults_path)
    storage_root = Path(
        str(root) if root is not None else app_config.defaults.storage.root
    ).expanduser()

    sources_config = _load_sources_config(sources_path)
    registry, adapters = _build_registry(sources_config)

    db_target = DbTarget(
        db_path=_db_path(storage_root, app_config), config=app_config.defaults.manifest
    )
    if not db_target.db_path.is_file():
        raise ConfigError(
            f"Manifest 数据库尚未初始化：{db_target.db_path}",
            detail={"db_path": str(db_target.db_path), "hint": "先运行 rdh db init"},
        )

    effective_config = app_config
    if root is not None:
        # ``--root`` 必须同时作用于存储布局与数据库位置，否则写下载会落回默认根。
        storage_config = app_config.defaults.storage.model_copy(update={"root": str(storage_root)})
        effective_config = app_config.model_copy(
            update={"defaults": app_config.defaults.model_copy(update={"storage": storage_config})}
        )

    context = McpContext(
        app_config=effective_config,
        sources_config=sources_config,
        storage_root=storage_root,
        db_target=db_target,
        registry=registry,
        adapters=adapters,
        storage=LocalStorageBackend(effective_config.defaults.storage, create_dirs=True),
        integrity=IntegrityChecker(effective_config.defaults.integrity),
        approval_authorizer=PlanApprovalAuthorizer(
            key_path=_resolve_key_path(approval_key_path, storage_root),
            clock=clock,
        ),
        optional=services if services is not None else OptionalServices(),
    )
    _ensure_approval_table(context)
    return context


def _build_registry(
    sources_config: SourcesConfig | None,
) -> tuple[SourceRegistry, tuple[DataSourceAdapter, ...]]:
    """构造注册表与适配器；单个源失败不阻止服务启动（工具会如实报告不可用）。"""
    if sources_config is None:
        return SourceRegistry.with_defaults(), ()
    try:
        registry = SourceRegistry.with_defaults().build(sources_config)
    except ConfigError:
        logger.warning("some data sources failed to build; continuing without them")
        return SourceRegistry.with_defaults(), ()
    adapters = tuple(
        entry.adapter
        for entry in registry.entries
        if entry.enabled and isinstance(entry.adapter, SourceAdapter)
    )
    return registry, adapters


def _resolve_key_path(explicit: str | Path | None, storage_root: Path) -> Path:
    """密钥路径优先级：显式参数 > 环境变量 > ``<root>/approval.key``。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(APPROVAL_KEY_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return default_approval_key_path(storage_root)


def _db_path(storage_root: Path, app_config: AppConfig) -> Path:
    return (storage_root / app_config.defaults.manifest.db_filename).expanduser().resolve()


def _load_sources_config(explicit: str | Path | None) -> SourcesConfig | None:
    """加载数据源配置；不存在时返回 ``None``（由工具如实报告，而不是启动失败）。"""
    candidate: Path
    if explicit is not None:
        candidate = Path(explicit)
        if not candidate.is_file():
            raise ConfigError(f"数据源配置文件不存在：{candidate}", detail={"path": str(candidate)})
    elif LOCAL_SOURCES_PATH.is_file():
        candidate = LOCAL_SOURCES_PATH
    elif DEFAULT_SOURCES_PATH.is_file():
        candidate = DEFAULT_SOURCES_PATH
    else:
        return None
    try:
        return load_sources(candidate)
    except ConfigError:
        logger.warning("sources config %s is invalid; dataset tools will report empty", candidate)
        return None


def _ensure_approval_table(context: McpContext) -> None:
    """启动时确保 ``mcp_approvals`` 存在（幂等；失败则拒绝启动，不静默降级）。"""
    factory = ManifestConnectionFactory(context.db_path, config=context.db_target.config)
    connection = factory.connect()
    try:
        ApprovalStore(connection).ensure_schema()
    finally:
        connection.close()


def attach_transports(context: McpContext) -> McpContext:
    """按需装配传输适配器（只有需要真正同步执行时才调用）。"""
    if context.transports:
        return context
    retry = RetryPolicy.from_config(context.defaults.retry)
    transports: tuple[TransferExecutor, ...] = (
        HttpTransportAdapter(config=context.defaults.transfer, retry=retry),
        S3TransportAdapter(
            config=context.defaults.transfer, s3_config=context.defaults.transfer.s3, retry=retry
        ),
    )
    return replace(context, transports=transports)
