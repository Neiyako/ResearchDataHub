"""环境配置系统（M2）：检查依赖 / 凭据 / 网络 / 磁盘，并给出**可操作**配置引导。

定位
----
面向 AI Coding Agent 的科研数据获取链路里，「环境配置引导」是一个独立环节：
Agent 需要知道「为什么现在不能下载」「缺什么」「去哪拿」「怎么验证」，而不是一句
"configure your credentials"。

本包回答四个问题
----------------
1. 这个 Provider **要求什么**（依赖包 / 凭据引用 / 网络端点）——
   :class:`~researchdatahub.environment.registry.ProviderRequirementsRegistry`。
2. 当前环境**满足了吗** —— 四个 checker：
   :class:`~researchdatahub.environment.dependencies.DependencyChecker`、
   :class:`~researchdatahub.environment.credentials.CredentialChecker`、
   :class:`~researchdatahub.environment.network.NetworkChecker`、
   :class:`~researchdatahub.environment.storage.DiskSpaceChecker`。
3. **整体结论**是什么 —— :class:`~researchdatahub.environment.validation.ConfigurationValidator`
   产出 :class:`~researchdatahub.environment.models.EnvironmentReport`（``schema_version="1.0"``）。
4. **用户该怎么做** —— :func:`get_environment_requirements` / :func:`render_environment_guide`。

安全不变式
----------
* 凭据只以引用形式存在（``env:`` / ``file:`` / ``keyring:``），代码里没有能装 secret 的字段；
* 报告 / CLI 输出 / 日志里**永不出现 secret 明文**；
* 绝不要求用户把令牌粘贴到聊天窗口；
* 网络检查只做只读 HEAD/GET，**绝不调用 cdsapi**。

快速用法
--------
>>> from researchdatahub.environment import validate_environment, ConfigurationValidator
>>> report = validate_environment("cds", check_network=False, storage_root=None)  # doctest: +SKIP
>>> report.status.value                                                          # doctest: +SKIP
'needs_configuration'
"""

from researchdatahub.environment.credentials import (
    CredentialChecker,
    load_credential_references,
    parse_credential_reference,
)
from researchdatahub.environment.dependencies import DependencyChecker, default_probe
from researchdatahub.environment.models import (
    ENVIRONMENT_SCHEMA_VERSION,
    CheckKind,
    ConfigurationGuide,
    CredentialKind,
    CredentialReference,
    CredentialRequirement,
    DependencyRequirement,
    EndpointRequirement,
    EnvironmentReport,
    GuidanceStep,
    OverallStatus,
    ProviderRequirements,
    RequirementResult,
    RequirementStatus,
    assert_report_has_no_secret,
    redact_secrets,
)
from researchdatahub.environment.network import (
    DEFAULT_TIMEOUT_SECONDS,
    NetworkChecker,
)
from researchdatahub.environment.registry import (
    PROVIDER_IDS,
    PROVIDER_REQUIREMENTS_REGISTRY,
    ProviderRequirementsRegistry,
    get_environment_requirements,
    get_requirements,
    normalize_provider_id,
)
from researchdatahub.environment.storage import (
    DEFAULT_FREE_SPACE_FLOOR_GB,
    DiskSpaceChecker,
)
from researchdatahub.environment.validation import (
    DEFAULT_STORAGE_ROOT,
    ConfigurationValidator,
    derive_overall_status,
    validate_environment,
)
from researchdatahub.environment.validation import (
    render_guide as render_environment_guide,
)

__all__ = [
    "DEFAULT_FREE_SPACE_FLOOR_GB",
    "DEFAULT_STORAGE_ROOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENVIRONMENT_SCHEMA_VERSION",
    "PROVIDER_IDS",
    "PROVIDER_REQUIREMENTS_REGISTRY",
    "CheckKind",
    "ConfigurationGuide",
    "ConfigurationValidator",
    "CredentialChecker",
    "CredentialKind",
    "CredentialReference",
    "CredentialRequirement",
    "DependencyChecker",
    "DependencyRequirement",
    "DiskSpaceChecker",
    "EndpointRequirement",
    "EnvironmentReport",
    "GuidanceStep",
    "NetworkChecker",
    "OverallStatus",
    "ProviderRequirements",
    "ProviderRequirementsRegistry",
    "RequirementResult",
    "RequirementStatus",
    "assert_report_has_no_secret",
    "default_probe",
    "derive_overall_status",
    "get_environment_requirements",
    "get_requirements",
    "load_credential_references",
    "normalize_provider_id",
    "parse_credential_reference",
    "redact_secrets",
    "render_environment_guide",
    "validate_environment",
]
