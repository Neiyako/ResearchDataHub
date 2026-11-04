"""CLI 组合根：把配置、数据库、适配器装配成命令需要的对象。

为什么单独一个模块
------------------
命令函数只应做"解析参数 → 调用 service → 呈现结果"。装配（读配置、开库、
构造 adapter、最后关闭连接/客户端）是**组合根**的职责，集中在这里可以保证：

* 每个命令用同一套装配逻辑，不会出现"某个命令忘了启 foreign_keys"这类分叉；
* 资源释放只有一个地方（:class:`CliContext` 负责 close），不靠每个命令自觉；
* 装配失败时给出统一的 :class:`~researchdatahub.core.errors.ConfigError`/``RdhError``。

**不允许**在这里写业务规则：业务规则在 ``planner`` / ``scheduler`` / ``transfer`` /
``integrity`` / ``manifest`` 各自的 service 层。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from researchdatahub.core.errors import ConfigError, RdhError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from researchdatahub.config.models import AppConfig, SourcesConfig
    from researchdatahub.manifest.cli_service import DbTarget
    from researchdatahub.manifest.repository import SqliteManifestRepository
    from researchdatahub.sources.registry import SourceRegistry
    from researchdatahub.transfer.base import TransportAdapterBase

__all__ = [
    "DEFAULT_DEFAULTS_PATH",
    "DEFAULT_SOURCES_PATH",
    "CliContext",
    "build_registry",
    "build_transports",
    "load_context",
    "open_repository",
]

DEFAULT_DEFAULTS_PATH: Path = Path("configs/defaults.yaml")
DEFAULT_SOURCES_PATH: Path = Path("configs/sources.example.yaml")
LOCAL_SOURCES_PATH: Path = Path("configs/sources.yaml")


def _resolve_sources_path(explicit: Path | None) -> Path:
    """选择数据源配置文件：显式 > 本地 `sources.yaml` > 示例文件。"""
    if explicit is not None:
        return explicit
    if LOCAL_SOURCES_PATH.is_file():
        return LOCAL_SOURCES_PATH
    print(
        f"[提示] 未找到 {LOCAL_SOURCES_PATH}，使用示例配置 {DEFAULT_SOURCES_PATH}。"
        "示例中的 cds 需要 CDSAPI_KEY，否则加载会失败。",
        file=sys.stderr,
    )
    return DEFAULT_SOURCES_PATH


@dataclass(frozen=True)
class CliContext:
    """一次 CLI 调用需要的装配结果。

    Attributes
    ----------
    app_config:
        已校验的全局配置（存储、并发、重试、完整性、调度参数）。
    sources_config:
        已展开 ``env:`` 的数据源声明。
    storage_root:
        实际使用的存储根（可能是 ``--root`` 覆盖后的值）。
    db_target:
        Manifest 数据库目标（路径 + 事务参数）。
    """

    app_config: AppConfig
    sources_config: SourcesConfig | None
    storage_root: Path
    db_target: DbTarget


def load_context(
    *,
    config_path: Path | None = None,
    sources_path: Path | None = None,
    storage_root: str | None = None,
    with_sources: bool = False,
) -> CliContext:
    """加载配置并解析数据库目标。

    Parameters
    ----------
    with_sources:
        是否加载数据源配置。**只有需要 adapter 的命令**（``plan create`` /
        ``task run`` / ``task reconcile``）才置 ``True``；``plan show`` / ``task status`` /
        ``report generate`` 只读数据库，不应因为"没配 CDS_KEY"而无法查看已有结果。

    Raises
    ------
    ConfigError
        配置文件缺失、校验失败或环境变量未设置。
    """
    from researchdatahub.config.loader import load_defaults, load_sources
    from researchdatahub.manifest.cli_service import resolve_db_target

    defaults_path = DEFAULT_DEFAULTS_PATH if config_path is None else config_path
    app_config = load_defaults(defaults_path)
    sources_config = None
    if with_sources:
        chosen_sources = _resolve_sources_path(sources_path)
        if not chosen_sources.is_file():
            raise ConfigError(
                f"数据源配置文件不存在：{chosen_sources}",
                detail={"path": str(chosen_sources)},
            )
        sources_config = load_sources(chosen_sources)
    target = resolve_db_target(defaults_path=defaults_path, storage_root=storage_root)
    if not target.db_path.parent.is_dir() or not target.db_path.is_file():
        raise ConfigError(
            f"Manifest 数据库尚未初始化：{target.db_path}",
            detail={"db_path": str(target.db_path), "hint": "先运行 rdh db init"},
        )
    root = Path(app_config.defaults.storage.root).expanduser()
    if storage_root is not None:
        root = Path(storage_root).expanduser()
    return CliContext(
        app_config=app_config,
        sources_config=sources_config,
        storage_root=root.resolve(strict=False),
        db_target=target,
    )


@contextmanager
def open_repository(context: CliContext) -> Iterator[SqliteManifestRepository]:
    """打开一个仓库实例，退出时关闭（每个线程/命令一个连接）。"""
    from researchdatahub.manifest.cli_service import open_manifest

    repository = open_manifest(context.db_target)
    try:
        yield repository
    finally:
        repository.close()


def build_registry(context: CliContext) -> SourceRegistry:
    """按配置构造数据源注册表（未实现的源会在注册时明确失败）。

    必须用 :meth:`SourceRegistry.with_defaults`：裸 ``SourceRegistry()`` 是**空注册表**，
    不含任何内建适配器类型，``build`` 会以 "unknown adapter type" 失败。这类装配错误
    只在真实运行 CLI 时暴露，所以由 ``tests/unit/test_cli_plan.py`` 覆盖。
    """
    from researchdatahub.sources.registry import SourceRegistry

    if context.sources_config is None:
        raise ConfigError(
            "该命令需要数据源配置，但上下文未加载 sources",
            detail={"hint": "load_context(..., with_sources=True)"},
        )
    return SourceRegistry.with_defaults().build(context.sources_config)


def build_transports(context: CliContext) -> Sequence[TransportAdapterBase]:
    """构造传输适配器（HTTP + S3 + CDS API）。调用方负责 ``close()``。

    CDS 资产（``access_route=cds_api``）的 ``origin_uri`` 是 CDS 结果接口返回的临时
    签名 https URL，但普通 :class:`HttpTransportAdapter` 只认 ``https`` 路由，不会接管
    它。少装配 :class:`CdsApiTransport` 时，``rdh task run`` 会直接以
    "no transport adapter supports access_route='cds_api'" 失败。
    """
    from researchdatahub.transfer.cds_api import CdsApiTransport
    from researchdatahub.transfer.http import HttpTransportAdapter
    from researchdatahub.transfer.retry import RetryPolicy
    from researchdatahub.transfer.s3 import S3TransportAdapter

    app_config = context.app_config
    retry = RetryPolicy.from_config(app_config.defaults.retry)
    transfer_config = app_config.defaults.transfer
    return (
        HttpTransportAdapter(config=transfer_config, retry=retry),
        S3TransportAdapter(
            config=transfer_config,
            s3_config=transfer_config.s3,
            retry=retry,
        ),
        # 继承 HttpTransportAdapter：流式/.part/续传/重试全部复用，只改路由判别与标识。
        CdsApiTransport(config=transfer_config, retry=retry),
    )


def close_transports(transports: Sequence[TransportAdapterBase]) -> None:
    """关闭全部传输适配器；单个关闭失败不影响其它（并如实报告）。"""
    failures: list[str] = []
    for adapter in transports:
        try:
            adapter.close()
        except RdhError as exc:  # 关闭失败不能遮住主流程的结果，但必须可见
            failures.append(f"{type(adapter).__name__}: {exc.message}")
    if failures:
        print("[警告] 关闭传输适配器时出错：" + "；".join(failures), file=sys.stderr)
