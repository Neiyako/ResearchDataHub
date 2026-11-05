"""磁盘检查子包：目标目录可写性 + 剩余空间（只读诊断，不创建目录）。

公开入口
--------
* :class:`~researchdatahub.environment.storage.checker.DiskSpaceChecker`
* :data:`~researchdatahub.environment.storage.checker.DEFAULT_FREE_SPACE_FLOOR_GB`
"""

from researchdatahub.environment.storage.checker import (
    DEFAULT_FREE_SPACE_FLOOR_GB,
    STORAGE_REQUIREMENT_NAME,
    DiskSpaceChecker,
)

__all__ = [
    "DEFAULT_FREE_SPACE_FLOOR_GB",
    "STORAGE_REQUIREMENT_NAME",
    "DiskSpaceChecker",
]
