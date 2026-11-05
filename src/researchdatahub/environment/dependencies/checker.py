"""Python 依赖检查：只看「模块能否被 import」，不 import 模块本身。

为什么用 ``importlib.util.find_spec`` 而不是 ``import``
------------------------------------------------------
* ``import cdsapi`` / ``import ee`` 会执行第三方初始化（读配置、连网、起线程），
  「检查环境」本身不应该有副作用；
* ``find_spec`` 只查 ``sys.path`` 元数据，毫秒级、可重复、可离线；
* 代价是它**不保证**模块真的能 import（例如 ABI 不匹配的 C 扩展）。这一取舍写在
  ``notes`` 里：``status=present`` 的含义是「已安装」，不是「已验证可用」；
  真正的可用性由 :func:`researchdatahub.environment.validate_environment` 之后的首个真实请求确认。

测试注入
--------
``probe`` 参数允许注入 ``module -> bool | None`` 的映射，使测试完全不依赖本机装了哪些包。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from typing import Final

from researchdatahub.environment.models import (
    CheckKind,
    DependencyRequirement,
    RequirementResult,
    RequirementStatus,
)

__all__ = [
    "DependencyChecker",
    "default_probe",
]

#: 模块探针：返回 ``True``（可导入）/ ``False``（不可导入）/ ``None``（探测本身出错）。
DependencyProbe = Callable[[str], "bool | None"]


def default_probe(module: str) -> bool | None:
    """用 :func:`importlib.util.find_spec` 判断模块是否可解析。

    Parameters
    ----------
    module:
        模块名（顶层包或点分路径），如 ``"netCDF4"`` / ``"googleapiclient.discovery"``。

    Returns
    -------
    bool | None
        ``True`` 表示能在 ``sys.path`` 上找到；``False`` 表示找不到；
        ``None`` 表示探测过程抛了异常（例如父包已损坏），调用方应标记为 ``unknown``。

    Notes
    -----
    对「父包存在但子模块缺失」的情况，``find_spec`` 会抛
    :class:`ModuleNotFoundError`，这里捕获并返回 ``False``。
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # ImportError 的常见子类：ModuleNotFoundError（父包不存在）。
        # ValueError：模块名带 __path__ 异常时的少数边界（例如 ``__main__.__spec__`` 为 None）。
        return False
    except Exception:
        return None


class DependencyChecker:
    """检查一批 :class:`DependencyRequirement` 是否已安装。

    Parameters
    ----------
    probe:
        模块探针；默认 :func:`default_probe`。测试注入固定映射以获得确定性结果。

    Examples
    --------
    >>> checker = DependencyChecker(probe={"httpx": True, "cdsapi": False}.__getitem__)
    >>> results = checker.check([DependencyRequirement(
    ...     module="httpx", package="httpx", purpose="传输层")])
    >>> results[0].status.value
    'present'
    """

    def __init__(self, probe: Callable[[str], bool | None] | None = None) -> None:
        self._probe: Callable[[str], bool | None] = default_probe if probe is None else probe

    def check(self, requirements: list[DependencyRequirement]) -> list[RequirementResult]:
        """逐个检查依赖，返回与输入同序的结果列表。

        Parameters
        ----------
        requirements:
            依赖要求列表（通常来自 ``ProviderRequirements.dependencies``）。

        Returns
        -------
        list[RequirementResult]
            ``status`` 为 ``present`` / ``missing``（必需缺） / ``optional``（可选缺） /
            ``unknown``（探针异常）。``guidance`` 给出可直接执行的 pip 命令。
        """
        return [self._check_one(item) for item in requirements]

    def _check_one(self, requirement: DependencyRequirement) -> RequirementResult:
        present = self._probe(requirement.module)
        if present is None:
            status = RequirementStatus.UNKNOWN
            guidance = (
                f"无法判断 {requirement.module} 是否可导入（父包可能已损坏）。"
                f"请执行：.venv/bin/pip install --force-reinstall {requirement.package}"
            )
        elif present:
            status = RequirementStatus.PRESENT
            guidance = ""
        elif requirement.required:
            status = RequirementStatus.MISSING
            guidance = (
                f"缺少 Python 包 {requirement.package}（导入名 {requirement.module}），"
                f"它是必需的：{requirement.purpose}。修复："
                f".venv/bin/pip install '{requirement.package}'，或安装项目全部依赖 "
                ".venv/bin/pip install -e '.[cds,sci]'"
            )
        else:
            status = RequirementStatus.OPTIONAL
            guidance = (
                f"{requirement.package} 未安装（可选）：{requirement.purpose}。"
                f"需要该功能时执行 .venv/bin/pip install '{requirement.package}'"
            )
        return RequirementResult(
            name=requirement.module,
            kind=CheckKind.DEPENDENCY,
            status=status,
            required=requirement.required,
            guidance=guidance,
            help_url=_PACKAGE_HELP.get(requirement.package),
            observed={
                "package": requirement.package,
                "importable": bool(present) if present is not None else None,
            },
        )


_PACKAGE_HELP: Final[Mapping[str, str]] = {
    "cdsapi": "https://pypi.org/project/cdsapi/",
    "xarray": "https://docs.xarray.dev/",
    "netCDF4": "https://unidata.github.io/netcdf4-python/",
    "httpx": "https://www.python-httpx.org/",
    "boto3": "https://boto3.amazonaws.com/v1/documentation/api/latest/index.html",
    "earthengine-api": "https://pypi.org/project/earthengine-api/",
    "keyring": "https://pypi.org/project/keyring/",
}
"""包名 → 官方文档 URL（写进 ``RequirementResult.help_url``，供 CLI 直接展示）。"""
