"""Provider 要求登记表：每个数据源**真实**需要的依赖、凭据、网络端点。

设计原则
--------
1. **只声明引用，不存值**：凭据一律写成
   :class:`~researchdatahub.environment.models.CredentialReference`
   （``env:`` / ``file:`` / ``keyring:``），登记表里不存在任何密钥字面量。
   单元测试
   ``test_env_registry.py::test_registry_contains_no_secret_like_literals``
   会扫描全部声明，确认没有疑似密钥的字符串混进来。
2. **声明必须有出处**：每条 URL / 变量名都来自官方文档或本项目 ``configs/sources*.yaml``；
   拿不准的写进 ``notes``，不臆造。
3. **可选项显式标注**：``required=False`` 的依赖在报告里是 ``optional`` 而非 ``missing``，
   避免把「HTTPS 路径用不到 boto3」误报成环境坏掉。

引导步骤（:func:`get_environment_requirements`）
------------------------------------------------
每步必须同时给出「做什么 / 为什么 / 去哪拿或存哪（真实 URL 与路径）/ 怎么验证」，
否则用户仍然不知道要动手改什么。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from researchdatahub.environment.models import (
    ConfigurationGuide,
    CredentialKind,
    CredentialReference,
    CredentialRequirement,
    DependencyRequirement,
    EndpointRequirement,
    GuidanceStep,
    ProviderRequirements,
)

__all__ = [
    "CDSAPI_RC_TEMPLATE",
    "CDS_DOCS_URL",
    "CDS_HOW_TO_API_URL",
    "CDS_LICENSE_URL",
    "CDS_REGISTER_URL",
    "CONDA_FORGE_EE_URL",
    "GEE_AUTH_URL",
    "GEE_PROJECT_URL",
    "GEE_REGISTER_URL",
    "KEYRING_CREDENTIAL_URL",
    "NASA_EARTHDATA_APPROVE_URL",
    "NASA_EARTHDATA_CREDENTIAL_URL",
    "NASA_EARTHDATA_LOGIN_URL",
    "NASA_EARTHDATA_PROFILE_URL",
    "NASA_EARTHDATA_RESOURCE",
    "NCAR_DATA_ACCESS_URL",
    "NCAR_DATA_BASE_URL",
    "NCAR_REGISTER_URL",
    "PROVIDER_IDS",
    "PROVIDER_REQUIREMENTS_REGISTRY",
    "RDH_HOME_DIR",
    "RDH_KEYRING_SERVICE",
    "SHAPPS_DATASPACE_URL",
    "UNKNOWN_PROVIDER_ID",
    "ProviderRequirementsRegistry",
    "get_environment_requirements",
    "get_requirements",
    "normalize_provider_id",
]


# --------------------------------------------------------------------------- #
# 官方文档 / 获取凭据的真实 URL
# --------------------------------------------------------------------------- #
CDS_REGISTER_URL: Final[str] = "https://cds.climate.copernicus.eu/user/register"
CDS_HOW_TO_API_URL: Final[str] = "https://cds.climate.copernicus.eu/how-to-api"
CDS_LICENSE_URL: Final[str] = (
    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels?tab=download"
)
CDS_DOCS_URL: Final[str] = "https://cds.climate.copernicus.eu/api"
CDSAPI_RC_TEMPLATE: Final[str] = "~/.cdsapirc"
"""cdsapi 自带的凭据文件位置（HTTP 网关路径下的 **可选** 来源；RDH 首选 ``.netrc``）。"""

NCAR_REGISTER_URL: Final[str] = "https://rda.ucar.edu/cgi-bin/login?gotopage=/data/access/"
"""NCAR RDA 免费注册页（登录后可生成 API key）。"""

NCAR_DATA_ACCESS_URL: Final[str] = "https://rda.ucar.edu/data/access/"
NCAR_DATA_BASE_URL: Final[str] = "https://data.rda.ucar.edu"

NASA_EARTHDATA_LOGIN_URL: Final[str] = "https://urs.earthdata.nasa.gov/"
NASA_EARTHDATA_PROFILE_URL: Final[str] = "https://urs.earthdata.nasa.gov/profile"
NASA_EARTHDATA_APPROVE_URL: Final[str] = (
    "https://urs.earthdata.nasa.gov/approve_app?client_id=e2WVk8Pw6weeLUKZYOxvTQ"
)
"""在 URS 里为「NASA GESDISC DATA ARCHIVE」应用授权（批准后才能读取 GES DISC 数据）。"""

NASA_EARTHDATA_RESOURCE: Final[str] = "NASA GESDISC DATA ARCHIVE"

NASA_EARTHDATA_CREDENTIAL_URL: Final[str] = (
    "https://urs.earthdata.nasa.gov/documentation/for_users/user_token"
)
"""Earthdata 个人令牌（Bearer token）的官方说明页。"""

SHAPPS_DATASPACE_URL: Final[str] = (
    "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings"
)
"""Copernicus Data Space 的 OAuth 客户端管理页（生成访问令牌）。"""

GEE_REGISTER_URL: Final[str] = "https://earthengine.google.com/signup/"
GEE_PROJECT_URL: Final[str] = "https://console.cloud.google.com/"
RDH_KEYRING_SERVICE: Final[str] = "researchdatahub"
"""RDH 自己在系统钥匙串里使用的 service 名（``keyring.set_password("researchdatahub", …)``）。"""

GEE_AUTH_URL: Final[str] = "https://developers.google.com/earth-engine/guides/auth"
CONDA_FORGE_EE_URL: Final[str] = "https://anaconda.org/conda-forge/earthengine-api"
KEYRING_CREDENTIAL_URL: Final[str] = "https://pypi.org/project/keyring/"

#: 用户配置目录（只读探测，永不回显文件内容）。
RDH_HOME_DIR: Final[str] = "~/.rdh"
NETRC_FILE: Final[str] = "~/.netrc"
CDSAPI_RC_FILE: Final[str] = CDSAPI_RC_TEMPLATE

# --------------------------------------------------------------------------- #
# 依赖声明（模块名 → PyPI 包名）
# --------------------------------------------------------------------------- #
_HTTPX = DependencyRequirement(
    module="httpx",
    package="httpx",
    required=True,
    purpose="RDH 的传输层与网络探测都基于 httpx；缺它整个包无法运行",
)
_NETCDF4 = DependencyRequirement(
    module="netCDF4",
    package="netCDF4",
    required=True,
    purpose="NetCDF 完整性校验（可打开性检查）需要它读取文件头",
)
_XARRAY = DependencyRequirement(
    module="xarray",
    package="xarray",
    required=True,
    purpose="科学元数据校验（变量/时间覆盖）依赖 xarray 打开数据集",
)
_CDSAPI = DependencyRequirement(
    module="cdsapi",
    package="cdsapi",
    required=True,
    purpose="CDS API 请求（submit → poll → 下载 URL）只通过官方 cdsapi 客户端发起",
)
_BOTO3 = DependencyRequirement(
    module="boto3",
    package="boto3",
    required=False,
    purpose="仅 S3 访问路径需要；NCAR 的 HTTPS 路径用不到它",
)
_EE = DependencyRequirement(
    module="ee",
    package="earthengine-api",
    required=True,
    purpose="Google Earth Engine 的 Python 客户端（导入名是 ee，不是 earthengine）",
)
_KERYPTING = DependencyRequirement(
    module="keyring",
    package="keyring",
    required=False,
    purpose="可选的系统钥匙串存取；不装也能用环境变量或凭据文件",
)

# --------------------------------------------------------------------------- #
# 凭据引用
# --------------------------------------------------------------------------- #
_CDS_KEY_ENV = CredentialReference(kind=CredentialKind.ENV, name="CDSAPI_KEY")
_CDS_KEY_RC = CredentialReference(kind=CredentialKind.FILE, name=CDSAPI_RC_FILE)
_CDS_URL_ENV = CredentialReference(kind=CredentialKind.ENV, name="CDSAPI_URL")
_CDS_URL_RC = CredentialReference(kind=CredentialKind.FILE, name=CDSAPI_RC_FILE)

_NCAR_KEY_ENV = CredentialReference(kind=CredentialKind.ENV, name="RDA_API_KEY")
_NCAR_KEY_NETRC = CredentialReference(kind=CredentialKind.FILE, name=NETRC_FILE)

_EARTHDATA_ENV = CredentialReference(kind=CredentialKind.ENV, name="EARTHDATA_TOKEN")
_EARTHDATA_NETRC = CredentialReference(kind=CredentialKind.FILE, name=NETRC_FILE)

_DATASPACE_KEY_ENV = CredentialReference(kind=CredentialKind.ENV, name="CDSAPI_KEY")
_DATASPACE_KEY_RC = CredentialReference(kind=CredentialKind.FILE, name=CDSAPI_RC_FILE)

_GEE_CRED_FILE = CredentialReference(
    kind=CredentialKind.FILE, name="~/.config/earthengine/credentials"
)
_GEE_PROJECT_ENV = CredentialReference(kind=CredentialKind.ENV, name="EE_PROJECT")


# --------------------------------------------------------------------------- #
# 五个 Provider 的真实要求
# --------------------------------------------------------------------------- #
_CDS = ProviderRequirements(
    provider="cds",
    display_name="Copernicus Climate Data Store (CDS)",
    source_id="cds",
    dependencies=[_HTTPX, _NETCDF4, _XARRAY, _CDSAPI, _KERYPTING],
    credentials=[
        CredentialRequirement(
            key="key",
            reference=_CDS_KEY_ENV,
            required=True,
            purpose="CDS API 个人访问令牌（Personal Access Token），请求 ERA5 时用于认证",
            how_to_obtain=CDS_HOW_TO_API_URL,
        ),
        CredentialRequirement(
            key="key",
            reference=_CDS_KEY_RC,
            required=True,
            purpose="cdsapi 官方客户端读取的凭据文件；RDH 用 cdsapi 发起请求，所以它必须存在",
            how_to_obtain=CDS_HOW_TO_API_URL,
        ),
        CredentialRequirement(
            key="url",
            reference=_CDS_URL_ENV,
            required=False,
            purpose="CDS API 根地址（公开地址，不是密钥）；默认 https://cds.climate.copernicus.eu/api",
            how_to_obtain=CDS_DOCS_URL,
        ),
        CredentialRequirement(
            key="url",
            reference=_CDS_URL_RC,
            required=False,
            purpose="凭据文件中的 url 行；缺省时 cdsapi 使用官方默认地址",
            how_to_obtain=CDS_HOW_TO_API_URL,
        ),
    ],
    endpoints=[
        EndpointRequirement(
            name="cds-api",
            url="https://cds.climate.copernicus.eu/api/retrieve/v1/",
            method="GET",
            required=True,
            purpose="CDS 异步检索入口连通性（只读 GET，不提交任何请求）",
        ),
    ],
    notes=[
        "CDS 的 RDH 集成走 cdsapi，因此凭据必须**同时**出现在 ~/.cdsapirc 里；"
        "CDSAPI_KEY 环境变量只是可选的辅助来源。",
        "在 CDS 网站登录后需要先接受对应数据集的许可协议（Licence），否则提交请求会 403。",
    ],
)

_NCAR = ProviderRequirements(
    provider="ncar",
    display_name="NSF NCAR Research Data Archive (GDEX)",
    source_id="ncar",
    dependencies=[_HTTPX, _NETCDF4, _XARRAY, _BOTO3, _KERYPTING],
    credentials=[
        CredentialRequirement(
            key="api_key",
            reference=_NCAR_KEY_ENV,
            required=False,
            purpose="RDA 个人 API key；**公开数据集不需要**，受限数据集或大批量下载才用",
            how_to_obtain=NCAR_REGISTER_URL,
        ),
        CredentialRequirement(
            key="api_key",
            reference=_NCAR_KEY_NETRC,
            required=False,
            purpose="curl/wget 风格的 ~/.netrc 凭据（machine data.rda.ucar.edu login …）",
            how_to_obtain=NCAR_DATA_ACCESS_URL,
        ),
    ],
    endpoints=[
        EndpointRequirement(
            name="rda-web",
            url="https://rda.ucar.edu/",
            method="HEAD",
            required=True,
            purpose="RDA 门户可达性（GDEX 目录查询的前置条件）",
        ),
        EndpointRequirement(
            name="rda-datasets-api",
            url="https://rda.ucar.edu/api/datasets/ds633.0",
            method="GET",
            required=True,
            purpose="GDEX RDA Datasets API：RDH 用它在发现阶段查数据集元数据",
        ),
        EndpointRequirement(
            name="data-server",
            url=f"{NCAR_DATA_BASE_URL}/",
            method="HEAD",
            required=True,
            purpose="GDEX 数据文件服务器：批量下载走这里",
        ),
    ],
    notes=[
        "NDA 公有数据（ds633.0 / ds633.1 等）匿名即可下载，不要求 API key。",
        "受限数据集必须先登录 https://rda.ucar.edu 接受条款并生成 API key。",
    ],
)

_NASA_EARTHDATA = ProviderRequirements(
    provider="nasa_earthdata",
    display_name="NASA Earthdata (GES DISC)",
    source_id=None,
    dependencies=[_HTTPX, _NETCDF4, _XARRAY, _KERYPTING],
    credentials=[
        CredentialRequirement(
            key="token",
            reference=_EARTHDATA_ENV,
            required=True,
            purpose=(
                "Earthdata Login 个人令牌（Bearer token）；GES DISC 的 OPeNDAP 与 HTTPS 下载用它"
            ),
            how_to_obtain=NASA_EARTHDATA_CREDENTIAL_URL,
        ),
        CredentialRequirement(
            key="netrc_login",
            reference=_EARTHDATA_NETRC,
            required=False,
            purpose=(
                "~/.netrc 里的 Earthdata Login 账号（OPeNDAP 客户端无法用 Bearer token 时使用）"
            ),
            how_to_obtain="https://urs.earthdata.nasa.gov/",
        ),
    ],
    endpoints=[
        EndpointRequirement(
            name="earthdata-login",
            url="https://urs.earthdata.nasa.gov/",
            method="HEAD",
            required=True,
            purpose="Earthdata Login 服务可达性",
        ),
        EndpointRequirement(
            name="ges-disc",
            url="https://goldsmr4.gesdisc.eosdis.nasa.gov/opendap/",
            method="GET",
            required=True,
            purpose="GES DISC OPeNDAP 入口：RDH 从这里取 MERRA-2 等数据集目录",
        ),
    ],
    notes=[
        "必须先在 URS 的 profile → Applications → Approved Apps 里批准 NASA GESDISC DATA ARCHIVE，"
        "否则即使账号正确，读取也会被 401/403 拒绝。",
        "NASA 明确要求不得在聊天窗口/issue 里粘贴令牌；令牌只在本地写入环境变量或 ~/.netrc。",
    ],
)

_COPERNICUS_DATASPACE = ProviderRequirements(
    provider="copernicus_datasphere",
    display_name="Copernicus Data Space Ecosystem",
    source_id=None,
    dependencies=[
        DependencyRequirement(
            module="httpx",
            package="httpx",
            required=True,
            purpose="Data Space 的 OData 目录与 S3 签名请求都用 httpx 发起",
        ),
        DependencyRequirement(
            module="boto3",
            package="boto3",
            required=False,
            purpose="S3 兼容访问路径需要；纯 OData/HTTPS 路径用不到",
        ),
        _NETCDF4,
        _KERYPTING,
    ],
    credentials=[
        CredentialRequirement(
            key="access_token",
            reference=CredentialReference(kind=CredentialKind.ENV, name="CDSE_TOKEN"),
            required=True,
            purpose="Data Space Sentinel Hub / OAuth 个人令牌；不设过期时间，适合脚本",
            how_to_obtain=SHAPPS_DATASPACE_URL,
        ),
        CredentialRequirement(
            key="netrc_login",
            reference=CredentialReference(
                kind=CredentialKind.KEYRING,
                name="username",
                service=RDH_KEYRING_SERVICE,
            ),
            required=False,
            purpose="可选的系统钥匙串保存账号；存在时用它换取短期 S3 密钥",
            how_to_obtain="https://dataspace.copernicus.eu/",
        ),
    ],
    endpoints=[
        EndpointRequirement(
            name="dataspace-portal",
            url="https://dataspace.copernicus.eu/",
            method="HEAD",
            required=True,
            purpose="Data Space 门户可达性",
        ),
        EndpointRequirement(
            name="catalogue-odata",
            url="https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$top=1",
            method="GET",
            required=True,
            purpose="OData 产品目录：RDH 用它发现 Sentinel 数据产品",
        ),
    ],
    notes=[
        "Copernicus Data Space 与 CDS（climate.copernicus.eu）是**两个不同系统**，账号不通用，"
        "但两者都接受名为 CDSAPI_KEY 的令牌变量，因此这里复用同名变量以避免与 cdsapi 冲突。",
        "S3 访问需要先用 OAuth 换取短期凭证（STS），本项目尚未实现该换算，报告里如实标注 "
        "'s3 路径未实现'。",
    ],
)

_GEE = ProviderRequirements(
    provider="gee",
    display_name="Google Earth Engine (Python client)",
    source_id=None,
    dependencies=[_HTTPX, _EE],
    credentials=[
        CredentialRequirement(
            key="credentials",
            reference=_GEE_CRED_FILE,
            required=True,
            purpose="earthengine authenticate 生成的 OAuth 凭据文件（服务账号或用户凭据）",
            how_to_obtain=GEE_AUTH_URL,
        ),
        CredentialRequirement(
            key="project",
            reference=_GEE_PROJECT_ENV,
            required=False,
            purpose="Cloud 项目 ID（ee.Initialize(project=…)；新账号必须显式指定）",
            how_to_obtain=GEE_PROJECT_URL,
        ),
    ],
    endpoints=[
        EndpointRequirement(
            name="ee-api",
            url="https://earthengine.googleapis.com/",
            method="HEAD",
            required=True,
            purpose="Earth Engine REST API 可达性",
        ),
    ],
    notes=[
        "GEE 需要先注册（免费但需审批）并启用 Earth Engine API，"
        "否则 ee.Initialize() 会报权限错误。",
        "凭据文件**不得**提交到仓库；earthengine-api 通过 conda-forge/pip 安装。",
    ],
)

PROVIDER_IDS: Final[tuple[str, ...]] = (
    "cds",
    "ncar",
    "nasa_earthdata",
    "copernicus_datasphere",
    "gee",
)
"""登记表中全部 provider id，顺序固定（报告与 CLI 输出按此顺序）。"""

UNKNOWN_PROVIDER_ID: Final[str] = "unknown"
"""未知 provider 在报告里的 provider 字段值。"""

_ENTRIES: Final[Mapping[str, ProviderRequirements]] = MappingProxyType(
    {item.provider: item for item in (_CDS, _NCAR, _NASA_EARTHDATA, _COPERNICUS_DATASPACE, _GEE)}
)


class ProviderRequirementsRegistry:
    """只读的 Provider 要求登记表。

    Examples
    --------
    >>> registry = ProviderRequirementsRegistry()
    >>> registry.get("cds").display_name
    'Copernicus Climate Data Store (CDS)'
    >>> registry.provider_ids
    ('cds', 'ncar', 'nasa_earthdata', 'copernicus_datasphere', 'gee')
    """

    def __init__(self) -> None:
        self._entries: Mapping[str, ProviderRequirements] = _ENTRIES

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """全部 provider id（固定顺序）。"""
        return PROVIDER_IDS

    def get(self, provider: str) -> ProviderRequirements | None:
        """按 id 取要求声明；大小写与 ``-``/``_`` 差异会被归一化。不存在返回 ``None``。"""
        return self._entries.get(normalize_provider_id(provider))

    def require(self, provider: str) -> ProviderRequirements:
        """按 id 取要求声明，不存在时抛 :class:`KeyError`（消息里列出合法 id）。

        Raises
        ------
        KeyError
            provider 未登记。
        """
        normalized = normalize_provider_id(provider)
        entry = self._entries.get(normalized)
        if entry is None:
            raise KeyError(
                f"unknown provider {provider!r}; known providers: {', '.join(PROVIDER_IDS)}"
            )
        return entry

    def all(self) -> tuple[ProviderRequirements, ...]:
        """全部要求声明（固定顺序）。"""
        return tuple(self._entries[provider_id] for provider_id in PROVIDER_IDS)

    def __contains__(self, provider: object) -> bool:
        """``"cds" in registry`` 语义（不区分大小写）。"""
        return isinstance(provider, str) and normalize_provider_id(provider) in self._entries

    def __len__(self) -> int:
        """登记表条目数。"""
        return len(self._entries)


PROVIDER_REQUIREMENTS_REGISTRY: Final[ProviderRequirementsRegistry] = ProviderRequirementsRegistry()
"""默认登记表实例（无状态，可全局共享）。"""


def normalize_provider_id(provider: str) -> str:
    """把用户输入归一化成登记表 id：小写、``-`` 与空格变 ``_``。

    Examples
    --------
    >>> normalize_provider_id("NASA-Earthdata")
    'nasa_earthdata'
    """
    return provider.strip().lower().replace("-", "_").replace(" ", "_")


# --------------------------------------------------------------------------- #
# 配置引导（可操作步骤）
# --------------------------------------------------------------------------- #
_VERIFY_CMD: Final[str] = (
    '.venv/bin/python -c "from researchdatahub.environment import validate_environment; '
    "print(validate_environment('{provider}').status)\""
)


def _verify(provider: str) -> str:
    """给出可复制粘贴的验证命令（只打印整体状态，不打印任何凭据）。"""
    return _VERIFY_CMD.format(provider=provider)


_CDS_GUIDE = ConfigurationGuide(
    provider="cds",
    summary="CDS 走官方 cdsapi 客户端，需要 cdsapi 包 + ~/.cdsapirc 凭据文件 + 已接受的许可协议。",
    steps=[
        GuidanceStep(
            action="注册 Copernicus 账号并登录",
            why="没有账号就拿不到个人访问令牌，所有 CDS 请求都会被拒绝",
            where=f"{CDS_REGISTER_URL}（已有账号直接登录）",
            verify="浏览器能打开 https://cds.climate.copernicus.eu/ 且右上角显示账号名",
        ),
        GuidanceStep(
            action="为要用的数据集接受许可协议（Licence）",
            why="CDS 对 ERA5 等数据集要求逐数据集同意条款，未接受的请求返回 403",
            where=f"{CDS_LICENSE_URL} → 页面底部 Accept licence",
            verify="数据集下载页不再显示 'Accept-licence' 提示",
        ),
        GuidanceStep(
            action="生成个人访问令牌（Personal Access Token）",
            why="cdsapi 用它在 HTTP 头里做认证；这是唯一需要保密的字符串",
            where=f"{CDS_HOW_TO_API_URL} → 复制页面上的 API key（形如 xxxxxxxx-xxxx-…）",
            verify="令牌已复制到剪贴板，且没有贴到任何聊天窗口/issue 里",
        ),
        GuidanceStep(
            action="把令牌写进 ~/.cdsapirc（不要写进仓库）",
            why="cdsapi 客户端启动时只读这个文件；RDH 通过 cdsapi 发请求，所以它必须存在",
            where=(
                f"创建/编辑 {CDSAPI_RC_FILE}，内容两行：\n"
                "    url: https://cds.climate.copernicus.eu/api\n"
                "    key: <粘贴令牌>   # 只在本机编辑，绝不要提交到 git\n"
                f"（权限建议 chmod 600 {CDSAPI_RC_FILE}）"
            ),
            verify=f"test -f {CDSAPI_RC_FILE} && echo present",
        ),
        GuidanceStep(
            action="（可选）把令牌导出为 CDSAPI_KEY",
            why="部分工具链直接读环境变量；RDH 也接受它作为辅助来源，但它不能替代 ~/.cdsapirc",
            where="在 ~/.zshrc 或 ~/.bashrc 里加：export CDSAPI_KEY='<粘贴令牌>'（同样不要外传）",
            verify="python -c \"import os; print(bool(os.environ.get('CDSAPI_KEY')))\" → True",
        ),
        GuidanceStep(
            action="安装 cdsapi 可选依赖",
            why="CDS 请求只能通过官方 cdsapi 客户端发起",
            where=".venv/bin/pip install -e '.[cds]'",
            verify=".venv/bin/python -c 'import cdsapi; print(cdsapi.__version__)'",
        ),
        GuidanceStep(
            action="确认网络能到 CDS API 入口",
            why="CDS 在境外，企业网/校园网可能被防火墙拦截",
            where="curl -sS -o /dev/null -w '%{http_code}' https://cds.climate.copernicus.eu/api/retrieve/v1/",
            verify=_verify("cds"),
        ),
    ],
)

_NCAR_GUIDE = ConfigurationGuide(
    provider="ncar",
    summary=(
        "NCAR RDA 的公有数据集（如 ds633.0）匿名即可读取，**不需要**任何凭据；"
        "只要求能访问 rda.ucar.edu 与 data 服务器。"
    ),
    steps=[
        GuidanceStep(
            action="确认目标数据集是否是公开访问（NDA / Public）",
            why="受限数据集（Restricted）才需要账号与 API key；公有数据集走匿名下载更简单",
            where=f"{NCAR_DATA_ACCESS_URL}（数据集介绍页会标注 'Data access' 等级）",
            verify="数据集页面出现 'Public' 或 'Download' 直链",
        ),
        GuidanceStep(
            action="（仅限受限数据集）注册 RDA 账号并生成 API key",
            why="受限数据集的 HTTPS 下载需要认证",
            where=f"{NCAR_REGISTER_URL} → 登录后进入 'Generate API key'",
            verify="页面显示 API key；把它写进 ~/.netrc 或 RDH_API_KEY，不要贴到聊天窗口",
        ),
        GuidanceStep(
            action="（仅限受限数据集）把凭据放进 ~/.netrc",
            why="curl/wget/python 请求库在下载时自动读取 netrc 认证",
            where=(
                f"{NETRC_FILE} 里加两行：\n"
                "    machine data.rda.ucar.edu\n"
                "    login <你的注册邮箱>\n"
                "    password <API key>   # chmod 600 ~/.netrc\n"
                "另一个机器名 rda.ucar.edu 同理"
            ),
            verify=f"test -f {NETRC_FILE} && echo present",
        ),
        GuidanceStep(
            action="确认能访问 RDA 门户与数据服务器",
            why="GDEX 目录查询与文件下载分属两个域名，任一不通都会失败",
            where=(
                "curl -sS -o /dev/null -w '%{http_code}\\n' https://rda.ucar.edu/ ; "
                "curl -sS -o /dev/null -w '%{http_code}\\n' https://data.rda.ucar.edu/"
            ),
            verify=_verify("ncar"),
        ),
        GuidanceStep(
            action="安装科学栈（xarray + netCDF4）",
            why="下载后的 NetCDF 完整性与科学元数据校验需要它们",
            where=".venv/bin/pip install -e '.[sci]'",
            verify=".venv/bin/python -c 'import xarray, netCDF4; print(xarray.__version__)'",
        ),
    ],
)

_NASA_GUIDE = ConfigurationGuide(
    provider="nasa_earthdata",
    summary="NASA GES DISC 需要 Earthdata Login 账号 + 在 URS 里批准 GES DISC 应用 + 个人令牌。",
    steps=[
        GuidanceStep(
            action="注册 Earthdata Login 账号（若还没有）",
            why="NASA 所有 EOSDIS 数据服务都用统一账号认证",
            where=NASA_EARTHDATA_LOGIN_URL,
            verify="能在 https://urs.earthdata.nasa.gov/profile 看到个人资料",
        ),
        GuidanceStep(
            action="在 URS 里批准 NASA GESDISC DATA ARCHIVE 应用",
            why="账号正确但应用未授权时，GES DISC 会返回 401/403，错误信息不会提示这一点",
            where=f"{NASA_EARTHDATA_APPROVE_URL} → Log in → Authorize",
            verify="profile → Applications → Approved Apps 里出现 NASA GESDISC DATA ARCHIVE",
        ),
        GuidanceStep(
            action="生成个人令牌（Bearer token）",
            why="HTTPS/OPeNDAP 客户端用它代替密码，随时可吊销",
            where=f"{NASA_EARTHDATA_PROFILE_URL} → Generate Token",
            verify="令牌已复制；**不要**贴到聊天窗口或 issue，只存本地",
        ),
        GuidanceStep(
            action="把令牌写进环境变量 EARTHDATA_TOKEN",
            why="RDH 的凭据检查只读环境变量是否存在，从不读取它的值",
            where=(
                "在 ~/.zshrc / ~/.bashrc 里加：export EARTHDATA_TOKEN='<粘贴令牌>'，"
                "或写进 "
                f"{RDH_HOME_DIR}/env（chmod 600，由你的 shell profile source）"
            ),
            verify="python -c \"import os; print(bool(os.environ.get('EARTHDATA_TOKEN')))\" → True",
        ),
        GuidanceStep(
            action="（可选）把账号写进 ~/.netrc 以支持 OPeNDAP 客户端",
            why="部分 OPeNDAP 库不支持 Bearer token，只认 Basic 认证",
            where=(
                f"{NETRC_FILE} 里加：\n"
                "    machine urs.earthdata.nasa.gov\n"
                "    login <用户名>\n"
                "    password <密码>   # chmod 600 ~/.netrc"
            ),
            verify=f"test -f {NETRC_FILE} && echo present",
        ),
        GuidanceStep(
            action="确认 GES DISC 可达",
            why="GES DISC 与 URS 是不同域名，网络策略可能只放行其中一个",
            where="curl -sS -o /dev/null -w '%{http_code}\\n' https://goldsmr4.gesdisc.eosdis.nasa.gov/opendap/",
            verify=_verify("nasa_earthdata"),
        ),
    ],
)

_COPERNICUS_GUIDE = ConfigurationGuide(
    provider="copernicus_datasphere",
    summary=(
        "Copernicus Data Space 用 CDSE OAuth 令牌访问 OData 目录；S3 路径需要额外换取 STS 凭证。"
    ),
    steps=[
        GuidanceStep(
            action="注册 Copernicus Data Space Ecosystem 账号",
            why="Sentinel 等数据需要登录才能生成令牌",
            where="https://dataspace.copernicus.eu/",
            verify="能打开 https://dataspace.copernicus.eu/ 并看到账号菜单",
        ),
        GuidanceStep(
            action="在 Dashboard 里生成访问令牌",
            why="OData / Sentinel Hub API 用 Bearer token 认证",
            where=(
                "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings → "
                "OAuth clients → Create new client → 复制 Client ID / Secret"
            ),
            verify="拿到了 client_id 与 client_secret（不贴到聊天窗口）",
        ),
        GuidanceStep(
            action="导出 CDSE_TOKEN 环境变量",
            why="RDH 的凭据检查读这个变量是否存在（只判存在，不读值）",
            where="export CDSE_TOKEN='<粘贴令牌>'（写进 ~/.zshrc，chmod 600）",
            verify="python -c \"import os; print(bool(os.environ.get('CDSE_TOKEN')))\" → True",
        ),
        GuidanceStep(
            action="确认 OData 目录可用",
            why="发现阶段依赖 OData 目录列出产品",
            where=(
                "curl -sS -o /dev/null -w '%{http_code}\\n' "
                "'https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$top=1'"
            ),
            verify=_verify("copernicus_datasphere"),
        ),
        GuidanceStep(
            action="（可选）安装 boto3 以支持 S3 兼容路径",
            why="Data Space 提供 S3 兼容端点，但需要先换取 STS 凭证；该换算尚未实现",
            where=".venv/bin/pip install boto3",
            verify=".venv/bin/python -c 'import boto3; print(boto3.__version__)'",
        ),
    ],
)

_GEE_GUIDE = ConfigurationGuide(
    provider="gee",
    summary="GEE 需要注册并获批 Earth Engine、安装 earthengine-api、执行一次授权生成凭据文件。",
    steps=[
        GuidanceStep(
            action="注册 Earth Engine 并等待审批",
            why="Earth Engine 是免费但需审批的服务，未获批的账号调用 API 会返回权限错误",
            where=GEE_REGISTER_URL,
            verify="https://code.earthengine.google.com/ 能打开并能运行最简单的 print(1)",
        ),
        GuidanceStep(
            action="创建（或选定）Cloud 项目并启用 Earth Engine API",
            why="新版 earthengine-api 要求 ee.Initialize(project='…')，否则报错",
            where=f"{GEE_PROJECT_URL} → 新建项目 → APIs & Services → 启用 Earth Engine API",
            verify="项目页左侧出现 Earth Engine API",
        ),
        GuidanceStep(
            action="安装 earthengine-api",
            why="导入名是 ee；它是 GEE 的唯一官方客户端",
            where=f".venv/bin/pip install earthengine-api（或 {CONDA_FORGE_EE_URL}）",
            verify=".venv/bin/python -c 'import ee; print(ee.__version__)'",
        ),
        GuidanceStep(
            action="执行一次授权，生成凭据文件",
            why="ee.Initialize() 需要本地凭据文件；凭据只在浏览器里完成 OAuth，不经过聊天窗口",
            where=(
                ".venv/bin/python -c 'import ee; ee.Authenticate()' "
                "（浏览器完成登录；凭据落到 ~/.config/earthengine/credentials）"
            ),
            verify="test -f ~/.config/earthengine/credentials && echo present",
        ),
        GuidanceStep(
            action="（可选）设置 EE_PROJECT",
            why="多项目环境下显式指定项目可避免初始化歧义",
            where="export EE_PROJECT='your-gcp-project-id'",
            verify="python -c \"import os; print(os.environ.get('EE_PROJECT'))\"",
        ),
        GuidanceStep(
            action="验证初始化",
            why="授权成功不等于初始化成功（项目/API 未启用时会失败）",
            where=(
                ".venv/bin/python -c 'import ee; ee.Initialize(project=...); "
                "print(ee.Number(1).getInfo())'"
            ),
            verify=_verify("gee"),
        ),
    ],
)

_GUIDES: Final[Mapping[str, ConfigurationGuide]] = MappingProxyType(
    {
        "cds": _CDS_GUIDE,
        "ncar": _NCAR_GUIDE,
        "nasa_earthdata": _NASA_GUIDE,
        "copernicus_datasphere": _COPERNICUS_GUIDE,
        "gee": _GEE_GUIDE,
    }
)


def get_environment_requirements(provider: str) -> ConfigurationGuide:
    """返回某个 Provider 的**可操作**配置引导（中文，含真实 URL 与验证命令）。

    Parameters
    ----------
    provider:
        provider id，见 :data:`PROVIDER_IDS`（大小写、``-``/``_`` 不敏感）。

    Returns
    -------
    ConfigurationGuide
        每步含 ``action`` / ``why`` / ``where`` / ``verify``。

    Raises
    ------
    KeyError
        provider 未登记；错误消息列出全部合法 id。

    Examples
    --------
    >>> guide = get_environment_requirements("CDS")
    >>> guide.provider
    'cds'
    >>> bool(guide.steps[0].where)
    True
    """
    normalized = normalize_provider_id(provider)
    guide = _GUIDES.get(normalized)
    if guide is None:
        raise KeyError(f"unknown provider {provider!r}; known providers: {', '.join(PROVIDER_IDS)}")
    return guide


def get_requirements(provider: str) -> ProviderRequirements:
    """按 id 取 Provider 的静态要求声明（:data:`PROVIDER_REQUIREMENTS_REGISTRY` 的便捷入口）。

    Raises
    ------
    KeyError
        provider 未登记。
    """
    return PROVIDER_REQUIREMENTS_REGISTRY.require(provider)
