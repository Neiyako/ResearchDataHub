"""数据源适配器：发现远端资产、解析元数据，**不搬运字节**。

接口定义见 :mod:`researchdatahub.core.ports` 的 ``DataSourceAdapter``；本包提供：

* :class:`~researchdatahub.sources.base.SourceAdapter` —— 公共基类与元数据标准化工具；
* :class:`~researchdatahub.sources.cds.CDSAdapter` —— Copernicus CDS（异步请求生命周期）；
* :class:`~researchdatahub.sources.ncar.NCARAdapter` —— NSF NCAR GDEX/RDA（HTTPS 目录 API）；
* :class:`~researchdatahub.sources.registry.SourceRegistry` —— 注册表与工厂。

边界（ADR-0002）：本包只产出 :class:`~researchdatahub.core.models.SourceAsset`
（含 ``origin_uri`` / ``access_route`` / ``size_bytes`` / 可选 ``checksum``），
下载、重试、落盘属于 ``transfer`` 包，二者互不 import。
"""

from researchdatahub.sources.base import AdapterCapabilities, SourceAdapter
from researchdatahub.sources.cds import (
    CDSAdapter,
    CdsJobSnapshot,
    CdsJobState,
    CdsProcessSchema,
    CdsRequestLifecycle,
    CdsRequestTranslator,
    CdsSubmission,
)
from researchdatahub.sources.ncar import GdexCatalogClient, NCARAdapter
from researchdatahub.sources.registry import SourceEntry, SourceRegistry

__all__ = [
    "AdapterCapabilities",
    "CDSAdapter",
    "CdsJobSnapshot",
    "CdsJobState",
    "CdsProcessSchema",
    "CdsRequestLifecycle",
    "CdsRequestTranslator",
    "CdsSubmission",
    "GdexCatalogClient",
    "NCARAdapter",
    "SourceAdapter",
    "SourceEntry",
    "SourceRegistry",
]
