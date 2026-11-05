"""CDSE OAuth2 认证：``client_credentials`` 换 Bearer token，**token 只在内存里**。

真实端点与已实测的响应形状（2026-11-05）
----------------------------------------
* Token 端点：``https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token``
  （来自 ``/.well-known/openid-configuration``，实测 200；``issuer`` =
  ``https://identity.dataspace.copernicus.eu/auth/realms/CDSE``，
  ``grant_types_supported`` 含 ``client_credentials``）。
* 用**故意无效**的 client 做形状探测：``POST`` 表单
  ``grant_type=client_credentials&client_id=...&client_secret=...``
  → ``HTTP 401 {"error": "invalid_client", "error_description": "Invalid client or Invalid
  client credentials"}``。这说明：grant type 与参数名被接受，失败原因是凭证本身。
* **成功响应的形状未验证**：本机没有 ``CDSE_CLIENT_ID`` / ``CDSE_CLIENT_SECRET``
  （实测环境变量均未设置），因此拿不到真实 token。本模块按 OAuth2 RFC 6749 §5.1
  的 `access_token` / `expires_in` / `token_type` 字段解析，并把"成功换取 token"
  标注为 **unverified**。

安全约束
--------
* token 只存在于 :class:`CdseTokenCache` 的内存字段里，**不写文件、不进日志**；
  ``__repr__`` 与 ``to_dict()`` 都不输出 token 值。
* client_secret 只从环境变量读一次，不在任何输出中回显。
* 缓存带提前刷新窗口（``expires_in - skew``），过期或 401 时重新换取。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

import httpx

from researchdatahub.core.errors import SourceUnavailableError
from researchdatahub.providers._http import build_client

__all__ = [
    "CDSE_REALM",
    "CDSE_TOKEN_ENDPOINT",
    "CLIENT_ID_ENV_VAR",
    "CLIENT_SECRET_ENV_VAR",
    "CdseCredentials",
    "CdseTokenCache",
    "probe_credentials",
]

CLIENT_ID_ENV_VAR: Final[str] = "CDSE_CLIENT_ID"
CLIENT_SECRET_ENV_VAR: Final[str] = "CDSE_CLIENT_SECRET"
CDSE_REALM: Final[str] = "CDSE"
CDSE_TOKEN_ENDPOINT: Final[str] = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
)
"""Token 端点（来自实测的 ``.well-known/openid-configuration`` 的 ``token_endpoint``）。"""

_DEFAULT_SKEW_SECONDS: Final[float] = 60.0


@dataclass(frozen=True)
class CdseCredentials:
    """CDSE 客户端凭证（**不含任何值**，只记录是否已配置）。

    ``client_id`` 本身不是秘密（OAuth2 公开标识），但为了统一口径，本对象也不暴露它：
    调用方只需要知道"能换 token 吗"。
    """

    configured: bool

    def missing(self) -> tuple[str, ...]:
        """缺失的环境变量名（用于 ``unverified`` 说明）。"""
        if self.configured:
            return ()
        return (
            f"env {CLIENT_ID_ENV_VAR} is not set",
            f"env {CLIENT_SECRET_ENV_VAR} is not set",
        )

    def to_dict(self) -> dict[str, bool]:
        """可序列化的快照（无秘密）。"""
        return {"configured": self.configured}


def probe_credentials(environ: Mapping[str, str] | None = None) -> CdseCredentials:
    """只检查 ``CDSE_CLIENT_ID`` / ``CDSE_CLIENT_SECRET`` 是否存在且非空（不读取内容）。"""
    env = os.environ if environ is None else environ
    configured = bool(env.get(CLIENT_ID_ENV_VAR, "").strip()) and bool(
        env.get(CLIENT_SECRET_ENV_VAR, "").strip()
    )
    return CdseCredentials(configured=configured)


class CdseTokenCache:
    """OAuth2 ``client_credentials`` token 的内存缓存（线程安全，**绝不落盘**）。

    用法
    ----
    ``token = cache.token(client)`` —— 未过期直接复用，过期或缺失时向 token 端点换取。
    ``cache.invalidate()`` —— 收到 401 时调用，强制下次重新换取。

    Raises
    ------
    SourceUnavailableError
        未配置凭证，或 token 端点返回非 200。
    """

    def __init__(
        self,
        *,
        token_endpoint: str = CDSE_TOKEN_ENDPOINT,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        skew_seconds: float = _DEFAULT_SKEW_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """构造缓存。

        Parameters
        ----------
        token_endpoint:
            token 端点；离线测试注入本地服务器 URL。
        http_client:
            注入的 HTTP 客户端；``None`` 时自建并负责关闭。
        clock:
            单调时钟（秒），用于计算过期；测试注入以免依赖真实时间。
        skew_seconds:
            提前刷新窗口。
        environ:
            环境变量映射；默认 ``os.environ``。
        """
        self._endpoint = token_endpoint
        self._http_client = http_client
        self._owns_http = http_client is None
        self._clock = clock
        self._skew = skew_seconds
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        env = os.environ if environ is None else environ
        self._client_id = env.get(CLIENT_ID_ENV_VAR, "").strip()
        self._client_secret = env.get(CLIENT_SECRET_ENV_VAR, "").strip()

    # -- 不泄漏 ------------------------------------------------------------ #
    def __repr__(self) -> str:
        """调试表示：**不包含 token 与 secret**。"""
        return f"CdseTokenCache(configured={self.configured}, cached={self._token is not None})"

    def __str__(self) -> str:
        """人类可读表示：**不包含 token 与 secret**。"""
        return self.__repr__()

    @property
    def configured(self) -> bool:
        """是否已配置客户端凭证。"""
        return bool(self._client_id and self._client_secret)

    def close(self) -> None:
        """关闭自建 HTTP 客户端并清空内存 token。"""
        if self._http_client is not None and self._owns_http:
            self._http_client.close()
        self._token = None
        self._expires_at = 0.0

    # -- token ------------------------------------------------------------- #
    def invalidate(self) -> None:
        """丢弃当前 token（收到 401 时使用）。"""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def has_valid_token(self) -> bool:
        """当前是否有未过期的缓存 token（**不返回 token 本身**）。"""
        with self._lock:
            return self._token is not None and self._clock() < self._expires_at

    def token(self, client: httpx.Client | None = None) -> str:
        """返回可用 token：命中缓存则复用，否则向 token 端点换取。

        Raises
        ------
        SourceUnavailableError
            未配置凭证，或 token 端点没有返回 ``access_token``。
        """
        # 先在锁内做快速命中判断，网络请求在锁外执行（避免持锁做 I/O）。
        with self._lock:
            if self._token is not None and self._clock() < self._expires_at:
                return self._token
        if not self.configured:
            raise SourceUnavailableError(
                "CDSE client credentials are not configured",
                source_id="copernicus",
                detail={"missing": list(CdseCredentials(False).missing())},
            )
        http = client if client is not None else self._client()
        payload = self._request_token(http)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise SourceUnavailableError(
                "CDSE token endpoint returned no access_token",
                source_id="copernicus",
                detail={"response_keys": sorted(payload)},
            )
        expires_in = payload.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else 0.0
        )
        with self._lock:
            self._token = access_token
            self._expires_at = self._clock() + max(lifetime - self._skew, 0.0)
            return self._token

    def _client(self) -> httpx.Client:
        """惰性创建 HTTP 客户端。"""
        if self._http_client is None:
            self._http_client = build_client(timeout=30.0)
            self._owns_http = True
        return self._http_client

    def _request_token(self, client: httpx.Client) -> Mapping[str, object]:
        """向 token 端点发 ``client_credentials`` 请求（表单编码）。

        Raises
        ------
        SourceUnavailableError
            HTTP 非 200，或响应不是 JSON 对象。
        """
        try:
            response = client.post(
                self._endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                "CDSE token endpoint is unreachable",
                source_id="copernicus",
                detail={"error": type(exc).__name__},
            ) from exc
        if response.status_code != 200:
            # 只取 error/error_description（不含请求头与凭证）。
            error = ""
            try:
                body = response.json()
                if isinstance(body, Mapping):
                    error = str(body.get("error", ""))
            except ValueError:
                error = ""
            raise SourceUnavailableError(
                f"CDSE token request failed (HTTP {response.status_code})",
                source_id="copernicus",
                detail={"status": response.status_code, "oauth_error": error},
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise SourceUnavailableError(
                "CDSE token endpoint returned a non-JSON body",
                source_id="copernicus",
                detail={"status": response.status_code},
            ) from exc
        if not isinstance(parsed, Mapping):
            raise SourceUnavailableError(
                "CDSE token endpoint response is not a JSON object",
                source_id="copernicus",
                detail={"observed_type": type(parsed).__name__},
            )
        return parsed
