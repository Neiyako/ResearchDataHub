"""S3 适配器：无网络环境下的协议逻辑测试 + 真实环境的明确 NOT RUN。

**没有真实 S3/MinIO 时不做「假装通过」的测试**：这里只测不依赖网络的部分——
URI 解析、ETag/multipart 判定、ClientError 分类、head_object 响应解析、适配器装配。
真正端到端（真实取对象、断点续传、403）在 ``tests/integration/test_transfer_s3.py``，
需要配置 ``RDH_TEST_S3_ENDPOINT`` / ``RDH_TEST_S3_BUCKET`` 才会运行，否则 pytest 报
``skipped``（报告里会写明 NOT RUN，而不是 PASS）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from tests.unit.test_transfer.helpers import make_http_asset, make_task_for

from researchdatahub.config.models import S3TransferConfig, TransferConfig
from researchdatahub.core.errors import PermanentTransportError, RetryableTransportError
from researchdatahub.transfer import (
    S3TransportAdapter,
    TransferErrorCode,
    classify_client_error,
    etag_md5_digest,
    is_multipart_etag,
    metadata_from_head,
    parse_s3_uri,
)

SINGLE_PART_ETAG = '"' + "a" * 32 + '"'
MULTIPART_ETAG = '"' + "b" * 32 + '-3"'


def make_client_error(code: str, status: int) -> ClientError:
    """构造一个真实的 botocore ``ClientError``（数据对象，不是 HTTP mock）。"""
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation_name="HeadObject",
    )


# --------------------------------------------------------------------------- #
# URI / ETag 纯函数
# --------------------------------------------------------------------------- #
def test_parse_s3_uri() -> None:
    assert parse_s3_uri("s3://rda-data/ds633.0/file.nc") == ("rda-data", "ds633.0/file.nc")
    for bad in ("https://bucket/key", "s3://bucket", "s3://", "s3:///key"):
        with pytest.raises(PermanentTransportError) as info:
            parse_s3_uri(bad)
        assert info.value.detail["error_code"] == str(TransferErrorCode.INVALID_URL)


def test_is_multipart_etag() -> None:
    assert is_multipart_etag(MULTIPART_ETAG) is True
    assert is_multipart_etag(SINGLE_PART_ETAG) is False
    assert is_multipart_etag(None) is False
    assert is_multipart_etag('"abc-1"') is True


def test_etag_md5_digest_only_for_single_part_hex() -> None:
    assert etag_md5_digest(SINGLE_PART_ETAG) == "a" * 32
    # 分段 ETag 永远不是文件 MD5
    assert etag_md5_digest(MULTIPART_ETAG) is None
    # 非 32 位 hex / 空 / 缺失 一律不比较
    assert etag_md5_digest('"not-a-digest"') is None
    assert etag_md5_digest("") is None
    assert etag_md5_digest(None) is None


# --------------------------------------------------------------------------- #
# ClientError 分类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "status", "retryable", "error_code", "condition"),
    [
        ("AccessDenied", 403, False, TransferErrorCode.S3_ACCESS_DENIED, None),
        ("NoSuchKey", 404, False, TransferErrorCode.S3_NOT_FOUND, None),
        ("NoSuchBucket", 404, False, TransferErrorCode.S3_NOT_FOUND, None),
        ("SlowDown", 503, True, TransferErrorCode.S3_THROTTLED, "http_429"),
        ("Throttling", 400, True, TransferErrorCode.S3_THROTTLED, "http_429"),
        ("InternalError", 500, True, TransferErrorCode.S3_SERVER_ERROR, "http_5xx"),
        ("RequestTimeout", 400, True, TransferErrorCode.S3_SERVER_ERROR, "http_5xx"),
        ("InvalidRequest", 400, False, TransferErrorCode.S3_CLIENT_ERROR, None),
    ],
)
def test_classify_client_error(
    code: str,
    status: int,
    retryable: bool,
    error_code: TransferErrorCode,
    condition: str | None,
) -> None:
    classified_retryable, classified_code, classified_condition = classify_client_error(
        make_client_error(code, status)
    )
    assert classified_retryable is retryable
    assert classified_code is error_code
    assert classified_condition == condition


def test_classify_client_error_marks_5xx_retryable_even_without_code() -> None:
    retryable, code, condition = classify_client_error(make_client_error("Unknown", 500))
    assert retryable is True
    assert code is TransferErrorCode.S3_SERVER_ERROR
    assert condition == "http_5xx"


# --------------------------------------------------------------------------- #
# head_object 解析
# --------------------------------------------------------------------------- #
def test_metadata_from_head_maps_all_fields() -> None:
    modified = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    metadata = metadata_from_head(
        origin_uri="s3://bucket/key.nc",
        transport_id="s3",
        head={
            "ContentLength": 1234,
            "ETag": SINGLE_PART_ETAG,
            "LastModified": modified,
            "ContentType": "application/x-netcdf",
        },
    )
    assert metadata.size_bytes == 1234
    assert metadata.etag == SINGLE_PART_ETAG
    assert metadata.last_modified == modified
    assert metadata.content_type == "application/x-netcdf"
    assert metadata.accepts_ranges is True  # S3 规范保证支持 Range


def test_metadata_from_head_requires_content_length() -> None:
    with pytest.raises(PermanentTransportError) as info:
        metadata_from_head(origin_uri="s3://bucket/key", transport_id="s3", head={})
    assert info.value.detail["error_code"] == str(TransferErrorCode.S3_MALFORMED_RESPONSE)


# --------------------------------------------------------------------------- #
# 适配器装配（不发起任何网络请求）
# --------------------------------------------------------------------------- #
@pytest.fixture
def adapter() -> S3TransportAdapter:
    config = TransferConfig(
        s3=S3TransferConfig(region_name="us-east-1", anonymous=True, verify_etag_as_md5=False)
    )
    instance = S3TransportAdapter(config=config, retry=None)
    try:
        yield instance
    finally:
        instance.close()


def test_supports_only_s3_route(adapter: S3TransportAdapter) -> None:
    from tests.conftest import make_asset, make_source_metadata

    s3_metadata = make_source_metadata(origin_uri="s3://bucket/key.nc", access_route="s3")
    s3_asset = make_asset(origin_uri="s3://bucket/key.nc", access_route="s3", metadata=s3_metadata)
    assert adapter.supports(s3_asset) is True
    http_asset = make_http_asset("https://example.test/f.bin", size_bytes=1)
    assert adapter.supports(http_asset) is False
    with pytest.raises(PermanentTransportError) as info:
        adapter.transfer(http_asset, Path("/tmp/none.bin"), task=make_task_for(http_asset))
    assert info.value.detail["error_code"] == str(TransferErrorCode.UNSUPPORTED_ASSET)


def test_close_is_idempotent_and_blocks_new_calls(adapter: S3TransportAdapter) -> None:
    adapter.close()
    adapter.close()
    from tests.conftest import make_asset, make_source_metadata

    metadata = make_source_metadata(origin_uri="s3://bucket/key.nc", access_route="s3")
    asset = make_asset(origin_uri="s3://bucket/key.nc", access_route="s3", metadata=metadata)
    with pytest.raises(RuntimeError):
        adapter.get_remote_metadata(asset)


def test_stream_chunk_size_respects_config_and_part_limit() -> None:
    default_adapter = S3TransportAdapter(
        config=TransferConfig(s3=S3TransferConfig(region_name="us-east-1", anonymous=True))
    )
    try:
        # 默认 64MB part 上限 > 1MiB 流式块：以更保守的 1MiB 为准。
        assert default_adapter._stream_chunk_size == 1_048_576
    finally:
        default_adapter.close()

    capped = S3TransportAdapter(
        config=TransferConfig(
            chunk_size_bytes=33_554_432,  # 32MiB（配置上限）
            s3=S3TransferConfig(
                region_name="us-east-1",
                anonymous=True,
                part_size_mb=5,
            ),
        )
    )
    try:
        assert capped._stream_chunk_size == 5 * 1024 * 1024
    finally:
        capped.close()


def test_etag_is_never_used_as_md5_by_default(adapter: S3TransportAdapter) -> None:
    # 默认 verify_etag_as_md5=False：即使 ETag 看起来像 MD5 也不比较。
    assert adapter.s3_config.verify_etag_as_md5 is False
    assert adapter._expected_md5(SINGLE_PART_ETAG, offset=0) is None
    assert adapter._expected_md5(MULTIPART_ETAG, offset=0) is None


def test_etag_md5_comparison_only_when_opted_in() -> None:
    config = TransferConfig(
        s3=S3TransferConfig(region_name="us-east-1", anonymous=True, verify_etag_as_md5=True)
    )
    instance = S3TransportAdapter(config=config)
    try:
        assert instance._expected_md5(SINGLE_PART_ETAG, offset=0) == "a" * 32
        # multipart ETag 永远不当 MD5
        assert instance._expected_md5(MULTIPART_ETAG, offset=0) is None
        # 续传只读到后半段，算不出全文 MD5，因此不比较
        assert instance._expected_md5(SINGLE_PART_ETAG, offset=100) is None
    finally:
        instance.close()


def test_s3_botocore_errors_are_translated(adapter: S3TransportAdapter) -> None:
    denied = adapter._translate_exception(make_client_error("AccessDenied", 403))
    assert isinstance(denied, PermanentTransportError)
    assert denied.detail["error_code"] == str(TransferErrorCode.S3_ACCESS_DENIED)
    throttled = adapter._translate_exception(make_client_error("SlowDown", 503))
    assert isinstance(throttled, RetryableTransportError)
    assert throttled.detail["error_code"] == str(TransferErrorCode.S3_THROTTLED)
    assert throttled.detail["condition"] == "http_429"
    assert adapter._translate_exception(ValueError("not a botocore error")) is None
