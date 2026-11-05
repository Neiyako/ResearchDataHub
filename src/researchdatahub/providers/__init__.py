"""Provider 层：面向 AI Coding Agent 的「数据集目录 + 能力」抽象（Provider Contract v1）。

这一层回答 Agent 的前置问题
---------------------------
「**有什么数据集、它真的有什么变量/覆盖范围、这个平台到底支持哪些操作**」——
发生在构造 :class:`~researchdatahub.core.models.DatasetRequest` **之前**。

与 ``sources/`` 的分工（ADR-0008）
---------------------------------
=============== ================================== ============================
层               包                                  职责
=============== ================================== ============================
目录与能力        ``providers/``（本包）              search / inspect / capabilities
资产发现与执行    ``sources/``                       DatasetRequest → SourceAsset
=============== ================================== ============================

两层**互不 import**：本包只依赖 :mod:`researchdatahub.core`，不依赖 ``sources``
（见 ``docs/providers/*.md`` 与 ``docs/decisions/ADR-0008-providers-vs-sources.md``）。

已实现的 Provider（均基于真实 API，2026-11-05 实测）
---------------------------------------------------
* :class:`~researchdatahub.providers.earthdata.provider.EarthdataProvider`
  —— NASA Earthdata（CMR：``collections.json`` / ``collections.umm_json`` /
  ``granules.json`` / ``variables.umm_json``，匿名可读）。
* :class:`~researchdatahub.providers.copernicus.provider.CopernicusProvider`
  —— Copernicus Data Space Ecosystem（STAC ``/stac/collections``、``/stac/search`` 与
  OData ``/odata/v1/Products``，匿名可读；下载需 Bearer，本机未验证）。
  **注意与 CDS（ECMWF）不是同一平台**。

MCP 层调用示例
--------------
.. code-block:: python

    from researchdatahub.providers import (
        DatasetFilter,
        get_provider,
        get_provider_capabilities,
        list_providers,
    )

    # get_provider_capabilities：纯内存，不发网络请求
    for capability in get_provider_capabilities():
        print(capability.provider_id, capability.implemented, capability.unsupported)

    with get_provider("earthdata") as provider:
        candidates = provider.search("precipitation", DatasetFilter(provider="GES_DISC"))
        detail = provider.inspect(candidates[0].dataset_id)
        variables = provider.get_variables(candidates[0].dataset_id)

不可虚构的保证
--------------
* 能力只有三种如实状态：``verified`` / ``unverified`` / ``unsupported``。
* 未实现的方法**抛** :class:`~researchdatahub.core.errors.NotImplementedFeatureError`，
  且必须同时出现在 ``capabilities().unsupported`` 里（:func:`assert_provider_contract`
  交叉校验）。不存在"返回空结果冒充成功"的路径。
"""

from researchdatahub.providers.base import (
    CAPABILITY_CONTRACT_VERSION,
    CONTRACT_METHODS,
    CREDENTIAL_FEATURES,
    CapabilityStatus,
    DatasetCandidate,
    DatasetDetail,
    DatasetFilter,
    DatasetVariable,
    ProviderCapabilities,
    ProviderContract,
    QueryTranslation,
    RequestEstimate,
    RequestValidation,
    TaskHandle,
    TaskStatusSnapshot,
    assert_provider_contract,
    probe_unsupported,
)
from researchdatahub.providers.registry import (
    DEFAULT_PROVIDER_IDS,
    PROVIDER_FACTORIES,
    Registry,
    build_default_registry,
    get_provider_capabilities,
)

__all__ = [
    "CAPABILITY_CONTRACT_VERSION",
    "CONTRACT_METHODS",
    "CREDENTIAL_FEATURES",
    "DEFAULT_PROVIDER_IDS",
    "PROVIDER_FACTORIES",
    "CapabilityStatus",
    "DatasetCandidate",
    "DatasetDetail",
    "DatasetFilter",
    "DatasetVariable",
    "Provider",
    "ProviderCapabilities",
    "ProviderContract",
    "QueryTranslation",
    "Registry",
    "RequestEstimate",
    "RequestValidation",
    "TaskHandle",
    "TaskStatusSnapshot",
    "assert_provider_contract",
    "build_default_registry",
    "get_provider",
    "get_provider_capabilities",
    "list_providers",
    "probe_unsupported",
]

Provider = ProviderContract
"""``Provider`` 是 :class:`ProviderContract` 的别名（MCP 层的公开叫法）。"""


def list_providers() -> tuple[str, ...]:
    """列出已注册的 Provider ID（稳定顺序，**不发网络请求**）。

    Returns
    -------
    tuple[str, ...]
        形如 ``("earthdata", "copernicus")``。
    """
    return DEFAULT_PROVIDER_IDS


def get_provider(
    provider_id: str,
    *,
    registry: Registry | None = None,
    **options: object,
) -> ProviderContract:
    """按 ID 构造 Provider（工厂函数签名见 :mod:`researchdatahub.providers.registry`）。

    Parameters
    ----------
    provider_id:
        ``"earthdata"`` / ``"copernicus"``（见 :func:`list_providers`）。
    registry:
        可选注册表（自定义或测试用）；``None`` 时用默认注册表。
    **options:
        透传给工厂的选项（如离线测试的 ``base_url`` / ``stac_root``）。

    Raises
    ------
    SourceNotFoundError
        ID 未注册。
    ConfigError
        传入了工厂不接受的选项。

    Notes
    -----
    返回的 Provider 需要调用方 ``close()``（或用作上下文管理器）。通过 ``**options``
    注入 ``http_client`` 等对象时，请同时保留对它的引用以便自行关闭。
    """
    active = registry if registry is not None else build_default_registry()
    return active.create(provider_id, **options)
