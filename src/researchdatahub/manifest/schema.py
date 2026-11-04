"""SQLite Manifest 的 schema 与版本化迁移定义。

设计要点
--------
* **版本表**：``schema_version`` 表逐行记录每次已应用的迁移（版本号、说明、时间）。
  不使用 ``PRAGMA user_version``：单一整数无法回答“哪个迁移在什么时候被应用”，
  而 schema 变更本身是需要审计的事实（ADR-0003 的审计原则同样适用于 schema）。
* **只向前迁移**：:data:`MIGRATIONS` 是有序元组，版本号从 1 开始连续递增；
  每个迁移在独立的 ``BEGIN IMMEDIATE`` 事务内执行，失败整体回滚，
  不会留下“迁移做了一半”的库。
* **为什么 DDL 写在 Python 常量里而不是 ``schema.sql``**：迁移必须是可测试、
  可追溯到具体版本的对象；放进 :class:`Migration` 元组后，测试可以直接断言
  “版本连续、语句非空、每个版本恰好一个迁移”，无需解析 SQL 文件，也不需要
  额外配置打包非 Python 文件。

表设计
------
9 张业务表：``sources`` / ``datasets`` / ``requests`` / ``assets`` / ``tasks`` /
``files`` / ``transfer_logs`` / ``integrity_reports`` / ``schema_version``，
并在 v2 增加 ``plans`` / ``plan_tasks`` 以支撑 :class:`AcquisitionPlan` 持久化。

关系与约束
----------
* ``assets.source_id -> sources.source_id``：资产必须挂在一个已登记数据源上；
* ``tasks.asset_id -> assets.asset_id``：任务必须引用一个已发现资产；
* ``files.task_id -> tasks.task_id``：文件记录必须挂在任务上（“文件记录与任务关联”的
  数据库级保证）；
* ``transfer_logs.task_id -> tasks.task_id``、``integrity_reports.task_id -> tasks.task_id``、
  ``integrity_reports.file_id -> files.file_id``：审计链不允许指向不存在的对象；
* ``tasks.plan_id`` / ``tasks.request_id`` **故意不加外键**：任务可以在计划被登记之前
  由测试或调度器直接写入，计划登记是幂等重放（``save_plan`` 不覆盖已推进的任务状态）。
  这是刻意的取舍，已在 ``repository.save_plan`` 的 docstring 里再次说明。

时间与 JSON
-----------
所有时间列都是 ISO 8601 UTC 字符串（``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``），
所有结构化列都是 JSON 文本。SQLite 没有原生 datetime/JSON 类型，格式统一由
:mod:`researchdatahub.manifest.rows` 的编解码函数负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "Migration",
    "latest_version",
]


@dataclass(frozen=True)
class Migration:
    """一次不可变的 schema 迁移。

    Parameters
    ----------
    version:
        迁移版本号，从 1 开始连续递增。
    description:
        人类可读的变更说明，写入 ``schema_version.description`` 供审计。
    statements:
        必须在**同一事务**内依次执行的 DDL/DML 语句；不允许为空。
    """

    version: int
    description: str
    statements: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        """定义期自检：版本号、说明与语句都不能为空。"""
        if self.version < 1:
            raise ValueError(f"migration version must be >= 1, got {self.version}")
        if not self.description.strip():
            raise ValueError("migration description must not be empty")
        if not self.statements:
            raise ValueError(f"migration v{self.version} has no statements")
        if any(not statement.strip() for statement in self.statements):
            raise ValueError(f"migration v{self.version} contains an empty statement")


_V1_STATEMENTS: Final[tuple[str, ...]] = (
    # -- schema 版本账本 -------------------------------------------------- #
    """
    CREATE TABLE schema_version (
        version     INTEGER PRIMARY KEY,
        description TEXT    NOT NULL,
        applied_at  TEXT    NOT NULL
    )
    """,
    # -- 数据源目录 ------------------------------------------------------- #
    """
    CREATE TABLE sources (
        source_id        TEXT PRIMARY KEY,
        display_name     TEXT NOT NULL,
        provider         TEXT NOT NULL,
        access_route     TEXT NOT NULL,
        dataset_id       TEXT NOT NULL,
        origin_uri       TEXT NOT NULL,
        protocol_version TEXT,
        license          TEXT,
        citation         TEXT,
        retrieved_at     TEXT NOT NULL,
        extensions       TEXT NOT NULL DEFAULT '{}',
        adapter_id       TEXT,
        updated_at       TEXT NOT NULL
    )
    """,
    # -- 数据集科学元数据 ------------------------------------------------- #
    """
    CREATE TABLE datasets (
        dataset_id TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        metadata   TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # -- 用户需求 --------------------------------------------------------- #
    """
    CREATE TABLE requests (
        request_id         TEXT PRIMARY KEY,
        dataset_id         TEXT NOT NULL,
        priority           INTEGER NOT NULL,
        payload            TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    # -- 远端资产 --------------------------------------------------------- #
    """
    CREATE TABLE assets (
        asset_id            TEXT PRIMARY KEY,
        source_id           TEXT NOT NULL REFERENCES sources(source_id),
        dataset_id          TEXT NOT NULL,
        access_route        TEXT NOT NULL,
        origin_uri          TEXT NOT NULL,
        file_name           TEXT NOT NULL,
        size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
        checksum            TEXT,
        remote_etag         TEXT,
        remote_last_modified TEXT,
        content_type        TEXT,
        time_slice          TEXT,
        level_slice         TEXT,
        metadata            TEXT NOT NULL,
        discovered_at       TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_assets_dataset ON assets(dataset_id)",
    "CREATE INDEX idx_assets_source ON assets(source_id)",
    # -- 任务 ------------------------------------------------------------- #
    """
    CREATE TABLE tasks (
        task_id            TEXT PRIMARY KEY,
        plan_id            TEXT NOT NULL,
        request_id         TEXT NOT NULL,
        asset_id           TEXT NOT NULL REFERENCES assets(asset_id),
        state              TEXT NOT NULL,
        priority           INTEGER NOT NULL,
        attempt            INTEGER NOT NULL CHECK (attempt >= 0),
        max_attempts       INTEGER NOT NULL,
        scheduled_at       TEXT,
        started_at         TEXT,
        finished_at        TEXT,
        last_transition_at TEXT,
        last_error         TEXT,
        worker_id          TEXT,
        notes              TEXT NOT NULL DEFAULT '[]',
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_tasks_dispatch ON tasks(state, priority, scheduled_at)",
    "CREATE INDEX idx_tasks_request ON tasks(request_id)",
    "CREATE INDEX idx_tasks_plan ON tasks(plan_id)",
    # -- 本地文件 --------------------------------------------------------- #
    """
    CREATE TABLE files (
        file_id             TEXT PRIMARY KEY,
        task_id             TEXT NOT NULL REFERENCES tasks(task_id),
        dataset_id          TEXT NOT NULL,
        source_id           TEXT NOT NULL,
        access_route        TEXT NOT NULL,
        origin_uri          TEXT NOT NULL,
        local_path          TEXT NOT NULL UNIQUE,
        size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
        checksum            TEXT NOT NULL,
        content_type        TEXT,
        remote_etag         TEXT,
        remote_last_modified TEXT,
        retrieved_at        TEXT NOT NULL,
        is_read_only        INTEGER NOT NULL,
        extensions          TEXT NOT NULL DEFAULT '{}',
        created_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_files_task ON files(task_id)",
    "CREATE INDEX idx_files_dataset ON files(dataset_id)",
    # -- 审计事件 --------------------------------------------------------- #
    """
    CREATE TABLE transfer_logs (
        event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id           TEXT NOT NULL REFERENCES tasks(task_id),
        event_type        TEXT NOT NULL,
        message           TEXT NOT NULL,
        bytes_transferred INTEGER NOT NULL DEFAULT 0 CHECK (bytes_transferred >= 0),
        metadata          TEXT NOT NULL DEFAULT '{}',
        timestamp         TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_transfer_logs_task ON transfer_logs(task_id)",
    # -- 完整性报告（只追加） --------------------------------------------- #
    """
    CREATE TABLE integrity_reports (
        report_id  TEXT PRIMARY KEY,
        task_id    TEXT NOT NULL REFERENCES tasks(task_id),
        file_id    TEXT REFERENCES files(file_id),
        status     TEXT NOT NULL,
        checks     TEXT NOT NULL DEFAULT '[]',
        failures   TEXT NOT NULL DEFAULT '[]',
        expected   TEXT NOT NULL DEFAULT '{}',
        observed   TEXT NOT NULL DEFAULT '{}',
        checked_at TEXT NOT NULL,
        checker_id TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_integrity_task ON integrity_reports(task_id)",
    "CREATE INDEX idx_integrity_file ON integrity_reports(file_id)",
    "CREATE INDEX idx_integrity_status ON integrity_reports(status)",
)

_V2_STATEMENTS: Final[tuple[str, ...]] = (
    # -- 计划与计划-任务顺序 ---------------------------------------------- #
    """
    CREATE TABLE plans (
        plan_id             TEXT PRIMARY KEY,
        request_id          TEXT NOT NULL,
        policy              TEXT NOT NULL,
        unmatched_variables TEXT NOT NULL DEFAULT '[]',
        extensions          TEXT NOT NULL DEFAULT '{}',
        generated_at        TEXT NOT NULL,
        created_at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_plans_request ON plans(request_id)",
    """
    CREATE TABLE plan_tasks (
        plan_id TEXT NOT NULL REFERENCES plans(plan_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        PRIMARY KEY (plan_id, task_id),
        UNIQUE (plan_id, ordinal)
    )
    """,
    "CREATE INDEX idx_plan_tasks_order ON plan_tasks(plan_id, ordinal)",
    # -- 租约与心跳（M1 新增契约字段） ------------------------------------ #
    "ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT",
    "ALTER TABLE tasks ADD COLUMN lease_expires_at TEXT",
    "CREATE INDEX idx_tasks_lease ON tasks(state, lease_expires_at)",
)

MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=1,
        description="M1 初始 schema：9 张业务表、外键与派发/关联索引",
        statements=_V1_STATEMENTS,
    ),
    Migration(
        version=2,
        description="M1 计划持久化（plans/plan_tasks）与任务租约字段"
        "（heartbeat_at/lease_expires_at）",
        statements=_V2_STATEMENTS,
    ),
)
"""全部迁移，按版本号严格升序。新增迁移必须追加在末尾，禁止修改已发布的条目。"""

SCHEMA_VERSION: Final[int] = len(MIGRATIONS)
"""当前代码期望的 schema 版本（= 已定义迁移的个数）。"""

LATEST_SCHEMA_VERSION: Final[int] = SCHEMA_VERSION
"""别名：最新 schema 版本，供外部工具/CLI 展示时语义更清晰。"""


def latest_version() -> int:
    """返回最新迁移的版本号（对 :data:`MIGRATIONS` 做一次真实校验）。

    Returns
    -------
    int
        最新版本号。

    Raises
    ------
    ValueError
        迁移版本号不是从 1 开始连续递增，或存在重复版本。
    """
    expected = 1
    for migration in MIGRATIONS:
        if migration.version != expected:
            raise ValueError(
                f"migrations must be contiguous from 1: expected v{expected}, "
                f"found v{migration.version}"
            )
        expected += 1
    if MIGRATIONS[-1].version != SCHEMA_VERSION:  # pragma: no cover - 定义期自检
        raise ValueError("MIGRATIONS tail version does not match SCHEMA_VERSION")
    return MIGRATIONS[-1].version
