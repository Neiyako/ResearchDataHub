"""``NetworkChecker`` 的测试：**全程离线**，绝不发真实外网请求，绝不调用 cdsapi。

两种注入方式都被覆盖
--------------------
1. :class:`httpx.MockTransport`：覆盖状态码 → 结论的映射、超时、连接失败、重定向；
2. 本地 :class:`http.server`（``local_http_server`` 夹具）：覆盖真实 socket/HTTP 路径。
"""

from __future__ import annotations

import httpx
import pytest

from researchdatahub.environment.models import CheckKind, EndpointRequirement, RequirementStatus
from researchdatahub.environment.network import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    NetworkChecker,
)


def _endpoint(url: str = "https://reachable.test/", *, required: bool = True, method: str = "HEAD"):
    return EndpointRequirement(
        name="demo",
        url=url,
        method="HEAD" if method == "HEAD" else "GET",
        required=required,
        purpose="测试",
    )


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_timeout_bounds() -> None:
    assert 3.0 <= DEFAULT_TIMEOUT_SECONDS <= 8.0
    assert MIN_TIMEOUT_SECONDS < DEFAULT_TIMEOUT_SECONDS < MAX_TIMEOUT_SECONDS
    with pytest.raises(ValueError, match="timeout must be within"):
        NetworkChecker(timeout=0.0)
    with pytest.raises(ValueError, match="timeout must be within"):
        NetworkChecker(timeout=MAX_TIMEOUT_SECONDS + 1)


@pytest.mark.parametrize("status_code", [200, 204, 301, 302])
def test_reachable_on_success(status_code: int) -> None:
    with _client(lambda request: httpx.Response(status_code)) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.PRESENT
    assert result.kind is CheckKind.NETWORK
    assert result.observed["status_code"] == status_code
    assert isinstance(result.observed["latency_ms"], float)


@pytest.mark.parametrize("status_code", [401, 403, 404, 405])
def test_auth_and_not_found_still_count_as_reachable(status_code: int) -> None:
    """端点活着就是活着：认证/不存在属于凭据或路径问题，不是网络故障。"""
    with _client(lambda request: httpx.Response(status_code)) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.PRESENT
    assert result.guidance == ""


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_server_errors_are_missing(status_code: int) -> None:
    with _client(lambda request: httpx.Response(status_code)) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.MISSING
    assert result.blocking is True
    assert str(status_code) in result.guidance


def test_optional_endpoint_failure_is_optional() -> None:
    with _client(lambda request: httpx.Response(503)) as client:
        result = NetworkChecker(client=client).check_one(_endpoint(required=False))
    assert result.status is RequirementStatus.OPTIONAL
    assert result.observed["degraded"] is True


def test_timeout_is_reported_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with _client(handler) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.MISSING
    assert result.observed["error"] == "Timeout"
    assert "无响应" in result.guidance


def test_connect_error_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail", request=request)

    with _client(handler) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.MISSING
    assert result.observed["error"] == "ConnectError"
    assert "curl -I" in result.guidance


def test_unexpected_exception_does_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("weird transport")

    with _client(handler) as client:
        result = NetworkChecker(client=client).check_one(_endpoint())
    assert result.status is RequirementStatus.MISSING
    assert str(result.observed["error"]).startswith("UnexpectedError")


def test_head_request_is_used_by_default() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200)

    with _client(handler) as client:
        NetworkChecker(client=client).check_one(_endpoint())
    assert seen == ["HEAD"]


def test_get_request_streams_first_byte_only() -> None:
    seen: list[str] = []
    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        headers.append(request.headers.get("range"))
        return httpx.Response(200, content=b"x" * 10)

    with _client(handler) as client:
        result = NetworkChecker(client=client).check_one(_endpoint(method="GET"))
    assert seen == ["GET"]
    assert headers == ["bytes=0-0"]
    assert result.status is RequirementStatus.PRESENT


def test_client_is_never_asked_for_credentials() -> None:
    """探测不得携带任何认证头（哪怕环境里配了 CDSAPI_KEY）。"""
    captured: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200)

    with _client(handler) as client:
        NetworkChecker(client=client).check_one(_endpoint())
    assert "authorization" not in {key.lower() for key in captured[0]}


def test_check_batch_preserves_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if "ok" in str(request.url) else 503)

    endpoints = [
        _endpoint("https://ok.test/"),
        _endpoint("https://bad.test/"),
        _endpoint("https://ok.test/again"),
    ]
    with _client(handler) as client:
        results = NetworkChecker(client=client).check(endpoints)
    assert [item.status for item in results] == [
        RequirementStatus.PRESENT,
        RequirementStatus.MISSING,
        RequirementStatus.PRESENT,
    ]


def test_follow_redirects_via_mock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://reachable.test/final"})
        return httpx.Response(200)

    with _client(handler) as client:
        result = NetworkChecker(client=client, follow_redirects=True).check_one(_endpoint())
    assert result.status is RequirementStatus.PRESENT
    assert result.observed["status_code"] == 200


def test_latency_uses_injected_clock() -> None:
    ticks = iter([0.0, 0.012])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with _client(handler) as client:
        checker = NetworkChecker(client=client, clock=lambda: next(ticks))
        result = checker.check_one(_endpoint())
    assert result.observed["latency_ms"] == 12.0


def test_check_url_convenience() -> None:
    with _client(lambda request: httpx.Response(200)) as client:
        result = NetworkChecker(client=client).check_url("https://reachable.test/", name="tmp")
    assert result.name == "tmp"
    assert result.status is RequirementStatus.PRESENT


# --------------------------------------------------------------------------- #
# 本地 http.server（真实 socket 路径）
# --------------------------------------------------------------------------- #
def test_local_http_server_ok(local_http_server: str) -> None:
    """真实 HTTP 往返：验证 Client 生命周期、HEAD 与 HTTP 解析都正常。"""
    endpoint = EndpointRequirement(
        name="local", url=f"{local_http_server}/ok", method="HEAD", purpose="本地回环"
    )
    with httpx.Client(timeout=5.0) as client:
        result = NetworkChecker(client=client).check_one(endpoint)
    assert result.status is RequirementStatus.PRESENT
    assert result.observed["status_code"] == 200
    assert isinstance(result.observed["latency_ms"], float)


def test_local_http_server_500(local_http_server: str) -> None:
    endpoint = EndpointRequirement(
        name="local", url=f"{local_http_server}/boom", method="HEAD", purpose="本地回环"
    )
    with httpx.Client(timeout=5.0) as client:
        result = NetworkChecker(client=client).check_one(endpoint)
    assert result.status is RequirementStatus.MISSING
    assert result.observed["status_code"] == 503
    assert "服务端故障" in result.guidance


def test_local_http_server_get(local_http_server: str) -> None:
    endpoint = EndpointRequirement(
        name="local", url=f"{local_http_server}/ok", method="GET", purpose="本地回环"
    )
    with httpx.Client(timeout=5.0) as client:
        result = NetworkChecker(client=client).check_one(endpoint)
    assert result.status is RequirementStatus.PRESENT


def test_default_client_path_uses_no_real_network(no_network: None, local_http_server: str) -> None:
    """不注入 client 时也只会走 MockTransport（``no_network`` 夹具），证明默认套件不联网。"""
    checker = NetworkChecker(timeout=1.0)
    result = checker.check_url(f"{local_http_server}/ok", name="blocked")
    # 真实客户端被 MockTransport 替换成「拒绝一切请求」→ 报 ConnectError 风格失败
    assert result.status is RequirementStatus.MISSING
    assert result.observed["error"] is not None
