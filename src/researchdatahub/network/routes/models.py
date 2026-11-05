"""Route 契约与实测链路统计模型。

Route 是"经哪条链路、哪个节点、向哪个提供方取数据"的完整描述
------------------------------------------------------------------
``Route`` 必须如实描述**这条链路实际具备的能力**：协议、是否支持断点续传、并发上限、
认证材料引用（``auth_ref`` 只是指针，绝不是密钥本身）。**不能假设所有平台都支持所有
协议**——例如 CDS 只能用 ``provider_async`` 轮询结果，SFTP 镜像未必支持 Range。因此
"协议不支持"必须是一个**显式结果**（:attr:`RouteSelection.rejected` 里带
``unsupported_protocol`` 原因），而不是静默地把它当可用链路。

链路性能只来自实测
------------------
:class:`RouteStats` 的每一个数字都来自真实传输（成功/失败/重试次数/字节数/耗时/首字节
延迟）。冷启动（``samples == 0``）时选路结果明确标注 ``basis="no_measurement"``，并按
"可用性 + 稳定排序"回退；**绝不按国家/地区/节点名猜速度**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from researchdatahub.core.models import JsonValue
from researchdatahub.network.workers.models import _Contract

__all__ = [
    "NO_MEASUREMENT",
    "ROUTE_PROTOCOLS",
    "MeasurementBasis",
    "Route",
    "RouteOption",
    "RouteRejection",
    "RouteRejectionCode",
    "RouteSelection",
    "RouteStats",
]

NO_MEASUREMENT: str = "no_measurement"
"""冷启动标记：该链路/提供方组合在库中**没有任何实测样本**。"""

ROUTE_PROTOCOLS: tuple[str, ...] = (
    "https",
    "rest",
    "http_range",
    "s3",
    "ftp",
    "sftp",
    "provider_api",
    "provider_async",
    "cloud_export",
)
"""Route 协议枚举（与任务书一致）。能力是否可用由 :class:`Route` 自己声明。"""

_PROTOCOL_SET = frozenset(ROUTE_PROTOCOLS)


class MeasurementBasis(StrEnum):
    """选路排序依据：``measured``（有实测样本）或 ``no_measurement``（冷启动回退）。"""

    MEASURED = "measured"
    NO_MEASUREMENT = "no_measurement"


class RouteRejectionCode(StrEnum):
    """一条 Route 被排除的结构化原因。"""

    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    PROTOCOL_NOT_SUPPORTED_BY_WORKER = "protocol_not_supported_by_worker"
    DISABLED = "disabled"
    CONCURRENCY_EXHAUSTED = "concurrency_exhausted"
    SIZE_EXCEEDS_LIMIT = "size_exceeds_limit"
    WORKER_OFFLINE = "worker_offline"
    PROVIDER_MISMATCH = "provider_mismatch"
    DATASET_MISMATCH = "dataset_mismatch"


class RouteRejection(_Contract):
    """一条被拒绝的 Route 及其原因（**必须回传给调用方**，不能只留下可用项）。"""

    route_id: str
    code: RouteRejectionCode
    detail: str

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {"route_id": self.route_id, "code": self.code.value, "detail": self.detail}


class Route(_Contract):
    """一条可执行链路：``(worker, provider, protocol, endpoint)`` 的一种组合。

    Parameters
    ----------
    route_id:
        链路的稳定标识（建议 ``<worker>:<provider>:<protocol>``）。
    protocol:
        链路协议，取值必须属于 :data:`ROUTE_PROTOCOLS`；不支持时由
        :class:`~researchdatahub.network.routes.manager.RouteManager` 显式判为
        ``unsupported_protocol``。
    worker_id:
        执行该链路的节点。
    provider_id:
        数据提供方（与资产 ``source_id`` 对应）。
    supports_resume:
        是否支持断点续传（决定能否续跑大文件）。
    max_concurrent:
        该链路的并发上限（真实限流值，不是"希望值"）。
    auth_ref:
        认证材料**引用**（例如 ``env:CDSAPI_KEY`` / ``secret://cds``）；**不存密钥**。
    base_url / dataset_ids / enabled / notes:
        端点与适用范围。
    """

    route_id: str = Field(min_length=2, max_length=128)
    protocol: str = Field(min_length=1, max_length=32)
    worker_id: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=128)
    supports_resume: bool = True
    max_concurrent: int = Field(default=1, ge=1, le=64)
    auth_ref: str | None = Field(default=None, max_length=256)
    base_url: str | None = Field(default=None, max_length=4096)
    dataset_ids: list[str] = Field(
        default_factory=list, max_length=256, description="适用数据集白名单（空=不限制）"
    )
    enabled: bool = True
    notes: str = Field(default="", max_length=512)

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, value: str) -> str:
        """协议必须是已知枚举值之一：拼错协议名会被当成"不支持"而不是"能用"。"""
        text = value.strip().lower()
        if text not in _PROTOCOL_SET:
            raise ValueError(
                f"unknown route protocol {value!r}; supported: {sorted(_PROTOCOL_SET)}"
            )
        return text

    @property
    def supports_protocol(self) -> bool:
        """该链路协议是否属于已实现集合（本模块所有 Route 恒为 ``True``）。"""
        return self.protocol in _PROTOCOL_SET

    @field_validator("base_url")
    @classmethod
    def _base_url_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().rstrip("/")
        if not text.startswith(("http://", "https://", "s3://", "sftp://", "ftp://")):
            raise ValueError("base_url must be an absolute URL with a known scheme")
        return text

    @field_validator("auth_ref")
    @classmethod
    def _auth_ref_is_pointer(cls, value: str | None) -> str | None:
        """``auth_ref`` 只能是引用形式（``env:`` / ``secret:`` / ``keychain:``）。

        直接写密钥会污染审计记录与日志，因此在模型的入口处就拒绝。
        """
        if value is None:
            return None
        text = value.strip()
        if not text.startswith(("env:", "secret:", "keychain:", "file:")):
            raise ValueError(
                "auth_ref must be a reference such as 'env:VAR_NAME' / 'secret:name'; "
                "never inline a secret value"
            )
        return text

    @model_validator(mode="after")
    def _dataset_ids_unique(self) -> Route:
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("dataset_ids must not contain duplicates")
        return self


class RouteStats(_Contract):
    """一条 ``(route, provider)`` 组合的**实测**性能统计。

    全部字段都由真实传输写入；``samples == 0`` 表示冷启动，任何"速度"结论都不成立。
    """

    route_id: str
    provider_id: str = Field(default="", description="统计所属提供方（空串表示未按提供方区分）")
    samples: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    transferred_bytes: int = Field(default=0, ge=0)
    latency_ms_total: float = Field(default=0.0, ge=0.0, description="首字节延迟累计（毫秒）")
    transfer_duration_s: float = Field(default=0.0, ge=0.0, description="传输耗时累计（秒）")
    last_outcome: str | None = Field(default=None, max_length=32)
    last_outcome_at: datetime | None = Field(
        default=None, description="最近一次样本的时间（成功或失败）"
    )
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("last_success_at", "last_failure_at", "last_outcome_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else value.astimezone(UTC)

    @property
    def success_rate(self) -> float | None:
        """实测成功率；无样本时为 ``None``（**不是** 0 或 1）。"""
        if self.samples == 0:
            return None
        return self.successes / self.samples

    @property
    def failure_rate(self) -> float | None:
        """实测失败率；无样本时为 ``None``。"""
        if self.samples == 0:
            return None
        return self.failures / self.samples

    @property
    def throughput_bps(self) -> float | None:
        """实测吞吐（字节/秒，含失败样本的总时长）；无耗时数据时为 ``None``。"""
        if self.transfer_duration_s <= 0:
            return None
        return self.transferred_bytes / self.transfer_duration_s

    @property
    def latency_ms(self) -> float | None:
        """平均首字节延迟（毫秒）；无样本时为 ``None``。"""
        if self.samples == 0:
            return None
        return self.latency_ms_total / self.samples

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示（``None`` 明确表示"没有测量"，不伪装成 0）。"""
        return {
            "route_id": self.route_id,
            "provider_id": self.provider_id,
            "samples": self.samples,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "failure_rate": self.failure_rate,
            "retry_count": self.retry_count,
            "transferred_bytes": self.transferred_bytes,
            "transfer_duration_s": self.transfer_duration_s,
            "latency_ms": self.latency_ms,
            "throughput_bps": self.throughput_bps,
            "last_outcome": self.last_outcome,
        }


class RouteOption(_Contract):
    """选路结果中的一条候选（按得分降序；``score is None`` 表示没有实测依据）。"""

    route: Route
    stats: RouteStats
    score: float | None = Field(default=None, description="实测综合分；冷启动时为 ``None``")
    basis: MeasurementBasis = MeasurementBasis.NO_MEASUREMENT
    worker_status: str = Field(default="healthy", max_length=16)
    rationale: str = Field(default="", max_length=512)

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示。"""
        return {
            "route_id": self.route.route_id,
            "protocol": self.route.protocol,
            "worker_id": self.route.worker_id,
            "provider_id": self.route.provider_id,
            "supports_resume": self.route.supports_resume,
            "max_concurrent": self.route.max_concurrent,
            "score": self.score,
            "basis": self.basis.value,
            "worker_status": self.worker_status,
            "rationale": self.rationale,
            "stats": self.stats.as_dict(),
        }


class RouteSelection(_Contract):
    """一次选路的结构化结果：候选排序 + 被拒原因 + 用户要求的 target 透传。

    ``basis`` 描述的是**首选候选**的依据：只要首选链路有实测样本就是 ``measured``；
    若首选是冷启动链路，则整份结果都标 ``no_measurement``（无论哪个），因为此时
    选择本身没有测量依据。
    """

    provider_id: str
    dataset_id: str
    size_bytes: int = Field(ge=0)
    target: str | None = None
    basis: MeasurementBasis = MeasurementBasis.NO_MEASUREMENT
    options: list[RouteOption] = Field(default_factory=list)
    rejected: list[RouteRejection] = Field(default_factory=list)

    @property
    def chosen(self) -> RouteOption | None:
        """首选链路；无候选时为 ``None``。"""
        return self.options[0] if self.options else None

    def as_dict(self) -> dict[str, JsonValue]:
        """JSON 友好表示（MCP ``get_route_status`` 直接使用）。"""
        return {
            "provider_id": self.provider_id,
            "dataset_id": self.dataset_id,
            "size_bytes": self.size_bytes,
            "target": self.target,
            "basis": self.basis.value,
            "chosen": None if self.chosen is None else self.chosen.as_dict(),
            "options": [option.as_dict() for option in self.options],
            "rejected": [item.as_dict() for item in self.rejected],
        }
