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
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final
from urllib.parse import quote, urlencode, urlsplit

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
    TemporalExtent,
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
    "RdaFileListFile",
    "RdaWebFile",
    "normalize_dsid",
    "parse_file_groups",
    "parse_file_list",
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
_DEFAULT_NCSS_ENDPOINT: Final[str] = "https://tds.gdex.ucar.edu/thredds/ncss/grid/files"
_DEFAULT_NCSS_MAX_BYTES: Final[int] = 64 * 1024 * 1024

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


@dataclass(frozen=True)
class RdaFileListFile:
    """GDEX ``filelist/{year}{gindex}`` 中的一条文件记录。"""

    file_name: str
    url: str
    data_path: str | None
    size_bytes: int
    data_format: str | None
    date_archived: datetime | None
    gindex: int
    raw: RdaJson


@dataclass(frozen=True)
class _NcssVariableRule:
    """一个显式配置的逻辑变量 → NCSS 参数/文件匹配规则。"""

    logical_name: str
    parameter: str
    filename_contains: str


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


def parse_file_list(payload: Mapping[str, Any]) -> tuple[RdaFileListFile, ...]:
    """解析 GDEX ``filelist/{year}{gindex}`` 的表格型响应。

    响应把文件信息放在 ``data.groups[].rows`` 的单元格列表中；这里按单元格的
    ``name`` 取值，不依赖列顺序。只接受带真实 URL 与正整数大小的条目。
    """
    data = _require_data(payload, context="filelist")
    if not isinstance(data, Mapping):
        raise MetadataError("GDEX filelist response 'data' is not an object")
    groups = data.get("groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        raise MetadataError("GDEX filelist response 'data.groups' is not a list")
    files: list[RdaFileListFile] = []
    for group in groups:
        if not isinstance(group, Mapping):
            raise MetadataError("GDEX filelist group is not an object")
        gindex_raw = group.get("gindex")
        try:
            gindex = int(str(gindex_raw))
        except (TypeError, ValueError) as exc:
            raise MetadataError(
                "GDEX filelist group has no usable 'gindex'",
                detail={"observed": json_safe(gindex_raw)},
            ) from exc
        rows = group.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise MetadataError("GDEX filelist group 'rows' is not a list")
        for row in rows:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
                raise MetadataError("GDEX filelist row is not a list")
            cells = [cell for cell in row if isinstance(cell, Mapping)]
            file_cell = next(
                (
                    cell
                    for cell in cells
                    if cell.get("is_file") is True
                    or cell.get("name") == "File Name"
                    or isinstance(cell.get("url"), str)
                ),
                None,
            )
            if file_cell is None:
                continue
            name = file_cell.get("value")
            url = file_cell.get("url")
            if not isinstance(name, str) or not name.strip():
                raise MetadataError("GDEX filelist file row has no file name")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise MetadataError(
                    "GDEX filelist file row has no absolute data URL",
                    detail={"file": name},
                )
            by_name = {
                str(cell.get("name")): cell for cell in cells if isinstance(cell.get("name"), str)
            }
            size_cell = by_name.get("Size", {})
            size = parse_size_bytes(size_cell.get("value"), field="filelist.Size")
            if size is None or size <= 0:
                raise MetadataError(
                    "GDEX filelist file row has no positive size",
                    detail={"file": name, "observed": json_safe(size_cell.get("value"))},
                )
            format_cell = by_name.get("Data Format", {})
            date_cell = by_name.get("Date Archived", {})
            data_path = file_cell.get("data_path")
            files.append(
                RdaFileListFile(
                    file_name=name.strip(),
                    url=url.strip(),
                    data_path=data_path.strip() if isinstance(data_path, str) else None,
                    size_bytes=size,
                    data_format=(
                        format_cell.get("value")
                        if isinstance(format_cell.get("value"), str)
                        else None
                    ),
                    date_archived=parse_timestamp(
                        date_cell.get("value"),
                        field="filelist.Date Archived",
                        allow_date_only=True,
                        date_only_assume_utc=True,
                    ),
                    gindex=gindex,
                    raw={str(key): json_safe(value) for key, value in file_cell.items()},
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

    def _get_json(
        self,
        path: str,
        *,
        context: str,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        url = f"{self._endpoint}{_API_ROOT}{path}"
        try:
            if timeout_seconds is None:
                response = self._client().get(url)
            else:
                response = self._client().get(url, timeout=httpx.Timeout(timeout_seconds))
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

    def list_file_list(
        self,
        dsid: str,
        gindex: int,
        year: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[RdaFileListFile, ...]:
        """列出大分组在指定年份的文件清单（``filelist/{year}{gindex}``）。"""
        if year < 1900 or year > 2200:
            raise MetadataError(
                "filelist year is outside the supported range", detail={"year": year}
            )
        payload = self._get_json(
            f"/datasets/{dsid}/filelist/{year}{gindex}/",
            context="filelist",
            timeout_seconds=timeout_seconds,
        )
        return parse_file_list(payload)

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
            "NCSS variable -> file naming rules are dataset-specific; configure "
            "rda.ncss.variables[].filename_contains instead of guessing",
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
                "GDEX large-group filelist /filelist/{year}{gindex}/ parsing "
                "(real response shape with 2024 pressure-level evidence)",
                "THREDDS NetcdfSubset URL/size probing when rda.ncss is explicitly configured",
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
        if self._ncss_config(request) is not None:
            return self._discover_ncss_assets(request, dsid)
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

    # -- NCAR THREDDS NetcdfSubset ---------------------------------------- #
    def _ncss_config(self, request: DatasetRequest) -> Mapping[str, Any] | None:
        """返回 NCSS 配置；请求扩展优先于数据源配置。"""
        candidate = request.extensions.get("rda.ncss")
        if candidate is None and self._config is not None:
            candidate = self._config.extensions.get("rda.ncss")
        if candidate is None:
            return None
        if not isinstance(candidate, Mapping):
            raise MetadataError(
                "rda.ncss must be an object with a 'variables' mapping",
                detail={"observed": json_safe(candidate)},
            )
        return candidate

    def _ncss_rules(
        self, request: DatasetRequest, config: Mapping[str, Any]
    ) -> tuple[_NcssVariableRule, ...]:
        raw_variables = config.get("variables")
        if not isinstance(raw_variables, Mapping) or not raw_variables:
            raise MetadataError(
                "rda.ncss.variables must be a non-empty mapping",
                detail={"observed": json_safe(raw_variables)},
            )
        requested = request.required_variables()
        rules: list[_NcssVariableRule] = []
        for logical_raw, spec in raw_variables.items():
            logical = str(logical_raw).strip().lower()
            if logical not in requested:
                continue
            if not isinstance(spec, Mapping):
                raise MetadataError(
                    "each rda.ncss.variables entry must be an object",
                    detail={"variable": logical, "observed": json_safe(spec)},
                )
            parameter = spec.get("var")
            contains = spec.get("filename_contains")
            if not isinstance(parameter, str) or not parameter.strip():
                raise MetadataError(
                    "rda.ncss variable entry requires non-empty 'var'",
                    detail={"variable": logical},
                )
            if not isinstance(contains, str) or not contains.strip():
                raise MetadataError(
                    "rda.ncss variable entry requires non-empty 'filename_contains' "
                    "(the variable-to-file naming rule is dataset-specific)",
                    detail={"variable": logical},
                )
            rules.append(
                _NcssVariableRule(
                    logical_name=logical,
                    parameter=parameter.strip(),
                    filename_contains=contains.strip(),
                )
            )
        return tuple(rules)

    def _discover_ncss_assets(self, request: DatasetRequest, dsid: str) -> Sequence[SourceAsset]:
        config = self._ncss_config(request)
        if config is None:
            raise MetadataError("NCSS discovery requires rda.ncss configuration")
        rules = self._ncss_rules(request, config)
        if not rules:
            raise SourceNotFoundError(
                "rda.ncss.variables does not cover any requested variable",
                source_id=self.source_id,
                detail={"requested": sorted(request.required_variables())},
            )
        max_bytes_raw = request.extensions.get("rda.ncss.max_bytes", config.get("max_bytes"))
        if max_bytes_raw is None:
            max_bytes = _DEFAULT_NCSS_MAX_BYTES
        elif (
            isinstance(max_bytes_raw, bool)
            or not isinstance(max_bytes_raw, int)
            or max_bytes_raw <= 0
        ):
            raise MetadataError(
                "rda.ncss.max_bytes must be a positive integer",
                detail={"observed": json_safe(max_bytes_raw)},
            )
        else:
            max_bytes = max_bytes_raw
        timeout_raw = config.get("timeout_seconds", 120.0)
        if (
            isinstance(timeout_raw, bool)
            or not isinstance(timeout_raw, (int, float))
            or timeout_raw <= 0
        ):
            raise MetadataError(
                "rda.ncss.timeout_seconds must be a positive number",
                detail={"observed": json_safe(timeout_raw)},
            )
        timeout_seconds = float(timeout_raw)
        filelist_timeout_raw = config.get("filelist_timeout_seconds", 120.0)
        if (
            isinstance(filelist_timeout_raw, bool)
            or not isinstance(filelist_timeout_raw, (int, float))
            or filelist_timeout_raw <= 0
        ):
            raise MetadataError(
                "rda.ncss.filelist_timeout_seconds must be a positive number",
                detail={"observed": json_safe(filelist_timeout_raw)},
            )
        filelist_timeout = float(filelist_timeout_raw)
        endpoint_raw = config.get("endpoint")
        endpoint = (
            endpoint_raw.rstrip("/")
            if isinstance(endpoint_raw, str) and endpoint_raw.strip()
            else _DEFAULT_NCSS_ENDPOINT
        )
        interval_raw = config.get("file_interval_seconds")
        if interval_raw is not None and (
            isinstance(interval_raw, bool)
            or not isinstance(interval_raw, (int, float))
            or interval_raw <= 0
        ):
            raise MetadataError(
                "rda.ncss.file_interval_seconds must be a positive number when provided",
                detail={"observed": json_safe(interval_raw)},
            )
        file_interval = (
            timedelta(seconds=float(interval_raw))
            if isinstance(interval_raw, (int, float))
            else None
        )
        stride_raw = config.get("time_stride", 1)
        if isinstance(stride_raw, bool) or not isinstance(stride_raw, int) or stride_raw < 1:
            raise MetadataError(
                "rda.ncss.time_stride must be a positive integer",
                detail={"observed": json_safe(stride_raw)},
            )

        catalog = self.catalog()
        time_filter = self._time_filter()
        years = _years_for_extent(
            request.metadata.temporal_coverage.start,
            request.metadata.temporal_coverage.end,
        )
        file_pools: dict[int, tuple[RdaFileListFile, ...]] = {}
        for year in years:
            file_pools[year] = catalog.list_file_list(
                dsid,
                self._resolve_gindex(request),
                year,
                timeout_seconds=filelist_timeout,
            )

        license_text = self._cached_license(dsid, catalog)
        spatial = request.metadata.spatial_extent
        assets: list[SourceAsset] = []
        seen_files: set[tuple[str, str]] = set()
        for rule in rules:
            matched_rule_file = False
            for year in years:
                for item in file_pools[year]:
                    if rule.filename_contains not in item.file_name:
                        continue
                    matched_rule_file = True
                    key = (rule.logical_name, item.url)
                    if key in seen_files:
                        continue
                    overlap = _file_time_overlap(
                        item.file_name,
                        request.metadata.temporal_coverage.start,
                        request.metadata.temporal_coverage.end,
                        time_filter=time_filter,
                        file_interval=file_interval,
                    )
                    if overlap is None:
                        continue
                    window_start, window_end, file_end = overlap
                    url = _build_ncss_url(
                        endpoint=endpoint,
                        dsid=dsid,
                        data_path=item.data_path or urlsplit(item.url).path,
                        parameter=rule.parameter,
                        request=request,
                        start=window_start,
                        end=min(window_end, file_end + timedelta(seconds=1))
                        if file_end is not None
                        else window_end,
                        time_stride=stride_raw,
                    )
                    size = self._ncss_content_length(url, max_bytes, timeout_seconds)
                    metadata_extensions: dict[str, JsonValue] = {
                        "rda.dsid": dsid,
                        "rda.gindex": item.gindex,
                        "rda.service": "THREDDS-NetcdfSubset",
                        "rda.variable": rule.parameter,
                        "rda.variable_names": [rule.logical_name],
                        "rda.no_cds": True,
                        "rda.file_name": item.file_name,
                        "rda.size_source": "ncss_head",
                        "rda.checksum": "unknown",
                        "rda.subset.lat": [spatial.south, spatial.north],
                        "rda.subset.lon": [spatial.west, spatial.east],
                        "rda.subset.levels_hpa": list(request.metadata.vertical_levels.levels),
                        "rda.subset.time": [window_start.isoformat(), window_end.isoformat()],
                        "rda.ncss.max_bytes": max_bytes,
                        "transfer.head_probe": "skip",
                    }
                    metadata = build_source_metadata(
                        source_id=self.source_id,
                        display_name=(
                            self._config.display_name
                            if self._config is not None
                            else f"RDA {dsid} NCSS"
                        ),
                        provider=(
                            self._config.provider if self._config is not None else "NSF NCAR RDA"
                        ),
                        access_route=AccessRoute.HTTPS,
                        dataset_id=dsid,
                        origin_uri=url,
                        retrieved_at=self.clock(),
                        protocol_version="rda-thredds-ncss/1.0",
                        license=license_text,
                        citation="ECMWF ERA5; NSF NCAR RDA THREDDS NetcdfSubset",
                        extensions=metadata_extensions,
                    )
                    file_name = (
                        f"ncar_{dsid}_{rule.logical_name}_"
                        f"{window_start:%Y%m%dT%H%MZ}_{window_end:%Y%m%dT%H%MZ}_ncss.nc"
                    )
                    assets.append(
                        build_asset(
                            source_id=self.source_id,
                            dataset_id=request.dataset_id,
                            access_route=AccessRoute.HTTPS,
                            origin_uri=url,
                            file_name=file_name,
                            size_bytes=size,
                            metadata=metadata,
                            discovered_at=self.clock(),
                            asset_id=asset_id_for(self.source_id, f"ncss:{key[0]}:{url}"),
                            checksum=None,
                            content_type="application/x-netcdf",
                            time_slice=TemporalExtent(start=window_start, end=window_end),
                            level_slice=(
                                list(request.metadata.vertical_levels.levels)
                                if request.metadata.vertical_levels.levels
                                else None
                            ),
                        )
                    )
                    seen_files.add(key)
                    if len(assets) > self._max_web_files:
                        raise SourceUnavailableError(
                            f"NCSS discovery produced more than {self._max_web_files} assets; "
                            "narrow the time range or raise max_web_files",
                            source_id=self.source_id,
                            detail={"dsid": dsid, "assets": len(assets)},
                        )
            if not matched_rule_file:
                logger.warning(
                    "NCSS filename rule did not match %s in years %s", rule.logical_name, years
                )
        if not assets:
            raise SourceNotFoundError(
                "NCSS discovery found no files matching the configured variable rules "
                "and time range",
                source_id=self.source_id,
                detail={"dsid": dsid, "years": list(years)},
            )
        return tuple(assets)

    def _ncss_content_length(self, url: str, max_bytes: int, timeout_seconds: float) -> int:
        """HEAD 一次 NCSS 生成结果，并执行单请求大小闸门。"""
        try:
            response = self.http.head(url, timeout=httpx.Timeout(timeout_seconds))
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(
                "NCAR NCSS HEAD failed",
                source_id=self.source_id,
                detail={"url": url, "error_type": type(exc).__name__},
            ) from exc
        try:
            if response.status_code != httpx.codes.OK:
                raise SourceUnavailableError(
                    f"NCAR NCSS HEAD returned HTTP {response.status_code}",
                    source_id=self.source_id,
                    detail={"url": url, "status": response.status_code},
                )
            raw = response.headers.get("Content-Length")
            try:
                size = int(raw) if raw is not None else 0
            except ValueError as exc:
                raise MetadataError(
                    "NCAR NCSS Content-Length is not an integer",
                    detail={"url": url, "observed": raw},
                ) from exc
            if size <= 0:
                raise MetadataError(
                    "NCAR NCSS did not provide a positive Content-Length",
                    detail={"url": url},
                )
            if size > max_bytes:
                raise SourceUnavailableError(
                    f"NCAR NCSS response {size} B exceeds configured limit {max_bytes} B",
                    source_id=self.source_id,
                    detail={"url": url, "size_bytes": size, "max_bytes": max_bytes},
                )
            return size
        finally:
            response.close()

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


def _years_for_extent(start: datetime, end: datetime) -> tuple[int, ...]:
    """返回闭开时间区间覆盖的年份（end 恰为新年零点时不误加一年）。"""
    last = end - timedelta(microseconds=1) if end > start else start
    return tuple(range(start.year, last.year + 1))


def _file_time_overlap(
    file_name: str,
    request_start: datetime,
    request_end: datetime,
    *,
    time_filter: tuple[re.Pattern[str], str] | None,
    file_interval: timedelta | None,
) -> tuple[datetime, datetime, datetime | None] | None:
    """由显式文件名规则计算请求与文件的交集；没有规则时不猜文件时间。"""
    if time_filter is None:
        return request_start, request_end, None
    compiled, format_text = time_filter
    match = compiled.search(file_name)
    if match is None:
        return None
    try:
        file_start = datetime.strptime(match.group("start"), format_text).replace(tzinfo=UTC)
        file_end = datetime.strptime(match.group("end"), format_text).replace(tzinfo=UTC)
    except ValueError as exc:
        raise MetadataError(
            "rda.file_time.format does not match the captured file timestamps",
            detail={"file": file_name, "format": format_text},
        ) from exc
    file_exclusive_end = file_end + (file_interval or timedelta(seconds=1))
    window_start = max(request_start, file_start)
    window_end = min(request_end, file_exclusive_end)
    if window_start >= window_end:
        return None
    return window_start, window_end, file_end


def _build_ncss_url(
    *,
    endpoint: str,
    dsid: str,
    data_path: str,
    parameter: str,
    request: DatasetRequest,
    start: datetime,
    end: datetime,
    time_stride: int,
) -> str:
    """按显式变量/区域/时段生成 THREDDS NetcdfSubset URL。"""
    if not data_path.startswith(f"/{dsid}/"):
        raise MetadataError(
            "GDEX filelist data_path does not belong to the resolved dataset",
            detail={"dsid": dsid, "data_path": data_path},
        )
    query_end = end - timedelta(seconds=1)
    if query_end < start:
        query_end = start
    spatial = request.metadata.spatial_extent
    query = urlencode(
        [
            ("var", parameter),
            ("north", f"{spatial.north:g}"),
            ("west", f"{spatial.west:g}"),
            ("east", f"{spatial.east:g}"),
            ("south", f"{spatial.south:g}"),
            ("horizStride", "1"),
            ("time_start", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("time_end", query_end.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("timeStride", str(time_stride)),
            ("accept", "netcdf"),
        ]
    )
    return f"{endpoint.rstrip('/')}{quote(data_path, safe='/')}?{query}"


def ncar_adapter_from_config(source_id: str, config: SourceConfig) -> NCARAdapter:
    """从 :class:`SourceConfig` 构造 :class:`NCARAdapter`（供 SourceRegistry 工厂使用）。"""
    return NCARAdapter(source_id=source_id, config=config)
