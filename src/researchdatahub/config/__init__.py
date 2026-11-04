"""配置子系统：YAML 加载、``env:`` 展开、严格校验与结构化校验结果。

模块边界
--------
* 只读配置，不做业务：不构造 :class:`~researchdatahub.core.models.DatasetRequest`，
  不访问网络，不写文件。
* ``config`` → ``core``（单向）。核心契约层不得反向依赖本包。
* 配置对象是不可变的 Pydantic 模型（``frozen=True``、``extra="forbid"``），
  加载完成后不会因为外部改 dict 而变化。

快速开始
--------
>>> from researchdatahub.config import load_defaults, load_sources
>>> app = load_defaults("configs/defaults.yaml")        # doctest: +SKIP
>>> sources = load_sources("configs/sources.example.yaml")  # doctest: +SKIP
>>> sources.sources["cds"].routes                        # doctest: +SKIP
frozenset({<AccessRoute.CDS_API: 'cds_api'>})
"""

from __future__ import annotations

from researchdatahub.config.loader import (
    EnvResolver,
    detect_config_kind,
    load_config,
    load_defaults,
    load_raw_config,
    load_sources,
    resolve_env_refs,
    validate_config_files,
)
from researchdatahub.config.models import (
    DEFAULT_STALE_TIMEOUT_SECONDS,
    AppConfig,
    ConcurrencyConfig,
    ConfigFileKind,
    ConfigKind,
    DefaultsConfig,
    Environment,
    FileValidation,
    IntegrityConfig,
    LogFormat,
    LoggingConfig,
    RetryConfig,
    SchedulerConfig,
    SourceConfig,
    SourcesConfig,
    StorageConfig,
    TransportBinding,
    ValidationResult,
)

__all__ = [
    "DEFAULT_STALE_TIMEOUT_SECONDS",
    "AppConfig",
    "ConcurrencyConfig",
    "ConfigFileKind",
    "ConfigKind",
    "DefaultsConfig",
    "EnvResolver",
    "Environment",
    "FileValidation",
    "IntegrityConfig",
    "LogFormat",
    "LoggingConfig",
    "RetryConfig",
    "SchedulerConfig",
    "SourceConfig",
    "SourcesConfig",
    "StorageConfig",
    "TransportBinding",
    "ValidationResult",
    "detect_config_kind",
    "load_config",
    "load_defaults",
    "load_raw_config",
    "load_sources",
    "resolve_env_refs",
    "validate_config_files",
]
