"""``DependencyChecker`` 的测试：探针注入，不依赖本机实际装了哪些包。"""

from __future__ import annotations

import pytest

from researchdatahub.environment.dependencies import DependencyChecker, default_probe
from researchdatahub.environment.models import CheckKind, DependencyRequirement, RequirementStatus


def _requirement(module: str = "cdsapi", *, required: bool = True, package: str | None = None):
    return DependencyRequirement(
        module=module,
        package=package or module,
        required=required,
        purpose="测试用途",
    )


def test_present_dependency() -> None:
    checker = DependencyChecker(probe=lambda module: True)
    result = checker.check([_requirement()])[0]
    assert result.status is RequirementStatus.PRESENT
    assert result.kind is CheckKind.DEPENDENCY
    assert result.required is True
    assert result.guidance == ""
    assert result.observed["importable"] is True


def test_missing_required_dependency_has_pip_guidance() -> None:
    checker = DependencyChecker(probe=lambda module: False)
    result = checker.check([_requirement("netCDF4")])[0]
    assert result.status is RequirementStatus.MISSING
    assert result.blocking is True
    assert "pip install 'netCDF4'" in result.guidance
    assert result.observed["importable"] is False


def test_missing_optional_dependency_is_optional_not_missing() -> None:
    checker = DependencyChecker(probe=lambda module: False)
    result = checker.check([_requirement("boto3", required=False)])[0]
    assert result.status is RequirementStatus.OPTIONAL
    assert result.blocking is False
    assert "可选" in result.guidance


def test_probe_failure_maps_to_unknown() -> None:
    checker = DependencyChecker(probe=lambda module: None)
    result = checker.check([_requirement()])[0]
    assert result.status is RequirementStatus.UNKNOWN
    assert "无法判断" in result.guidance


def test_checker_preserves_order_and_calls_probe_once_per_requirement() -> None:
    calls: list[str] = []

    def probe(module: str) -> bool:
        calls.append(module)
        return module != "xarray"

    checker = DependencyChecker(probe=probe)
    results = checker.check([_requirement("httpx"), _requirement("xarray"), _requirement("cdsapi")])
    assert [item.name for item in results] == ["httpx", "xarray", "cdsapi"]
    assert calls == ["httpx", "xarray", "cdsapi"]


def test_help_url_for_known_package() -> None:
    checker = DependencyChecker(probe=lambda module: False)
    result = checker.check([_requirement("cdsapi")])[0]
    assert result.help_url == "https://pypi.org/project/cdsapi/"


def test_default_probe_reports_installed_stdlib_and_missing_fake() -> None:
    """默认探针走真实 ``find_spec``：stdlib 必有、伪造模块名必无。"""
    assert default_probe("json") is True
    assert default_probe("definitely_not_a_real_module_xyz") is False


def test_default_probe_does_not_import_module() -> None:
    """探针不得触发第三方模块的 import 副作用（例如 cdsapi 会读配置/连网）。"""
    import sys

    before = set(sys.modules)
    default_probe("boto3")
    added = set(sys.modules) - before
    assert "boto3" not in added, "probe must not import the module"


def test_empty_requirements_returns_empty_list() -> None:
    assert DependencyChecker(probe=lambda module: True).check([]) == []


@pytest.mark.parametrize("module", ["ee", "netCDF4", "googleapiclient.discovery"])
def test_probe_handles_dotted_and_case_sensitive_names(module: str) -> None:
    """探针按名查询（大小写敏感）；这里只验证不抛异常。"""
    assert default_probe(module) in (True, False)
