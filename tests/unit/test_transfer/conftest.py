"""传输单测的 fixtures：每个测试一个独立的真实 HTTP 服务器。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.unit.test_transfer.http_server import TransferTestServer

__all__ = ["server", "sleep_calls"]


@pytest.fixture
def server() -> Iterator[TransferTestServer]:
    """起一个 ``127.0.0.1:0`` 的真实 HTTP 服务器，测试结束关闭（含线程）。"""
    instance = TransferTestServer()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def sleep_calls() -> list[float]:
    """注入给适配器的 sleeper 记录器：记录每次重试等待的秒数。"""
    return []
