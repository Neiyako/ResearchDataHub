"""断点续传：真实 HTTP 服务器上的中断、资源变化与三重自证。

续传的硬条件（缺一不可）：

1. ``Accept-Ranges: bytes``（HEAD 明确声明）；
2. 请求 ``Range`` 后收到 ``206``，且 ``Content-Range`` 起点等于请求偏移；
3. sidecar 记录的 ETag（优先）或 Last-Modified 与现场探测一致。

这里逐个构造“只有部分条件成立”的真实场景，验证实现要么续传、要么整文件重下，
并在 ``TransferResult.resumed`` 里如实反映。
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import pytest
from tests.unit.test_transfer.helpers import (
    fast_retry,
    make_http_adapter,
    make_http_asset,
    make_task_for,
    write_sidecar_fixture,
)
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.core.models import TransferStatus
from researchdatahub.transfer import (
    PermanentTransferError,
    TransferErrorCode,
    part_path_for,
    read_sidecar,
    sidecar_path_for,
    write_sidecar,
)
from researchdatahub.transfer.staging import PartSidecar

BODY = bytes(range(256)) * 800  # 204800
OLD_DATE = format_datetime(datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC), usegmt=True)
NEW_DATE = format_datetime(datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC), usegmt=True)
TRUNCATE_AT = 100_000  # > chunk_size(64KiB)：保证至少一个 chunk 先落盘再中断


def _asset(
    server: TransferTestServer, path: str, body: bytes = BODY, etag: str | None = '"v1"'
) -> object:
    return make_http_asset(server.url(path), size_bytes=len(body), etag=etag)


def _seed_partial(
    asset: object,
    destination: Path,
    *,
    part_bytes: bytes,
    etag: str | None = '"v1"',
    last_modified: datetime | None = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC),
    expected_size: int = len(BODY),
    origin_uri: str | None = None,
) -> None:
    """手工构造“上一次留下的 .part + sidecar”。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path_for(destination).write_bytes(part_bytes)
    if origin_uri is None:
        write_sidecar_fixture(
            destination,
            asset=asset,  # type: ignore[arg-type]
            size_written=len(part_bytes),
            etag=etag,
            last_modified=last_modified,
            expected_size=expected_size,
        )
        return
    write_sidecar(
        sidecar_path_for(destination),
        PartSidecar(
            origin_uri=origin_uri,
            transport_id="https",
            task_id="task-0001",
            expected_size=expected_size,
            etag=etag,
            last_modified=last_modified,
            size_written=len(part_bytes),
            updated_at=datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC),
        ),
    )


def _interrupt_once(
    server: TransferTestServer,
    path: str,
    destination: Path,
    *,
    etag: str | None = '"v1"',
    body: bytes = BODY,
) -> tuple[object, Path, bytes]:
    """做一次必然失败（中途截断）的下载，返回 ``(asset, destination, partial_bytes)``。"""
    server.add(
        path,
        Resource(
            body=body,
            etag=etag,
            last_modified=OLD_DATE,
            truncate_after=TRUNCATE_AT,
        ),
    )
    adapter = make_http_adapter(retry=fast_retry(max_attempts=1))
    asset = _asset(server, path, body=body, etag=etag)
    result = adapter.transfer_outcome(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.FAILED
    assert result.retryable is True
    part = part_path_for(destination)
    assert part.exists()
    partial = part.read_bytes()
    assert 0 < len(partial) < len(body)
    assert sidecar_path_for(destination).exists()
    adapter.close()
    return asset, destination, partial


# --------------------------------------------------------------------------- #
# 允许续传：三重自证全部成立
# --------------------------------------------------------------------------- #
def test_interrupted_transfer_resumes_from_partial_offset(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/resume.bin"
    asset, destination, partial = _interrupt_once(server, path, tmp_path / "payload.bin")
    before = len(server.requests_for(path))

    adapter = make_http_adapter()
    task = make_task_for(asset)  # type: ignore[arg-type]
    result = adapter.transfer(asset, destination, task=task)  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is True
    assert destination.read_bytes() == BODY
    assert result.actual_size == len(BODY)
    phase2 = server.requests_for(path)[before:]
    ranged_gets = [r.range_header for r in phase2 if r.method == "GET"]
    assert ranged_gets == [f"bytes={len(partial)}-"]
    adapter.close()


def test_preexisting_part_and_sidecar_resume_when_validators_match(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/manual-resume.bin"
    server.add(path, Resource(body=BODY, etag='"v1"', last_modified=OLD_DATE))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    partial = BODY[:70000]
    _seed_partial(asset, destination, part_bytes=partial)

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is True
    assert destination.read_bytes() == BODY
    gets = [r.range_header for r in server.requests_for(path) if r.method == "GET"]
    assert gets == ["bytes=70000-"]
    adapter.close()


def test_failed_attempt_persists_sidecar_identity(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/sidecar.bin"
    _, destination, partial = _interrupt_once(server, path, tmp_path / "payload.bin")
    sidecar = read_sidecar(sidecar_path_for(destination))
    assert sidecar is not None
    assert sidecar.origin_uri == server.url(path)
    assert sidecar.transport_id == "https"
    assert sidecar.etag == '"v1"'
    assert sidecar.expected_size == len(BODY)
    assert sidecar.size_written == len(partial)


# --------------------------------------------------------------------------- #
# 拒绝续传：资源已变化（同 URL）
# --------------------------------------------------------------------------- #
def test_etag_change_forces_full_redownload(server: TransferTestServer, tmp_path: Path) -> None:
    path = "/changed-etag.bin"
    asset, destination, _partial = _interrupt_once(server, path, tmp_path / "payload.bin")
    new_body = bytes(range(256)) * 600  # 更短的新版本
    server.add(path, Resource(body=new_body, etag='"v2"', last_modified=NEW_DATE))
    before = len(server.requests_for(path))

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == new_body
    phase2 = server.requests_for(path)[before:]
    assert all(r.range_header is None for r in phase2 if r.method == "GET")
    adapter.close()


def test_size_change_with_same_etag_forces_full_redownload(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/changed-size.bin"
    asset, destination, _partial = _interrupt_once(server, path, tmp_path / "payload.bin")
    new_body = bytes(range(256)) * 500
    server.add(path, Resource(body=new_body, etag='"v1"', last_modified=OLD_DATE))
    before = len(server.requests_for(path))

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == new_body
    phase2 = server.requests_for(path)[before:]
    assert all(r.range_header is None for r in phase2 if r.method == "GET")
    adapter.close()


def test_last_modified_change_without_etag_forces_full_redownload(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/changed-lm.bin"
    asset, destination, _partial = _interrupt_once(
        server, path, tmp_path / "payload.bin", etag=None
    )
    new_body = bytes(range(256)) * 700
    server.add(path, Resource(body=new_body, etag=None, last_modified=NEW_DATE))
    before = len(server.requests_for(path))

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == new_body
    phase2 = server.requests_for(path)[before:]
    assert all(r.range_header is None for r in phase2 if r.method == "GET")
    adapter.close()


# --------------------------------------------------------------------------- #
# 拒绝续传：服务端不配合 Range / Content-Range 不合法
# --------------------------------------------------------------------------- #
def test_server_ignoring_range_causes_full_redownload(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/ignore-range.bin"
    server.add(path, Resource(body=BODY, etag='"v1"', ignore_range=True))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:60000])

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    gets = [r.range_header for r in server.requests_for(path) if r.method == "GET"]
    assert gets == ["bytes=60000-", None]  # 先尝试 Range，被 200 拒绝后整下
    adapter.close()


def test_content_range_start_mismatch_causes_full_redownload(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/bad-range.bin"
    server.add(path, Resource(body=BODY, etag='"v1"', bad_content_range_start=0))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:60000])

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    gets = [r.range_header for r in server.requests_for(path) if r.method == "GET"]
    assert gets == ["bytes=60000-", None]
    adapter.close()


def test_accept_ranges_missing_prevents_resume(server: TransferTestServer, tmp_path: Path) -> None:
    path = "/no-accept-ranges.bin"
    server.add(path, Resource(body=BODY, etag='"v1"', accepts_ranges=False))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:60000])

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]

    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    gets = [r.range_header for r in server.requests_for(path) if r.method == "GET"]
    assert gets == [None]
    adapter.close()


# --------------------------------------------------------------------------- #
# 拒绝续传：本地证据不足 / 策略要求人工介入
# --------------------------------------------------------------------------- #
def test_partial_without_sidecar_is_restarted(server: TransferTestServer, tmp_path: Path) -> None:
    path = "/no-sidecar.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path_for(destination).write_bytes(BODY[:40000])

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    assert [r.range_header for r in server.requests_for(path) if r.method == "GET"] == [None]
    adapter.close()


def test_corrupt_sidecar_is_not_trusted(server: TransferTestServer, tmp_path: Path) -> None:
    path = "/corrupt-sidecar.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path_for(destination).write_bytes(BODY[:40000])
    sidecar_path_for(destination).write_text("{not-json", encoding="utf-8")

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    assert [r.range_header for r in server.requests_for(path) if r.method == "GET"] == [None]
    adapter.close()


def test_partial_complete_unverified_is_restarted(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/complete-part.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY)  # .part 大小恰好等于远端

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    assert [r.range_header for r in server.requests_for(path) if r.method == "GET"] == [None]
    adapter.close()


def test_partial_larger_than_remote_is_restarted(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/oversize-part.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY + b"junk")

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    adapter.close()


def test_changed_origin_uri_in_sidecar_is_restarted(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/moved.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(
        asset,
        destination,
        part_bytes=BODY[:30000],
        origin_uri="https://other.example/elsewhere.bin",
    )
    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    adapter.close()


def test_unverifiable_identity_policy_manual_requires_intervention(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/no-validators.bin"
    server.add(path, Resource(body=BODY, etag=None, last_modified=None))
    asset = make_http_asset(server.url(path), size_bytes=len(BODY))
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:30000], etag=None, last_modified=None)

    adapter = make_http_adapter(unverifiable_resume_policy="manual")
    with pytest.raises(PermanentTransferError) as info:
        adapter.transfer(asset, destination, task=make_task_for(asset))
    assert info.value.error_code == str(TransferErrorCode.RESUME_IDENTITY_UNKNOWN)
    assert part_path_for(destination).exists() is True  # 保留给人工判断
    assert sidecar_path_for(destination).exists() is True
    adapter.close()


def test_unverifiable_identity_default_policy_redownloads(
    server: TransferTestServer, tmp_path: Path
) -> None:
    path = "/no-validators-restart.bin"
    server.add(path, Resource(body=BODY, etag=None, last_modified=None))
    asset = make_http_asset(server.url(path), size_bytes=len(BODY))
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:30000], etag=None, last_modified=None)

    adapter = make_http_adapter()
    result = adapter.transfer(asset, destination, task=make_task_for(asset))
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert destination.read_bytes() == BODY
    adapter.close()


def test_resume_disabled_in_config_restarts(server: TransferTestServer, tmp_path: Path) -> None:
    path = "/resume-disabled.bin"
    server.add(path, Resource(body=BODY, etag='"v1"'))
    asset = _asset(server, path)
    destination = tmp_path / "payload.bin"
    _seed_partial(asset, destination, part_bytes=BODY[:30000])

    from tests.unit.test_transfer.helpers import fast_transfer_config

    adapter = make_http_adapter(config=fast_transfer_config(resume_enabled=False))
    result = adapter.transfer(asset, destination, task=make_task_for(asset))  # type: ignore[arg-type]
    assert result.status is TransferStatus.SUCCEEDED
    assert result.resumed is False
    assert [r.range_header for r in server.requests_for(path) if r.method == "GET"] == [None]
    adapter.close()
