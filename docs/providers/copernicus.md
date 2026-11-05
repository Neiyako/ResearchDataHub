# Copernicus Data Space Ecosystem Provider（`providers/copernicus/`）

面向 AI Coding Agent 的 **CDSE 数据集目录与能力**封装，走 **STAC** 与 **OData** 两条真实路径。

> ⚠️ **CDSE ≠ CDS**。CDS（`cds.climate.copernicus.eu`，ECMWF）是气候再分析（ERA5），
> 已有实现 `sources/cds.py`；CDSE（`dataspace.copernicus.eu`）是哥白尼**卫星**数据
> （Sentinel-1/2/3/5P、CLMS 等）。两者端点、认证、数据模型都不同，本 Provider
> **不**复用 `cdsapi`，也**不**声称能取 ERA5。

- 代码：`src/researchdatahub/providers/copernicus/`（`cdse.py` 解析 / `auth.py` OAuth2 /
  `provider.py` 契约实现）
- 测试：`tests/unit/test_providers/test_copernicus_provider.py`（离线，用真实响应样本）
  + `tests/integration/test_providers_network.py`（`-m network`）
- 实现日期：**2026-11-05**

---

## 1. 真实端点（全部 curl 实测）

| 用途 | 端点 | 实测结果（2026-11-05） |
|---|---|---|
| STAC 落地文档 | `GET /stac/` | **200**，`{"type":"Catalog","id":"cdse-stac","stac_version":"1.0.0"}` |
| STAC 集合列表 | `GET /stac/collections` | **200**，`numberMatched=428`，`numberReturned=10` |
| STAC 集合详情 | `GET /stac/collections/{id}` | **200**（`sentinel-2-l2a`） |
| STAC 检索 | `POST /stac/search` | **200**，GeoJSON `FeatureCollection` |
| OData 产品 | `GET /odata/v1/Products` | **200**（含 `$top`/`$filter`/`$expand`/`$orderby`） |
| OData 属性 schema | `GET /odata/v1/Attributes` | **200**（26 个集合名 → 属性名列表） |
| OIDC 发现 | `GET https://identity.dataspace.copernicus.eu/auth/realms/CDSE/.well-known/openid-configuration` | **200** |
| Token | `POST /auth/realms/CDSE/protocol/openid-connect/token` | **401**（用故意无效的 client 探测形状） |
| ❌ OData 集合列表 | `GET /odata/v1/Collections` | **404** —— 因此不实现，集合枚举只走 STAC |
| ❌ OData 根 / 元数据 | `GET /odata/v1/`、`GET /odata/v1/$metadata` | **404** |

### 1.1 STAC 集合列表

```bash
curl -sS -m 30 "https://catalogue.dataspace.copernicus.eu/stac/collections" \
  -o stac_collections.json -w "HTTP=%{http_code} bytes=%{size_download}\n"
```

实测：`HTTP=200 bytes=41336`，`numberMatched=428`、`numberReturned=10`。

关键字段：`collections[].{id, title, description, license, keywords, extent, providers,
auth:schemes, storage:schemes}`；`links` 里分页指向 **`https://stac.dataspace.copernicus.eu/v1/...`**
（与 `catalogue.dataspace.copernicus.eu` 是**不同主机**）。

**踩坑记录 1（最重要）**：`GET /stac/collections` **不认小 `limit`**，且排序与 `q` 的相关性无关：

```bash
# 不传 limit 与 limit=5 都只返回 10 条；q=sentinel-2 命中的 145 条里，前 10 条全是 CLMS 产品
curl -sS "https://catalogue.dataspace.copernicus.eu/stac/collections?q=sentinel-2&limit=5"
#   numberMatched=145, 返回的前 5 条: clms_ba_global_300m_daily_v3_cog, ...

# limit=1000 才一次返回全部 145 条命中（耗时约 74s）
curl -sS -m 90 "https://catalogue.dataspace.copernicus.eu/stac/collections?limit=1000&q=sentinel-2"
#   numberReturned=145, 其中包含 sentinel-1-*, sentinel-2-l1c, sentinel-2-l2a, sentinel-3-*
```

因此本 Provider 的做法是：**每页 1000 条翻页取全量 → 本地按相关性排序 → 再截 `limit`**
（`CDSE_COLLECTION_PAGE_SIZE=1000`、`CDSE_MAX_COLLECTION_PAGES=4`）。若只发一次 `limit=5`，
`search("sentinel-2")` 会返回 CLMS 产品——那是真实踩到的错误结果。

### 1.2 STAC 集合详情

```bash
curl -sS -m 60 "https://catalogue.dataspace.copernicus.eu/stac/collections/sentinel-2-l2a" \
  -o stac_s2collection.json -w "HTTP=%{http_code} bytes=%{size_download}\n"
```

实测：`HTTP=200 bytes=25596`

真实结构要点：

```json
{
  "id": "sentinel-2-l2a",
  "title": "Sentinel-2 Level-2A",
  "license": "other",                         // ← STAC 合法值，但不是 SPDX 标识
  "sci:citation": "Copernicus Sentinel data [Year]",
  "sci:doi": "10.5270/S2_-znk9xsj",
  "extent": {
    "spatial":  {"bbox": [[-180, -90, 180, 90]]},
    "temporal": {"interval": [["2015-06-27T10:25:31Z", null]]}   // ← 结束时间为 null（仍在更新）
  },
  "providers": [{"name": "ESA", "roles": ["producer"]}, ...],
  "bands": [{"name": "B01", "description": "Coastal aerosol (band 1)", "gsd": 60, "eo:common_name": "coastal"}, ...],
  "item_assets": {"AOT_10m": ..., "B01_60m": ..., ...},
  "summaries": {"gsd": [10], ...},
  "auth:schemes": {"s3": {...}, "oidc": {"type": "openIdConnect", "openIdConnectUrl": "..."}},
  "storage:schemes": {"cdse-s3": {...}, "creodias-s3": {...}}
}
```

**踩坑记录 2**：`extent.temporal.interval` 的结束时间可以是 `null`。这不等于"未知结束时间"，
而是"仍在更新"。本实现返回 `temporal_coverage=None` + `ends_at_present=True`，并在
`notes` 里说明"未推断结束日期"；`validate_request` 只用**起点**做硬校验。

**踩坑记录 3**：STAC **不声明物理单位**。`bands[].gsd` 是地面分辨率（米），不是物理单位，
因此 `DatasetVariable.units` 保持 `None`，gsd 记在 `data_type` 里（`"gsd=10m"`）。

### 1.3 STAC 检索

```bash
curl -sS -m 40 -X POST "https://catalogue.dataspace.copernicus.eu/stac/search" \
  -H "Content-Type: application/json" \
  -d '{"collections":["sentinel-2-l2a"],"limit":2,"bbox":[10,45,11,46],
       "datetime":"2024-06-01T00:00:00Z/2024-06-30T23:59:59Z"}' \
  -o stac_search.json -w "HTTP=%{http_code} bytes=%{size_download} t=%{time_total}s\n"
```

实测：`HTTP=200 bytes=143261 t=11.039667s`

真实 feature 的关键字段：

```json
{
  "id": "S2A_MSIL2A_20240629T102021_N0510_R065_T32TPS_20240629T152049",
  "collection": "sentinel-2-l2a",
  "bbox": [10.290, 45.939, 11.755, 46.946],
  "links": [{"rel": "enclosure", "href": "s3://eodata/Sentinel-2/.../...SAFE/", "auth:refs": ["s3"]}, ...],
  "assets": {
    "AOT_10m": {
      "href": "s3://eodata/Sentinel-2/.../AOT_10m.jp2",
      "file:size": 8807570,
      "file:checksum": "162063c094860aa3...",          // ← 小写 hex，算法未在响应里声明
      "alternate": {"https": {
        "href": "https://download.dataspace.copernicus.eu/odata/v1/Products(a1134837-...)/Nodes(...)/$value",
        "auth:refs": ["oidc"]                          // ← 下载需要 Bearer
      }},
      "raster:scale": 0.001, "proj:code": "EPSG:32632", ...
    }, ...
  }
}
```

**踩坑记录 4**：并非所有资产的 `alternate.https` 都指向 CDSE。实测有一个资产指向
**CREODIAS**（`https://datahub.creodias.eu/...`）——那是**另一套服务**。本实现只收主机为
`https://download.dataspace.copernicus.eu` 的 URL（`CDSE_HTTPS_DOWNLOAD_HOST`）。

### 1.4 OData 产品

```bash
curl -sS -m 40 "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?\$top=2&\$expand=Attributes&\$filter=Collection/Name%20eq%20%27SENTINEL-2%27&\$orderby=ContentDate/Start%20desc" \
  -o odata_products.json -w "HTTP=%{http_code} bytes=%{size_download} t=%{time_total}s\n"
```

实测：`HTTP=200 bytes=10705 t=8.937807s`

```json
{
  "@odata.context": "$metadata#Products(Attributes())",
  "@odata.nextLink": "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?...&%24skip=2",
  "value": [{
    "Id": "018805e8-b419-4040-b6e0-12b3cd883320",
    "Name": "S2C_MSIL2A_20261105T124031_N0513_R052_T26SPF_20261105T134312.SAFE",
    "ContentDate": {"Start": "2026-11-05T12:40:31.025000Z", "End": "2026-11-05T12:40:31.025000Z"},
    "ContentLength": 607269158,                 // ← 字节
    "S3Path": "/eodata/Sentinel-2/MSI/L2A/2026/11/05/....SAFE",
    "Online": true,
    "GeoFootprint": {"type": "Polygon", "coordinates": [[[...]]]},
    "Footprint": "geography'SRID=4326;POLYGON ((...))",
    "Checksum": [
      {"Value": "31aeeec5930af4f765efca3af4d98566", "Algorithm": "MD5"},
      {"Value": "92ced0265141b32e5c748fb6b85d1ef9c8ab323bdf63b1417f9e93914beb7387", "Algorithm": "BLAKE3"}
    ],
    "Attributes": [
      {"Name": "origin", "Value": "ESA", "ValueType": "String"},
      {"Name": "tileId", "Value": "26SPF"},
      {"Name": "cloudCover", "Value": 47.305813, "ValueType": "Double"},
      {"Name": "orbitNumber", "Value": 11319, "ValueType": "Integer"}, ...
    ]
  }]
}
```

**踩坑记录 5**：`ContentDate.Start` 与 `ContentDate.End` 对单景产品**完全相同**
（`2026-11-05T12:40:31.025000Z`）——那是"成像时刻"而不是区间。核心契约 `TemporalExtent`
要求 `start < end`，因此 `OdataProduct.temporal_extent()` 返回 `None`，**不**人为加一秒凑区间
（否则会编造不存在的时间覆盖）。

### 1.5 OData 属性 schema

```bash
curl -sS -m 30 "https://catalogue.dataspace.copernicus.eu/odata/v1/Attributes?\$top=2"
```

实测：`HTTP=200 bytes=23840`，顶层是 `{集合名: [{"Name","ValueType"}, ...]}`，共 26 个集合名
（`COP-DEM` / `SENTINEL-2` / `SENTINEL-1` / `CLMS` / `ERA5` …）。

**踩坑记录 6**：这是**产品属性**（`cloudCover`、`orbitNumber`），**不是科学变量**。
且它按 OData 的 `SENTINEL-2` 分组，与 STAC 的 `sentinel-2-l2a` **不是一一对应**。本实现
只在 id **精确命中**时才给出属性名，否则输出 `cdse.odata_attribute_source="unmapped"`
（不猜 "sentinel-2-l2a" → "SENTINEL-2" 的映射）。

### 1.6 OAuth2 token

```bash
# 用故意无效的 client 探测响应形状（不是真实凭证）
curl -sS -m 30 -X POST \
  "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=INVALID_PROBE_ONLY&client_secret=INVALID_PROBE_ONLY" \
  -w "\nHTTP=%{http_code}\n"
```

实测：`HTTP=401` `{"error":"invalid_client","error_description":"Invalid client or Invalid client credentials"}`

这说明 `grant_type=client_credentials` 与参数名被接受，失败原因是**凭证本身**。
`/.well-known/openid-configuration`（实测 200）确认：

- `issuer` = `https://identity.dataspace.copernicus.eu/auth/realms/CDSE`
- `token_endpoint` = `.../protocol/openid-connect/token`
- `grant_types_supported` 含 `client_credentials`

**未验证**：成功换取 token 的路径（本机无凭证）。代码按 RFC 6749 解析
`access_token` / `expires_in` / `token_type`，并在响应缺 `access_token` 时显式失败。

### 1.7 错误形状

| 场景 | 实测响应 | 本实现的映射 |
|---|---|---|
| 集合不存在 | `HTTP 404` `{"code":"NotFoundError","description":"Collection ... does not exist."}` | `SourceNotFoundError` |
| 不存在的 OData 端点 | `HTTP 404` `{"detail":"Not Found"}` | 不实现（`/Collections` 等） |
| 5xx / 429 | — | `RetryableTransportError` |
| 401 / 403 | — | `SourceUnavailableError` |

---

## 2. 认证（OAuth2 client credentials）

- 环境变量：`CDSE_CLIENT_ID` + `CDSE_CLIENT_SECRET`（**二者都存在**才算已配置）。
- `CdseTokenCache`：内存缓存 + 提前刷新窗口（`expires_in - 60s`）+ `invalidate()` 应对 401。
- 安全约束（单元测试覆盖）：
  - token **永不落盘、永不进日志**；`repr()` / `str()` 不含 token 与 secret；
  - 先做锁内命中判断、**锁外发网络请求**（避免持锁 I/O 与死锁）；
  - 401 只提取 `error` / `error_description`，不带请求头。
- **匿名可读的元数据**（STAC/OData 检索）不需要 token。

**本机 2026-11-05 实测状态**：`CDSE_CLIENT_ID` / `CDSE_CLIENT_SECRET` 均未设置 →
"带 Bearer 的 `/odata/v1/Products({Id})/$value` 下载"记为 **unverified**。

---

## 3. 能力矩阵（逐项如实标注）

| 能力 | 状态 | 证据 / 说明 |
|---|---|---|
| `search`（STAC 集合检索） | **verified** | `GET /stac/collections` 200 匿名（含 `q`、`numberMatched`）；全量翻页 + 本地排序 |
| `inspect`（STAC 集合详情） | **verified** | `GET /stac/collections/{id}` 200 匿名 |
| `get_metadata` | **verified** | STAC 摘要 + OData 属性 schema（精确命中才给名字） |
| `get_variables`（STAC bands） | **verified** | `sentinel-2-l2a` 实测 12 个波段条目；无 `bands` 的集合返回 `None` |
| `validate_request` | **verified** | 基于 STAC 时间/空间声明；`end=null` 时只用起点硬校验 |
| `estimate_request` | **verified** | `POST /stac/search` 的 `assets[].file:size` 汇总 |
| OData 产品检索（`/odata/v1/Products`） | **verified** | 200 匿名；`ContentLength`、`Checksum(MD5/BLAKE3)`、`GeoFootprint` |
| OData 属性 schema（`/odata/v1/Attributes`） | **verified** | 200 匿名（但**不是**变量清单） |
| `submit_task` / `get_task_status` / `retrieve_result` / `cancel_task` | **unsupported** | CDSE 目录**没有**作业语义 → 抛 `NotImplementedFeatureError` |
| Bearer 下载 `/odata/v1/Products({Id})/$value` | **unverified** | 本机无 `CDSE_CLIENT_ID`/`CDSE_CLIENT_SECRET`，未做真实下载 |
| Token 成功换取路径 | **unverified** | 只观测到 401 `invalid_client` 形状 |
| S3 直连（`s3://eodata`） | **unsupported** | STAC 里能看到 `s3://eodata/...` 与 `auth:schemes.s3`，但凭证与端点未验证，**不写假实现** |
| 云处理（Sentinel Hub / On-Demand） | **unsupported** | 属于另一套授权体系，未验证 |
| OData `/Collections` 枚举 | **unsupported** | 实测 404；集合枚举只走 STAC |

`capabilities().notes` 里还写明"unsupported access paths（s3_direct / cloud_processing）"
与"`/Collections` 返回 404"，避免调用方误以为存在。

---

## 4. 公开 API（供 MCP 层调用）

```python
from researchdatahub.providers import DatasetFilter, get_provider

with get_provider("copernicus") as provider:
    caps = provider.capabilities()
    candidates = provider.search("sentinel-2", DatasetFilter(limit=5))
    detail = provider.inspect("sentinel-2-l2a")            # DatasetDetail
    variables = provider.get_variables("sentinel-2-l2a")   # 12 个真实波段
    metadata = provider.get_metadata("sentinel-2-l2a")     # Mapping[str, JsonValue]
    estimate = provider.estimate_request("sentinel-2-l2a",
                                         DatasetFilter(limit=2, temporal=window,
                                                       bbox=(10, 45, 11, 46)))
```

- `dataset_id` 就是 **STAC collection id**（如 `sentinel-2-l2a`、`clms_ba_global_300m_daily_v4_cog`）。
- `translate_query()` 会明确写清"为什么 `limit` 是本地生效的"（见 1.1 的踩坑记录），
  以及 `short_name` / `platform` / `temporal` 在集合检索里没有实测等价参数。
- `estimate_request()` 的明细显式标注每条 item 的 `download_requires_bearer: true`。

---

## 5. 与 `sources/` 的职责边界（ADR-0008）

| 层 | 包 | 输入 | 输出 | 面向 |
|---|---|---|---|---|
| 目录与能力 | `providers/copernicus/`（本模块） | 检索条件（关键词/时空） | `DatasetCandidate` / `DatasetDetail` / `ProviderCapabilities` | **Agent**（MCP `search_datasets` 等） |
| 资产发现与执行 | `sources/`（现有 CDS/NCAR 适配器） | 已校验的 `DatasetRequest` | `SourceAsset`（文件级） | 执行层（planner → scheduler → transfer） |

- 本 Provider **不下载字节、不写文件、不绕过 `integrity`**。
- `providers/` **不 import** `sources/`（AST 静态检查 + 运行期检查双重兜底）。
- **V1 不提供桥接**（写作用域禁止修改 `sources/`）。**后续桥接方案**：
  1. `providers.inspect()` → Agent 确认 → 构造 `DatasetRequest`；
  2. 新增 `sources/cdse.py`（`SourceAdapter` 子类）：用 `POST /stac/search` 列出 item，
     把每个 item 的**唯一**下载目标（优先 OData `$value`，需要 `CdseTokenCache`）映射为
     `SourceAsset(origin_uri=..., size_bytes=assets 汇总)`；
  3. 传输层复用现有 HTTPS 适配器并注入 `Authorization: Bearer`；
  4. 届时把"带 Bearer 的下载"从 unverified 提升为 verified，需要真实凭证 + 真实字节校验。

---

## 6. 运行测试

```bash
# 离线（默认套件排除 network 标记）
.venv/bin/python -m pytest tests/unit/test_providers -q

# 真实联网
.venv/bin/python -m pytest tests/integration/test_providers_network.py -m network -q
```

`tests/unit/test_providers/fixtures/` 下的样本全部是 2026-11-05 的真实响应快照
（体积大的样本做了裁剪，并在文件内 `_provenance` 注明）。
