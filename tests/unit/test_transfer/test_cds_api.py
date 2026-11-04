"""CDS API 传输适配器：路由判别 + 复用 HTTP 语义的真实 socket 测试。

不访问外网：所有请求打到 ``127.0.0.1`` 上的真实 ``http.server``（见 ``http_server.py``），
走真实 TCP 语义（HEAD/GET、Content-Length、Range、连接中断）。

CDS 真正的 ``submit → poll → results`` 在 ``sources/cds.py``，不在传输层；这里只验证
“拿到临时签名 URL 之后”的传输行为，以及接线约束：``cds_api`` 资产必须由
:class:`CdsApiTransport` 接管，普通 ``HttpTransportAdapter`` 不得抢走它。
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.conftest import make_asset, make_source_metadata
from tests.unit.test_transfer.helpers import (
    fast_retry,
    fast_transfer_config,
    make_http_adapter,
    make_http_asset,
    make_task_for,
    write_sidecar_fixture,
)
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.cli._services import CliContext, build_transports, close_transports
from researchdatahub.config.models import AppConfig
from researchdatahub.core.errors import PermanentTransportError, ScheduleError
from researchdatahub.core.models import AccessRoute, SourceAsset, TransferStatus
from researchdatahub.scheduler.transport_pool import TransportPool
from researchdatahub.transfer import (
    CdsApiTransport,
    PermanentTransferError,
    TransferErrorCode,
    part_path_for,
    sidecar_path_for,
)

if TYPE_CHECKING:
    from researchdatahub.manifest.cli_service import DbTarget

HTTP_DATE = format_datetime(datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC), usegmt=True)
BODY = bytes(range(256)) * 800  # 204800 字节，配合 64KiB chunk 产生 4 个分块


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def make_cds_asset(
    uri: str,
    *,
    size_bytes: int,
    source_id: str = "cds",
    request_id: str = "cds-request-0001",
    file_name: str = "cds-result.nc",
) -> SourceAsset:
    """构造一个 ``cds_api`` 资产：``origin_uri`` 是 CDS 结果临时签名 URL。

    ``metadata.extensions`` 带上真实链路里的 ``cds.request_id`` / ``cds.request``，
    这样测试构造的资产与 ``CDSAdapter._asset_from_result()`` 的形状一致。
    """
    metadata = make_source_metadata(
        source_id=source_id,
        display_name="Copernicus CDS",
        provider="Copernicus CDS",
        access_route=AccessRoute.CDS_API,
        origin_uri=uri,
        extensions={
            "cds.request_id": request_id,
            "cds.request": {"collection_id": "reanalysis-era5-pressure-levels"},
        },
    )
    return make_asset(
        asset_id="asset-cds-0001",
        source_id=source_id,
        origin_uri=uri,
        access_route=AccessRoute.CDS_API,
        size_bytes=size_bytes,
        metadata=metadata,
        file_name=file_name,
    )


def make_cds_adapter(**kwargs: object) -> CdsApiTransport:
    """构造指向本地真实 HTTP 服务器的 CDS 传输适配器（直连，忽略系统代理）。"""
    kwargs.setdefault("config", fast_transfer_config())
    kwargs.setdefault("retry", fast_retry())
    return CdsApiTransport(trust_env=False, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 支持性 / 拒绝条件
# --------------------------------------------------------------------------- #
def test_transport_id_is_cds_api_and_only_accepts_cds_route(
    server: TransferTestServer,
) -> None:
    adapter = make_cds_adapter()
    try:
        assert adapter.transport_id == "cds_api"
        assert adapter.supports(make_cds_asset(server.url("/result.nc"), size_bytes=1)) is True
        # 普通 https 资产归 HttpTransportAdapter，本类不抢
        assert adapter.supports(make_http_asset(server.url("/plain.bin"), size_bytes=1)) is False
        s3_asset = make_asset(
            origin_uri="s3://bucket/key.nc",
            access_route=AccessRoute.S3,
            metadata=make_source_metadata(origin_uri="s3://bucket/key.nc"),
        )
        assert adapter.supports(s3_asset) is False
        # 路由是 cds_api 但 origin_uri 不是 http(s)：没有 HTTP 语义可复用，明确拒绝
        assert adapter.supports(make_cds_asset("s3://bucket/result.nc", size_bytes=1)) is False
        assert (
            adapter.supports(make_cds_asset("ftp://cdn.example/result.nc", size_bytes=1)) is False
        )
    finally:
        adapter.close()
    # close 之后不再声明支持任何资产（幂等关闭的继承语义）
    assert adapter.supports(make_cds_asset(server.url("/result.nc"), size_bytes=1)) is False


def test_http_adapter_does_not_take_over_cds_api_asset(
    server: TransferTestServer, tmp_path: Path
) -> None:
    cds_asset = make_cds_asset(server.url("/result.nc"), size_bytes=10)
    http_adapter = make_http_adapter()
    try:
        assert http_adapter.supports(cds_asset) is False
        with pytest.raises(PermanentTransportError) as info:
            http_adapter.transfer(cds_asset, tmp_path / "result.nc", task=make_task_for(cds_asset))
        assert info.value.detail["error_code"] == str(TransferErrorCode.UNSUPPORTED_ASSET)
        assert info.value.detail["transport_id"] == "https"
        assert "does not support" in info.value.message
        # 支持性检查在发请求之前：远端一个字节都不该被请求
        assert server.requests_for("/result.nc") == []
    finally:
        http_adapter.close()


def test_transport_pool_selects_cds_api_adapter(server: TransferTestServer) -> None:
    http_adapter = make_http_adapter()
    cds_adapter = make_cds_adapter()
    try:
        pool = TransportPool([http_adapter, cds_adapter])
        assert pool.transport_ids == ("https", "cds_api")
        cds_asset = make_cds_asset(server.url("/result.nc"), size_bytes=1)
        https_asset = make_http_asset(server.url("/plain.bin"), size_bytes=1)
        assert pool.select(cds_asset) is cds_adapter
        assert pool.select(https_asset) is http_adapter
    finally:
        pool.close()


def test_pool_without_cds_transport_reproduces_the_original_failure(
    server: TransferTestServer,
) -> None:
    """回归守护：只装配 HTTP+S3 时，CDS 资产必须报明确的 "no transport" 而不是静默失败。"""
    http_adapter = make_http_adapter()
    try:
        pool = TransportPool([http_adapter])
        with pytest.raises(ScheduleError) as info:
            pool.select(make_cds_asset(server.url("/result.nc"), size_bytes=1))
        assert "no transport adapter supports access_route='cds_api'" in info.value.message
        assert info.value.detail["transports"] == ["https"]
    finally:
        pool.close()


def test_build_transports_wires_cds_adapter_and_close_is_idempotent() -> None:
    context = CliContext(
        app_config=AppConfig(),
        sources_config=None,
        storage_root=Path("/tmp/rdh-test"),
        db_target=cast("DbTarget", object()),
    )
    transports = build_transports(context)
    try:
        assert [transport.transport_id for transport in transports] == ["https", "s3", "cds_api"]
        assert isinstance(transports[-1], CdsApiTransport)
        pool = TransportPool(transports)
        assert isinstance(
            pool.select(make_cds_asset("https://example.invalid/result.nc", size_bytes=1)),
            CdsApiTransport,
        )
    finally:
        close_transports(transports)
    # close 幂等：再关一次不抛异常（CLI 的异常清理路径可能重复调用）
    close_transports(transports)


# --------------------------------------------------------------------------- #
# 成功下载：真实流式 + 原子提交 + 签名 URL 原样保留
# --------------------------------------------------------------------------- #
def test_transfer_downloads_signed_url_bytes_unchanged(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = BODY
    server.add("/api/retrieve/result.nc", Resource(body=body, etag='"v1"', last_modified=HTTP_DATE))
    adapter = make_cds_adapter()
    try:
        # 真实 CDS 结果 URL 带签名查询串；它必须被原样请求、原样记录为 source_uri
        signed_url = server.url("/api/retrieve/result.nc") + "?token=temp-signed"
        asset = make_cds_asset(signed_url, size_bytes=len(body))
        destination = tmp_path / "result.nc"
        result = adapter.transfer(asset, destination, task=make_task_for(asset))

        assert result.status is TransferStatus.SUCCEEDED
        assert result.transport_id == "cds_api"
        assert result.source_uri == signed_url
        assert result.expected_size == len(body)
        assert result.actual_size == len(body)
        assert destination.read_bytes() == body
        assert part_path_for(destination).exists() is False
        assert sidecar_path_for(destination).exists() is False
        # 服务器按去查询串的 path 路由；HEAD 探测 + 一次 GET 全量
        requests = server.requests_for("/api/retrieve/result.nc")
        assert [record.method for record in requests] == ["HEAD", "GET"]
    finally:
        adapter.close()


def test_resume_reuses_partial_with_cds_sidecar(server: TransferTestServer, tmp_path: Path) -> None:
    """继承自 HTTP 的断点续传在 ``cds_api`` 路由下同样生效（sidecar 记录 cds_api）。"""
    body = BODY
    server.add("/resume.nc", Resource(body=body, etag='"v4"', last_modified=HTTP_DATE))
    adapter = make_cds_adapter()
    try:
        asset = make_cds_asset(server.url("/resume.nc"), size_bytes=len(body))
        destination = tmp_path / "resume.nc"
        part = part_path_for(destination)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(body[:50000])
        write_sidecar_fixture(
            destination,
            asset=asset,
            size_written=50000,
            etag='"v4"',
            last_modified=datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC),
            transport_id="cds_api",
        )

        result = adapter.transfer(asset, destination, task=make_task_for(asset))
        assert result.status is TransferStatus.SUCCEEDED
        assert result.resumed is True
        assert destination.read_bytes() == body
        gets = [r for r in server.requests_for("/resume.nc") if r.method == "GET"]
        assert gets[0].range_header == "bytes=50000-"
    finally:
        adapter.close()


# --------------------------------------------------------------------------- #
# 失败分类：404 / 405 / 签名过期 403 / 断流重试
# --------------------------------------------------------------------------- #
def test_404_is_permanent_not_found_and_leaves_no_staging(
    server: TransferTestServer, tmp_path: Path
) -> None:
    adapter = make_cds_adapter()
    try:
        asset = make_cds_asset(server.url("/gone-result.nc"), size_bytes=10)
        destination = tmp_path / "gone-result.nc"
        with pytest.raises(PermanentTransferError) as info:
            adapter.transfer(asset, destination, task=make_task_for(asset))
        assert info.value.error_code == str(TransferErrorCode.HTTP_NOT_FOUND)
        assert info.value.result.status is TransferStatus.FAILED
        assert info.value.result.retryable is False
        assert part_path_for(destination).exists() is False
        assert sidecar_path_for(destination).exists() is False
    finally:
        adapter.close()


def test_head_405_falls_back_to_ranged_get_then_downloads(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = b"cds-head-fallback" * 100
    server.add(
        "/nohead-result.nc",
        Resource(body=body, etag='"v2"', head_supported=False),
    )
    adapter = make_cds_adapter()
    try:
        asset = make_cds_asset(server.url("/nohead-result.nc"), size_bytes=len(body))
        metadata = adapter.get_remote_metadata(asset)
        assert metadata.size_bytes == len(body)
        assert metadata.http_status in {200, 206}
        requests = server.requests_for("/nohead-result.nc")
        assert requests[0].method == "HEAD"
        ranged_gets = [record for record in requests if record.method == "GET"]
        assert ranged_gets[0].range_header == "bytes=0-0"

        destination = tmp_path / "nohead-result.nc"
        result = adapter.transfer(asset, destination, task=make_task_for(asset))
        assert result.status is TransferStatus.SUCCEEDED
        assert destination.read_bytes() == body
    finally:
        adapter.close()


def test_get_405_is_permanent_client_error(server: TransferTestServer, tmp_path: Path) -> None:
    """HEAD 可用但 GET 被 405 拒绝：不可重试的客户端错误，只尝试一次。"""
    resource = server.add("/method-not-allowed.nc", Resource(body=b"x" * 10, status_plan=[405]))
    adapter = make_cds_adapter(retry=fast_retry(max_attempts=1))
    try:
        asset = make_cds_asset(server.url("/method-not-allowed.nc"), size_bytes=10)
        with pytest.raises(PermanentTransferError) as info:
            adapter.transfer(asset, tmp_path / "method-not-allowed.nc", task=make_task_for(asset))
        assert info.value.error_code == str(TransferErrorCode.HTTP_CLIENT_ERROR)
        assert info.value.result.retryable is False
        assert len(resource.get_requests) == 1
    finally:
        adapter.close()


def test_expired_signed_url_403_is_permanent_access_denied(
    server: TransferTestServer, tmp_path: Path
) -> None:
    """临时签名 URL 过期的真实表现：403 → 不可重试；传输层不做自动 re-resolve。"""
    resource = server.add("/expired-result.nc", Resource(body=b"secret", status_plan=[403]))
    adapter = make_cds_adapter()
    try:
        asset = make_cds_asset(server.url("/expired-result.nc"), size_bytes=6)
        with pytest.raises(PermanentTransferError) as info:
            adapter.transfer(asset, tmp_path / "expired-result.nc", task=make_task_for(asset))
        assert info.value.error_code == str(TransferErrorCode.HTTP_ACCESS_DENIED)
        assert info.value.result.retryable is False
        # 不自动重签：本适配器只发了这一次 GET，恢复只能靠重新 plan 拿到新 URL
        assert len(resource.get_requests) == 1
    finally:
        adapter.close()


def test_truncated_stream_is_retried_and_completed(
    server: TransferTestServer, tmp_path: Path
) -> None:
    """连接中断（声明完整 Content-Length 却提前断开）沿用 HTTP 适配器的重试语义。"""
    body = BODY
    resource = server.add(
        "/truncated-result.nc",
        Resource(body=body, etag='"v3"', accepts_ranges=False, truncate_after=50000),
    )
    adapter = make_cds_adapter(sleeper=lambda _seconds: None)
    try:
        asset = make_cds_asset(server.url("/truncated-result.nc"), size_bytes=len(body))
        destination = tmp_path / "truncated-result.nc"
        result = adapter.transfer(asset, destination, task=make_task_for(asset))
        assert result.status is TransferStatus.SUCCEEDED
        assert result.retries_used == 1
        assert result.resumed is False
        assert destination.read_bytes() == body
        assert len(resource.get_requests) == 2
    finally:
        adapter.close()
