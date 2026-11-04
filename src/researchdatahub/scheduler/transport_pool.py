"""传输适配器选择：按 ``SourceAsset.access_route`` 找实现，找不到就明确失败。

调度器**不**自己写 ``if route == "s3"``：路由到实现的映射完全由适配器的
``supports(asset)`` 决定（ADR-0002：换源不需要改调度器，换协议不需要改 source）。
"""

from __future__ import annotations

from collections.abc import Sequence

from researchdatahub.core.errors import ScheduleError
from researchdatahub.core.models import SourceAsset
from researchdatahub.scheduler.models import TransferExecutor

__all__ = ["TransportPool"]


class TransportPool:
    """一组传输适配器；``select`` 返回第一个声明支持该资产的实现。"""

    def __init__(self, transports: Sequence[TransferExecutor]) -> None:
        if not transports:
            raise ScheduleError(
                "scheduler requires at least one transport adapter",
                detail={"transports": 0},
            )
        self._transports: tuple[TransferExecutor, ...] = tuple(transports)

    @property
    def transport_ids(self) -> tuple[str, ...]:
        """已装配的传输实现 ID（诊断/报告用）。"""
        return tuple(transport.transport_id for transport in self._transports)

    def select(self, asset: SourceAsset) -> TransferExecutor:
        """选择能处理该资产的传输适配器。

        Raises
        ------
        ScheduleError
            没有任何已装配的传输实现支持 ``asset.access_route``。
        """
        for transport in self._transports:
            if transport.supports(asset):
                return transport
        raise ScheduleError(
            f"no transport adapter supports access_route={asset.access_route.value!r} "
            f"for asset {asset.asset_id!r}; configured transports: {self.transport_ids}",
            detail={
                "asset_id": asset.asset_id,
                "access_route": asset.access_route.value,
                "origin_uri": asset.origin_uri,
                "transports": list(self.transport_ids),
            },
        )

    def close(self) -> None:
        """关闭所有传输实现（幂等；由调用方决定何时关闭）。"""
        for transport in self._transports:
            transport.close()
