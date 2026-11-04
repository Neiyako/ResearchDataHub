"""HTTP 适配器：真实 socket 行为测试（元数据、状态码、超时、取消、原子落盘）。

这些测试**不 mock HTTP**：每个用例都经过真实 TCP + ``http.server``，因此断言的是
真实协议行为（Content-Length、Range、连接中断、超时），而不是“mock 被调用了”。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from tests.unit.test_transfer.helpers import (
    fast_retry,
    fast_transfer_config,
    make_http_adapter,
    make_http_asset,
    make_task_for,
)
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.core.errors import (
    PermanentTransportError,
    RetryableTransportError,
)
from researchdatahub.core.models import AccessRoute, TransferStatus
from researchdatahub.transfer import (
    PermanentTransferError,
    TransferCancelledError,
    TransferErrorCode,
    TransferEventSink,
    TransferEventType,
    part_path_for,
    sidecar_path_for,
)

HTTP_DATE = format_datetime(datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC), usegmt=True)
BODY = bytes(range(256)) * 800  # 204800 字节，配合 64KiB chunk 产生 4 个分块


# --------------------------------------------------------------------------- #
# 元数据 / 能力
# --------------------------------------------------------------------------- #
def test_remote_metadata_exposes_size_etag_last_modified_and_content_type(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = b"x" * 1000
    resource = server.add(
        "/meta.bin",
        Resource(
            body=body,
            etag='"abc"',
            last_modified=HTTP_DATE,
            content_type="application/x-netcdf",
        ),
    )
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/meta.bin"), size_bytes=len(body))
    metadata = adapter.get_remote_metadata(asset)
    assert metadata.http_status == 200
    assert metadata.size_bytes == len(body)
    assert metadata.etag == '"abc"'
    assert metadata.last_modified is not None
    assert metadata.last_modified.year == 2026
    assert metadata.content_type == "application/x-netcdf"
    assert metadata.accepts_ranges is True
    assert resource.head_requests == 1
    adapter.close()


def test_probe_resume_reports_range_support_and_validators(
    server: TransferTestServer,
) -> None:
    body = b"y" * 512
    server.add("/probe.bin", Resource(body=body, etag='"e1"', last_modified=HTTP_DATE))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/probe.bin"), size_bytes=len(body))
    probe = adapter.probe_resume(asset)
    assert probe.accepts_ranges is True
    assert probe.content_length == len(body)
    assert probe.etag == '"e1"'
    assert probe.identity_known is True
    adapter.close()


def test_head_not_allowed_falls_back_to_ranged_get(server: TransferTestServer) -> None:
    body = b"z" * 321
    resource = server.add("/nohead.bin", Resource(body=body, etag='"e2"', head_supported=False))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/nohead.bin"), size_bytes=len(body))
    metadata = adapter.get_remote_metadata(asset)
    assert metadata.size_bytes == len(body)
    assert metadata.http_status in {200, 206}
    requests = server.requests_for("/nohead.bin")
    assert requests[0].method == "HEAD"
    ranged_gets = [record for record in requests if record.method == "GET"]
    assert ranged_gets[0].range_header == "bytes=0-0"
    assert resource.head_requests == 1
    adapter.close()


def test_supports_only_https_route_and_rejects_s3(
    server: TransferTestServer,
) -> None:
    adapter = make_http_adapter()
    http_asset = make_http_asset(server.url("/x.bin"), size_bytes=1)
    assert adapter.supports(http_asset) is True
    from tests.conftest import make_asset, make_source_metadata

    s3_metadata = make_source_metadata(origin_uri="s3://bucket/key.nc")
    s3_asset = make_asset(
        origin_uri="s3://bucket/key.nc",
        access_route=AccessRoute.S3,
        metadata=s3_metadata,
    )
    assert adapter.supports(s3_asset) is False
    from researchdatahub.core.errors import PermanentTransportError

    with pytest.raises(PermanentTransportError) as info:
        adapter.transfer(s3_asset, Path("/tmp/never.bin"), task=make_task_for(s3_asset))
    assert info.value.detail["error_code"] == str(TransferErrorCode.UNSUPPORTED_ASSET)
    adapter.close()


# --------------------------------------------------------------------------- #
# 成功路径：流式、原子提交、进度、事件
# --------------------------------------------------------------------------- #
def test_success_streams_to_destination_atomically(
    server: TransferTestServer, tmp_path: Path, sleep_calls: list[float]
) -> None:
    body = BODY
    server.add("/ok.bin", Resource(body=body, etag='"v1"', last_modified=HTTP_DATE))
    adapter = make_http_adapter(sleeper=sleep_calls.append)
    asset = make_http_asset(server.url("/ok.bin"), size_bytes=len(body), etag='"v1"')
    destination = tmp_path / "out.bin"
    task = make_task_for(asset)
    result = adapter.transfer(asset, destination, task=task)

    assert result.status is TransferStatus.SUCCEEDED
    assert destination.read_bytes() == body
    assert result.bytes_transferred == len(body)
    assert result.expected_size == len(body)
    assert result.actual_size == len(body)
    assert result.resumed is False
    assert result.retryable is False
    assert part_path_for(destination).exists() is False
    assert sidecar_path_for(destination).exists() is False
    assert sleep_calls == []
    adapter.close()


def test_progress_callback_is_monotonic_and_reaches_total(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = BODY
    server.add("/progress.bin", Resource(body=body, etag='"v1"'))
    updates: list[object] = []
    adapter = make_http_adapter(on_progress=updates.append)
    asset = make_http_asset(server.url("/progress.bin"), size_bytes=len(body))
    destination = tmp_path / "progress.bin"
    result = adapter.transfer(asset, destination, task=make_task_for(asset))

    assert result.status is TransferStatus.SUCCEEDED
    assert len(updates) >= 3  # 204800 / 65536 => 4 次
    written = [update.bytes_written for update in updates]  # type: ignore[attr-defined]
    assert written == sorted(written)
    assert written[-1] == len(body)
    assert updates[-1].fraction == 1.0  # type: ignore[attr-defined]
    adapter.close()


def test_events_are_structured_and_reach_optional_callback(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = b"event-body" * 100
    server.add("/events.bin", Resource(body=body, etag='"v1"'))
    delivered: list[object] = []
    sink = TransferEventSink(callback=delivered.append)
    adapter = make_http_adapter(event_sink=sink)
    asset = make_http_asset(server.url("/events.bin"), size_bytes=len(body))
    destination = tmp_path / "events.bin"
    result = adapter.transfer(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.SUCCEEDED

    events = sink.snapshot()
    types = {event.event_type for event in events}
    assert {
        str(TransferEventType.STARTED),
        str(TransferEventType.HTTP_STATUS),
        str(TransferEventType.RANGE_PROBE),
        str(TransferEventType.RESUME_DECISION),
        str(TransferEventType.BYTES_FLUSHED),
        str(TransferEventType.COMPLETED),
    } <= types
    assert all(event.task_id == "task-0001" for event in events)
    completed = [e for e in events if e.event_type == str(TransferEventType.COMPLETED)]
    assert completed[0].metadata["size_bytes"] == len(body)
    assert len(delivered) == len(events)
    adapter.close()


def test_atransfer_uses_same_semantics(server: TransferTestServer, tmp_path: Path) -> None:
    body = b"async" * 1000
    server.add("/async.bin", Resource(body=body, etag='"v1"'))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/async.bin"), size_bytes=len(body))
    destination = tmp_path / "async.bin"
    task = make_task_for(asset)
    result = asyncio.run(adapter.atransfer(asset, destination, task=task))
    assert result.status is TransferStatus.SUCCEEDED
    assert destination.read_bytes() == body
    adapter.close()


def test_close_is_idempotent_and_blocks_new_transfers(
    server: TransferTestServer, tmp_path: Path
) -> None:
    server.add("/closed.bin", Resource(body=b"abc", etag='"v1"'))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/closed.bin"), size_bytes=3)
    adapter.close()
    adapter.close()
    assert adapter.supports(asset) is False
    with pytest.raises(RuntimeError):
        adapter.transfer(asset, tmp_path / "closed.bin", task=make_task_for(asset))
    with pytest.raises(RuntimeError):
        adapter.get_remote_metadata(asset)


def test_http_exception_translation_table() -> None:
    """客户端异常 → core 传输异常的映射表（分类逻辑，不伪造 HTTP 响应）。"""
    adapter = make_http_adapter()
    try:
        cases: list[tuple[Exception, TransferErrorCode, bool]] = [
            (httpx.ConnectTimeout("t"), TransferErrorCode.TIMEOUT_CONNECT, True),
            (httpx.ReadTimeout("t"), TransferErrorCode.TIMEOUT_READ, True),
            (httpx.WriteTimeout("t"), TransferErrorCode.TIMEOUT_WRITE, True),
            (httpx.PoolTimeout("t"), TransferErrorCode.TIMEOUT_POOL, True),
            (httpx.ConnectError("c"), TransferErrorCode.CONNECTION_FAILED, True),
            (httpx.ReadError("r"), TransferErrorCode.CONNECTION_RESET, True),
            (httpx.RemoteProtocolError("p"), TransferErrorCode.PROTOCOL_ERROR, True),
            (httpx.TooManyRedirects("r"), TransferErrorCode.TOO_MANY_REDIRECTS, False),
            (httpx.UnsupportedProtocol("u"), TransferErrorCode.UNSUPPORTED_PROTOCOL, False),
            (httpx.InvalidURL("i"), TransferErrorCode.INVALID_URL, False),
        ]
        for exc, code, retryable in cases:
            translated = adapter._translate_exception(exc)
            assert translated is not None, exc
            assert translated.detail["error_code"] == str(code)
            if retryable:
                assert isinstance(translated, RetryableTransportError)
                assert translated.detail["condition"] in {"timeout", "connection_reset"}
            else:
                assert isinstance(translated, PermanentTransportError)
        assert adapter._translate_exception(ValueError("bug")) is None
    finally:
        adapter.close()


# --------------------------------------------------------------------------- #
# 目标已存在保护
# --------------------------------------------------------------------------- #
def test_existing_destination_with_matching_size_is_skipped(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = b"keep-me" * 100
    server.add("/existing.bin", Resource(body=body, etag='"v1"'))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/existing.bin"), size_bytes=len(body))
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"x" * len(body))

    result = adapter.transfer(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.SKIPPED
    assert destination.read_bytes() == b"x" * len(body)  # 原文件绝不被覆盖
    assert server.requests_for("/existing.bin") == []  # 连探测都没发
    adapter.close()


def test_existing_destination_with_different_size_is_refused(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = b"a" * 500
    server.add("/conflict.bin", Resource(body=body, etag='"v1"'))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/conflict.bin"), size_bytes=len(body))
    destination = tmp_path / "conflict.bin"
    destination.write_bytes(b"partial")
    with pytest.raises(PermanentTransferError) as info:
        adapter.transfer(asset, destination, task=make_task_for(asset))
    assert info.value.error_code == str(TransferErrorCode.DESTINATION_EXISTS)
    assert info.value.result.status is TransferStatus.FAILED
    assert destination.read_bytes() == b"partial"
    adapter.close()


# --------------------------------------------------------------------------- #
# 状态码 / 重试
# --------------------------------------------------------------------------- #
def test_404_is_permanent_and_leaves_no_staging_files(
    server: TransferTestServer, tmp_path: Path
) -> None:
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/missing.bin"), size_bytes=10)
    destination = tmp_path / "missing.bin"
    with pytest.raises(PermanentTransferError) as info:
        adapter.transfer(asset, destination, task=make_task_for(asset))
    assert info.value.error_code == str(TransferErrorCode.HTTP_NOT_FOUND)
    assert info.value.result.retryable is False
    assert part_path_for(destination).exists() is False
    assert sidecar_path_for(destination).exists() is False
    adapter.close()


def test_403_is_permanent_access_denied(server: TransferTestServer, tmp_path: Path) -> None:
    server.add("/denied.bin", Resource(body=b"secret", status_plan=[403]))
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/denied.bin"), size_bytes=6)
    with pytest.raises(PermanentTransferError) as info:
        adapter.transfer(asset, tmp_path / "denied.bin", task=make_task_for(asset))
    assert info.value.error_code == str(TransferErrorCode.HTTP_ACCESS_DENIED)
    adapter.close()


def test_500_is_retried_then_succeeds(server: TransferTestServer, tmp_path: Path) -> None:
    body = b"retry-ok" * 100
    resource = server.add("/retry.bin", Resource(body=body, etag='"v1"', status_plan=[500, 500]))
    adapter = make_http_adapter(sleeper=lambda _seconds: None)
    asset = make_http_asset(server.url("/retry.bin"), size_bytes=len(body))
    destination = tmp_path / "retry.bin"
    result = adapter.transfer(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.SUCCEEDED
    assert result.retries_used == 2
    assert destination.read_bytes() == body
    assert len(resource.get_requests) == 3
    adapter.close()


def test_500_exhausted_returns_retryable_failure_result(
    server: TransferTestServer, tmp_path: Path, sleep_calls: list[float]
) -> None:
    resource = server.add("/always500.bin", Resource(body=b"x", status_plan=[500, 500, 500, 500]))
    adapter = make_http_adapter(retry=fast_retry(max_attempts=2), sleeper=sleep_calls.append)
    asset = make_http_asset(server.url("/always500.bin"), size_bytes=1)
    result = adapter.transfer_outcome(asset, tmp_path / "always500.bin", task=make_task_for(asset))
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.HTTP_SERVER_ERROR)
    assert result.retryable is True
    assert result.retries_used == 1
    assert len(resource.get_requests) == 2
    assert len(sleep_calls) == 1
    adapter.close()


def test_429_honours_retry_after_header(
    server: TransferTestServer, tmp_path: Path, sleep_calls: list[float]
) -> None:
    body = b"rate-limited" * 10
    server.add(
        "/rate.bin",
        Resource(body=body, etag='"v1"', status_plan=[429], retry_after="0.25"),
    )
    adapter = make_http_adapter(
        retry=fast_retry(backoff_max_seconds=1.0), sleeper=sleep_calls.append
    )
    asset = make_http_asset(server.url("/rate.bin"), size_bytes=len(body))
    result = adapter.transfer(asset, tmp_path / "rate.bin", task=make_task_for(asset))
    assert result.status is TransferStatus.SUCCEEDED
    assert result.retries_used == 1
    assert sleep_calls == [pytest.approx(0.25)]
    adapter.close()


def test_connection_refused_is_retryable(server: TransferTestServer, tmp_path: Path) -> None:
    port = server.port
    server.close()  # 端口关闭后连接会被拒绝
    adapter = make_http_adapter(retry=fast_retry(max_attempts=2), sleeper=lambda _seconds: None)
    asset = make_http_asset(f"http://127.0.0.1:{port}/gone.bin", size_bytes=10)
    result = adapter.transfer_outcome(asset, tmp_path / "gone.bin", task=make_task_for(asset))
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.CONNECTION_FAILED)
    assert result.retryable is True
    assert result.retries_used == 1
    adapter.close()


def test_read_timeout_is_retryable(server: TransferTestServer, tmp_path: Path) -> None:
    body = b"slow" * 4096
    server.add(
        "/slow-read.bin",
        Resource(body=body, etag='"v1"', slow_chunk_seconds=0.5, send_chunk_bytes=1024),
    )
    config = fast_transfer_config(
        timeout_seconds=5.0, connect_timeout_seconds=1.0, read_timeout_seconds=0.1
    )
    adapter = make_http_adapter(
        config=config, retry=fast_retry(max_attempts=1, backoff_max_seconds=0.01)
    )
    asset = make_http_asset(server.url("/slow-read.bin"), size_bytes=len(body))
    result = adapter.transfer_outcome(asset, tmp_path / "slow-read.bin", task=make_task_for(asset))
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.TIMEOUT_READ)
    assert result.retryable is True
    adapter.close()


def test_read_timeout_still_active_without_total_timeout(
    server: TransferTestServer, tmp_path: Path
) -> None:
    """timeout_seconds=None 只取消“总时长上限”；read 空闲超时仍是卡死兜底。"""
    body = b"stall" * 4096
    server.add(
        "/stall.bin",
        Resource(body=body, etag='"v1"', slow_chunk_seconds=0.5, send_chunk_bytes=1024),
    )
    config = fast_transfer_config(
        timeout_seconds=None, connect_timeout_seconds=1.0, read_timeout_seconds=0.1
    )
    adapter = make_http_adapter(
        config=config, retry=fast_retry(max_attempts=1, backoff_max_seconds=0.01)
    )
    asset = make_http_asset(server.url("/stall.bin"), size_bytes=len(body))
    result = adapter.transfer_outcome(asset, tmp_path / "stall.bin", task=make_task_for(asset))
    assert config.timeout_seconds is None
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.TIMEOUT_READ)
    assert result.retryable is True
    adapter.close()


def test_total_timeout_aborts_long_transfer(server: TransferTestServer, tmp_path: Path) -> None:
    body = b"t" * 20480
    server.add(
        "/slow-total.bin",
        Resource(body=body, etag='"v1"', slow_chunk_seconds=0.1, send_chunk_bytes=1024),
    )
    config = fast_transfer_config(
        timeout_seconds=1.0, connect_timeout_seconds=0.5, read_timeout_seconds=0.5
    )
    adapter = make_http_adapter(
        config=config, retry=fast_retry(max_attempts=1, backoff_max_seconds=0.01)
    )
    asset = make_http_asset(server.url("/slow-total.bin"), size_bytes=len(body))
    result = adapter.transfer_outcome(asset, tmp_path / "slow-total.bin", task=make_task_for(asset))
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.TIMEOUT_TOTAL)
    assert result.retryable is True
    adapter.close()


# --------------------------------------------------------------------------- #
# 总超时口径：默认无总时长上限，显式设置才生效
# --------------------------------------------------------------------------- #
def _slow_large_resource(server: TransferTestServer, path: str) -> bytes:
    """40 块 × 4KiB、每块 sleep 30ms ≈ 1.2s 的慢速大文件（远超 0.3s 的小总超时）。"""
    body = b"L" * (4096 * 40)  # 163840 字节
    server.add(
        path,
        Resource(
            body=body,
            etag='"v1"',
            send_chunk_bytes=4096,
            slow_chunk_seconds=0.03,
        ),
    )
    return body


def test_default_config_has_no_total_timeout() -> None:
    from researchdatahub.config.models import TransferConfig

    assert TransferConfig().timeout_seconds is None
    assert TransferConfig(timeout_seconds=120.0).timeout_seconds == 120.0
    adapter = make_http_adapter()
    try:
        assert adapter.config.timeout_seconds is None
    finally:
        adapter.close()


def test_large_stream_not_aborted_by_total_deadline(
    server: TransferTestServer, tmp_path: Path
) -> None:
    """默认配置下慢速大文件必须下完：总时长不是失败条件。"""
    body = _slow_large_resource(server, "/slow-large.bin")
    adapter = make_http_adapter(config=fast_transfer_config())
    asset = make_http_asset(server.url("/slow-large.bin"), size_bytes=len(body))
    destination = tmp_path / "slow-large.bin"

    started = time.monotonic()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))
    elapsed = time.monotonic() - started

    assert result.status is TransferStatus.SUCCEEDED
    assert result.expected_size == len(body)
    assert result.actual_size == len(body)
    assert destination.read_bytes() == body
    # 确实慢：总耗时远超 0.3s，若存在固定总超时会必然误杀
    assert elapsed > 1.0
    adapter.close()


def test_explicit_small_total_timeout_triggers_timeout_total(
    server: TransferTestServer, tmp_path: Path
) -> None:
    """同一慢速流：显式设置小总超时才触发 TIMEOUT_TOTAL（证明两种口径都生效）。"""
    body = _slow_large_resource(server, "/slow-large-timeout.bin")
    config = fast_transfer_config(
        timeout_seconds=1.0,
        connect_timeout_seconds=0.5,
        read_timeout_seconds=0.5,
    )
    adapter = make_http_adapter(
        config=config, retry=fast_retry(max_attempts=1, backoff_max_seconds=0.01)
    )
    asset = make_http_asset(server.url("/slow-large-timeout.bin"), size_bytes=len(body))
    destination = tmp_path / "slow-large-timeout.bin"
    result = adapter.transfer_outcome(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.FAILED
    assert result.error_code == str(TransferErrorCode.TIMEOUT_TOTAL)
    assert result.retryable is True
    assert destination.exists() is False
    adapter.close()


# --------------------------------------------------------------------------- #
# 中断 / 截断：重下与原子性
# --------------------------------------------------------------------------- #
def test_truncated_body_is_retried_and_restarted_without_range(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = BODY
    resource = server.add(
        "/truncated.bin",
        Resource(
            body=body,
            etag='"v1"',
            accepts_ranges=False,
            truncate_after=50000,
        ),
    )
    adapter = make_http_adapter(sleeper=lambda _seconds: None)
    asset = make_http_asset(server.url("/truncated.bin"), size_bytes=len(body))
    destination = tmp_path / "truncated.bin"
    result = adapter.transfer(asset, destination, task=make_task_for(asset))

    assert result.status is TransferStatus.SUCCEEDED
    assert result.retries_used == 1
    assert result.resumed is False
    assert destination.read_bytes() == body
    assert len(resource.get_requests) == 2
    assert all(record.range_header is None for record in server.requests_for("/truncated.bin"))
    adapter.close()


def test_destination_only_appears_after_atomic_replace(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = BODY
    server.add(
        "/atomic.bin",
        Resource(body=body, etag='"v1"', slow_chunk_seconds=0.02, send_chunk_bytes=16384),
    )
    adapter = make_http_adapter()
    asset = make_http_asset(server.url("/atomic.bin"), size_bytes=len(body))
    destination = tmp_path / "atomic.bin"
    task = make_task_for(asset)

    thread = threading.Thread(
        target=lambda: adapter.transfer(asset, destination, task=task), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not part_path_for(destination).exists():
            time.sleep(0.01)
        assert part_path_for(destination).exists()
        assert destination.exists() is False  # 读者永远看不到半截正式文件
    finally:
        thread.join(timeout=10.0)
    assert thread.is_alive() is False
    assert destination.read_bytes() == body
    adapter.close()


def test_cancel_mid_stream_keeps_resumable_partial(
    server: TransferTestServer, tmp_path: Path
) -> None:
    body = BODY
    server.add(
        "/cancel.bin",
        Resource(body=body, etag='"v1"', slow_chunk_seconds=0.02, send_chunk_bytes=16384),
    )
    asset = make_http_asset(server.url("/cancel.bin"), size_bytes=len(body))
    destination = tmp_path / "cancel.bin"
    cancel = threading.Event()

    def on_progress(update: object) -> None:
        if update.bytes_written >= 16384:  # type: ignore[attr-defined]
            cancel.set()

    adapter = make_http_adapter(on_progress=on_progress)
    with pytest.raises(TransferCancelledError) as info:
        adapter.transfer(asset, destination, task=make_task_for(asset), cancel_event=cancel)
    assert info.value.error_code == str(TransferErrorCode.CANCELLED)
    assert info.value.result.status is TransferStatus.FAILED
    assert info.value.result.retryable is False
    part = part_path_for(destination)
    assert part.exists() is True
    assert 0 < part.stat().st_size < len(body)
    assert sidecar_path_for(destination).exists() is True
    adapter.close()

    # 取消不是失败：保留的半成品可以续传成功
    resumed_adapter = make_http_adapter()
    result = resumed_adapter.transfer(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is True
    assert destination.read_bytes() == body
    resumed_adapter.close()
