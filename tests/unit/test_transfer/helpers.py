"""传输测试的共享构造器：真实 pydantic 对象 + 可控配置，不使用 Mock 伪造数据。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import make_asset, make_source_metadata, make_task

from researchdatahub.config.models import TransferConfig
from researchdatahub.core.models import AccessRoute, AcquisitionTask, SourceAsset
from researchdatahub.transfer import HttpTransportAdapter, RetryPolicy
from researchdatahub.transfer.retry import DEFAULT_RETRY_CONDITIONS

__all__ = [
    "fast_retry",
    "fast_transfer_config",
    "make_http_adapter",
    "make_http_asset",
    "make_task_for",
    "write_sidecar_fixture",
]


def make_http_asset(
    uri: str,
    *,
    size_bytes: int,
    etag: str | None = None,
    last_modified: datetime | None = None,
    file_name: str = "payload.bin",
) -> SourceAsset:
    """构造一个 ``https`` 访问路径的资产，指向本地真实 HTTP 服务器。"""
    metadata = make_source_metadata(origin_uri=uri, access_route=AccessRoute.HTTPS)
    return make_asset(
        asset_id="asset-http-0001",
        origin_uri=uri,
        access_route=AccessRoute.HTTPS,
        size_bytes=size_bytes,
        checksum=None,
        remote_etag=etag,
        remote_last_modified=last_modified,
        metadata=metadata,
        file_name=file_name,
    )


def fast_transfer_config(**overrides: object) -> TransferConfig:
    """测试用传输配置：小 chunk（多分块）+ 明确超时，避免真实长等待。"""
    payload: dict[str, object] = {
        # 默认与生产一致：不设“单次尝试总时长上限”，只靠 connect/read 兜底卡死。
        # 需要验证总超时行为的用例显式传 timeout_seconds=<非 None>。
        "timeout_seconds": None,
        "connect_timeout_seconds": 2.0,
        "read_timeout_seconds": 5.0,
        "chunk_size_bytes": 65536,
        "max_redirects": 5,
        "verify_tls": True,
        "resume_enabled": True,
    }
    payload.update(overrides)
    return TransferConfig(**payload)  # type: ignore[arg-type]


def fast_retry(**overrides: object) -> RetryPolicy:
    """测试用重试策略：毫秒级退避 + 无抖动，断言可预期的次数与延迟。"""
    payload: dict[str, object] = {
        "max_attempts": 3,
        "backoff_seconds": 0.001,
        "backoff_max_seconds": 0.05,
        "jitter": 0.0,
        "retry_on": sorted(DEFAULT_RETRY_CONDITIONS),
    }
    payload.update(overrides)
    return RetryPolicy(**payload)  # type: ignore[arg-type]


def make_http_adapter(
    *,
    config: TransferConfig | None = None,
    retry: RetryPolicy | None = None,
    **kwargs: object,
) -> HttpTransportAdapter:
    """构造一个使用测试默认参数的 HTTP 适配器（直连，忽略系统代理）。"""
    return HttpTransportAdapter(
        config=config or fast_transfer_config(),
        retry=retry or fast_retry(),
        trust_env=False,
        **kwargs,  # type: ignore[arg-type]
    )


def make_task_for(asset: SourceAsset) -> AcquisitionTask:
    """构造属于该资产的任务。"""
    return make_task(asset=asset)


def write_sidecar_fixture(
    destination: Path,
    *,
    asset: SourceAsset,
    size_written: int,
    etag: str | None = None,
    last_modified: datetime | None = None,
    expected_size: int | None = None,
    transport_id: str = "https",
) -> None:
    """手工写一份 sidecar（模拟“上一次进程留下的半成品”）。"""
    from researchdatahub.transfer.staging import PartSidecar, sidecar_path_for, write_sidecar

    sidecar = PartSidecar(
        origin_uri=asset.origin_uri,
        transport_id=transport_id,
        task_id="task-0001",
        expected_size=expected_size if expected_size is not None else asset.size_bytes,
        etag=etag,
        last_modified=last_modified,
        size_written=size_written,
        updated_at=datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_sidecar(sidecar_path_for(destination), sidecar)


def ranges_sent(records: Sequence[object]) -> list[str | None]:
    """从请求记录中提取 ``Range`` 头（None 表示未发 Range）。"""
    result: list[str | None] = []
    for record in records:
        result.append(getattr(record, "range_header", None))
    return result
