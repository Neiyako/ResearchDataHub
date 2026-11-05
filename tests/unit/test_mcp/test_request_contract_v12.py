"""MCP 入参对**请求契约 v1.2** 的支持（跨层接线测试）。

背景：`planner.request_v12` 定义了面向 Agent 的版本化契约，而 MCP 的 ``request`` 参数原先
只认既有的分段 YAML 形状。两者形状不同（v1.2 有 ``schema_version``/``selection``/``output``），
不接通就等于"契约写了但没人用"。本文件锁住这条接线，并锁住两件不能容忍的事：

1. ``auto`` / ``auto_discover`` **不得**被悄悄解析成某个数据集（必须先发现）；
2. 非法契约必须是结构化 ``invalid_arguments``，而不是 500 或静默忽略字段。
"""

from __future__ import annotations

from typing import Any

import pytest

from researchdatahub.interfaces.mcp.errors import MCPError
from researchdatahub.interfaces.mcp.tools import planning_tools

V12_REQUEST: dict[str, Any] = {
    "schema_version": "1.2",
    "request_id": "era5-contract-test",
    "dataset": {
        "provider": "cds",
        "dataset_id": "reanalysis-era5-pressure-levels",
        "product_family": "ERA5",
    },
    "selection": {
        "variables": [
            {"name": "cloud_liquid_water", "units": "kg m**-2", "provider_native": {"cds": "clwc"}},
            {"name": "temperature", "units": "K", "provider_native": {"cds": "t"}},
        ],
        "temporal": {"start": "2015-01-01", "end": "2024-12-31", "frequency": "hourly"},
        "spatial": {"type": "bounding_box", "bounds": [73, 18, 135, 54]},
        "vertical": {"kind": "pressure", "levels": [1000.0, 850.0, 500.0]},
    },
    "output": {"format": "netcdf", "destination": "local", "path": "./data/raw"},
}


def _load(request: dict[str, Any]) -> tuple[Any, tuple[str, ...]]:
    return planning_tools._load_request({"request": request}, tool="validate_request")


def test_contract_v12_is_detected_and_converted() -> None:
    dataset_request, warnings = _load(V12_REQUEST)
    assert dataset_request.request_id == "era5-contract-test"
    assert dataset_request.dataset_id == "reanalysis-era5-pressure-levels"
    assert dataset_request.preferred_sources == ["cds"]
    assert [spec.name for spec in dataset_request.metadata.variables] == [
        "cloud_liquid_water",
        "temperature",
    ]
    # 语义名 → 原生名的映射必须落到执行契约的扩展字段里（完整性检查要用）
    assert dataset_request.metadata.extensions["rdh.provider_native"]["temperature"] == {"cds": "t"}
    # 日期边界：end 为纯日期 → 次日 00:00 UTC（闭开区间）
    assert dataset_request.metadata.temporal_coverage.end.isoformat().startswith("2025-01-01")
    # 坐标顺序固定 W,S,E,N
    assert dataset_request.metadata.extensions["rdh.coordinate_order"] == "west,south,east,north"
    assert warnings == ()


def test_contract_v12_without_schema_version_but_with_selection() -> None:
    """只给 ``selection`` 不给 ``schema_version`` 时同样按 v1.2 处理（默认版本即当前版本）。"""
    payload = dict(V12_REQUEST)
    del payload["schema_version"]
    dataset_request, _ = _load(payload)
    assert dataset_request.dataset_id == "reanalysis-era5-pressure-levels"


def test_v12_and_legacy_shapes_are_distinguished() -> None:
    """两种形状不能互相"吞掉"：分派逻辑必须能把它们分开。"""
    assert planning_tools._is_contract_v12(V12_REQUEST) is True
    assert planning_tools._is_contract_v12({**V12_REQUEST, "schema_version": None}) is True
    legacy = {
        "request": {"name": "x"},
        "dataset": {"provider": "cds", "name": "abc"},
        "temporal": {"start": "2011-01-01", "end": "2011-12-31"},
        "spatial": {"type": "global"},
    }
    assert planning_tools._is_contract_v12(legacy) is False


def test_auto_dataset_is_rejected_with_actionable_hint() -> None:
    """``auto`` 不能被悄悄解析——必须先做数据源发现。"""
    payload = {
        **V12_REQUEST,
        "dataset": {"provider": "auto", "dataset_id": "auto_discover"},
    }
    with pytest.raises(MCPError) as excinfo:
        _load(payload)
    error = excinfo.value
    assert error.code == "invalid_arguments"
    hint = str(error.detail.get("hint", ""))
    assert "search_datasets" in hint


def test_invalid_contract_field_is_structured_error() -> None:
    """非法空间范围必须是 invalid_arguments，且带上定位信息。"""
    payload = {
        **V12_REQUEST,
        "selection": {
            **V12_REQUEST["selection"],
            "spatial": {"type": "bounding_box", "bounds": [135, 18, 73, 54]},  # 经度倒置
        },
    }
    with pytest.raises(MCPError) as excinfo:
        _load(payload)
    assert excinfo.value.code == "invalid_arguments"


def test_unknown_format_rejected_not_silently_coerced() -> None:
    payload = {**V12_REQUEST, "output": {"format": "parquet", "path": "./data/raw"}}
    with pytest.raises(MCPError) as excinfo:
        _load(payload)
    assert excinfo.value.code == "invalid_arguments"


def test_legacy_sectioned_format_still_works() -> None:
    """既有分段格式不能被 v1.2 分支抢走（向后兼容）。"""
    legacy = {
        "request": {"name": "legacy", "preferred_sources": ["cds"], "max_sources": 1},
        "dataset": {
            "provider": "cds",
            "name": "reanalysis-era5-pressure-levels",
            "product": "reanalysis",
            "product_family": "ERA5",
        },
        "variables": [{"name": "clwc", "units": "kg kg**-1"}],
        "temporal": {"start": "2011-01-01", "end": "2011-12-31", "frequency": "daily"},
        "spatial": {"type": "global"},
    }
    dataset_request, _ = _load(legacy)
    assert dataset_request.dataset_id == "reanalysis-era5-pressure-levels"
