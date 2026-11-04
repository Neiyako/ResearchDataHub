"""本地存储：临时区 / 原始区 / 隔离区 / 归档区的原子移动与只读保护。

公开 API
--------
* :class:`~researchdatahub.storage.local.LocalStorageBackend`：实现
  :class:`~researchdatahub.core.ports.StorageBackend`，目录布局完全由
  :class:`~researchdatahub.config.models.StorageConfig` 决定。
* :class:`~researchdatahub.storage.local.PublishResult` /
  :class:`~researchdatahub.storage.local.PublishStatus`：区分「真正提交」与「内容相同跳过」。
* 提交日志（``<file>.commit.json``）与崩溃恢复：
  :class:`~researchdatahub.storage.local.CommitJournal`、
  :class:`~researchdatahub.storage.local.CommitRecovery`、
  :class:`~researchdatahub.storage.local.OrphanScan`。
* 路径安全规则：:func:`~researchdatahub.storage.naming.sanitize_file_name`、
  :func:`~researchdatahub.storage.naming.validate_component`。

设计文档
--------
原子提交、跨文件系统（EXDEV）策略、提交日志格式与恢复决策表都写在
``storage/local.py`` 的模块 docstring 与 ``LocalStorageBackend`` 各方法 docstring 中。
"""

from researchdatahub.storage.local import (
    COMMIT_JOURNAL_SUFFIX,
    CommitJournal,
    CommitRecovery,
    CommitStatus,
    LocalStorageBackend,
    OrphanScan,
    PublishResult,
    PublishStatus,
    RecoveryAction,
)
from researchdatahub.storage.naming import (
    MAX_FILE_NAME_LENGTH,
    ensure_within,
    sanitize_file_name,
    sha256_file,
    validate_component,
)

__all__ = [
    "COMMIT_JOURNAL_SUFFIX",
    "MAX_FILE_NAME_LENGTH",
    "CommitJournal",
    "CommitRecovery",
    "CommitStatus",
    "LocalStorageBackend",
    "OrphanScan",
    "PublishResult",
    "PublishStatus",
    "RecoveryAction",
    "ensure_within",
    "sanitize_file_name",
    "sha256_file",
    "validate_component",
]
