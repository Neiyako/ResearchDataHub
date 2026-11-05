"""Provider 层共用的小工具：HTTP 客户端装配、远端 JSON 读取与错误映射。

为什么不复用 ``sources/base.py``
-------------------------------
``sources/`` 的 ``SourceAdapter`` 绑定的是"资产发现"生命周期（``describe`` /
``discover_assets`` / ``health_check``），与服务端错误码的映射面向传输重试；
Provider 层只需要"发请求 → 拿 JSON → 映射错误"，并且必须**不带任何凭证**地记录
请求 URL 以形成证据链。两个目标不同，强行复用会把两层的错误语义捆在一起。
本模块只依赖 ``core``，不 import ``sources`` 与 ``transfer``。

错误映射（**与远端真实行为一致**，见 ``docs/providers/*.md`` 的 curl 记录）
-------------------------------------------------------------------------
* 4xx（含 401/403 凭证问题、404 数据集不存在、400 参数非法）→ 不可重试；
  * 404 → :class:`~researchdatahub.core.errors.SourceNotFoundError`
  * 400/422 → :class:`~researchdatahub.core.errors.MetadataError`（参数形状不对）
  * 401/403 → :class:`~researchdatahub.core.errors.SourceUnavailableError`（凭证不足）
* 429 / 5xx / 连接错误 / 超时 → :class:`~researchdatahub.core.errors.RetryableTransportError`
  （``retry_after_seconds`` 从 ``Retry-After`` 读取）
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from researchdatahub.core.errors import (
    MetadataError,
    RetryableTransportError,
    SourceNotFoundError,
    SourceUnavailableError,
)

__all__ = [
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "USER_AGENT",
    "as_utc",
    "build_client",
    "get_json",
    "request_json",
    "utcnow",
]

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIMEOUT_SECONDS: Final[float] = 60.0
"""目录查询的默认超时（秒）。比 ``sources`` 的 30s 宽一些：STAC ``/collections`` 实测需要 33s。"""

USER_AGENT: Final[str] = "researchdatahub-providers/0.1"
"""所有 Provider 请求共用的 User-Agent（不含任何凭证信息）。"""


def utcnow() -> datetime:
    """返回当前 UTC 时间（可被注入的 ``clock`` 覆盖）。"""
    return datetime.now(UTC)


def as_utc(value: datetime, *, field: str) -> datetime:
    """把带时区的 ``datetime`` 规整为 UTC。

    Raises
    ------
    MetadataError
        传入 naive 时间（时区不明的时间在科研数据里不可接受）。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetadataError(f"{field} must be timezone-aware", detail={"field": field})
    return value.astimezone(UTC)


def build_client(
    *,
    base_url: str = "",
    timeout: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    headers: Mapping[str, str] | None = None,
) -> httpx.Client:
    """构造 Provider 用的 HTTP 客户端（跟随重定向、统一 UA）。

    ``base_url`` 允许注入本地测试服务器（``http://127.0.0.1:<port>``），
    这样离线单测可以回放真实响应样本而不 mock socket。
    """
    merged: dict[str, str] = {"User-Agent": USER_AGENT}
    merged.update(headers or {})
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers=merged,
    )


def request_json(
    client: httpx.Client,
    *,
    url: str,
    params: Mapping[str, str | int | float] | None = None,
    method: str = "GET",
    json_body: Any = None,
    provider: str = "unknown",
) -> Any:
    """发请求并解析 JSON，把 HTTP/网络失败映射为 :mod:`researchdatahub.core.errors` 的异常。

    Parameters
    ----------
    client:
        已装配的 :class:`httpx.Client`。
    url:
        相对或绝对 URL（``client.base_url`` 会参与拼接）。
    params:
        查询参数；``None`` 值由调用方提前剔除（本函数不做隐式过滤）。
    method:
        ``"GET"`` 或 ``"POST"``。
    json_body:
        POST 请求体。
    provider:
        仅用于日志与异常 ``source_id``，不含凭证。

    Raises
    ------
    SourceNotFoundError
        远端返回 404。
    MetadataError
        远端返回 400/422（请求形状不被接受），或响应不是合法 JSON。
    SourceUnavailableError
        远端返回 401/403（凭证缺失或不足）。
    RetryableTransportError
        远端返回 429/5xx，或发生连接/超时错误。
    """
    try:
        response = client.request(method, url, params=dict(params or {}), json=json_body)
    except httpx.TimeoutException as exc:
        raise RetryableTransportError(
            f"{provider} metadata request timed out",
            source_id=provider,
            detail={"url": url, "method": method},
        ) from exc
    except httpx.HTTPError as exc:
        raise RetryableTransportError(
            f"{provider} metadata request failed at the transport layer",
            source_id=provider,
            detail={"url": url, "method": method, "error": type(exc).__name__},
        ) from exc

    status = response.status_code
    if status == 404:
        raise SourceNotFoundError(
            f"{provider} returned 404 for the requested resource",
            source_id=provider,
            detail={"url": url, "status": status, "body": _body_excerpt(response)},
        )
    if status in (400, 422):
        raise MetadataError(
            f"{provider} rejected the request shape (HTTP {status})",
            source_id=provider,
            detail={"url": url, "status": status, "body": _body_excerpt(response)},
        )
    if status in (401, 403):
        raise SourceUnavailableError(
            f"{provider} denied access (HTTP {status}); credentials are missing or insufficient",
            source_id=provider,
            detail={"url": url, "status": status},
        )
    if status == 429 or status >= 500:
        raise RetryableTransportError(
            f"{provider} is unavailable temporarily (HTTP {status})",
            source_id=provider,
            detail={"url": url, "status": status},
            retry_after_seconds=_retry_after(response),
        )
    if status >= 300:
        raise MetadataError(
            f"{provider} returned an unexpected HTTP status {status}",
            source_id=provider,
            detail={"url": url, "status": status, "body": _body_excerpt(response)},
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MetadataError(
            f"{provider} returned a non-JSON body",
            source_id=provider,
            detail={"url": url, "status": status, "body": _body_excerpt(response)},
        ) from exc


def get_json(
    client: httpx.Client,
    *,
    url: str,
    params: Mapping[str, str | int | float] | None = None,
    provider: str = "unknown",
) -> Any:
    """:func:`request_json` 的 GET 便捷包装。"""
    return request_json(client, url=url, params=params, method="GET", provider=provider)


def _retry_after(response: httpx.Response) -> float | None:
    """解析 ``Retry-After`` 头（秒数形式）；无法解析返回 ``None``。"""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _body_excerpt(response: httpx.Response, *, limit: int = 512) -> str:
    """响应体摘录（用于错误诊断；**不含请求头**，因此不会泄漏凭证）。"""
    text = response.text
    return text[:limit]
