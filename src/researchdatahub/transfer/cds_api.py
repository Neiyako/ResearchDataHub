"""CDS API 传输适配器：把 ``access_route=cds_api`` 的结果文件下载下来。

为什么只有一个“路由判别”子类
-----------------------------
``CDSAdapter._discover_assets()`` 已经把 ``submit → poll → results`` 全流程走完，交到
调度器的 ``SourceAsset`` 里 ``origin_uri`` 是 **CDS 结果接口返回的临时签名 https URL**
（可直接 ``GET``，不需要 ``Authorization`` 头）。因此传输层要做的字节搬运与
:class:`~researchdatahub.transfer.http.HttpTransportAdapter` 完全一样：``httpx`` 流式
下载、``.part`` 暂存 + ``os.replace`` 原子提交、断点续传三重自证、大小严格校验、按
状态码分类的有限重试。两者只差两件事：

1. **路由判别**：只接管 ``access_route=cds_api`` 的资产。普通 ``https`` 传输不接管
   ``cds_api``（否则两个适配器争同一资产），本类也不接管 ``https`` 资产（不替普通
   下载改道）。
2. **``transport_id="cds_api"``**：事件、``TransferResult``、``.part`` sidecar 里的
   实现标识，把 CDS 结果下载与普通 https 下载分开归因。

所以本模块**只覆写 ``TRANSPORT_ID`` 与 ``_supports_asset``**，不复制任何传输逻辑；
流式/续传/重试语义的唯一来源仍是 ``transfer/http.py`` 与 ``transfer/base.py``。

真实边界（使用前必须知道）
--------------------------
* **临时签名 URL 会过期**。CDS 结果地址是短时效签名链接，过期后服务端通常回
  ``401``/``403``（HTTPS 交换层也可能是带过期说明的 403）。本适配器**不做自动
  re-resolve**：换一张新签名 URL 需要凭证重新 ``submit → poll``，那是数据源适配器 +
  计划器的职责，不在传输层。按 HTTP 适配器既有的状态码分类，``401``/``403`` 是
  **不可重试**的 ``http_access_denied``，任务以 FAILED 结束；恢复路径是重新执行
  ``rdh plan create``（产生新的 ``cds.request_id`` 与新 URL）再 ``rdh task run``。
  诊断时可用 ``SourceAsset.metadata.extensions["cds.request_id"]`` 判断是否同一个 CDS
  请求。
* **签名 URL 的校验器未必稳定**：若 ETag/Last-Modified 在两次探测之间变化，基类的
  ``decide_resume`` 会拒绝续传并整文件重下（安全侧失败：绝不拼接来源不同的前缀）。
* **URL 里带签名参数**（token 之类的查询串）：传输层把它当不透明 URI，不解析、不打印
  请求正文；但 ``origin_uri`` 会作为续传身份写进本地 sidecar（``~/.rdh`` 暂存区）。
  不要把这类 URL 粘贴进文档、issue 或聊天记录。
* 本次**没有**用真实 CDS 请求在调度器内跑过端到端（需要 CDS 凭证与实际排队），因此
  README 与 ``docs/development-plan.md`` 里该链路仍如实标为 NOT RUN。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from researchdatahub.core.models import AccessRoute, SourceAsset
from researchdatahub.transfer.http import HttpTransportAdapter

__all__ = ["CdsApiTransport"]


class CdsApiTransport(HttpTransportAdapter):
    """``cds_api`` 路由的传输实现：下载 CDS 结果签名 URL（语义全部继承 HTTP）。

    只覆盖路由判别与实现标识；构造参数、关闭语义、``.part``/sidecar/重试行为与
    :class:`~researchdatahub.transfer.http.HttpTransportAdapter` 完全一致。
    """

    TRANSPORT_ID = "cds_api"

    def _supports_asset(self, asset: SourceAsset) -> bool:
        """只接管 ``cds_api`` 路由且 ``origin_uri`` 是绝对 http(s) URL 的资产。"""
        if asset.access_route.value != AccessRoute.CDS_API.value:
            return False
        parts = urlsplit(asset.origin_uri)
        return parts.scheme in {"http", "https"} and bool(parts.hostname)
