"""Route Intelligence 的预期失败类型。"""

from __future__ import annotations

from researchdatahub.core.errors import RdhError

__all__ = [
    "NoQualifiedRouteError",
    "RouteExecutionError",
    "RouteNotQualifiedError",
    "RoutePolicyError",
]


class RoutePolicyError(RdhError):
    """route policy 拒绝了一次下载（不是网络传输失败）。"""

    default_code = "RDH_ROUTE_POLICY"


class NoQualifiedRouteError(RoutePolicyError):
    """没有 route 满足当前能力/资质/规模策略。"""

    default_code = "RDH_NO_QUALIFIED_ROUTE"


class RouteNotQualifiedError(RoutePolicyError):
    """调用方要求直接使用一条未获得资质的 route。"""

    default_code = "RDH_ROUTE_NOT_QUALIFIED"


class RouteExecutionError(RoutePolicyError):
    """route 已获准，但下载执行器未配置或执行失败。"""

    default_code = "RDH_ROUTE_EXECUTION"
