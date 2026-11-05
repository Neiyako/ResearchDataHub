"""磁盘检查：目标目录**能否写**、**还剩多少空间**。

为什么不用 ``shutil.disk_usage`` 单打独斗
----------------------------------------
``shutil.disk_usage`` 只回答「空间够不够」，不回答「这个进程能不能写」。macOS/企业策略下
目录存在且空间充足、但 ``EACCES``/``EPERM`` 的情况很常见。因此两条一起测：

1. **写权限**：在目标目录里创建并**立即删除**一个临时文件（``tempfile``，带随机后缀）。
   不写数据、不碰已有文件；任何异常 → 不可写 + 原因。
2. **剩余空间**：``shutil.disk_usage``，剩余字节数与 ``free_space_floor_gb`` 比较。

目录不存在时**不自动创建**：创建目录属于初始化动作（``rdh db init``），
环境检查只做只读诊断；报告里会说明「目录不存在，首次运行时创建」。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

from researchdatahub.environment.models import (
    CheckKind,
    RequirementResult,
    RequirementStatus,
)

__all__ = [
    "DEFAULT_FREE_SPACE_FLOOR_GB",
    "STORAGE_REQUIREMENT_NAME",
    "DiskSpaceChecker",
]

DEFAULT_FREE_SPACE_FLOOR_GB: Final[float] = 20.0
"""与 ``config.models.StorageConfig.free_space_floor_gb`` 的默认值保持一致（20 GiB）。"""

STORAGE_REQUIREMENT_NAME: Final[str] = "storage-root"

_BYTES_PER_GB: Final[int] = 1024**3


def _expand(path_text: str) -> Path:
    """展开 ``~`` 与环境变量，但**不**创建目录。"""
    return Path(os.path.expandvars(path_text)).expanduser()


class DiskSpaceChecker:
    """检查一个存储目录的可写性与剩余空间。

    Parameters
    ----------
    root:
        目标目录（``~/.rdh``）。只接受字符串或 :class:`pathlib.Path`。
    free_space_floor_gb:
        剩余空间下限（GiB）；默认 20.0，与配置默认值一致。
    probe_write:
        是否真的做写入探测；``False`` 时只查存在性与空间（用于无副作用的只读检查）。

    Examples
    --------
    >>> checker = DiskSpaceChecker(tmp_path)          # doctest: +SKIP
    >>> result = checker.check()                       # doctest: +SKIP
    >>> result.status.value                            # doctest: +SKIP
    'present'
    """

    def __init__(
        self,
        root: str | Path,
        *,
        free_space_floor_gb: float = DEFAULT_FREE_SPACE_FLOOR_GB,
        probe_write: bool = True,
    ) -> None:
        if free_space_floor_gb < 0:
            raise ValueError("free_space_floor_gb must be >= 0")
        self._root_text = str(root)
        self._root = _expand(self._root_text)
        self._floor_gb = free_space_floor_gb
        self._probe_write = probe_write

    @property
    def root(self) -> Path:
        """展开后的目标目录（只读属性，不创建）。"""
        return self._root

    def check(self) -> RequirementResult:
        """返回存储目录的检查结果。

        Returns
        -------
        RequirementResult
            ``name`` 为 :data:`STORAGE_REQUIREMENT_NAME`；``observed`` 含
            ``path`` / ``exists`` / ``writable`` / ``free_gb`` / ``free_space_floor_gb``。
            存储是**必需**项：目录不可写 → ``missing``。
        """
        observed: dict[str, str | int | float | bool | None] = {
            "path": str(self._root),
            "free_space_floor_gb": self._floor_gb,
        }
        try:
            exists = self._root.exists()
            is_dir = self._root.is_dir() if exists else False
        except OSError as exc:
            return self._result(
                RequirementStatus.MISSING,
                guidance=f"无法访问存储目录 {self._root}：{exc}。请检查路径与权限。",
                observed={**observed, "exists": False, "error": type(exc).__name__},
            )
        observed["exists"] = exists
        observed["is_dir"] = is_dir
        if not exists:
            return self._result(
                RequirementStatus.MISSING,
                guidance=(
                    f"存储目录 {self._root} 不存在。首次使用前创建它："
                    f"mkdir -p {self._root} && chmod 700 {self._root}；"
                    "或在配置里把 storage.root 指向已有目录。"
                ),
                observed=observed,
            )
        if not is_dir:
            return self._result(
                RequirementStatus.MISSING,
                guidance=f"{self._root} 存在但不是目录；请把 storage.root 指向一个目录。",
                observed=observed,
            )

        writable, write_problem = self._check_writable()
        observed["writable"] = writable
        if not writable:
            return self._result(
                RequirementStatus.MISSING,
                guidance=(
                    f"存储目录 {self._root} 不可写（{write_problem}）。"
                    f"修复：chown -R $(id -u) {self._root} 或 chmod u+rwx {self._root}；"
                    "注意不要在系统目录（/usr、/System）下建数据区。"
                ),
                observed=observed,
            )

        try:
            usage = shutil.disk_usage(self._root)
        except OSError as exc:
            return self._result(
                RequirementStatus.UNKNOWN,
                guidance=(
                    f"无法读取 {self._root} 所在分区的剩余空间（{exc}）；"
                    "空间是否足够需要你手动用 df -h 复核。"
                ),
                observed=observed,
            )
        free_gb = round(usage.free / _BYTES_PER_GB, 2)
        total_gb = round(usage.total / _BYTES_PER_GB, 2)
        observed.update(
            {
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_gb": round(usage.used / _BYTES_PER_GB, 2),
            }
        )
        if free_gb < self._floor_gb:
            return self._result(
                RequirementStatus.MISSING,
                guidance=(
                    f"{self._root} 剩余空间 {free_gb} GiB，低于下限 {self._floor_gb} GiB。"
                    f"清理旧数据或把 storage.root 换到大盘（df -h {self._root} 查看）。"
                ),
                observed=observed,
            )
        return self._result(
            RequirementStatus.PRESENT,
            guidance="",
            observed=observed,
        )

    # -- 内部 -------------------------------------------------------------- #
    def _check_writable(self) -> tuple[bool, str]:
        """实际写一个临时文件再删除；返回 ``(可写, 原因)``。"""
        if not self._probe_write:
            return os.access(self._root, os.W_OK), "未做写入探测（probe_write=False）"
        try:
            descriptor, name = tempfile.mkstemp(prefix=".rdh-env-probe-", dir=str(self._root))
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc.strerror or exc}"
        try:
            os.write(descriptor, b"rdh-env-probe\n")
        except OSError as exc:  # 磁盘满也会走到这里
            os.close(descriptor)
            _safe_unlink(Path(name))
            return False, f"{type(exc).__name__}: {exc.strerror or exc}"
        else:
            os.close(descriptor)
        try:
            Path(name).unlink()
        except OSError:
            _safe_unlink(Path(name))
        return True, ""

    def _result(
        self,
        status: RequirementStatus,
        *,
        guidance: str,
        observed: dict[str, str | int | float | bool | None],
    ) -> RequirementResult:
        return RequirementResult(
            name=STORAGE_REQUIREMENT_NAME,
            kind=CheckKind.STORAGE,
            status=status,
            required=True,
            guidance=guidance,
            help_url=None,
            observed=observed,
        )


def _safe_unlink(path: Path) -> None:
    """尽力删除临时文件；删除失败不掩盖原始错误。"""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
