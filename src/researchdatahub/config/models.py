"""配置文件（YAML）对应的强类型 schema。

设计取舍
--------
* 与 :mod:`researchdatahub.core.models` 的关系：核心契约描述**运行时数据**
  （需求、资产、任务、清单）；本模块描述**配置**（存储根、并发、重试、数据源声明）。
  配置模型不进入 Manifest，也不进入 ``schema_version`` 的迁移范围，因此这里不携带
  ``schema_version``；配置格式的演进用「加字段给默认值」+ ``extra="forbid"`` 来兜底。
* ``extra="forbid"``：拼写错误的键（``retries`` 写成 ``retry``）必须立刻失败，
  而不是静默使用默认值——静默吞配置错误是运维事故的常见来源。
* 所有默认值写成显式 ``Field(default=...)``，使 ``configs/defaults.yaml`` 与
  「代码内默认值」保持同一来源可核对。
* 依赖方向：``config`` → ``core``（允许）；``core`` 绝不 import ``config``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from researchdatahub.core.models import AccessRoute, IntegrityCheckKind, JsonValue

__all__ = [
    "DEFAULT_STALE_TIMEOUT_SECONDS",
    "AppConfig",
    "ConcurrencyConfig",
    "ConfigFileKind",
    "ConfigKind",
    "DefaultsConfig",
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
]

DEFAULT_STALE_TIMEOUT_SECONDS = 3600
"""RUNNING 任务过期阈值（秒）。与 :data:`researchdatahub.core.state.DEFAULT_STALE_TIMEOUT_SECONDS`
保持同值；配置文件里可覆盖，但不能小于 60 秒（否则会把慢任务误判为已崩溃）。"""

IdStr = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
"""配置中的标识符（数据源 ID、传输 ID）。"""

PathStr = Annotated[str, StringConstraints(min_length=1, max_length=4096, strip_whitespace=True)]
"""路径字符串。允许 ``~`` 与 ``${RDH_HOME}`` 语义，具体展开由 loader 负责。"""


class _ConfigModel(BaseModel):
    """配置模型的公共配置。配置不可变：加载后不允许就地改写（防止半途修改生效不一致）。"""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        use_enum_values=False,
    )


class ConfigKind(StrEnum):
    """配置文件类型。``defaults`` 是全局默认值，``sources`` 是数据源声明。"""

    DEFAULTS = "defaults"
    SOURCES = "sources"

    @property
    def top_level_key(self) -> str:
        """该类型文件在 YAML 中要求的顶层键名（用于给出可操作的错误消息）。"""
        return "defaults" if self is ConfigKind.DEFAULTS else "sources"


# --------------------------------------------------------------------------- #
# defaults.yaml
# --------------------------------------------------------------------------- #
class LogFormat(StrEnum):
    """日志格式。``console`` 给人看，``jsonl`` 给采集/检索用。"""

    CONSOLE = "console"
    JSONL = "jsonl"


class LogLevel(StrEnum):
    """日志级别。"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Environment(StrEnum):
    """运行环境标签，用于日志与报告标注，不改变功能开关（M0 不做环境切换逻辑）。"""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class StorageConfig(_ConfigModel):
    """本地存储布局。

    ``root`` 支持三种写法：
    * ``~/.rdh``（波浪号展开）——默认值；
    * ``${RDH_HOME}`` 或 ``env:RDH_HOME``（环境变量引用，由 loader 展开；缺失即报错）；
    * 相对路径（相对仓库根/当前工作目录，由使用方决定，loader 只做类型与安全校验）。
    """

    root: PathStr = Field(default="~/.rdh", description="状态根目录（Manifest 库、隔离区、日志）")
    data_dir: str = Field(default="data", max_length=128, description="数据根（相对 root）")
    quarantine_dir: str = Field(
        default="quarantine", max_length=128, description="校验失败文件的隔离区（相对 root）"
    )
    tmp_dir: str = Field(default="tmp", max_length=128, description="临时区（相对 root）")
    free_space_floor_gb: float = Field(
        default=20.0, ge=0.0, le=1_000_000.0, description="进入隔离/停止下载前的磁盘剩余阈值"
    )
    min_backend: Literal["local"] = Field(
        default="local", description="M0 只支持本地后端；M1 扩展 s3/posix"
    )

    @field_validator("root")
    @classmethod
    def _root_not_bare_var(cls, value: str) -> str:
        if value in {"$", "${", "~"}:
            raise ValueError("storage.root must be a concrete path, not a lone placeholder")
        return value

    @field_validator("data_dir", "quarantine_dir", "tmp_dir")
    @classmethod
    def _relative_subdir(cls, value: str) -> str:
        if value.startswith(("/", "~")) or value.startswith("env:"):
            raise ValueError(
                f"subdirectory {value!r} must be relative to storage.root "
                "(no leading '/', '~' or 'env:')"
            )
        return value


class ConcurrencyConfig(_ConfigModel):
    """并发度。``0`` 表示由运行方按 CPU 核数决定（loader 不做平台猜测）。"""

    workers: int = Field(default=4, ge=0, le=64, description="传输线程数（0=自动）")
    per_source_limit: int = Field(
        default=2, ge=1, le=32, description="同一数据源的并发上限，避免触发远端限流"
    )
    discovery_workers: int = Field(default=2, ge=0, le=16, description="发现阶段并发（0=自动）")


class RetryConfig(_ConfigModel):
    """重试策略：指数退避 + 抖动。所有字段显式，禁止隐式 magic number。"""

    max_attempts: int = Field(default=3, ge=1, le=32, description="单任务最大尝试次数")
    backoff_seconds: float = Field(default=5.0, gt=0.0, le=3600.0, description="指数退避基数（秒）")
    backoff_max_seconds: float = Field(
        default=300.0, gt=0.0, le=86_400.0, description="单次退避上限（秒）"
    )
    jitter: float = Field(default=0.2, ge=0.0, le=1.0, description="均匀抖动比例 [0, 1]")
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "connection_reset", "http_5xx", "http_429"],
        max_length=16,
        description="可重试条件（传输层据此把异常映射为 RetryableTransportError）",
    )

    @model_validator(mode="after")
    def _backoff_within_cap(self) -> RetryConfig:
        if self.backoff_seconds > self.backoff_max_seconds:
            raise ValueError(
                f"retry.backoff_seconds ({self.backoff_seconds}) must not exceed "
                f"retry.backoff_max_seconds ({self.backoff_max_seconds})"
            )
        if len(set(self.retry_on)) != len(self.retry_on):
            raise ValueError("retry.retry_on contains duplicate conditions")
        return self

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待秒数（``attempt`` 从 1 开始，不含抖动）。

        抖动由调用方叠加（便于测试固定随机源）；此处只给确定性部分。
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        raw = self.backoff_seconds * (2 ** (attempt - 1))
        return float(min(raw, self.backoff_max_seconds))


class IntegrityConfig(_ConfigModel):
    """默认完整性策略。``UNKNOWN`` 绝不等于通过（见 core.models.IntegrityReport）。"""

    default_checks: list[IntegrityCheckKind] = Field(
        default_factory=lambda: [
            IntegrityCheckKind.SIZE_MATCH,
            IntegrityCheckKind.CHECKSUM_MATCH,
            IntegrityCheckKind.NETCDF_OPENABLE,
        ],
        min_length=1,
        max_length=len(IntegrityCheckKind),
    )
    stale_timeout_seconds: int = Field(
        default=DEFAULT_STALE_TIMEOUT_SECONDS,
        ge=60,
        le=604_800,
        description="RUNNING 超过该时长视为崩溃遗留，可被恢复流程接管",
    )
    fail_fast: bool = Field(default=False, description="首个检查失败即终止后续检查")

    @field_validator("default_checks")
    @classmethod
    def _unique_checks(cls, value: list[IntegrityCheckKind]) -> list[IntegrityCheckKind]:
        if len(set(value)) != len(value):
            raise ValueError("integrity.default_checks contains duplicates")
        return value


class LoggingConfig(_ConfigModel):
    """日志配置。``redact_env`` 列出写日志前必须打码的环境变量名。"""

    level: LogLevel = LogLevel.INFO
    format: LogFormat = LogFormat.CONSOLE
    file: str | None = Field(default=None, max_length=4096, description="日志文件（相对 root）")
    redact_env: list[str] = Field(
        default_factory=lambda: ["CDSAPI_KEY"],
        max_length=64,
        description="日志中必须被打码的环境变量名（密钥绝不明文落盘）",
    )

    @field_validator("redact_env")
    @classmethod
    def _env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or not name.replace("_", "").isalnum() or not name.isupper():
                raise ValueError(f"redact_env entry {name!r} must be an UPPER_SNAKE_CASE env name")
        return value


class SchedulerConfig(_ConfigModel):
    """调度器参数。

    ``free_space_floor_gb`` 与 ``storage.free_space_floor_gb`` 分工：前者用于
    「开始排队前的总量预检」，后者用于「运行中每次落盘前的复检」。
    """

    max_workers: int = Field(default=4, ge=1, le=64)
    free_space_floor_gb: float = Field(default=20.0, ge=0.0, le=1_000_000.0)
    priority_aging_seconds: int = Field(
        default=1800, ge=0, le=86_400, description="等待超时后提升优先级，防止低优先级饿死"
    )


class DefaultsConfig(_ConfigModel):
    """``configs/defaults.yaml`` 的 ``defaults:`` 段。"""

    environment: Environment = Environment.DEVELOPMENT
    storage: StorageConfig = Field(default_factory=StorageConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


class AppConfig(_ConfigModel):
    """完整的 ``defaults.yaml`` 文档：必须带顶层 ``defaults:`` 键，避免把 sources 误当默认值。"""

    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)


# --------------------------------------------------------------------------- #
# sources.yaml
# --------------------------------------------------------------------------- #
class TransportBinding(_ConfigModel):
    """**传输绑定**：某个访问路径下用哪个传输适配器。

    传输适配器只知道「怎么搬字节」，数据源适配器只知道「数据在哪、元数据长什么样」
    （ADR-0002）。二者的连接点就是这张表：``sources.<id>.transports[<route>]``。
    """

    route: AccessRoute = Field(description="访问路径（s3/https/globus/zarr/cds_api/local）")
    adapter: IdStr = Field(description="传输适配器 ID，如 's3' / 'https' / 'globus' / 'zarr'")
    implemented: bool = Field(default=False, description="该传输适配器在 M1 前不可用时保持 False")
    options: dict[str, JsonValue] = Field(
        default_factory=dict, description="传输层选项（并发分块、超时等），键需命名空间化"
    )

    @field_validator("options")
    @classmethod
    def _namespaced_options(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(
                    f"transport option key {key!r} must be namespaced, e.g. 's3.part_size_mb'"
                )
        return value


class SourceConfig(_ConfigModel):
    """一个数据源的声明。**只声明，不实现**：``implemented=false`` 必须被 CLI 与计划器识别。"""

    provider: str = Field(min_length=1, max_length=128, description="机构名，如 'NSF NCAR RDA'")
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    adapter: IdStr = Field(description="数据源适配器实现 ID（M0 未实现，值为契约名）")
    implemented: bool = Field(default=False, description="M0 一律 False；M1 落地后改 True")
    transports: list[TransportBinding] = Field(min_length=1, max_length=8)
    datasets: list[str] = Field(
        default_factory=list, max_length=128, description="该源提供的逻辑数据集 ID"
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="凭证引用，值必须是 env:VAR 形式且已在 loader 中展开为真实值",
    )
    endpoint: str | None = Field(default=None, max_length=4096)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    enabled: bool = Field(default=True, description="false 时计划器完全忽略该源")
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("transports")
    @classmethod
    def _unique_routes(cls, value: list[TransportBinding]) -> list[TransportBinding]:
        routes = [binding.route for binding in value]
        if len(set(routes)) != len(routes):
            raise ValueError("duplicate access route in transports")
        return value

    @field_validator("extensions")
    @classmethod
    def _namespaced_extensions(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if "." not in key:
                raise ValueError(f"extension key {key!r} must be namespaced, e.g. 'ncar.grid'")
        return value

    @model_validator(mode="after")
    def _implemented_implies_adapter(self) -> SourceConfig:
        if self.implemented and not self.adapter:
            raise ValueError("implemented source must declare an adapter id")
        return self

    @property
    def routes(self) -> frozenset[AccessRoute]:
        """该源可用的访问路径集合（供计划器选择传输实现）。"""
        return frozenset(binding.route for binding in self.transports)

    @property
    def usable_routes(self) -> frozenset[AccessRoute]:
        """已实现的传输路径集合。M0 全部为空，计划器据此拒绝出计划而不是假成功。"""
        return frozenset(binding.route for binding in self.transports if binding.implemented)


class SourcesConfig(_ConfigModel):
    """``configs/sources.yaml`` 的 ``sources:`` 段。键是数据源 ID（小写标识符）。"""

    sources: dict[IdStr, SourceConfig] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def _id_shape(cls, value: dict[str, SourceConfig]) -> dict[str, SourceConfig]:
        for source_id in value:
            if source_id != source_id.lower() or not source_id.replace("_", "").isalnum():
                raise ValueError(
                    f"source id {source_id!r} must be lowercase alphanumeric/underscore "
                    "(it is used as a DatasetRequest.preferred_sources entry)"
                )
        return value

    def enabled_ids(self) -> list[str]:
        """启用中的数据源 ID（保持声明顺序）。"""
        return [sid for sid, cfg in self.sources.items() if cfg.enabled]

    def required_env_vars(self) -> dict[str, list[str]]:
        """``source_id -> [凭证键]``：说明每个源依赖哪些凭证（值为已展开的真实值）。

        只返回键名，不回显值——报告与日志可以安全打印此结构。
        """
        return {
            sid: sorted(cfg.credentials) for sid, cfg in self.sources.items() if cfg.credentials
        }


# --------------------------------------------------------------------------- #
# 校验结果（CLI 与测试共用的稳定接口）
# --------------------------------------------------------------------------- #
class ConfigFileKind(StrEnum):
    """被校验文件的种类标签，出现在 :class:`FileValidation` 的 ``kind`` 字段。"""

    DEFAULTS = "defaults"
    SOURCES = "sources"
    UNKNOWN = "unknown"


class FileValidation(_ConfigModel):
    """单个配置文件的校验结果。``errors`` 为空 ⇔ ``ok=True``（由模型不变式强制）。"""

    path: str = Field(description="被校验文件的路径（字符串形式，不解析为绝对路径）")
    kind: ConfigFileKind
    ok: bool
    errors: list[str] = Field(default_factory=list)
    env_vars: list[str] = Field(
        default_factory=list, description="该文件中引用到的环境变量名（不含值）"
    )

    @model_validator(mode="after")
    def _ok_iff_no_errors(self) -> FileValidation:
        if self.ok and self.errors:
            raise ValueError("ok=True requires an empty errors list")
        if not self.ok and not self.errors:
            raise ValueError("ok=False requires at least one error message")
        return self


class ValidationResult(_ConfigModel):
    """:func:`researchdatahub.config.loader.validate_config_files` 的返回结构。

    ``ok`` 是全部文件 ``ok`` 的合取；调用方（CLI）据此决定退出码。
    """

    ok: bool
    files: list[FileValidation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aggregate_ok(self) -> ValidationResult:
        expected = all(item.ok for item in self.files)
        if self.ok != expected:
            raise ValueError(
                f"ValidationResult.ok={self.ok} contradicts per-file results (expected {expected})"
            )
        return self

    @property
    def failed_files(self) -> list[FileValidation]:
        """未通过的文件（便于 CLI 只打印失败项）。"""
        return [item for item in self.files if not item.ok]

    @property
    def error_messages(self) -> list[str]:
        """扁平化的错误列表，格式 ``"<path>: <error>"``，供 CLI 逐行输出。"""
        return [f"{item.path}: {error}" for item in self.files for error in item.errors]
