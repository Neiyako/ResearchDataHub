"""配置验证：把四个 checker 组合成一份 :class:`EnvironmentReport`。

整体状态推导（唯一实现，检查与再验证共用）
------------------------------------------
对每个 Provider 收集全部 :class:`RequirementResult` 后：

1. 存在**阻塞项**（``required=True`` 且 ``status=missing``）→ ``NEEDS_CONFIGURATION``，
   可修复（装包 / 配凭据 / 开网络）；
2. 没有阻塞项但有 ``unknown`` 项 → ``NEEDS_CONFIGURATION``（``UNKNOWN`` 绝不等于通过）；
3. 否则 → ``OK``。

``UNAVAILABLE`` 用于**结构性**不可用：1) 网络检查显示端点处于 5xx / 超过重试也无法到达，
且该端点是必需项；2) 用户把 ``require_network=True`` 而网络检查整体被跳过时。判定集中在
:func:`derive_overall_status`，避免 CLI / MCP / 测试各写一份。

「检查」与「再验证」共用实现
----------------------------
:func:`validate_environment` 是 :meth:`ConfigurationValidator.validate` 的便捷包装，
区别只在参数默认值（再验证时 ``check_network=True``），逻辑完全一致。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from researchdatahub.core.errors import ConfigError
from researchdatahub.environment.credentials.checker import CredentialChecker
from researchdatahub.environment.dependencies.checker import DependencyChecker
from researchdatahub.environment.models import (
    CheckKind,
    EnvironmentReport,
    OverallStatus,
    ProviderRequirements,
    RequirementResult,
    RequirementStatus,
)
from researchdatahub.environment.network.checker import NetworkChecker
from researchdatahub.environment.registry import (
    PROVIDER_REQUIREMENTS_REGISTRY,
    ProviderRequirementsRegistry,
    get_environment_requirements,
    normalize_provider_id,
)
from researchdatahub.environment.storage.checker import (
    DEFAULT_FREE_SPACE_FLOOR_GB,
    DiskSpaceChecker,
)

__all__ = [
    "DEFAULT_STORAGE_ROOT",
    "STRUCTURAL_FAILURE_STATUSES",
    "ConfigurationValidator",
    "derive_overall_status",
    "render_guide",
    "validate_environment",
]

DEFAULT_STORAGE_ROOT: Final[str] = "~/.rdh"
"""未显式指定存储根时使用的位置（与 ``config.models.StorageConfig.root`` 默认值一致）。"""

STRUCTURAL_FAILURE_STATUSES: Final[frozenset[int]] = frozenset({500, 501, 502, 503, 504})
"""视为「服务端结构性不可用」的 HTTP 状态码（不是本机配置能修复的）。"""


class _Clock(Protocol):
    """可注入的时钟（返回 UTC datetime）。"""

    def __call__(self) -> datetime: ...


def _utcnow() -> datetime:
    """默认时钟：UTC 当前时间。"""
    return datetime.now(UTC)


def derive_overall_status(requirements: list[RequirementResult]) -> OverallStatus:
    """根据一组检查结果推导整体状态（**纯函数**，无副作用，便于单测）。

    Parameters
    ----------
    requirements:
        该 Provider 的全部检查结果。

    Returns
    -------
    OverallStatus
        ``ok`` / ``needs_configuration`` / ``unavailable``。

    Notes
    -----
    * ``optional`` 与 ``present`` 都视为满足；
    * ``unknown`` 表示「无法判定」，一律降级为 ``needs_configuration``，不允许被忽略；
    * 结构性失败（必需端点 5xx）→ ``unavailable``，优先于统一的可修复判定。

    Examples
    --------
    >>> derive_overall_status([]).value
    'ok'
    """
    blocking = [item for item in requirements if item.blocking]
    if not blocking:
        unknown = [
            item
            for item in requirements
            if item.status is RequirementStatus.UNKNOWN and item.required
        ]
        return OverallStatus.NEEDS_CONFIGURATION if unknown else OverallStatus.OK
    structural = [item for item in blocking if _is_structural(item)]
    if structural:
        return OverallStatus.UNAVAILABLE
    return OverallStatus.NEEDS_CONFIGURATION


def _is_structural(result: RequirementResult) -> bool:
    """该阻塞项是否是「本机配置无法修复」的结构性失败。"""
    if result.kind is CheckKind.NETWORK:
        status_code = result.observed.get("status_code")
        return isinstance(status_code, int) and status_code in STRUCTURAL_FAILURE_STATUSES
    return False


class ConfigurationValidator:
    """把依赖 / 凭据 / 网络 / 磁盘四个 checker 组合成 Provider 环境报告。

    Parameters
    ----------
    registry:
        Provider 要求登记表；默认
        :data:`~researchdatahub.environment.registry.PROVIDER_REQUIREMENTS_REGISTRY`。
    dependency_checker:
        依赖检查器；``None`` 时用默认探针（真实 ``find_spec``）。
    credential_checker:
        凭据检查器；``None`` 时读 ``os.environ`` + 真实 keyring。
    network_checker:
        网络检查器；``None`` 时新建（默认 5 秒超时）。注入 MockTransport 即可完全离线。
    sources_path:
        可选：``configs/sources.yaml`` 路径。给出时会读取其中的 ``credentials:`` 引用，
        在报告中追加一条「配置文件凭据引用」检查（不展开 env、不取值）。
    redirect_probe_failure:
        把网络探测里的「连接类失败」（超时/DNS）降级为 ``unknown`` 而不是 ``missing``
        ——某些环境禁止出网探测，此时「连不上」不能断定服务不可用。默认 ``False``（如实报 missing）。
    clock:
        注入时钟（报告里的 ``checked_at``）。

    Examples
    --------
    >>> validator = ConfigurationValidator(
    ...     dependency_checker=DependencyChecker(probe=lambda m: True),
    ...     credential_checker=CredentialChecker(environ={"CDSAPI_KEY": "x"}),
    ... )
    >>> report = validator.validate("cds", check_network=False)
    >>> report.provider, report.schema_version
    ('cds', '1.0')
    """

    def __init__(
        self,
        *,
        registry: ProviderRequirementsRegistry | None = None,
        dependency_checker: DependencyChecker | None = None,
        credential_checker: CredentialChecker | None = None,
        network_checker: NetworkChecker | None = None,
        sources_path: str | Path | None = None,
        redirect_probe_failure: bool = False,
        clock: _Clock = _utcnow,
    ) -> None:
        self._registry = PROVIDER_REQUIREMENTS_REGISTRY if registry is None else registry
        self._dependencies = dependency_checker or DependencyChecker()
        self._credentials = credential_checker or CredentialChecker()
        self._network = network_checker or NetworkChecker()
        self._sources_path = Path(sources_path) if sources_path is not None else None
        self._redirect_probe_failure = redirect_probe_failure
        self._clock = clock

    # -- 公开 API ---------------------------------------------------------- #
    @property
    def registry(self) -> ProviderRequirementsRegistry:
        """使用的登记表（只读）。"""
        return self._registry

    def validate(
        self,
        provider: str,
        *,
        storage_root: str | Path | None = None,
        free_space_floor_gb: float = DEFAULT_FREE_SPACE_FLOOR_GB,
        check_network: bool = False,
        timeout: float | None = None,
    ) -> EnvironmentReport:
        """检查单个 Provider 的环境，返回 :class:`EnvironmentReport`。

        Parameters
        ----------
        provider:
            provider id（见 :data:`~researchdatahub.environment.registry.PROVIDER_IDS`）。
        storage_root:
            存储根；``None`` 表示不检查磁盘（Provider 本身与本地磁盘无关时使用）。
        free_space_floor_gb:
            剩余空间下限。
        check_network:
            是否做网络探测；默认 ``False``（检查应当快且离线可用，再验证时才打开）。
        timeout:
            覆盖网络探测超时（秒），仅在本实例自带 network_checker 时生效。

        Returns
        -------
        EnvironmentReport
            含逐条 ``requirements`` 与推导出的整体 ``status``；未知 provider 时
            ``status=unavailable``、``errors`` 里说明原因（**不抛异常**，便于 MCP 直接返回）。
        """
        requirements = self._registry.get(provider)
        if requirements is None:
            return self._unknown_provider_report(provider)

        started = time.perf_counter()
        results: list[RequirementResult] = []
        checks_run: list[str] = []

        results.extend(self._dependencies.check(requirements.dependencies))
        checks_run.append("dependencies")

        results.extend(self._credentials.check_credentials(requirements.credentials))
        checks_run.append("credentials")

        sources_result = self._sources_credentials_result(provider, requirements)
        if sources_result is not None:
            results.append(sources_result)
            checks_run.append("sources-credentials")

        if check_network:
            network_checker = self._network_for(timeout)
            endpoint_results = network_checker.check(requirements.endpoints)
            if self._redirect_probe_failure:
                endpoint_results = [_soften_connect_failure(item) for item in endpoint_results]
            results.extend(endpoint_results)
            checks_run.append("network")

        if storage_root is not None:
            storage = DiskSpaceChecker(
                storage_root, free_space_floor_gb=free_space_floor_gb
            ).check()
            results.append(storage)
            checks_run.append("storage")

        status = derive_overall_status(results)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return EnvironmentReport(
            provider=requirements.provider,
            display_name=requirements.display_name,
            status=status,
            requirements=results,
            summary=self._summarize(requirements, results, status, check_network),
            checked_at=self._clock().isoformat(),
            checks_run=checks_run,
            duration_ms=elapsed_ms,
        )

    def validate_many(
        self,
        providers: list[str] | tuple[str, ...] | None = None,
        **kwargs: object,
    ) -> list[EnvironmentReport]:
        """批量检查（默认检查登记表里的全部 Provider）。

        Notes
        -----
        ``kwargs`` 原样透传给 :meth:`validate`；不要在这里传入与单 Provider 冲突的参数。
        """
        selected = self._registry.provider_ids if providers is None else tuple(providers)
        # kwargs 的类型在运行时由 validate 的正关键字参数约束，这里显式收窄以通过 mypy strict。
        storage_root = kwargs.get("storage_root")
        return [
            self.validate(
                provider,
                storage_root=storage_root if isinstance(storage_root, (str, Path)) else None,
                check_network=bool(kwargs.get("check_network", False)),
            )
            for provider in selected
        ]

    # -- 内部 -------------------------------------------------------------- #
    def _network_for(self, timeout: float | None) -> NetworkChecker:
        if timeout is None:
            return self._network
        return NetworkChecker(
            timeout=timeout,
            client=self._network._client if isinstance(self._network, NetworkChecker) else None,
            follow_redirects=True,
        )

    def _sources_credentials_result(
        self, provider: str, requirements: ProviderRequirements
    ) -> RequirementResult | None:
        """检查 ``configs/sources.yaml`` 里该 Provider 对应的数据源凭据引用是否存在。

        只做两件事：读 YAML 结构、把引用交给 :class:`CredentialChecker` 判定存在性。
        **不展开 env**，因此不会把真实值带进内存。
        """
        if self._sources_path is None or not requirements.source_id:
            return None
        from researchdatahub.environment.credentials.checker import (
            load_credential_references,
        )

        try:
            mapping = load_credential_references(self._sources_path)
        except (OSError, ConfigError) as exc:
            return RequirementResult(
                name=f"sources.yaml:{requirements.source_id}",
                kind=CheckKind.CREDENTIAL,
                status=RequirementStatus.UNKNOWN,
                required=False,
                guidance=(
                    f"无法读取 {self._sources_path}（{type(exc).__name__}）："
                    "该文件缺失时 RDH 使用内置默认声明；如需自定义请 "
                    "cp configs/sources.example.yaml configs/sources.yaml。"
                ),
                observed={"path": str(self._sources_path)},
            )
        except Exception as exc:
            return RequirementResult(
                name=f"sources.yaml:{requirements.source_id}",
                kind=CheckKind.CREDENTIAL,
                status=RequirementStatus.UNKNOWN,
                required=False,
                guidance=(
                    f"{self._sources_path} 解析失败（{type(exc).__name__}）；请修正 YAML 语法。"
                ),
                observed={"path": str(self._sources_path)},
            )
        references = mapping.get(requirements.source_id, {})
        if not references:
            return RequirementResult(
                name=f"sources.yaml:{requirements.source_id}",
                kind=CheckKind.CREDENTIAL,
                status=RequirementStatus.OPTIONAL,
                required=False,
                guidance=(
                    f"{self._sources_path} 中没有 {requirements.source_id} 的 credentials 段；"
                    "使用内置声明即可。"
                ),
                observed={"path": str(self._sources_path), "references": 0},
            )
        results = [
            self._credentials.check_reference(
                key, reference, required=False, purpose="来自 sources.yaml 的声明"
            )
            for key, reference in references.items()
        ]
        missing = [item for item in results if item.status is RequirementStatus.OPTIONAL]
        guidance = "" if not missing else "；".join(item.guidance for item in missing)
        return RequirementResult(
            name=f"sources.yaml:{requirements.source_id}",
            kind=CheckKind.CREDENTIAL,
            status=RequirementStatus.PRESENT if not missing else RequirementStatus.OPTIONAL,
            required=False,
            guidance=guidance,
            observed={
                "path": str(self._sources_path),
                "references": len(references),
                "missing": len(missing),
            },
        )

    def _summarize(
        self,
        requirements: ProviderRequirements,
        results: list[RequirementResult],
        status: OverallStatus,
        check_network: bool,
    ) -> str:
        """构造中文一句话结论 + 下一步动作（``OK`` 时为空串）。"""
        if status is OverallStatus.OK:
            return ""
        blocking = [item for item in results if item.blocking]
        unknown = [
            item for item in results if item.status is RequirementStatus.UNKNOWN and item.required
        ]
        if status is OverallStatus.UNAVAILABLE:
            names = "、".join(item.name for item in blocking)
            return (
                f"{requirements.display_name} 当前不可用（结构性故障）：{names}。"
                "这不是本机配置能修复的，请稍后重试或查看服务状态页。"
            )
        parts: list[str] = []
        if blocking:
            parts.append(f"缺少 {len(blocking)} 项必需配置：{'、'.join(i.name for i in blocking)}")
        if unknown:
            parts.append(f"{len(unknown)} 项无法判定：{'、'.join(i.name for i in unknown)}")
        if not check_network:
            parts.append("未做网络探测（check_network=False）")
        parts.append(
            "下一步：按 guidance 逐条修复，然后重新执行 "
            f"validate_environment('{requirements.provider}', check_network=True)"
        )
        return "；".join(parts) + "。"

    def _unknown_provider_report(self, provider: str) -> EnvironmentReport:
        normalized = normalize_provider_id(provider)
        return EnvironmentReport(
            provider=normalized or "unknown",
            display_name="",
            status=OverallStatus.UNAVAILABLE,
            requirements=[],
            summary=(
                f"未知的 provider {provider!r}；已登记：{', '.join(self._registry.provider_ids)}。"
            ),
            checked_at=self._clock().isoformat(),
            checks_run=[],
            duration_ms=0.0,
            errors=[
                f"unknown provider {provider!r}; known providers: "
                f"{', '.join(self._registry.provider_ids)}"
            ],
        )


def _soften_connect_failure(result: RequirementResult) -> RequirementResult:
    """把「连接类失败」降级为 ``unknown``（保留原始观测值，便于人工判断）。"""
    if result.kind is not CheckKind.NETWORK:
        return result
    error = result.observed.get("error")
    if error is None:
        return result
    return result.model_copy(
        update={
            "status": RequirementStatus.UNKNOWN if result.required else RequirementStatus.OPTIONAL,
            "guidance": result.guidance + "（本环境禁止出网探测时，此项不代表服务不可用）",
        }
    )


def validate_environment(
    provider: str,
    *,
    storage_root: str | Path | None = DEFAULT_STORAGE_ROOT,
    free_space_floor_gb: float = DEFAULT_FREE_SPACE_FLOOR_GB,
    check_network: bool = True,
    timeout: float | None = None,
    environ: Mapping[str, str] | None = None,
    sources_path: str | Path | None = None,
    validator: ConfigurationValidator | None = None,
) -> EnvironmentReport:
    """**配置完成后的再验证**：依赖 + 凭据存在性 + 网络连通性 + 磁盘，共用同一实现。

    与「检查」的区别只是参数默认值——它默认开启网络探测（再验证时最关心「现在通不通」），
    其余逻辑完全走 :meth:`ConfigurationValidator.validate`，不存在第二套判定。

    Parameters
    ----------
    provider:
        provider id。
    storage_root:
        存储根；默认 ``~/.rdh``。传 ``None`` 跳过磁盘检查。
    free_space_floor_gb:
        剩余空间下限（GiB）。
    check_network:
        是否探测端点；默认 ``True``。
    timeout:
        网络探测超时（秒），默认 5。
    environ:
        环境变量来源；默认 ``os.environ``。测试传显式 dict。
    sources_path:
        可选的 ``configs/sources.yaml``，用于核对配置里的凭据引用。
    validator:
        注入一个构造好的 :class:`ConfigurationValidator`（测试用；给出时忽略其它检查器相关参数）。

    Returns
    -------
    EnvironmentReport
        含逐条结果与整体 ``status``；永远不抛异常（未知 provider 也会得到报告）。

    Examples
    --------
    >>> report = validate_environment("cds", check_network=False, storage_root=None)
    >>> report.provider
    'cds'
    >>> report.ok in (True, False)
    True
    """
    active = validator
    if active is None:
        active = ConfigurationValidator(
            credential_checker=CredentialChecker(environ),
            sources_path=sources_path,
        )
    return active.validate(
        provider,
        storage_root=storage_root,
        free_space_floor_gb=free_space_floor_gb,
        check_network=check_network,
        timeout=timeout,
    )


def render_guide(provider: str) -> str:
    """返回某 Provider 的可操作配置引导（Markdown 文本），供 CLI 直接打印。"""
    return get_environment_requirements(provider).render_markdown()
