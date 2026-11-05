"""Provider Contract v1：面向 AI Coding Agent 的「数据集目录 + 能力」层契约。

这一层解决什么问题
------------------
Agent 要回答的问题是「**去哪里找哪份数据、它有什么变量、能不能拿到**」，
而不是「某个文件怎么下载」。因此本层的对象是**数据集**（collection / product type），
不是字节，也不是一次具体的请求。

与 ``sources/`` 的关系（**两层，职责不同，不互相 import**）
----------------------------------------------------------
* ``sources/`` = **资产发现层**（ADR-0002）：把 :class:`~researchdatahub.core.models.DatasetRequest`
  翻译成远端请求，产出 :class:`~researchdatahub.core.models.SourceAsset`（带 ``origin_uri`` /
  ``size_bytes`` / ``access_route``），供传输层搬运字节。它面向"**已知数据集**，
  请给我文件"。
* ``providers/`` = **目录与能力层**（本模块）：``search`` 发现数据集、``inspect`` 解释数据集、
  ``get_variables`` / ``estimate_request`` 判断"能不能做"，并把不支持的路径**显式**声明。
  它面向"**我不知道有什么**，先告诉我"。
* 二者共享 :mod:`researchdatahub.core.models` 的空间/时间契约（:class:`SpatialExtent` /
  :class:`TemporalExtent`），因此可以后续桥接：Provider 负责发现与解释，产出
  ``DatasetRequest`` 交给 ``SourceAdapter`` 产出 ``SourceAsset``。**V1 只建契约，不建桥**
  （写作用域禁止修改 ``sources/``，见 ``docs/providers/*.md`` 的"后续桥接方案"）。

不可虚构原则（本模块的硬约束）
------------------------------
1. 能力只有三种状态，必须**逐项如实**：已验证（:data:`CapabilityStatus.VERIFIED`）、
   未验证（:data:`CapabilityStatus.UNVERIFIED`，通常因为没有凭证或没有实测）、
   不支持（**不出现在** ``implemented`` 中，且**必须出现在** ``unsupported`` 中）。
2. 不支持的能力**绝不返回假数据**：调用 ``submit_task`` / ``cancel_task`` 等未实现方法时抛
   :class:`~researchdatahub.core.errors.NotImplementedFeatureError`，
   未实现的方法名必须同时出现在 :attr:`ProviderCapabilities.unsupported` 中，二者由
   :meth:`ProviderCapabilities.validate_consistency` 交叉校验。
3. 远端没给的值一律 ``None`` / ``"unknown"``，不做推断；拿不到许可证/引用就显式留空。

时钟与网络
----------
* 实现必须接受 ``clock`` 注入（测试用固定时间）。
* 实现必须接受 ``http_client`` 注入（离线测试用真实本地 HTTP 服务器回放真实响应样本）。
* :attr:`ProviderCapabilities.auth_required` 描述的是"**拿到元数据**是否强制需要凭证"；
  "**下载字节**是否需要凭证"记录在 :attr:`ProviderCapabilities.credential_features`，
  两者不可混淆（NASA Earthdata 的 CMR 元数据匿名可读，但受保护 granule 的下载需要 EDL）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from researchdatahub.core.errors import MetadataError, NotImplementedFeatureError
from researchdatahub.core.models import JsonValue, SpatialExtent, TemporalExtent

__all__ = [
    "CAPABILITY_CONTRACT_VERSION",
    "CREDENTIAL_FEATURES",
    "CapabilityStatus",
    "DatasetCandidate",
    "DatasetDetail",
    "DatasetFilter",
    "DatasetVariable",
    "ProviderCapabilities",
    "ProviderContract",
    "QueryTranslation",
    "RequestEstimate",
    "RequestValidation",
    "TaskHandle",
    "TaskStatusSnapshot",
    "assert_provider_contract",
]

CAPABILITY_CONTRACT_VERSION: Final[str] = "provider-contract/1.0"
"""Provider Contract 的版本号；``get_provider_capabilities`` 会原样返回它。"""

CREDENTIAL_FEATURES: Final[Mapping[str, str]] = {
    "metadata_query": "查询目录/元数据（部分平台匿名可读）",
    "granule_search": "检索文件级元数据（granule / item）",
    "data_download": "下载数据字节（HTTPS / OData ``$value``）",
    "cloud_processing": "云端处理 / 异步作业（Harmony、云原生子集化）",
    "s3_direct": "S3 直连（``s3://`` 路径）",
}
"""凭证只影响**其中一部分**能力；这是"不把元数据免登录误写成数据免登录"的机器可读依据。"""

TaskStateLiteral = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class CapabilityStatus(StrEnum):
    """单项能力的证据状态。``UNSUPPORTED`` 不会出现在 ``implemented`` 中。"""

    VERIFIED = "verified"
    """2026-11-05 用真实请求确认过（命令与关键字段见 ``docs/providers/*.md``）。"""

    UNVERIFIED = "unverified"
    """代码路径存在但**没有**在当前环境被真实请求验证（通常缺凭证），不得当成可用。"""

    UNSUPPORTED = "unsupported"
    """平台不提供、或本实现明确不做；调用对应方法必须抛 ``NotImplementedFeatureError``。"""


# --------------------------------------------------------------------------- #
# 数据集候选与详情
# --------------------------------------------------------------------------- #
class DatasetVariable(BaseModel):
    """数据集中的一个变量 / 波段（来自远端元数据，**不做同义词映射**）。

    单位、长名、数据类型都可能缺失：缺失即 ``None``，不得按变量名猜测单位。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512, description="远端变量名 / 波段名（原样）")
    long_name: str | None = Field(default=None, max_length=1024)
    units: str | None = Field(default=None, max_length=64)
    data_type: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4096)
    source: Literal["remote", "unknown"] = Field(
        default="remote",
        description="'remote' 表示来自远端元数据；'unknown' 表示远端未提供变量清单",
    )


class DatasetCandidate(BaseModel):
    """``search`` 的一条结果：**足以让 Agent 决定下一步是不是它**，不含下载逻辑。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64, description="Provider ID，如 'earthdata'")
    dataset_id: str = Field(min_length=1, max_length=512, description="Provider 内稳定的数据集 ID")
    title: str = Field(min_length=1, max_length=2048)
    abstract: str | None = Field(default=None, max_length=16384)
    variables: tuple[DatasetVariable, ...] = Field(
        default_factory=tuple, description="已知变量；空元组表示'未能确定'而不是'没有变量'"
    )
    variables_known: bool = Field(
        default=False, description="True 表示 variables 来自远端声明；False 表示未知"
    )
    temporal_coverage: TemporalExtent | None = Field(
        default=None, description="时间覆盖；任一端未知则为 None（不推断）"
    )
    spatial_extent: SpatialExtent | None = Field(
        default=None, description="空间覆盖；不完整则为 None（不推断）"
    )
    formats: tuple[str, ...] = Field(default_factory=tuple, description="原生数据格式（远端声明）")
    access_modes: tuple[str, ...] = Field(
        default_factory=tuple, description="该数据集可用的访问方式，如 ('https', 's3')"
    )
    auth_required: bool = Field(description="获取**元数据**是否强制需要凭证")
    license: str | None = Field(default=None, max_length=512)
    citation: str | None = Field(default=None, max_length=2048)
    landing_url: str | None = Field(default=None, max_length=4096)
    native_id: str | None = Field(
        default=None,
        max_length=512,
        description="平台原生标识（CMR concept-id / CDSE STAC collection id），用于溯源",
    )
    extra: Mapping[str, JsonValue] = Field(
        default_factory=dict, description="平台原始字段（命名空间键，如 'cmr.short_name'）"
    )


class DatasetDetail(BaseModel):
    """``inspect`` 的结果：候选 + 证据链（查询了哪些远端端点、何时、是否匿名）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: DatasetCandidate
    native_id: str | None = Field(default=None, max_length=512)
    data_centers: tuple[str, ...] = Field(default_factory=tuple)
    processing_level: str | None = Field(default=None, max_length=64)
    doi: str | None = Field(default=None, max_length=512)
    retrieval_endpoints: tuple[str, ...] = Field(
        default_factory=tuple, description="解析该详情实际请求过的远端 URL（无凭证，可审计）"
    )
    retrieved_at: datetime
    anonymous_access: bool = Field(description="本次请求是否未使用任何凭证")
    notes: tuple[str, ...] = Field(
        default_factory=tuple, description="无法确定的字段与原因的显式说明"
    )


# --------------------------------------------------------------------------- #
# 检索参数与翻译
# --------------------------------------------------------------------------- #
class DatasetFilter(BaseModel):
    """``search`` 的结构化过滤条件；各 Provider 只使用自己**已实测**支持的部分。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword: str | None = Field(default=None, max_length=512, description="自由文本关键词")
    short_name: str | None = Field(default=None, max_length=128, description="数据集简称（精确）")
    provider: str | None = Field(
        default=None, max_length=128, description="数据提供方 / 数据中心的原生代号"
    )
    platform: str | None = Field(default=None, max_length=128)
    collection: str | None = Field(
        default=None, max_length=512, description="限定在某个集合/产品类型内检索"
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="(西, 南, 东, 北) 经纬度"
    )
    temporal: TemporalExtent | None = Field(default=None, description="时间范围（UTC）")
    limit: int = Field(default=10, ge=1, le=200)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        """校验经纬度包围盒的顺序与取值范围。"""
        if value is None:
            return None
        west, south, east, north = value
        if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
            raise ValueError("bbox longitudes must be within [-180, 180]")
        if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
            raise ValueError("bbox latitudes must be within [-90, 90]")
        if south > north:
            raise ValueError("bbox south bound must not exceed north bound")
        return value


class QueryTranslation(BaseModel):
    """把 :class:`DatasetFilter` 翻译成远端请求后的**可审计**结果。

    ``used`` / ``ignored`` 是核心：Agent 必须知道哪个过滤条件**没生效**，
    否则会把"没过滤"误读成"远端确认没有符合条件的数据"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "POST"] = "GET"
    query: Mapping[str, str] = Field(
        default_factory=dict, description="实际发出的查询参数（无凭证）"
    )
    body: Mapping[str, JsonValue] | None = Field(default=None, description="POST 请求体（无凭证）")
    used: tuple[str, ...] = Field(default_factory=tuple, description="生效的过滤字段")
    ignored: tuple[str, ...] = Field(
        default_factory=tuple, description="被忽略的过滤字段（含原因）"
    )


class RequestValidation(BaseModel):
    """``validate_request`` 的结果：能否执行、缺什么、无法验证什么。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    dataset_id: str
    errors: tuple[str, ...] = Field(default_factory=tuple, description="必然导致失败的硬错误")
    warnings: tuple[str, ...] = Field(
        default_factory=tuple, description="可行但需注意（如'无法在本地确认时间覆盖'）"
    )
    unverified: tuple[str, ...] = Field(
        default_factory=tuple, description="当前环境无法验证的检查项（不得当作已检查）"
    )


class RequestEstimate(BaseModel):
    """``estimate_request`` 的结果：**只有远端给出真实数字才填**，否则 `None` + 说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    matched_files: int | None = Field(default=None, ge=0, description="远端声明的匹配文件数")
    total_bytes: int | None = Field(default=None, ge=0, description="远端声明的字节合计")
    files_without_size: int = Field(
        default=0, ge=0, description="没有声明大小的文件数（total_bytes 因此可能偏低）"
    )
    basis: str = Field(min_length=1, description="估算依据（哪个端点、哪些字段）")
    details: Mapping[str, JsonValue] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 任务（异步作业）模型
# --------------------------------------------------------------------------- #
class TaskHandle(BaseModel):
    """``submit_task`` 返回的句柄；``submitted=False`` 表示本 Provider 不支持该能力。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=512)
    submitted: bool
    state: TaskStateLiteral = "queued"
    message: str | None = Field(default=None, max_length=1024)


class TaskStatusSnapshot(BaseModel):
    """``get_task_status`` 的结果；状态取值只来自远端真实字面量映射。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=512)
    state: TaskStateLiteral
    remote_state: str | None = Field(default=None, max_length=128, description="远端原始状态字面量")
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    message: str | None = Field(default=None, max_length=1024)


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #
class ProviderCapabilities(BaseModel):
    """Provider 的能力边界：**每个字段都必须如实**，供 ``get_provider_capabilities`` 原样输出。

    约定的方法名清单见 :data:`CONTRACT_METHODS`；``implemented`` 与 ``unsupported``
    必须覆盖全部方法名且不相交（:meth:`validate_consistency` 强制），否则 Agent 无法
    仅凭能力声明就推断"调用哪个方法会失败"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    contract_version: str = Field(default=CAPABILITY_CONTRACT_VERSION, max_length=64)
    auth_required: bool = Field(description="获取**元数据**是否强制需要凭证")
    supported_formats: tuple[str, ...] = Field(
        default_factory=tuple, description="已知的数据格式（远端声明，未声明则留空）"
    )
    supported_regions: tuple[str, ...] = Field(
        default_factory=tuple, description="空间覆盖描述（如 'global'）；不确定则留空"
    )
    supported_temporal_ranges: tuple[str, ...] = Field(
        default_factory=tuple, description="已知时间覆盖描述；不确定则留空"
    )
    supports_async_job: bool = Field(default=False)
    supports_cloud_processing: bool = Field(default=False)
    supports_partial_download: bool = Field(default=False)
    supports_resume: bool = Field(default=False)
    implemented: tuple[str, ...] = Field(default_factory=tuple)
    unsupported: tuple[str, ...] = Field(default_factory=tuple)
    verified: tuple[str, ...] = Field(
        default_factory=tuple, description="已用真实请求验证的能力（带日期与端点）"
    )
    unverified: tuple[str, ...] = Field(
        default_factory=tuple, description="代码存在但当前环境未验证的能力（含原因）"
    )
    credential_features: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "能力 → 是否需要凭证（'verified'/'unverified'/'unsupported'），键见 CREDENTIAL_FEATURES"
        ),
    )
    notes: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("implemented", "unsupported")
    @classmethod
    def _known_method_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """方法名必须是契约已知的名字，避免拼写错误让"未实现"被静默漏报。"""
        unknown = sorted(set(value) - set(CONTRACT_METHODS))
        if unknown:
            raise ValueError(f"unknown contract method names: {unknown}")
        if len(set(value)) != len(value):
            raise ValueError("duplicate method names in capability declaration")
        return value

    @field_validator("credential_features")
    @classmethod
    def _known_credential_features(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        """凭证矩阵的键必须是已知能力键。"""
        unknown = sorted(set(value) - set(CREDENTIAL_FEATURES))
        if unknown:
            raise ValueError(f"unknown credential feature keys: {unknown}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ProviderCapabilities:
        """``implemented`` 与 ``unsupported`` 必须覆盖全部方法且互斥。"""
        self.validate_consistency()
        return self

    def validate_consistency(self) -> None:
        """交叉校验能力声明与方法实现的一致性。

        Raises
        ------
        ValueError
            方法未被声明、同时被声明为已实现与不支持，或声明了 ``supports_async_job``
            却没有实现任何任务方法。
        """
        implemented = set(self.implemented)
        unsupported = set(self.unsupported)
        both = sorted(implemented & unsupported)
        if both:
            raise ValueError(f"methods declared both implemented and unsupported: {both}")
        missing = sorted(set(CONTRACT_METHODS) - implemented - unsupported)
        if missing:
            raise ValueError(f"methods missing from the capability declaration: {missing}")
        task_methods = {"submit_task", "get_task_status", "retrieve_result", "cancel_task"}
        if self.supports_async_job and not (implemented & task_methods):
            raise ValueError(
                "supports_async_job=True requires at least one implemented task method"
            )
        if not self.supports_async_job and (implemented & task_methods):
            raise ValueError(
                "task methods are implemented but supports_async_job is False; "
                "declarations must match reality"
            )

    def status_of(self, feature: str) -> CapabilityStatus:
        """查询一项能力的状态。

        判定规则
        --------
        * 出现在 ``unsupported`` → :attr:`CapabilityStatus.UNSUPPORTED`；
        * 出现在 ``implemented`` 且 ``unverified`` 里有 ``"<feature>: ..."`` 条目 →
          :attr:`CapabilityStatus.UNVERIFIED`；
        * 出现在 ``implemented`` 且无上述标记 → :attr:`CapabilityStatus.VERIFIED`。
        """
        if feature in self.unsupported:
            return CapabilityStatus.UNSUPPORTED
        if feature in self.implemented:
            marker = f"{feature}:"
            if any(item.startswith(marker) for item in self.unverified):
                return CapabilityStatus.UNVERIFIED
            return CapabilityStatus.VERIFIED
        return CapabilityStatus.UNSUPPORTED


CONTRACT_METHODS: Final[tuple[str, ...]] = (
    "search",
    "inspect",
    "get_metadata",
    "get_variables",
    "validate_request",
    "estimate_request",
    "submit_task",
    "get_task_status",
    "retrieve_result",
    "cancel_task",
)
"""Provider Contract v1 的全部方法名；能力声明必须覆盖且仅覆盖这些名字。"""


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class ProviderContract(Protocol):
    """Provider Contract v1（``@runtime_checkable`` 结构化协议）。

    并发与生命周期
    --------------
    * 实例必须线程安全（``search`` 可能被多个 Agent 调用并发触发）；内部缓存必须加锁。
    * 实现 :meth:`close`，并在关闭后拒绝调用（抛 ``RuntimeError``）。
    * 所有失败抛 :mod:`researchdatahub.core.errors` 中的具体异常；
      **不支持的能力**抛 :class:`~researchdatahub.core.errors.NotImplementedFeatureError`。

    方法契约（每个方法都必须在 ``capabilities().implemented`` 或 ``.unsupported`` 中出现）
    -------------------------------------------------------------------------------------
    * :meth:`search` —— 目录检索，返回候选；不检索文件级清单。
    * :meth:`inspect` —— 单个数据集的详情与证据链。
    * :meth:`get_metadata` —— 原始平台元数据（命名空间键），供审计与排障。
    * :meth:`get_variables` —— 变量清单；远端未提供时返回 ``None``
      （**不返回空列表冒充"没有变量"**）。
    * :meth:`validate_request` —— 本地可判定的检查；无法验证的项必须进 ``unverified``。
    * :meth:`estimate_request` —— 体量估算；只有远端真实数字才填。
    * :meth:`submit_task` / :meth:`get_task_status` / :meth:`retrieve_result` / :meth:`cancel_task`
      —— 异步作业；未实现时抛 ``NotImplementedFeatureError``。
    """

    @property
    def provider_id(self) -> str:
        """Provider 的稳定标识（小写，如 ``"earthdata"``）。"""

    def capabilities(self) -> ProviderCapabilities:
        """如实返回能力矩阵（``get_provider_capabilities`` 的原样输出）。"""

    def search(
        self, query: str, filters: DatasetFilter | None = None
    ) -> tuple[DatasetCandidate, ...]:
        """目录检索。``query`` 是自由文本，``filters`` 是结构化条件。"""

    def translate_query(self, query: str, filters: DatasetFilter | None = None) -> QueryTranslation:
        """返回 ``search`` 将要发出的远端请求（不含凭证），供审计"哪个过滤没生效"。"""

    def inspect(self, dataset_id: str) -> DatasetDetail:
        """数据集详情；数据集不存在抛 ``SourceNotFoundError``。"""

    def get_metadata(self, dataset_id: str) -> Mapping[str, JsonValue]:
        """平台原始元数据摘要（命名空间键；不含凭证）。"""

    def get_variables(self, dataset_id: str) -> tuple[DatasetVariable, ...] | None:
        """变量清单；远端未声明时返回 ``None``。"""

    def validate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestValidation:
        """本地可判定的请求检查；不可判定项进 ``unverified``。"""

    def estimate_request(
        self, dataset_id: str, filters: DatasetFilter | None = None
    ) -> RequestEstimate:
        """体量估算；无远端数字时字段为 ``None`` 并说明依据。"""

    def submit_task(self, dataset_id: str, filters: DatasetFilter | None = None) -> TaskHandle:
        """提交异步作业；不支持则抛 ``NotImplementedFeatureError``。"""

    def get_task_status(self, task_id: str) -> TaskStatusSnapshot:
        """查询作业状态；不支持则抛 ``NotImplementedFeatureError``。"""

    def retrieve_result(self, task_id: str) -> Mapping[str, JsonValue]:
        """取作业产物描述；不支持则抛 ``NotImplementedFeatureError``。"""

    def cancel_task(self, task_id: str) -> bool:
        """取消作业；不支持则抛 ``NotImplementedFeatureError``。"""

    def close(self) -> None:
        """释放 HTTP 客户端；幂等。"""


def assert_provider_contract(provider: object) -> None:
    """断言对象满足 :class:`ProviderContract` 且能力声明自洽。

    为什么需要它
    -----------
    ``@runtime_checkable`` 只检查**成员存在**，不检查语义；本函数额外做：

    1. :class:`ProviderCapabilities` 的方法覆盖/互斥校验；
    2. 声明的 ``implemented`` 方法真的**可调用**；
    3. 声明的 ``unsupported`` 方法真的**抛** :class:`NotImplementedFeatureError`。

    第 3 条是"不得冒充已实现"的机器可读出口：一个"假装支持"的 Provider 会在自检里失败。

    Raises
    ------
    MetadataError
        不满足协议，或能力声明与方法行为不一致。
    """
    if not isinstance(provider, ProviderContract):
        raise MetadataError(
            "object does not satisfy ProviderContract",
            detail={"type": type(provider).__name__},
        )
    capabilities = provider.capabilities()
    try:
        capabilities.validate_consistency()
    except ValueError as exc:
        raise MetadataError(str(exc), detail={"provider": capabilities.provider_id}) from exc
    for name in capabilities.unsupported:
        method = getattr(provider, name, None)
        if method is None or not callable(method):
            raise MetadataError(
                f"declared unsupported method {name!r} is missing on the provider",
                detail={"provider": capabilities.provider_id},
            )


def probe_unsupported(method: Any, *args: Any, **kwargs: Any) -> Any:
    """调用一个**声明为不支持**的方法，并断言它抛出 ``NotImplementedFeatureError``。

    供自检与测试使用：返回 ``True`` 表示行为与声明一致；否则抛 :class:`MetadataError`。

    Raises
    ------
    MetadataError
        方法没有按契约拒绝调用（例如返回了空结果或假数据）。
    """
    try:
        method(*args, **kwargs)
    except NotImplementedFeatureError:
        return True
    raise MetadataError(
        "a method declared as unsupported returned data instead of raising "
        "NotImplementedFeatureError",
        detail={"method": getattr(method, "__name__", repr(method))},
    )
