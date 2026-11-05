"""环境配置系统的数据契约（Pydantic v2）。

本模块只描述**检查结果的结构**，不执行任何检查；执行逻辑在
:mod:`researchdatahub.environment.registry`（要求声明）与四个 checker 子模块
（依赖 / 凭据 / 网络 / 磁盘）里。

安全不变式（贯穿整个包）
------------------------
* 凭据**只以引用形式**存在：:class:`CredentialRequirement` 记录变量名、文件路径或
  keyring 的 ``service/username``，**没有任何字段可以承载 secret 值**。
* :class:`RequirementResult` 的 ``observed`` 只允许出现「计数 / 文件名 / 端点」这类
  可公开信息，禁止写入任何凭证内容（由 :func:`assert_report_has_no_secret` 兜底检查）。
* ``status`` 是「检查结论」，不是「配置项是否存在」：``OPTIONAL`` 表示该要求对当前
  Provider 是可选的，缺失它不算失败。

为什么单独建模而不是直接复用 config.models
------------------------------------------
``config`` 描述的是**用户写的配置**（不可变、``extra="forbid"``、要能被 YAML 表达）；
``environment`` 描述的是**运行时探测结果**（每次执行都不同、带延迟与退出码）。
二者生命周期不同，混用会把「探测结果」塞进「配置 schema」。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

__all__ = [
    "CREDENTIAL_REF_PREFIXES",
    "ENVIRONMENT_SCHEMA_VERSION",
    "REDACTED_PLACEHOLDER",
    "CheckKind",
    "ConfigurationGuide",
    "CredentialKind",
    "CredentialReference",
    "CredentialRequirement",
    "DependencyRequirement",
    "EndpointRequirement",
    "EnvironmentReport",
    "GuidanceStep",
    "OverallStatus",
    "ProviderRequirements",
    "RequirementResult",
    "RequirementStatus",
    "assert_report_has_no_secret",
    "redact_secrets",
]

ENVIRONMENT_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
"""环境报告的契约版本。加字段给默认值保持 1.x；删改字段升 2.0。"""

REDACTED_PLACEHOLDER: Final[str] = "***REDACTED***"
"""打码后替换进去的占位符；测试与文档共用同一个常量，避免拼写漂移。"""

CREDENTIAL_REF_PREFIXES: Final[tuple[str, ...]] = ("env:", "file:", "keyring:")
"""凭据引用的三种合法前缀。**没有第四种**：任何裸字面量都视为配置错误。"""


class CheckKind(StrEnum):
    """检查类别。``ENVIRONMENT_REPORT.requirements[].kind`` 用来给引导分组。"""

    DEPENDENCY = "dependency"
    CREDENTIAL = "credential"
    NETWORK = "network"
    STORAGE = "storage"


class RequirementStatus(StrEnum):
    """单条要求的检查结论。

    * ``PRESENT``：已就绪（依赖可导入 / 凭据存在 / 端点可达 / 目录可写）。
    * ``MISSING``：未就绪，**必须**修复（``required=True`` 时会拖垮整体状态）。
    * ``OPTIONAL``：未就绪但该要求是可选的，不算失败（例如 ``boto3`` 对 NCAR HTTPS 路径）。
    * ``UNKNOWN``：无法判定（探测过程本身出错，或未执行该检查）。

    ``UNKNOWN`` 绝不等于通过——与 :class:`~researchdatahub.core.models.IntegrityReport`
    的 ``UNKNOWN`` 语义保持一致。
    """

    PRESENT = "present"
    MISSING = "missing"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"

    @property
    def ok(self) -> bool:
        """是否可以在「整体状态」推导中被视为满足（``OPTIONAL`` 与 ``PRESENT`` 都算）。"""
        return self in (RequirementStatus.PRESENT, RequirementStatus.OPTIONAL)


class OverallStatus(StrEnum):
    """一个 Provider 的整体环境状态。

    * ``OK``：全部必需要求已就绪。
    * ``NEEDS_CONFIGURATION``：缺可修复的东西（依赖没装、凭据没配、端点不可达），
      用户按引导做完即可用。
    * ``UNAVAILABLE``：当前环境**结构性**不可用（必需依赖的安装不可行、平台不支持），
      或者用户显式跳过必需的修复项；引导里会说明原因。
    """

    OK = "ok"
    NEEDS_CONFIGURATION = "needs_configuration"
    UNAVAILABLE = "unavailable"


class CredentialKind(StrEnum):
    """凭据的存放形式。决定「检查存在性」的具体手段。"""

    ENV = "env"
    FILE = "file"
    KEYRING = "keyring"


_NameStr = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
_UrlStr = Annotated[str, StringConstraints(min_length=1, max_length=2048, strip_whitespace=True)]


class _EnvModel(BaseModel):
    """环境检查结果的公共配置。

    与 ``config.models._ConfigModel`` 的关键差别：这里**不是** ``frozen``——检查结果是
    逐步填充的构建产物，checker 需要往列表里追加结果。但 ``extra="forbid"`` 保留，
    防止拼错字段名时静默丢数据。
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        use_enum_values=False,
    )


# --------------------------------------------------------------------------- #
# 要求声明（静态，来自 registry）
# --------------------------------------------------------------------------- #
class CredentialReference(_EnvModel):
    """**凭据引用**：只描述去哪找，绝不携带值。

    Parameters
    ----------
    kind:
        ``env`` = 环境变量；``file`` = 文件里的某一行（或其本身）；``keyring`` = 系统钥匙串。
    name:
        ``env`` 时是变量名；``file`` 时是文件路径模板；``keyring`` 时是「用户名或项目名」。
    service:
        ``keyring`` 时的 service 名（``keyring.get_password(service, name)`` 的第一个参数）。
        其它 kind 必须为 ``None``。

    Notes
    -----
    该模型没有、也不应该有 ``value`` / ``secret`` / ``token`` 字段。测试
    ``test_env_models.py::test_credential_reference_has_no_value_field`` 会反向校验这一点。
    """

    kind: CredentialKind
    name: _NameStr = Field(description="变量名 / 文件路径模板 / keyring 用户名")
    service: str | None = Field(
        default=None, max_length=128, description="keyring service 名（仅 kind=keyring）"
    )

    @model_validator(mode="after")
    def _service_only_for_keyring(self) -> CredentialReference:
        if self.kind is CredentialKind.KEYRING and not self.service:
            raise ValueError("keyring credential reference requires a service name")
        if self.kind is not CredentialKind.KEYRING and self.service is not None:
            raise ValueError("service is only meaningful for keyring credential references")
        return self

    @property
    def is_secret(self) -> bool:
        """该引用是否指向**密钥类**内容。

        凭据文件/变量里也有公开值（如 CDS 的 ``url`` 行），它们不需要保密；用这个属性把
        「必须保密」和「必须存在」两类要求区分开，测试与报告可以据此断言。
        """
        return (
            self.kind in (CredentialKind.ENV, CredentialKind.KEYRING)
            or "TOKEN" in self.name.upper()
        )

    def describe(self) -> str:
        """人类可读的「去哪找」描述（不含任何密钥）。"""
        if self.kind is CredentialKind.ENV:
            return f"环境变量 {self.name}"
        if self.kind is CredentialKind.FILE:
            return f"文件 {self.name}"
        return f"系统钥匙串（service={self.service}, user={self.name}）"


class CredentialRequirement(_EnvModel):
    """一条凭据要求：某个 Provider 的某个凭据**应该存在于哪里**。"""

    key: _NameStr = Field(description="逻辑键名，如 'key' / 'token'；与 sources.yaml 对齐")
    reference: CredentialReference
    required: bool = Field(default=True)
    purpose: str = Field(min_length=1, max_length=512, description="这条凭据用来干什么")
    how_to_obtain: str = Field(
        default="", max_length=1024, description="去哪拿（真实 URL），空表示无需获取"
    )

    @field_validator("key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        if not value.replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"credential key {value!r} must be alphanumeric/underscore/dot")
        return value


class DependencyRequirement(_EnvModel):
    """一条 Python 依赖要求。``module`` 是 ``importlib`` 能解析的模块名（不是 PyPI 名）。"""

    module: _NameStr = Field(description="可导入的模块名，如 'netCDF4'")
    package: _NameStr = Field(description="PyPI 包名，install 引导里用这个")
    required: bool = Field(default=True)
    purpose: str = Field(min_length=1, max_length=512)


class EndpointRequirement(_EnvModel):
    """一条网络端点要求（只读探测，不登录、不下载）。"""

    name: _NameStr
    url: _UrlStr = Field(description="探测用 URL；必须是 https")
    method: Literal["HEAD", "GET"] = Field(default="HEAD")
    required: bool = Field(default=True)
    purpose: str = Field(min_length=1, max_length=512)

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        """生产端点必须是 https；``http://`` 只允许回环地址（测试用本地 http.server）。"""
        if value.startswith("https://"):
            return value
        if value.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return value
        raise ValueError(
            f"endpoint url must be https (plain http is only allowed for loopback tests): {value!r}"
        )


class GuidanceStep(_EnvModel):
    """一条**可操作**的配置步骤。四条字段缺一不可（否则用户还是不知道怎么做）。"""

    action: str = Field(min_length=1, max_length=256, description="要做什么")
    why: str = Field(min_length=1, max_length=512, description="为什么需要")
    where: str = Field(min_length=1, max_length=1024, description="去哪拿 / 存到哪（含真实 URL）")
    verify: str = Field(min_length=1, max_length=512, description="怎么验证生效")


class ConfigurationGuide(_EnvModel):
    """一个 Provider 的完整配置引导。"""

    provider: str
    summary: str = Field(min_length=1, max_length=1024, description="一句话现状说明")
    steps: list[GuidanceStep] = Field(default_factory=list, max_length=32)

    def render_markdown(self) -> str:
        """渲染为 Markdown（中文标题 + 编号步骤），供 CLI/docs 直接打印。"""
        lines = [f"### {self.provider}", "", self.summary, ""]
        for index, step in enumerate(self.steps, start=1):
            lines.extend(
                [
                    f"{index}. **{step.action}**",
                    f"   - 为什么：{step.why}",
                    f"   - 去哪 / 存哪：{step.where}",
                    f"   - 验证：`{step.verify}`",
                ]
            )
        return "\n".join(lines)


class ProviderRequirements(_EnvModel):
    """一个 Provider 的**静态**要求清单（数据来自 :mod:`researchdatahub.environment.registry`）。"""

    provider: str = Field(min_length=1, max_length=128, description="稳定的 provider id")
    display_name: str = Field(min_length=1, max_length=128)
    source_id: str | None = Field(
        default=None, description="对应 configs/sources.yaml 的数据源 ID（若有）"
    )
    dependencies: list[DependencyRequirement] = Field(default_factory=list, max_length=32)
    credentials: list[CredentialRequirement] = Field(default_factory=list, max_length=32)
    endpoints: list[EndpointRequirement] = Field(default_factory=list, max_length=32)
    notes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _unique_names(self) -> ProviderRequirements:
        for label, names in (
            ("dependency module", [item.module for item in self.dependencies]),
            (
                "credential reference",
                [f"{item.key}@{item.reference.kind}" for item in self.credentials],
            ),
            ("endpoint name", [item.name for item in self.endpoints]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"duplicate {label} in provider {self.provider!r}")
        return self

    @property
    def required_dependencies(self) -> list[DependencyRequirement]:
        """必需依赖（``required=True``）。"""
        return [item for item in self.dependencies if item.required]

    @property
    def required_credentials(self) -> list[CredentialRequirement]:
        """必需凭据（``required=True``）。"""
        return [item for item in self.credentials if item.required]


# --------------------------------------------------------------------------- #
# 检查结果（动态，来自各 checker）
# --------------------------------------------------------------------------- #
class RequirementResult(_EnvModel):
    """单条要求的检查结果。``status=OPTIONAL`` 表示「可选且未就绪，不算失败」。"""

    name: Annotated[str, StringConstraints(min_length=1, max_length=512, strip_whitespace=True)] = (
        Field(description="依赖模块名 / 凭据键名 / 端点名 / 存储路径（含引用描述，可能较长）")
    )
    kind: CheckKind
    status: RequirementStatus
    required: bool = True
    guidance: str = Field(default="", max_length=2048, description="不可用时怎么修（中文）")
    help_url: str | None = Field(default=None, max_length=2048, description="去哪拿/去哪看")
    observed: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        description="可公开的观测值（计数、文件名、延迟、HTTP 状态码）；**禁止放 secret**",
    )

    @model_validator(mode="after")
    def _optional_only_when_not_required(self) -> RequirementResult:
        if self.status is RequirementStatus.OPTIONAL and self.required:
            raise ValueError(
                f"requirement {self.name!r} cannot be both required=True and status='optional'"
            )
        if self.status is RequirementStatus.MISSING and not self.required:
            # 非必需项缺失时状态必须是 OPTIONAL，否则整体状态推导会误判。
            raise ValueError(
                f"requirement {self.name!r} is not required; a missing non-required item "
                "must report status='optional', not 'missing'"
            )
        return self

    @property
    def blocking(self) -> bool:
        """是否拖垮整体状态：必需且未就绪。"""
        return self.required and self.status is RequirementStatus.MISSING

    @property
    def unresolved(self) -> bool:
        """是否「未就绪或无法判定」（``PRESENT`` 之外的一切）。"""
        return self.status not in (RequirementStatus.PRESENT, RequirementStatus.OPTIONAL)


class EnvironmentReport(_EnvModel):
    """一个 Provider 的完整环境报告：``{provider, status, requirements: []}``。"""

    schema_version: Literal["1.0"] = ENVIRONMENT_SCHEMA_VERSION
    provider: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    status: OverallStatus
    requirements: list[RequirementResult] = Field(default_factory=list, max_length=256)
    #: 整体状态的人类可读解释（中文，含下一步动作）。为空 ⇔ ``status=OK``。
    summary: str = Field(default="", max_length=2048)
    #: 本次检查的时间戳（UTC，ISO 8601）。便于把报告与日志对齐。
    checked_at: str = Field(default="", max_length=64)
    #: 本次执行了哪些检查（``dependencies`` / ``credentials`` / ``network`` / ``storage``）。
    checks_run: list[str] = Field(default_factory=list, max_length=8)
    #: 本次检查耗时（毫秒）。放在字段里而不是 ``errors``，避免把正常信息伪装成错误。
    duration_ms: float = Field(default=0.0, ge=0.0)
    #: 报告级的不可恢复错误（例如 registry 里根本没有这个 provider）。
    errors: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _ok_implies_nothing_blocking(self) -> EnvironmentReport:
        blocking = [item for item in self.requirements if item.blocking]
        if self.status is OverallStatus.OK and blocking:
            raise ValueError(
                f"status='ok' contradicts blocking requirement(s): "
                f"{', '.join(item.name for item in blocking)}"
            )
        return self

    @property
    def ok(self) -> bool:
        """是否可以直接使用（``status=OK``）。"""
        return self.status is OverallStatus.OK

    @property
    def blocking_requirements(self) -> list[RequirementResult]:
        """拖垮整体状态的必需项。"""
        return [item for item in self.requirements if item.blocking]

    @property
    def by_kind(self) -> dict[CheckKind, list[RequirementResult]]:
        """按检查类别分组（保持类别固定顺序，缺失的类别不出现在字典里）。"""
        grouped: dict[CheckKind, list[RequirementResult]] = {}
        for item in self.requirements:
            grouped.setdefault(item.kind, []).append(item)
        return grouped

    def to_json_dict(self) -> dict[str, object]:
        """稳定 JSON 结构（``schema_version`` 在前，便于人读 diff）。

        Notes
        -----
        该字典会被 CLI 直接 ``json.dumps`` 输出；它**不得**包含任何 secret。
        写入前由 :func:`assert_report_has_no_secret` 做过兜底检查。
        """
        return self.model_dump(mode="json", exclude_none=True)


# --------------------------------------------------------------------------- #
# 安全兜底
# --------------------------------------------------------------------------- #
def redact_secrets(text: str, secrets: object) -> str:
    """把 ``text`` 中出现的 secret 值替换成 :data:`REDACTED_PLACEHOLDER`。

    Parameters
    ----------
    text:
        任意要输出的文本（错误消息 / 观测值 / 引导步骤）。
    secrets:
        需要打码的值集合；非字符串项被忽略，空串被忽略（否则会把所有文本打碎）。

    Returns
    -------
    str
        打码后的文本。按**长度降序**替换，避免短 secret 先吃掉长 secret 的前缀。
    """
    candidates = _iter_secret_strings(secrets)
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, REDACTED_PLACEHOLDER)
    return text


def _iter_secret_strings(values: object) -> list[str]:
    """递归收集非空字符串（dict 取 value，list/tuple/set 展开，Path 转成字符串）。"""
    found: list[str] = []
    if values is None:
        return found
    if isinstance(values, str):
        if values.strip():
            found.append(values)
        return found
    if isinstance(values, Path):
        return _iter_secret_strings(str(values))
    if isinstance(values, dict):
        for item in values.values():
            found.extend(_iter_secret_strings(item))
        return found
    if isinstance(values, (list, tuple, set, frozenset)):
        for item in values:
            found.extend(_iter_secret_strings(item))
        return found
    return found


def assert_report_has_no_secret(report: EnvironmentReport, secrets: object) -> None:
    """断言报告中不含任何 secret 明文；命中则抛 :class:`AssertionError` 并指出位置。

    Parameters
    ----------
    report:
        待输出的报告。
    secrets:
        用来比对的 secret 值（单值 / 列表 / dict 均可）；空串会被忽略。

    Raises
    ------
    AssertionError
        报告序列化后的 JSON 中出现任一 secret 值。

    Notes
    -----
    这是**兜底**而非主防线：主防线是模型层根本没有能装 secret 的字段。
    测试里用 sentinel 字符串（如 ``"SENTINEL-SECRET-...")`` 断言它不出现在 JSON 中。
    """
    payload = report.to_json_dict()
    text = repr(payload)
    for secret in _iter_secret_strings(secrets):
        if secret in text:
            raise AssertionError(
                f"environment report for provider {report.provider!r} contains a secret value "
                f"(length={len(secret)}); this is a security defect"
            )
