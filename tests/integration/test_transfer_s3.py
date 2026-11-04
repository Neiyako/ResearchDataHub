"""真实 S3/MinIO 集成测试：**没有环境就明确 NOT RUN（skipped），不允许 mock 冒充**。

需要的环境变量（缺任何一个 → 整个模块 ``skipped``，报告里显示 NOT RUN）：

* ``RDH_TEST_S3_ENDPOINT``：S3 兼容端点，例如 ``http://127.0.0.1:9000``（MinIO）；
* ``RDH_TEST_S3_BUCKET``：测试用桶名（不存在时测试会创建）；
* ``RDH_TEST_S3_REGION``（可选，默认 ``us-east-1``）；
* 标准凭证 ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``（MinIO 默认 minioadmin）。

运行方式::

    RDH_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \\
    RDH_TEST_S3_BUCKET=rdh-test \\
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
    PYTHONPATH=src .venv/bin/python -m pytest tests/integration/test_transfer_s3.py -q -m network
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from tests.conftest import make_asset, make_source_metadata, make_task

from researchdatahub.config.models import S3TransferConfig, TransferConfig
from researchdatahub.core.models import AccessRoute, TransferStatus
from researchdatahub.transfer import (
    PermanentTransferError,
    S3TransportAdapter,
    etag_md5_digest,
    is_multipart_etag,
    parse_s3_uri,
)

ENDPOINT = os.environ.get("RDH_TEST_S3_ENDPOINT")
BUCKET = os.environ.get("RDH_TEST_S3_BUCKET")
REGION = os.environ.get("RDH_TEST_S3_REGION", "us-east-1")

_ENV_READY = bool(ENDPOINT and BUCKET)

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not _ENV_READY,
        reason=(
            "real S3/MinIO not configured (set RDH_TEST_S3_ENDPOINT + RDH_TEST_S3_BUCKET); "
            "S3 end-to-end is NOT RUN, not mocked"
        ),
    ),
]


def _client() -> Any:
    """构造指向测试端点的 boto3 客户端。"""
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )


def _adapter(*, verify_etag_as_md5: bool = False) -> S3TransportAdapter:
    """构造指向测试端点的 S3 适配器。"""
    assert ENDPOINT is not None
    s3_config = S3TransferConfig(
        endpoint_url=ENDPOINT,
        region_name=REGION,
        use_ssl=ENDPOINT.startswith("https://"),
        verify_etag_as_md5=verify_etag_as_md5,
    )
    return S3TransportAdapter(config=TransferConfig(s3=s3_config))


def _make_asset(uri: str, size_bytes: int, etag: str, modified: datetime) -> Any:
    """构造指向真实桶对象的资产。"""
    metadata = make_source_metadata(origin_uri=uri, access_route="s3")
    return make_asset(
        asset_id="asset-s3-it-0001",
        origin_uri=uri,
        access_route=AccessRoute.S3,
        size_bytes=size_bytes,
        checksum=None,
        remote_etag=etag,
        remote_last_modified=modified,
        metadata=metadata,
    )


@pytest.fixture(scope="module")
def bucket_client() -> Any:
    """确保桶存在（已存在则复用；对象使用唯一 key，不破坏桶内既有数据）。"""
    assert ENDPOINT is not None
    assert BUCKET is not None
    client = _client()
    try:
        client.head_bucket(Bucket=BUCKET)
    except Exception:
        client.create_bucket(Bucket=BUCKET)
    return client


def test_s3_roundtrip_metadata_and_streaming(bucket_client: Any, tmp_path: Path) -> None:
    """真实 MinIO/S3：head_object 元数据、get_object 流式下载、大小自证。"""
    key = "rdh-it/roundtrip.bin"
    body = bytes(range(256)) * 1024  # 256 KiB
    bucket_client.put_object(
        Bucket=BUCKET, Key=key, Body=body, ContentType="application/octet-stream"
    )
    head = bucket_client.head_object(Bucket=BUCKET, Key=key)
    uri = f"s3://{BUCKET}/{key}"
    asset = _make_asset(uri, len(body), str(head["ETag"]), datetime.now(UTC))

    adapter = _adapter(verify_etag_as_md5=True)
    try:
        metadata = adapter.get_remote_metadata(asset)
        assert metadata.size_bytes == len(body)
        assert metadata.etag == str(head["ETag"])
        destination = tmp_path / "roundtrip.bin"
        result = adapter.transfer(asset, destination, task=make_task(asset=asset))
        assert result.status is TransferStatus.SUCCEEDED
        assert destination.read_bytes() == body
        assert result.expected_size == len(body)
        assert result.actual_size == len(body)
        assert parse_s3_uri(uri) == (BUCKET, key)
    finally:
        adapter.close()


def test_s3_missing_key_is_permanent(bucket_client: Any, tmp_path: Path) -> None:
    """真实 S3：不存在的 key → 不可重试（NoSuchKey/404）。"""
    del bucket_client
    uri = f"s3://{BUCKET}/rdh-it/missing-{datetime.now(UTC).timestamp()}.bin"
    asset = _make_asset(uri, 1, '"0"', datetime.now(UTC))
    adapter = _adapter()
    try:
        with pytest.raises(PermanentTransferError, match="S3"):
            adapter.transfer(asset, tmp_path / "missing.bin", task=make_task(asset=asset))
    finally:
        adapter.close()


def test_s3_etag_discipline_helpers_are_consistent() -> None:
    """纯函数在集成模块里再确认一次：multipart ETag 绝不当 MD5。"""
    assert is_multipart_etag('"d41d8cd98f00b204e9800998ecf8427e-2"') is True
    assert etag_md5_digest('"d41d8cd98f00b204e9800998ecf8427e-2"') is None
    assert etag_md5_digest('"d41d8cd98f00b204e9800998ecf8427e"') == (
        "d41d8cd98f00b204e9800998ecf8427e"
    )
