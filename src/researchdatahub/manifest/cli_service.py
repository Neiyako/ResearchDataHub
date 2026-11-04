"""``rdh db init / db status / db migrate`` 的 service 层。

职责边界
--------
CLI 只解析参数、调用本模块、把返回的 dataclass / ``DatabaseStatus`` 渲染成表格；
**所有**数据库路径解析、迁移、状态统计、错误语义都在这里，便于单测直接覆盖，
不需要启动子进程或伪造 Typer 上下文。

路径来源
--------
数据库文件 = ``storage.root / manifest.db_filename``（``configs/defaults.yaml`` 的
``defaults.manifest`` 段）。``storage.root`` 支持 ``~`` 展开；生产环境默认
``~/.rdh``。测试必须用 ``tmp_path`` 构造 :class:`DbTarget`，禁止落到真实用户目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from researchdatahub.config.loader import load_defaults
from researchdatahub.config.models import ManifestConfig
from researchdatahub.core.errors import ManifestError
from researchdatahub.core.models import DatabaseStatus
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.manifest.repository import SqliteManifestRepository

__all__ = [
    "DEFAULT_DEFAULTS_PATH",
    "DbInitResult",
    "DbMigrateResult",
    "DbTarget",
    "db_init",
    "db_migrate",
    "db_status",
    "open_manifest",
    "resolve_db_target",
]

#: 默认配置文件路径（相对当前工作目录，与仓库布局一致）。
DEFAULT_DEFAULTS_PATH: Path = Path("configs/defaults.yaml")


@dataclass(frozen=True)
class DbTarget:
    """数据库目标：文件路径 + Manifest 配置（CLI 与服务层之间的稳定契约）。

    Parameters
    ----------
    db_path:
        SQLite 文件路径。
    config:
        ``defaults.manifest`` 段（busy_timeout / 写重试次数等）。
    """

    db_path: Path
    config: ManifestConfig


@dataclass(frozen=True)
class DbInitResult:
    """``rdh db init`` 的可打印结果。"""

    db_path: Path
    created: bool
    previous_version: int
    schema_version: int
    applied_versions: tuple[int, ...]


@dataclass(frozen=True)
class DbMigrateResult:
    """``rdh db migrate`` 的可打印结果。"""

    db_path: Path
    previous_version: int
    schema_version: int
    applied: tuple[str, ...]


def resolve_db_target(
    *,
    defaults_path: str | Path | None = None,
    storage_root: str | Path | None = None,
) -> DbTarget:
    """从配置文件解析 :class:`DbTarget`。

    Parameters
    ----------
    defaults_path:
        ``defaults.yaml`` 路径；``None`` 时使用 :data:`DEFAULT_DEFAULTS_PATH`。
    storage_root:
        覆盖 ``storage.root``（测试或便携部署用）；``None`` 时取配置文件里的值。

    Returns
    -------
    DbTarget
        绝对路径 + Manifest 配置。

    Raises
    ------
    ConfigError
        配置文件缺失或校验失败（由 :func:`researchdatahub.config.loader.load_defaults` 抛出）。
    """
    app_config = load_defaults(DEFAULT_DEFAULTS_PATH if defaults_path is None else defaults_path)
    manifest_config = app_config.defaults.manifest
    if storage_root is not None:
        root = Path(storage_root).expanduser()
    else:
        root = Path(app_config.defaults.storage.root).expanduser()
    db_path = (root / manifest_config.db_filename).resolve()
    return DbTarget(db_path=db_path, config=manifest_config)


def open_manifest(target: DbTarget) -> SqliteManifestRepository:
    """为当前线程打开一个仓库实例（调用方负责 ``close()``）。

    Parameters
    ----------
    target:
        数据库目标。

    Returns
    -------
    SqliteManifestRepository
        已打开连接的仓库。
    """
    factory = ManifestConnectionFactory(target.db_path, config=target.config)
    return SqliteManifestRepository(factory)


def _require_parent(target: DbTarget) -> None:
    """确保数据库父目录存在（``db init`` / ``db migrate`` 负责创建，查询命令不创建）。"""
    try:
        target.db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ManifestError(
            f"cannot create manifest database directory {target.db_path.parent}: {exc}",
            detail={"db_path": str(target.db_path)},
        ) from exc


def db_init(target: DbTarget) -> DbInitResult:
    """创建数据库目录、初始化 schema（幂等）并返回迁移摘要。

    Parameters
    ----------
    target:
        数据库目标。

    Returns
    -------
    DbInitResult
        是否新建文件、迁移前后版本、实际应用的迁移版本号。

    Raises
    ------
    ManifestError
        目录无法创建、数据库无法打开或迁移失败（迁移失败整体回滚）。
    """
    created = not target.db_path.exists()
    _require_parent(target)
    factory = ManifestConnectionFactory(target.db_path, config=target.config)
    report = factory.migrate()
    return DbInitResult(
        db_path=target.db_path,
        created=created,
        previous_version=report.previous_version,
        schema_version=report.schema_version,
        applied_versions=tuple(migration.version for migration in report.applied),
    )


def db_migrate(target: DbTarget) -> DbMigrateResult:
    """应用待执行迁移（幂等）并返回摘要；数据库不存在时按空库执行全部迁移。

    Raises
    ------
    ManifestError
        目录无法创建、数据库无法打开或迁移失败。
    """
    _require_parent(target)
    factory = ManifestConnectionFactory(target.db_path, config=target.config)
    report = factory.migrate()
    return DbMigrateResult(
        db_path=target.db_path,
        previous_version=report.previous_version,
        schema_version=report.schema_version,
        applied=tuple(
            f"v{migration.version}: {migration.description}" for migration in report.applied
        ),
    )


def db_status(target: DbTarget) -> DatabaseStatus:
    """读取数据库状态快照（``rdh db status`` 的数据来源）。

    Raises
    ------
    ManifestError
        数据库文件不存在或尚未初始化（提示用户先执行 ``rdh db init``）。
    """
    if not target.db_path.is_file():
        raise ManifestError(
            f"manifest database does not exist: {target.db_path}",
            detail={"db_path": str(target.db_path), "hint": "run 'rdh db init'"},
        )
    repository = open_manifest(target)
    try:
        return repository.status()
    finally:
        repository.close()
