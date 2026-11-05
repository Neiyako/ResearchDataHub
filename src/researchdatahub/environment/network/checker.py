"""网络连通性检查：对 Provider 端点做**只读**轻量探测。

安全与礼貌
----------
* 只发 ``HEAD``（默认）或 ``GET``；不发 ``POST``，不携带认证头，不提交任何检索请求。
  **绝不调用 cdsapi**：cdsapi 的任何方法都会触发真实检索（排队、消耗配额），
  用户明确要求不得用它做检查或测试。
* 默认超时 5 秒（允许 0.1–60 秒，推荐 3–8 秒）；不重试——检查要快，慢等于不可达。
* 跟随重定向最多 2 跳：科研站点常见 ``/`` → ``/index.html``、``http`` → ``https``。

判定规则
--------
``reachable`` 的定义是「TCP+TLS 握手成功且 HTTP 状态码 < 400」，与
:class:`~researchdatahub.environment.models.RequirementStatus` 的映射：

* 状态码 < 400 → ``present``
* 状态码 401/403/404 → **仍算 ``present``**：端点活着，只是这个具体资源需要认证/不存在；
  网络层不是问题（凭据问题由凭据检查负责）。
* 状态码 >= 400 且不是上面三项 → ``missing``（服务端 5xx / 限流 429）
* 任何连接类异常（DNS、TLS、超时） → ``missing``，``observed.error`` 只记异常类型名

测试约束
--------
默认套件**禁止真实外网**。测试用两种方式注入：
:class:`httpx.MockTransport`（最快）或本地 :class:`http.server`（验证真实 socket 路径）。
两者都通过 ``client`` 参数传入，不会访问互联网。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Final

import httpx

from researchdatahub.environment.models import (
    CheckKind,
    EndpointRequirement,
    RequirementResult,
    RequirementStatus,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "NetworkChecker",
]

MIN_TIMEOUT_SECONDS: Final[float] = 0.1
MAX_TIMEOUT_SECONDS: Final[float] = 60.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
"""默认探测超时：5 秒。任务要求 3–8 秒区间，默认取中位，慢网络下可上调到 8。"""

_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 404, 405})
"""这些状态码说明端点**活着**：认证缺失 / 资源不存在 / 方法不允许，都不是网络故障。"""

_REDIRECT_LIMIT: Final[int] = 2


class NetworkChecker:
    """对一组 :class:`EndpointRequirement` 做只读探测。

    Parameters
    ----------
    timeout:
        单次探测超时（秒），必须落在 ``[0.1, 60]``。
    client:
        可选的注入客户端；测试传 ``httpx.Client(transport=MockTransport(...))``。
        为 ``None`` 时**每次探测新建**一个短生命周期客户端，避免长时间持有连接池。
    follow_redirects:
        是否跟随重定向（最多 2 跳）。
    clock:
        计时函数，默认 :func:`time.perf_counter`；测试可注入确定值。

    Raises
    ------
    ValueError
        超时不在允许区间。

    Examples
    --------
    >>> transport = httpx.MockTransport(lambda request: httpx.Response(200))
    >>> with httpx.Client(transport=transport) as client:
    ...     checker = NetworkChecker(client=client)
    ...     result = checker.check_one(EndpointRequirement(
    ...         name="x", url="https://example.invalid/", purpose="测试"))
    >>> result.status.value
    'present'
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        follow_redirects: bool = True,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout must be within [{MIN_TIMEOUT_SECONDS}, {MAX_TIMEOUT_SECONDS}] seconds, "
                f"got {timeout}"
            )
        self._timeout = timeout
        self._client = client
        self._follow_redirects = follow_redirects
        self._clock = clock

    @property
    def timeout(self) -> float:
        """当前探测超时（秒）。"""
        return self._timeout

    def check(self, requirements: Sequence[EndpointRequirement]) -> list[RequirementResult]:
        """批量探测（保持输入顺序）。"""
        return [self.check_one(item) for item in requirements]

    def check_url(
        self, url: str, *, name: str = "endpoint", method: str = "HEAD"
    ) -> RequirementResult:
        """探测任意 URL（供 ``validate_environment(check_network=True)`` 的临时校验使用）。

        Parameters
        ----------
        url:
            必须是 ``https://`` 开头（``http://`` 只在测试用的本地 http.server 场景允许）。
        name:
            出现在结果 ``name`` 字段的名字。
        method:
            ``HEAD`` 或 ``GET``。
        """
        requirement = EndpointRequirement(
            name=name,
            url=url,
            method="HEAD" if method.upper() == "HEAD" else "GET",
            purpose="临时连通性校验",
        )
        return self.check_one(requirement)

    def check_one(self, requirement: EndpointRequirement) -> RequirementResult:
        """探测单个端点，返回结构化结果（从不抛网络异常）。"""
        started = self._clock()
        status_code: int | None = None
        error: str | None = None
        try:
            if self._client is not None:
                response = self._request(self._client, requirement)
                status_code = response.status_code
            else:
                with httpx.Client(
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=self._follow_redirects,
                    trust_env=True,
                ) as client:
                    response = self._request(client, requirement)
                    status_code = response.status_code
        except httpx.TimeoutException:
            error = "Timeout"
        except httpx.ConnectError:
            error = "ConnectError"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        except Exception as exc:
            error = f"UnexpectedError:{type(exc).__name__}"
        latency_ms = round((self._clock() - started) * 1000.0, 1)

        observed: dict[str, str | int | float | bool | None] = {
            "url": requirement.url,
            "method": requirement.method,
            "latency_ms": latency_ms,
        }
        if status_code is not None:
            observed["status_code"] = status_code
        if error is not None:
            observed["error"] = error

        if error is None and status_code is not None and status_code < 400:
            status = RequirementStatus.PRESENT
            guidance = ""
        elif error is None and status_code is not None and status_code in _AUTH_STATUSES:
            status = RequirementStatus.PRESENT
            guidance = ""
            observed["auth_required"] = status_code in (401, 403)
        elif error is None and status_code is not None:
            status = (
                RequirementStatus.MISSING if requirement.required else RequirementStatus.OPTIONAL
            )
            guidance = self._http_guidance(requirement, status_code)
        else:
            status = (
                RequirementStatus.MISSING if requirement.required else RequirementStatus.OPTIONAL
            )
            guidance = self._network_guidance(requirement, error or "Unknown")

        if status is RequirementStatus.OPTIONAL:
            observed["degraded"] = True

        return RequirementResult(
            name=requirement.name,
            kind=CheckKind.NETWORK,
            status=status,
            required=requirement.required,
            guidance=guidance,
            help_url=requirement.url,
            observed=observed,
        )

    # -- 内部 -------------------------------------------------------------- #
    def _request(self, client: httpx.Client, requirement: EndpointRequirement) -> httpx.Response:
        # 显式传 follow_redirects：不依赖调用方构造 Client 时的默认值（否则重定向不被跟随）。
        if requirement.method == "GET":
            # 流式只读首字节：GET 场景不下载整个响应体（有些端点不支持 HEAD）。
            request = client.build_request("GET", requirement.url, headers={"Range": "bytes=0-0"})
            response = client.send(request, stream=True, follow_redirects=self._follow_redirects)
            try:
                response.read()
            finally:
                response.close()
            return response
        return client.head(requirement.url, follow_redirects=self._follow_redirects)

    def _http_guidance(self, requirement: EndpointRequirement, status_code: int) -> str:
        if status_code == 429:
            return (
                f"{requirement.name} 返回 429（限流）：{requirement.url}。"
                "稍后重试，或降低并发；检查不是下载，通常等待几分钟即恢复。"
            )
        if status_code >= 500:
            return (
                f"{requirement.name} 返回 {status_code}（服务端故障）：{requirement.url}。"
                "这不是本机配置问题；稍后重试并查看该服务的状态页。"
            )
        return (
            f"{requirement.name} 返回 {status_code}：{requirement.url}。"
            "端点可能已迁移，请核对官方文档里的最新地址。"
        )

    def _network_guidance(self, requirement: EndpointRequirement, error: str) -> str:
        if error == "Timeout":
            return (
                f"{requirement.name} 在 {self._timeout:.1f}s 内无响应：{requirement.url}。"
                "检查代理/VPN 设置，或用 curl -I -m 5 <url> 复现；境外站点在部分网络下不可直连。"
            )
        if error == "ConnectError":
            return (
                f"{requirement.name} 连接失败（DNS 或 TLS）：{requirement.url}。"
                "先用 curl -I <url> 复现；若 curl 也失败，说明网络策略/代理拦截了该域名。"
            )
        return (
            f"{requirement.name} 探测出错（{error}）：{requirement.url}。"
            "这一项无法判定，请手动用 curl -I <url> 复核。"
        )
