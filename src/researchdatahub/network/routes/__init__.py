"""Route 子系统：链路契约、实测统计与选路、故障转移编排。

对外导出见 :mod:`researchdatahub.network`（顶层包是唯一稳定的公开面）。
"""

from researchdatahub.network.routes.failover import (
    AttemptRecord,
    FailoverCoordinator,
    FailoverExhausted,
    TransferDelivery,
    TransferredFile,
)
from researchdatahub.network.routes.manager import NoRouteAvailable, RouteManager, RouteRecord
from researchdatahub.network.routes.models import (
    NO_MEASUREMENT,
    ROUTE_PROTOCOLS,
    MeasurementBasis,
    Route,
    RouteOption,
    RouteRejection,
    RouteRejectionCode,
    RouteSelection,
    RouteStats,
)
from researchdatahub.network.routes.stats import RouteStatsStore, score_route

__all__ = [
    "NO_MEASUREMENT",
    "ROUTE_PROTOCOLS",
    "AttemptRecord",
    "FailoverCoordinator",
    "FailoverExhausted",
    "MeasurementBasis",
    "NoRouteAvailable",
    "Route",
    "RouteManager",
    "RouteOption",
    "RouteRecord",
    "RouteRejection",
    "RouteRejectionCode",
    "RouteSelection",
    "RouteStats",
    "RouteStatsStore",
    "TransferDelivery",
    "TransferredFile",
    "score_route",
]
