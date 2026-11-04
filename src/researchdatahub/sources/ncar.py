r"""NSF NCAR GDEX / RDA 数据源适配器。

远端形态（**2026-11-04 实测**，不是记忆）
----------------------------------------
旧地址 ``https://rda.ucar.edu`` 会重定向到 ``https://gdex.ucar.edu``（GDEX，Geoscience
Data Exchange）。GDEX 公开了 "RDA Datasets API"（OpenAPI 3.0.3，见
``https://gdex.ucar.edu/api/schema/``，实测 200）。本适配器只使用**实测返回 200、
无需登录**的端点：

* ``GET /api/datasets/`` —— 数据集清单（实测 1731 条，``id`` 形如 ``d633000``）
* ``GET /api/datasets/{dsid}/groups/`` —— 根文件分组（字段 ``grpid`` / ``title`` /
  ``gindex`` / ``webcnt``）
* ``GET /api/datasets/{dsid}/webfiles/{gindex}/`` —— 分组内可直接 HTTP 下载的文件
  （字段 ``wfile`` / ``data_size`` / ``data_format`` / ``date_modified`` / ``status``）
* ``GET /api/datasets/{dsid}/data_license/`` —— 许可证（实测 CC-BY-4.0）
* ``GET /api/datasets/{dsid}/temporal/``、``/spatial_coverage/``、``/data_formats/``

数据集 ID 形态：OpenAPI 声明 ``d\\d{6}``；实测旧式 ``ds633.0`` 的页面会重定向到
``d633000``（``https://gdex.ucar.edu/datasets/ds633.0/`` → ``/datasets/d633000/``），
因此 :func:`normalize_dsid` 按这个**已观测的映射规则**转换（不是猜测别的规则）。

数据文件 URL：``filelist`` 响应里每条文件行给出真实 URL
（``https://data.gdex.ucar.edu/{dsid}/{wfile}``，实测 HEAD 200），与 ``webfiles``
的 ``wfile`` 拼接规则一致，故 :data:`GDEX_DATA_HOST` 取该主机。

**未验证/受限项**（不得当成事实）
----------------------------------
* "科学变量 → RDA 文件分组（gindex）"的映射没有公开、稳定的机器可读规则；
  因此 ``discover_assets`` **要求显式指定** ``rda.gindex``（请求扩展或配置扩展），
  不按变量名猜分组。
* 文件名的"数据时间区间"编码规则随数据集/产品不同，本适配器不内置任何模式；
  若要按时间裁剪，必须在配置里给出 ``rda.file_time`` 的 ``pattern`` 与 ``format``。
* ``webfiles`` 的 ``data_size`` 是**目录声明值**，未与文件字节核验：实测样本
  ``d633000/catalogs/d633000-osdf.csv`` 的目录声明为 106391 B，而 2026-11-04
  实际 GET 为 39382 B（目录快照过期）。资产扩展里显式标记
  ``rda.size_source="catalog_unverified"``，最终以传输/校验层为准。
* 校验和：``webfiles`` 响应没有校验和字段（实测字段集合），因此
  ``SourceAsset.checksum=None``，扩展标记 ``rda.checksum="unknown"``。
* S3 / Globus / Zarr 访问路径：本适配器**只产出 HTTPS 资产**；其它 route 需要各自的
  已证实路径规则（桶名/端点/前缀），本模块不虚构。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import httpx

from researchdatahub.config.models import SourceConfig
from researchdatahub.core.errors import (
    MetadataError,
    SourceError,
    SourceNotFoundError,
    SourceUnavailableError,
)
from researchdatahub.core.models import (
    AccessRoute,
    DatasetRequest,
    JsonValue,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.sources.base import (
    DEFAULT_TIMEOUT_SECONDS,
    AdapterCapabilities,
    SourceAdapter,
    asset_id_for,
    build_asset,
    build_source_metadata,
    json_safe,
    parse_size_bytes,
    parse_timestamp,
    utcnow,
)

__all__ = [
    "GDEX_DATA_HOST",
    "GDEX_ENDPOINT",
    "GdexCatalogClient",
    "NCARAdapter",
    "RdaFileGroup",
    "RdaWebFile",
    "normalize_dsid",
    "parse_file_groups",
    "parse_web_files",
]

logger = logging.getLogger(__name__)

GDEX_ENDPOINT: Final[str] = "https://gdex.ucar.edu"
"""GDEX 主站（实测：``/api/schema/`` 与 ``/api/datasets/`` 均返回 200）。"""

GDEX_DATA_HOST: Final[str] = "https://data.gdex.ucar.edu"
"""数据文件主机（实测：``filelist`` 返回的 URL 主机 + HEAD 200）。"""

_API_ROOT: Final[str] = "/api"
_LEGACY_DSID_PATTERN: Final = re.compile(r"^ds(\d{3})\.(\d{1,3})$")
_GDEX_DSID_PATTERN: Final = re.compile(r"^d\d{6}$")
_MAX_WEB_FILE_METADATA: Final[int] = 5000

RdaJson = Mapping[str, JsonValue]


def normalize_dsid(value: str) -> str:
    r"""把数据集 ID 规范化为 GDEX 的 ``d\d{6}`` 形态。

    实测映射：``ds633.0`` → ``d633000``（旧页面 301 到 GDEX 新页面，
    ``https://gdex.ucar.edu/datasets/ds633.0/`` → ``/datasets/d633000/``）。

    Raises
    ------
    MetadataError
        既不是 ``d123456`` 也不是可转换的 ``dsNNN.M`` 形态。
    """
    text = value.strip()
    if _GDEX_DSID_PATTERN.match(text):
        return text
    legacy = _LEGACY_DSID_PATTERN.match(text)
    if legacy is not None:
        return f"d{legacy.group(1)}{legacy.group(2).zfill(3)}"
    raise MetadataError(
        "dataset id must be a GDEX dsid ('d123456') or legacy RDA id ('ds633.0')",
        detail={"observed": text},
    )


@dataclass(frozen=True)
class RdaFileGroup:
    """GDEX 根文件分组（``/groups/`` 响应中的一项）。"""

    gindex: int
    grpid: str | None
    title: str | None
    web_file_count: int | None
    raw: RdaJson

    def to_extension(self) -> dict[str, JsonValue]:
        """供资产 extensions 保存的扁平描述。"""
        return {
            "rda.gindex": self.gindex,
            "rda.grpid": self.grpid,
            "rda.group_title": self.title,
            "rda.group_web_file_count": self.web_file_count,
        }


@dataclass(frozen=True)
class RdaWebFile:
    """GDEX 分组内的一个可下载文件（``/webfiles/{gindex}/`` 响应中的一项）。"""

    wfile: str
    size_bytes: int | None
    data_format: str | None
    date_modified: datetime | None
    status: str | None
    raw: RdaJson

    @property
    def file_name(self) -> str:
        """文件名（``wfile`` 的最后一段）。"""
        return self.wfile.rstrip("/").rsplit("/", 1)[-1]

    @property
    def size_source(self) -> str:
        """大小来源标记：目录声明值，未与字节核验。"""
        return "catalog_unverified"


def parse_file_groups(payload: Mapping[str, Any]) -> tuple[RdaFileGroup, ...]:
    """解析 ``/api/datasets/{dsid}/groups/`` 响应。

    Raises
    ------
    MetadataError
        响应包装或 ``data`` 形状不符合实测形态。
    """
    data = _require_data(payload, context="groups")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise MetadataError(
            "GDEX groups response 'data' is not a list",
            detail={"observed_type": type(data).__name__},
        )
    groups: list[RdaFileGroup] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise MetadataError("GDEX groups response item is not an object")
        gindex_raw = item.get("gindex")
        if isinstance(gindex_raw, bool) or not isinstance(gindex_raw, (int, str)):
            raise MetadataError(
                "GDEX file group has no usable 'gindex'",
                detail={"observed": json_safe(gindex_raw)},
            )
        try:
            gindex = int(gindex_raw)
        except (TypeError, ValueError) as exc:
            raise MetadataError(
                "GDEX file group 'gindex' is not an integer",
                detail={"observed": json_safe(gindex_raw)},
            ) from exc
        web_count = item.get("webcnt")
        groups.append(
            RdaFileGroup(
                gindex=gindex,
                grpid=item.get("grpid") if isinstance(item.get("grpid"), str) else None,
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                web_file_count=web_count if isinstance(web_count, int) else None,
                raw={str(key): json_safe(value) for key, value in item.items()},
            )
        )
    return tuple(groups)


def parse_web_files(payload: Mapping[str, Any]) -> tuple[RdaWebFile, ...]:
    """解析 ``/api/datasets/{dsid}/webfiles/{gindex}/`` 响应。

    Raises
    ------
    MetadataError
        ``data`` 不是列表、条目不是对象或缺少 ``wfile``。
    """
    data = _require_data(payload, context="webfiles")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise MetadataError(
            "GDEX webfiles response 'data' is not a list",
            detail={"observed_type": type(data).__name__},
        )
    files: list[RdaWebFile] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise MetadataError("GDEX webfiles response item is not an object")
        wfile = item.get("wfile")
        if not isinstance(wfile, str) or not wfile.strip():
            raise MetadataError(
                "GDEX webfile entry has no 'wfile' path",
                detail={"observed": json_safe(item)},
            )
        files.append(
            RdaWebFile(
                wfile=wfile.strip(),
                size_bytes=parse_size_bytes(item.get("data_size"), field="data_size"),
                data_format=(
                    item.get("data_format") if isinstance(item.get("data_format"), str) else None
                ),
                date_modified=parse_timestamp(
                    item.get("date_modified"),
                    field="date_modified",
                    allow_date_only=True,
                    date_only_assume_utc=True,
                ),
                status=item.get("status") if isinstance(item.get("status"), str) else None,
                raw={str(key): json_safe(value) for key, value in item.items()},
            )
        )
    return tuple(files)


def _require_data(payload: Mapping[str, Any], *, context: str) -> Any:
    status = payload.get("status")
    if isinstance(status, str) and status.lower() not in ("ok", "success"):
        raise SourceUnavailableError(
            f"GDEX {context} API returned an error status",
            detail={
                "status": status,
                "error_messages": json_safe(payload.get("error_messages")),
            },
        )
    if "data" not in payload:
        raise MetadataError(
            f"GDEX {context} response has no 'data' field",
            detail={"keys": sorted(str(key) for key in payload)},
        )
    return payload["data"]


class GdexCatalogClient:
    """GDEX "RDA Datasets API" 的最小客户端（只读、JSON）。

    所有失败都折算为本项目异常：404 → :class:`SourceNotFoundError`；
    其它 HTTP/连接错误 → :class:`SourceUnavailableError`；JSON 形状错误 → :class:`MetadataError`。
    """

    def __init__(
        self,
        *,
        endpoint: str = GDEX_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http: httpx.Client | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._http = http
        self._owns_http = http is None

    @property
    def endpoint(self) -> str:
        """API 根地址。"""
        return self._endpoint

    def close(self) -> None:
        """释放内部 HTTP 客户端（幂等）。"""
        if self._http is not None and self._owns_http:
            self._http.close()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
                headers={"User-Agent": "researchdatahub-sources/0.1"},
            )
            self._owns_http = True
        return self._http

    def _get_json(self, path: str, *, context: str) -> Mapping[str, Any]:
        url = f"{self._endpoint}{_API_ROOT}{path}"
        try:
            response = self._client().get(url)
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                f"GDEX {context} request failed",
                detail={"url": url, "error_type": type(exc).__name__},
            ) from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise SourceNotFoundError(
                f"GDEX {context} endpoint returned 404",
                detail={"url": url},
            )
        if response.status_code != httpx.codes.OK:
            raise SourceUnavailableError(
                f"GDEX {context} endpoint returned HTTP {response.status_code}",
                detail={"url": url, "status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetadataError(
                f"GDEX {context} response is not valid JSON",
                detail={"url": url},
            ) from exc
        if not isinstance(payload, Mapping):
            raise MetadataError(
                f"GDEX {context} response is not a JSON object",
                detail={"url": url},
            )
        return payload

    def list_groups(self, dsid: str) -> tuple[RdaFileGroup, ...]:
        """列出数据集的根文件分组。"""
        payload = self._get_json(f"/datasets/{dsid}/groups/", context="groups")
        return parse_file_groups(payload)

    def list_web_files(self, dsid: str, gindex: int) -> tuple[RdaWebFile, ...]:
        """列出某分组内可通过 HTTPS 下载的文件。"""
        payload = self._get_json(f"/datasets/{dsid}/webfiles/{gindex}/", context="webfiles")
        return parse_web_files(payload)

    def dataset_license(self, dsid: str) -> str | None:
        """读取数据集许可证名称；响应没有名称时返回 ``None``。"""
        payload = self._get_json(f"/datasets/{dsid}/data_license/", context="data_license")
        data = _require_data(payload, context="data_license")
        if isinstance(data, Mapping):
            name = data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def dataset_formats(self, dsid: str) -> tuple[str, ...]:
        """读取数据集声明格式（``data_formats`` 的 description）。"""
        payload = self._get_json(f"/datasets/{dsid}/data_formats/", context="data_formats")
        data = _require_data(payload, context="data_formats")
        if not isinstance(data, Mapping):
            return ()
        raw_formats = data.get("data_formats")
        if not isinstance(raw_formats, Sequence) or isinstance(raw_formats, (str, bytes)):
            return ()
        formats: list[str] = []
        for item in raw_formats:
            if isinstance(item, Mapping) and isinstance(item.get("description"), str):
                formats.append(str(item["description"]))
        return tuple(formats)

    def dataset_temporal(self, dsid: str) -> tuple[datetime | None, datetime | None]:
        """读取数据集时间覆盖（``start_date`` / ``end_date``）。"""
        payload = self._get_json(f"/datasets/{dsid}/temporal/", context="temporal")
        data = _require_data(payload, context="temporal")
        if not isinstance(data, Mapping):
            return None, None
        temporal = data.get("temporal")
        if not isinstance(temporal, Mapping):
            return None, None
        return (
            parse_timestamp(temporal.get("start_date"), field="start_date"),
            parse_timestamp(temporal.get("end_date"), field="end_date"),
        )

    def dataset_title(self, dsid: str) -> str | None:
        """从 ``/api/datasets/`` 清单查标题（大响应；只在 describe 时调用）。"""
        payload = self._get_json("/datasets/", context="datasets")
        data = _require_data(payload, context="datasets")
        if not isinstance(data, Mapping):
            return None
        datasets = data.get("datasets")
        if not isinstance(datasets, Sequence) or isinstance(datasets, (str, bytes)):
            return None
        for item in datasets:
            if isinstance(item, Mapping) and item.get("id") == dsid:
                title = item.get("title")
                if isinstance(title, str) and title.strip():
                    return title.strip()
                return None
        return None


class NCARAdapter(SourceAdapter):
    """NSF NCAR GDEX/RDA 数据源适配器（发现阶段，不下载字节）。"""

    adapter_id: ClassVar[str] = "rda"

    def __init__(
        self,
        *,
        source_id: str = "ncar",
        config: SourceConfig | None = None,
        endpoint: str | None = None,
        data_host: str | None = None,
        dataset_ids: Mapping[str, str] | None = None,
        catalog: GdexCatalogClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_web_files: int = _MAX_WEB_FILE_METADATA,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(
            source_id=source_id,
            config=config,
            timeout=timeout,
            http_client=http_client,
            clock=clock,
        )
        self._endpoint = endpoint
        self._data_host = data_host
        self._dataset_ids_override = dict(dataset_ids) if dataset_ids is not None else None
        self._catalog = catalog
        self._max_web_files = max_web_files
        self._lock = threading.Lock()
        self._catalog_client: GdexCatalogClient | None = None
        self._title_cache: str | None = None

    @property
    def supported_routes(self) -> frozenset[str]:
        """当前实现只产出 HTTPS 资产（S3/Globus/Zarr 路径规则未验证）。"""
        return frozenset({AccessRoute.HTTPS.value})

    # -- 配置解析 ---------------------------------------------------------- #
    @property
    def endpoint(self) -> str:
        """GDEX API 根：显式参数 > 配置 endpoint > 已实测默认 ``https://gdex.ucar.edu``。"""
        if self._endpoint:
            return self._endpoint.rstrip("/")
        if self._config is not None and self._config.endpoint:
            return self._config.endpoint.rstrip("/")
        return GDEX_ENDPOINT

    @property
    def endpoint_verified(self) -> bool:
        """当前 endpoint 是否属于本次实测验证过的 GDEX 主机（含旧域名的实测重定向）。

        实测 2026-11-04：``https://rda.ucar.edu/api/datasets/d633000/groups/`` 会
        301 到 ``https://gdex.ucar.edu/api/...`` 并返回 200 JSON，因此旧域名可作为
        已验证别名；``httpx`` 客户端开启 ``follow_redirects``。
        """
        host = httpx.URL(self.endpoint).host or ""
        return host == "rda.ucar.edu" or host == "gdex.ucar.edu" or host.endswith(".gdex.ucar.edu")

    @property
    def data_host(self) -> str:
        """数据文件主机：显式参数 > 配置 ``rda.data_host`` > GDEX 实测主机。

        Raises
        ------
        SourceUnavailableError
            endpoint 不是已验证的 GDEX 主机且没有显式配置 data host；
            此时文件 URL 规则无法确认，拒绝拼接。
        """
        if self._data_host:
            return self._data_host.rstrip("/")
        if self._config is not None:
            configured = self._config.extensions.get("rda.data_host")
            if isinstance(configured, str) and configured.strip():
                return configured.strip().rstrip("/")
        if self.endpoint_verified:
            return GDEX_DATA_HOST
        raise SourceUnavailableError(
            "data host for this RDA endpoint is not verified; "
            "configure sources.<id>.extensions['rda.data_host'] explicitly",
            source_id=self.source_id,
            detail={"endpoint": self.endpoint},
        )

    @property
    def dataset_ids(self) -> Mapping[str, str]:
        """逻辑数据集 ID → GDEX dsid 的映射（来自构造参数或配置扩展，不做猜测）。"""
        if self._dataset_ids_override is not None:
            return dict(self._dataset_ids_override)
        mapping: dict[str, str] = {}
        if self._config is not None:
            extension_map = self._config.extensions.get("rda.dataset_map")
            if isinstance(extension_map, Mapping):
                mapping.update(
                    {str(key): str(value) for key, value in extension_map.items() if value}
                )
            single = self._config.extensions.get("rda.dsid")
            declared = list(self._config.datasets)
            if isinstance(single, str) and single.strip() and len(declared) == 1:
                mapping.setdefault(declared[0], single.strip())
            legacy_group = self._config.extensions.get("ncar.dataset_group")
            if isinstance(legacy_group, str) and legacy_group.strip():
                normalized = normalize_dsid(legacy_group)
                for dataset_id in declared:
                    mapping.setdefault(dataset_id, normalized)
        return mapping

    def resolve_dsid(self, dataset_id: str) -> str:
        """解析请求的逻辑数据集 ID 对应的 GDEX dsid。

        Raises
        ------
        SourceNotFoundError
            没有配置映射；错误信息给出建议的配置键（不猜测远端目录）。
        """
        mapped = self.dataset_ids.get(dataset_id)
        if not mapped:
            raise SourceNotFoundError(
                f"no verified RDA dsid mapping for dataset {dataset_id!r}; configure "
                "sources.<id>.extensions['rda.dsid'] (single dataset) or "
                "['rda.dataset_map'] (dict), or keep the legacy 'ncar.dataset_group'",
                source_id=self.source_id,
                detail={"dataset_id": dataset_id, "configured": sorted(self.dataset_ids)},
            )
        return normalize_dsid(mapped)

    # -- 目录客户端 -------------------------------------------------------- #
    def catalog(self) -> GdexCatalogClient:
        """惰性构造 :class:`GdexCatalogClient`。"""
        if self._catalog is not None:
            return self._catalog
        with self._lock:
            if self._catalog_client is None:
                self._catalog_client = GdexCatalogClient(
                    endpoint=self.endpoint,
                    timeout=self._timeout,
                    http=self._http_client,
                )
            return self._catalog_client

    def close(self) -> None:
        """关闭目录客户端与基类 HTTP 客户端（幂等）。"""
        if self._catalog_client is not None:
            self._catalog_client.close()
        if self._catalog is not None:
            self._catalog.close()
        super().close()

    # -- 配置校验 ---------------------------------------------------------- #
    def validate_configuration(self) -> tuple[str, ...]:
        """检查静态配置（不发网络请求）；返回问题列表，空元组表示通过。"""
        issues: list[str] = []
        try:
            parsed = httpx.URL(self.endpoint)
        except (httpx.InvalidURL, ValueError):
            issues.append(f"invalid RDA endpoint URL: {self.endpoint!r}")
        else:
            if parsed.scheme not in ("http", "https") or not parsed.host:
                issues.append("RDA endpoint must be an absolute http(s) URL")
        if not self.dataset_ids:
            issues.append(
                "no RDA dataset mapping: configure sources.<id>.datasets plus "
                "extensions['rda.dsid'] / ['rda.dataset_map'] "
                "or the legacy ['ncar.dataset_group']"
            )
        else:
            for dataset_id, dsid in self.dataset_ids.items():
                try:
                    normalize_dsid(dsid)
                except MetadataError:
                    issues.append(f"invalid RDA dsid for dataset {dataset_id!r}: {dsid!r}")
        if not self._is_available():
            issues.append(
                "data host cannot be verified for this endpoint; configure "
                "extensions['rda.data_host'] explicitly"
            )
        try:
            self._time_filter()
        except MetadataError as exc:
            issues.append(exc.message)
        return tuple(issues)

    # -- 能力 -------------------------------------------------------------- #
    def _is_available(self) -> bool:
        """端点已验证，或运维显式配置了 data host 并为此负责。"""
        if self.endpoint_verified:
            return True
        if self._data_host:
            return True
        if self._config is not None:
            configured = self._config.extensions.get("rda.data_host")
            return isinstance(configured, str) and bool(configured.strip())
        return False

    def capabilities(self) -> AdapterCapabilities:
        """如实列出已验证/未验证的能力边界。"""
        unverified = [
            "scientific variable -> RDA file group (gindex) mapping: not machine-readable; "
            "discover_assets requires an explicit rda.gindex",
            "per-file data time range: filename convention is dataset-specific and not built in; "
            "configure rda.file_time to filter by time",
            "file sizes are catalog-declared and not verified against bytes "
            "(observed 2026-11-04: catalog 106391 B vs actual GET 39382 B for one sample)",
            "checksums: GDEX webfiles API exposes none; SourceAsset.checksum is None",
            "S3/Globus/Zarr access routes: no verified bucket/prefix/endpoint rules in this build",
        ]
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            source_id=self.source_id,
            available=self._is_available(),
            routes=("https",),
            verified=(
                "GDEX RDA Datasets API /api/datasets/ (real GET 2026-11-04)",
                "GDEX dataset file groups /groups/ and web files /webfiles/{gindex}/ "
                "(real GET 2026-11-04)",
                "GDEX data file URLs on https://data.gdex.ucar.edu (real HEAD 200 2026-11-04)",
                "legacy dsid conversion ds633.0 -> d633000 (observed redirect 2026-11-04)",
            ),
            unverified=tuple(unverified),
            missing_credentials=(),
            reason=(
                None
                if self._is_available()
                else "RDA endpoint is not a verified GDEX host and no explicit rda.data_host "
                "is configured; file URL construction cannot be verified"
            ),
        )

    # -- SourceAdapter 钩子 ------------------------------------------------ #
    def _describe(self) -> SourceMetadata:
        declared = list(self._config.datasets) if self._config is not None else []
        dataset_id = declared[0] if declared else next(iter(self.dataset_ids), "")
        dsid = self.resolve_dsid(dataset_id)
        catalog = self.catalog()
        license_text = catalog.dataset_license(dsid)
        formats = catalog.dataset_formats(dsid)
        start, end = catalog.dataset_temporal(dsid)
        title = catalog.dataset_title(dsid)
        extensions: dict[str, JsonValue] = {
            "rda.api_root": self.endpoint,
            "rda.dsid": dsid,
            "rda.data_host": self.data_host,
            "rda.data_formats": list(formats),
            "rda.size_source": "catalog_unverified",
            "rda.checksum": "unknown",
        }
        if title is not None:
            extensions["rda.dataset_title"] = title
        if start is not None:
            extensions["rda.temporal_start"] = start.isoformat()
        if end is not None:
            extensions["rda.temporal_end"] = end.isoformat()
        display_name = (
            title
            if title is not None
            else (self._config.display_name if self._config is not None else f"RDA {dsid}")
        )
        provider = self._config.provider if self._config is not None else "NSF NCAR RDA"
        return build_source_metadata(
            source_id=self.source_id,
            display_name=display_name,
            provider=provider,
            access_route=AccessRoute.HTTPS,
            dataset_id=dsid,
            origin_uri=f"{self.endpoint}/datasets/{dsid}/",
            retrieved_at=self.clock(),
            protocol_version="rda-datasets-api/1.0.0",
            license=license_text,
            citation=None,
            extensions=extensions,
        )

    def _health_check(self) -> bool:
        if not self.dataset_ids:
            raise SourceNotFoundError(
                "RDA adapter has no dataset mapping configured; cannot probe a dataset endpoint",
                source_id=self.source_id,
            )
        dsid = normalize_dsid(next(iter(self.dataset_ids.values())))
        groups = self.catalog().list_groups(dsid)
        return bool(groups)

    def _discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        dsid = self.resolve_dsid(request.dataset_id)
        gindex = self._resolve_gindex(request)
        files = self.catalog().list_web_files(dsid, gindex)
        if not files:
            raise SourceNotFoundError(
                f"GDEX file group {gindex} of {dsid!r} contains no web files",
                source_id=self.source_id,
                detail={"dsid": dsid, "gindex": gindex},
            )
        filtered = self._filter_by_time(files, request)
        if len(filtered) > self._max_web_files:
            raise SourceUnavailableError(
                f"GDEX file group {gindex} has {len(filtered)} web files, above the "
                f"configured limit {self._max_web_files}; configure rda.file_time to "
                "reduce the selection or raise max_web_files",
                source_id=self.source_id,
                detail={"dsid": dsid, "gindex": gindex, "files": len(filtered)},
            )
        catalog = self.catalog()
        license_text = self._cached_license(dsid, catalog)
        data_host = self.data_host
        assets: list[SourceAsset] = []
        for item in filtered:
            origin_uri = f"{data_host}/{dsid}/{item.wfile}"
            metadata_extensions: dict[str, JsonValue] = {
                "rda.dsid": dsid,
                "rda.gindex": gindex,
                "rda.wfile": item.wfile,
                "rda.size_source": item.size_source,
                "rda.checksum": "unknown",
                "rda.status": item.status,
                "rda.time_known": False,
            }
            if item.date_modified is not None:
                metadata_extensions["rda.date_modified"] = item.date_modified.isoformat()
            if item.data_format is not None:
                metadata_extensions["rda.data_format"] = item.data_format
            metadata = build_source_metadata(
                source_id=self.source_id,
                display_name=(
                    self._config.display_name if self._config is not None else f"RDA {dsid}"
                ),
                provider=self._config.provider if self._config is not None else "NSF NCAR RDA",
                access_route=AccessRoute.HTTPS,
                dataset_id=dsid,
                origin_uri=origin_uri,
                retrieved_at=self.clock(),
                protocol_version="rda-datasets-api/1.0.0",
                license=license_text,
                citation=None,
                extensions=metadata_extensions,
            )
            assets.append(
                build_asset(
                    source_id=self.source_id,
                    dataset_id=request.dataset_id,
                    access_route=AccessRoute.HTTPS,
                    origin_uri=origin_uri,
                    file_name=item.file_name,
                    size_bytes=item.size_bytes,
                    metadata=metadata,
                    discovered_at=self.clock(),
                    asset_id=asset_id_for(self.source_id, f"{dsid}:{item.wfile}"),
                    checksum=None,
                    content_type=None,
                    time_slice=None,
                )
            )
        return tuple(assets)

    # -- 分组/时间过滤 ----------------------------------------------------- #
    def _resolve_gindex(self, request: DatasetRequest) -> int:
        """解析文件分组：请求扩展优先，其次配置扩展；都没有则明确拒绝。"""
        candidate = request.extensions.get("rda.gindex")
        if candidate is None and self._config is not None:
            candidate = self._config.extensions.get("rda.gindex")
        if candidate is None:
            raise MetadataError(
                "RDA discovery requires an explicit file group: set request extension "
                "'rda.gindex' or config extension 'rda.gindex' (variable->group mapping "
                "is not machine-readable and is not guessed)",
                source_id=self.source_id,
                detail={"dataset_id": request.dataset_id},
            )
        if isinstance(candidate, bool) or not isinstance(candidate, (int, str)):
            raise MetadataError(
                "rda.gindex must be an integer file group index",
                detail={"observed": json_safe(candidate)},
            )
        try:
            return int(candidate)
        except (TypeError, ValueError) as exc:
            raise MetadataError(
                "rda.gindex must be an integer file group index",
                detail={"observed": json_safe(candidate)},
            ) from exc

    def _time_filter(self) -> tuple[re.Pattern[str], str] | None:
        configured = None
        if self._config is not None:
            configured = self._config.extensions.get("rda.file_time")
        if configured is None:
            return None
        if not isinstance(configured, Mapping):
            raise MetadataError(
                "rda.file_time must be an object with 'pattern' (regex, groups start/end) "
                "and 'format' (strptime format)",
                detail={"observed": json_safe(configured)},
            )
        pattern_text = configured.get("pattern")
        format_text = configured.get("format")
        if not isinstance(pattern_text, str) or not isinstance(format_text, str):
            raise MetadataError(
                "rda.file_time requires string 'pattern' and 'format' fields",
                detail={"observed": json_safe(configured)},
            )
        try:
            compiled = re.compile(pattern_text)
        except re.error as exc:
            raise MetadataError(
                "rda.file_time.pattern is not a valid regular expression",
                detail={"pattern": pattern_text},
            ) from exc
        for group_name in ("start", "end"):
            if group_name not in compiled.groupindex:
                raise MetadataError(
                    "rda.file_time.pattern must define named groups 'start' and 'end'",
                    detail={"pattern": pattern_text},
                )
        return compiled, format_text

    def _filter_by_time(
        self, files: Sequence[RdaWebFile], request: DatasetRequest
    ) -> tuple[RdaWebFile, ...]:
        time_filter = self._time_filter()
        if time_filter is None:
            return tuple(files)
        compiled, format_text = time_filter
        extent = request.metadata.temporal_coverage
        selected: list[RdaWebFile] = []
        matched = 0
        for item in files:
            match = compiled.search(item.file_name)
            if match is None:
                continue
            matched += 1
            try:
                start = datetime.strptime(match.group("start"), format_text).replace(tzinfo=UTC)
                end = datetime.strptime(match.group("end"), format_text).replace(tzinfo=UTC)
            except ValueError as exc:
                raise MetadataError(
                    "rda.file_time.format does not match the captured file timestamps",
                    detail={"file": item.file_name, "format": format_text},
                ) from exc
            if start < extent.end and end > extent.start:
                selected.append(item)
        if matched == 0:
            raise MetadataError(
                "rda.file_time.pattern did not match any file name in this group; "
                "the configured filename convention is probably wrong",
                detail={"group_files": len(files)},
            )
        return tuple(selected)

    def _cached_license(self, dsid: str, catalog: GdexCatalogClient) -> str | None:
        if self._source_metadata is not None and self._source_metadata.license is not None:
            return self._source_metadata.license
        try:
            return catalog.dataset_license(dsid)
        except SourceError:
            return None


def ncar_adapter_from_config(source_id: str, config: SourceConfig) -> NCARAdapter:
    """从 :class:`SourceConfig` 构造 :class:`NCARAdapter`（供 SourceRegistry 工厂使用）。"""
    return NCARAdapter(source_id=source_id, config=config)
