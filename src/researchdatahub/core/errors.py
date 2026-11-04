"""ResearchDataHub 的统一异常层次。

设计原则
--------
1. 所有异常都继承 :class:`RdhError`，调用方可以一条 ``except RdhError`` 捕获本项目
   的全部预期失败；未被 ``RdhError`` 包裹的异常视为程序缺陷（bug），不应被静默吞掉。
2. 异常携带结构化上下文（``task_id`` / ``source_id`` / ``detail``），便于写进
   Manifest 的 ``last_error`` 字段并生成可读报告，而不是只留一句字符串。
3. 异常粒度服务于调度决策：
   - :class:`RetryableTransportError` 触发 ``RETRY_WAIT``；
   - :class:`PermanentTransportError` 直接 ``FAILED``；
   - :class:`IntegrityError` 进入 ``QUARANTINED``（文件移到隔离区，不删除）。
4. 不吞异常：本模块不提供任何 ``ignore_errors`` 开关。

分层
----
``RdhError``
├── ``ConfigError``            配置读取/校验失败
├── ``ModelValidationError``   数据契约校验失败（区别于 pydantic 的 ValidationError）
├── ``SourceError``            数据源适配器错误
│   ├── ``SourceNotFoundError``
│   ├── ``SourceUnavailableError``
│   └── ``MetadataError``
├── ``TransportError``         传输层错误
│   ├── ``RetryableTransportError``
│   └── ``PermanentTransportError``
├── ``IntegrityError``         完整性/元数据校验失败
├── ``StorageError``           本地存储操作失败
├── ``ManifestError``          SQLite 持久化失败
├── ``ScheduleError``          调度器错误（并发/优先级/预算）
├── ``StateTransitionError``   非法状态转换
└── ``NotImplementedFeatureError``  M0 未实现功能的明确拒绝
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "ConfigError",
    "IntegrityError",
    "ManifestError",
    "MetadataError",
    "ModelValidationError",
    "NotImplementedFeatureError",
    "PermanentTransportError",
    "RdhError",
    "RetryableTransportError",
    "ScheduleError",
    "SourceError",
    "SourceNotFoundError",
    "SourceUnavailableError",
    "StateTransitionError",
    "StorageError",
    "TransportError",
]


class RdhError(Exception):
    """所有 ResearchDataHub 预期异常的基类。

    Parameters
    ----------
    message:
        人类可读的错误说明（英文工程日志用，便于 grep）。
    task_id:
        关联的采集任务 ID（如果错误发生在任务上下文之外则为 ``None``）。
    source_id:
        关联的数据源 ID。
    detail:
        附加的结构化信息，必须可 JSON 序列化，会写入 Manifest 的错误记录。
    """

    default_code: ClassVar[str] = "RDH_ERROR"

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.task_id = task_id
        self.source_id = source_id
        self.detail: dict[str, Any] = dict(detail or {})

    @property
    def code(self) -> str:
        """稳定的机器可读错误码，用于报告与日志聚合。"""
        return self.default_code

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典（写入 manifest、报告）。"""
        return {
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
            "task_id": self.task_id,
            "source_id": self.source_id,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        """人类可读的完整错误描述，含错误码与任务上下文。"""
        parts = [f"[{self.code}] {self.message}"]
        if self.task_id:
            parts.append(f"task_id={self.task_id}")
        if self.source_id:
            parts.append(f"source_id={self.source_id}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)

    def __repr__(self) -> str:
        """调试表示，避免输出超长的 detail。"""
        return f"{type(self).__name__}({self.message!r}, task_id={self.task_id!r})"


# --------------------------------------------------------------------------- #
# 配置与数据契约
# --------------------------------------------------------------------------- #
class ConfigError(RdhError):
    """配置文件缺失、YAML 语法错误、schema 校验失败或环境变量引用无法解析。"""

    default_code = "RDH_CONFIG"


class ModelValidationError(RdhError):
    """构造数据契约对象时业务规则不满足（如时间区间倒置、单位缺失）。"""

    default_code = "RDH_MODEL_INVALID"


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #
class SourceError(RdhError):
    """数据源适配器相关错误的基类。"""

    default_code = "RDH_SOURCE"


class SourceNotFoundError(SourceError):
    """配置中不存在指定的数据源 ID，或远端目录/数据集不存在。"""

    default_code = "RDH_SOURCE_NOT_FOUND"


class SourceUnavailableError(SourceError):
    """数据源暂时不可用（服务维护、配额耗尽、认证失败）。"""

    default_code = "RDH_SOURCE_UNAVAILABLE"


class MetadataError(SourceError):
    """远端元数据缺失、格式错误或与声明不一致。"""

    default_code = "RDH_METADATA"


# --------------------------------------------------------------------------- #
# 传输
# --------------------------------------------------------------------------- #
class TransportError(RdhError):
    """传输层错误基类。

    Attributes
    ----------
    retryable:
        是否适合重试。子类通过类属性设定，避免调度器靠 isinstance 分支蔓延。
    retry_after_seconds:
        服务端建议的等待时间（如 HTTP ``Retry-After``），可为 ``None``。
    """

    default_code = "RDH_TRANSPORT"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        source_id: str | None = None,
        detail: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, task_id=task_id, source_id=source_id, detail=detail)
        self.retry_after_seconds = retry_after_seconds


class RetryableTransportError(TransportError):
    """可重试的传输错误：连接重置、超时、5xx、限流（429）。"""

    default_code = "RDH_TRANSPORT_RETRYABLE"
    retryable = True


class PermanentTransportError(TransportError):
    """不可重试的传输错误：404、403、校验失败的文件内容、协议不支持。"""

    default_code = "RDH_TRANSPORT_PERMANENT"
    retryable = False


# --------------------------------------------------------------------------- #
# 完整性与存储
# --------------------------------------------------------------------------- #
class IntegrityError(RdhError):
    """文件完整性或科学元数据校验失败。触发文件进入隔离区（QUARANTINED）。"""

    default_code = "RDH_INTEGRITY"


class StorageError(RdhError):
    """本地存储操作失败：磁盘满、权限不足、路径冲突、原子移动失败。"""

    default_code = "RDH_STORAGE"


class ManifestError(RdhError):
    """SQLite 持久化失败：schema 版本不兼容、事务冲突、记录不存在。"""

    default_code = "RDH_MANIFEST"


# --------------------------------------------------------------------------- #
# 调度与状态机
# --------------------------------------------------------------------------- #
class ScheduleError(RdhError):
    """调度器错误：超出并发上限、优先级非法、预算不足。"""

    default_code = "RDH_SCHEDULE"


class StateTransitionError(RdhError):
    """尝试执行非法状态转换。

    携带 ``from_state`` / ``to_state`` 以便状态机单测与运维排查。
    """

    default_code = "RDH_STATE_TRANSITION"

    def __init__(
        self,
        message: str,
        *,
        from_state: str,
        to_state: str,
        task_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged = {"from_state": from_state, "to_state": to_state, **(detail or {})}
        super().__init__(message, task_id=task_id, detail=merged)
        self.from_state = from_state
        self.to_state = to_state


class NotImplementedFeatureError(RdhError):
    """M0 未实现功能的显式拒绝。

    CLI 用它替代“假成功”。任何 M0 之外的命令都必须抛出它，而不是打印成功。
    """

    default_code = "RDH_NOT_IMPLEMENTED"

    def __init__(self, feature: str, *, planned_for: str = "M1") -> None:
        super().__init__(
            f"feature '{feature}' is not implemented yet (planned for {planned_for})",
            detail={"feature": feature, "planned_for": planned_for},
        )
        self.feature = feature
        self.planned_for = planned_for
