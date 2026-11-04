"""planner 单测的共享装配：真实 YAML 文本 + 协议形状的测试数据源（不 mock 核心语义）。

这里的数据源适配器只实现 ``core.ports.DataSourceAdapter`` 的形状（发现资产、产出
``SourceAsset``），不包含任何下载/网络逻辑——计划器本来就是"只发现不下载"。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from researchdatahub.core.models import (
    AccessRoute,
    DatasetRequest,
    SourceAsset,
    SourceMetadata,
    TemporalExtent,
)
from researchdatahub.planner.request import LoadedRequest, load_request_text_detailed

FIXED_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_REQUEST = REPO_ROOT / "examples" / "era5-request.yaml"

BASE_DOCUMENT: dict[str, object] = {
    "request": {
        "name": "era5_cloud_water_2011",
        "priority": "normal",
        "preferred_sources": ["cds", "ncar"],
        "max_sources": 1,
    },
    "dataset": {
        "provider": "cds",
        "name": "reanalysis-era5-pressure-levels",
        "product": "reanalysis",
        "product_family": "ERA5",
        "version": "unknown",
    },
    "variables": [
        {"name": "clwc", "units": "kg kg**-1"},
        {"name": "t", "units": "K"},
    ],
    "temporal": {"start": "2011-01-01", "end": "2011-12-31", "frequency": "hourly"},
    "spatial": {"type": "global"},
    "vertical": {"levels": [1000, 925, 850], "kind": "pressure"},
    "storage": {"preserve_original": True},
    "validation": {"verify_size": True, "calculate_sha256": True},
}


def document(**overrides: object) -> dict[str, object]:
    """构造分段格式的 YAML 文档（浅覆盖顶层段）。"""
    doc = copy.deepcopy(BASE_DOCUMENT)
    doc.update(overrides)
    return doc


def yaml_text(doc: Mapping[str, object] | None = None) -> str:
    """把文档序列化为 YAML 文本（键顺序稳定，便于断言行号）。"""
    return yaml.safe_dump(dict(doc or BASE_DOCUMENT), sort_keys=False, allow_unicode=True)


def load_doc(doc: Mapping[str, object] | None = None) -> LoadedRequest:
    """用真实 loader 解析分段格式文档。"""
    return load_request_text_detailed(yaml_text(doc), source="<test>.yaml")


def load_default_request() -> DatasetRequest:
    """默认分段需求（2011 全年、hourly、clwc+t）。"""
    return load_doc().request


def make_source_metadata(
    *,
    source_id: str,
    origin_uri: str,
    route: AccessRoute = AccessRoute.HTTPS,
    extensions: Mapping[str, object] | None = None,
) -> SourceMetadata:
    """构造数据源侧元数据。"""
    return SourceMetadata(
        source_id=source_id,
        display_name=f"{source_id} display",
        provider=f"{source_id} provider",
        access_route=route,
        dataset_id="reanalysis-era5-pressure-levels",
        origin_uri=origin_uri,
        retrieved_at=FIXED_NOW,
        extensions=dict(extensions or {}),  # type: ignore[arg-type]
    )


def make_asset(
    *,
    source_id: str = "ncar",
    file_name: str = "e5.1-pressure-2011.nc",
    route: AccessRoute = AccessRoute.HTTPS,
    size_bytes: int = 4096,
    time_slice: TemporalExtent | None = None,
    variables: Sequence[str] | None = None,
    checksum: str | None = None,
    asset_id: str | None = None,
) -> SourceAsset:
    """构造一个远端资产（``variables`` 写进命名空间化扩展，用于覆盖判定）。"""
    origin = f"https://data.example.invalid/{source_id}/{file_name}"
    extensions: dict[str, object] = {}
    if variables is not None:
        extensions["test.variables"] = list(variables)
    metadata = make_source_metadata(
        source_id=source_id, origin_uri=origin, route=route, extensions=extensions
    )
    return SourceAsset(
        asset_id=asset_id or f"asset-{source_id}-{file_name}".lower().replace(".", "-")[:63],
        source_id=source_id,
        dataset_id="reanalysis-era5-pressure-levels",
        access_route=route,
        origin_uri=origin,
        file_name=file_name,
        size_bytes=size_bytes,
        checksum=None if checksum is None else {"value": checksum},
        metadata=metadata,
        discovered_at=FIXED_NOW,
        time_slice=time_slice,
    )


class FakeAdapter:
    """符合 ``DataSourceAdapter`` 协议形状的测试数据源。"""

    def __init__(
        self,
        source_id: str,
        *,
        assets: Sequence[SourceAsset] = (),
        error: Exception | None = None,
        split_granularity: str | None = None,
        calls: list[DatasetRequest] | None = None,
    ) -> None:
        self._source_id = source_id
        self._assets = tuple(assets)
        self._error = error
        self.split_granularity = split_granularity
        self.calls = calls if calls is not None else []

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return frozenset({"https", "cds_api"})

    def describe(self) -> SourceMetadata:
        if self._assets:
            return self._assets[0].metadata
        return make_source_metadata(
            source_id=self._source_id,
            origin_uri=f"https://data.example.invalid/{self._source_id}",
        )

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return self._assets

    def health_check(self) -> bool:
        return self._error is None
