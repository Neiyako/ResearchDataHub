# NASA Earthdata Provider（`providers/earthdata/`）

面向 AI Coding Agent 的 **NASA Earthdata 数据集目录与能力**封装。检索走 **CMR**
（Common Metadata Repository），元数据全部**匿名可读**；数据下载需要 **Earthdata Login
（EDL）** 凭证。

- 代码：`src/researchdatahub/providers/earthdata/`（`cmr.py` 解析 / `credentials.py` 凭证探测 /
  `provider.py` 契约实现）
- 契约：`src/researchdatahub/providers/base.py`（Provider Contract v1）
- 测试：`tests/unit/test_providers/test_earthdata_provider.py`（离线，用真实响应样本）
  + `tests/integration/test_providers_network.py`（`-m network`，真实联网）
- 实现日期：**2026-11-05**

---

## 1. 真实端点（全部 curl 实测）

CMR 根：`https://cmr.earthdata.nasa.gov`

| 用途 | 端点 | 实测结果（2026-11-05） |
|---|---|---|
| 集合检索 | `GET /search/collections.json` | **HTTP 200**，`t≈1.4s` |
| 集合详情（UMM-C） | `GET /search/collections.umm_json` | **HTTP 200**，`t≈2.5s` |
| 文件级检索（granule） | `GET /search/granules.json` | **HTTP 200**，`t≈2.5s` |
| 变量元数据 | `GET /search/variables.umm_json` | **HTTP 200**，`t≈2.8s` |

### 1.1 集合检索

```bash
curl -sS -m 30 -o cmr_collections.json -w "HTTP=%{http_code} bytes=%{size_download} t=%{time_total}s\n" \
  "https://cmr.earthdata.nasa.gov/search/collections.json?keyword=precipitation&page_size=3&provider=GES_DISC"
```

实测输出：`HTTP=200 bytes=64755 t=1.416049s`

关键字段（真实响应）：

```json
{
  "feed": {
    "updated": "...",
    "id": "...",
    "title": "ECHO dataset metadata",
    "entry": [{
      "id": "C2179081499-GES_DISC",          // ← concept-id 就在 id 字段
      "short_name": "GPM_2ADPR",
      "title": "GPM DPR Precipitation Profile L2A 1.5 hours 5 km V07 (GPM_2ADPR)",
      "summary": "Version 07 is the current version ...",
      "data_center": "GES_DISC",
      "time_start": "2014-03-08T00:00:00.000Z",
      "time_end": "2026-03-01T23:59:59.999Z",
      "boxes": ["-70 -180 70 180"],          // ← 顺序是 S W N E
      "version_id": "07",
      "processing_level_id": "2",
      "original_format": "UMM_JSON",
      "has_variables": true,
      "cloud_hosted": true,
      "links": [ ... ]
    }]
  }
}
```

**踩坑记录**：该响应里**没有** `concept-id` 字段（实测值为 `null`），concept-id 就是 `entry[].id`。

支持的查询参数（逐个 curl 验证为 200）：`keyword`、`short_name`、`provider`、`temporal`
（`start,end`）、`bounding_box`（`W,S,E,N`）、`page_size`、`page_num`、`sort_key`。

### 1.2 集合详情（UMM-C）

```bash
curl -sS -m 30 -o umm_collection.json -w "HTTP=%{http_code} bytes=%{size_download}\n" \
  "https://cmr.earthdata.nasa.gov/search/collections.umm_json?concept_id=C2179081499-GES_DISC"
```

实测：`HTTP=200 bytes=36788`

真实结构要点：

- 顶层是 `{"hits": 1, "took": N, "items": [{"meta": {...}, "umm": {...}}]}`（**不是** `feed`）。
- **空间范围在 `SpatialExtent`（单数）**：
  `SpatialExtent.HorizontalSpatialDomain.Geometry.BoundingRectangles[].{West,South,East,North}BoundingCoordinate`
  - 实测 `SpatialExtents`（复数）为 `null` —— 写解析器时踩过。
- 时间范围在 `TemporalExtents[].RangeDateTimes[].{BeginningDateTime,EndingDateTime}`。
- 原生格式在 `ArchiveAndDistributionInformation.FileArchiveInformation[].Format`
  （实测 `"HDF5"`）。
- DOI 在 `DOI.DOI`（实测 `"10.5067/GPM/DPR/GPM/2A/07"`）。
- 数据处理级别在 `ProcessingLevel.Id`（实测 `"2"`）。
- 许可证/使用约束在 `UseConstraints.Description`（**不是 SPDX 标识符**）。
- 引用在 `CollectionCitations[]`（`Creator` / `Title` / `Publisher` / `ReleaseDate`）。
- 落地页在 `RelatedUrls[]` 中 `Type == "DATA SET LANDING PAGE"` 的 `URL`。
- 变量关联在 `items[0].meta.associations.variables`（concept-id 数组，实测 281 个）。

### 1.3 文件级检索（granule）

```bash
curl -sS -m 30 -o cmr_granules.json -w "HTTP=%{http_code} bytes=%{size_download} t=%{time_total}s\n" \
  "https://cmr.earthdata.nasa.gov/search/granules.json?concept_id=C2179081499-GES_DISC&page_size=2"
```

实测：`HTTP=200 bytes=13602 t=2.319167s`

```json
{
  "feed": {"entry": [{
    "id": "G2187436353-GES_DISC",
    "collection_concept_id": "C2179081499-GES_DISC",
    "producer_granule_id": "2A.GPM.DPR.V9-20211125.20140308-S220950-E234217.000144.V07A.HDF5",
    "granule_size": "823.5269718170166",     // ← 字符串，单位 MB
    "time_start": "2014-03-08T22:09:50.674Z",
    "time_end":   "2014-03-08T23:42:18.044Z",
    "original_format": "ECHO10",
    "links": [
      {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#",
       "href": "https://data.gesdisc.earthdata.nasa.gov/data/GPM_L2/GPM_2ADPR.07/2014/067/...HDF5"},
      {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#",
       "href": "s3://gesdisc-cumulus-prod-protected/GPM_L2/.../...HDF5"},
      {"rel": "http://esipfed.org/ns/fedsearch/1.1/metadata#",
       "title": "api endpoint to retrieve temporary credentials valid for same-region direct s3 access",
       "href": "https://data.gesdisc.earthdata.nasa.gov/s3credentials"},
      {"rel": "http://esipfed.org/ns/fedsearch/1.1/service#",
       "title": "OPeNDAP request URL (GET DATA : OPENDAP DATA)"}
    ]
  }]}
}
```

**踩坑记录 1**：`granule_size` 是 **MB** 的字符串，`"823.5269718170166"` → `863530618` 字节
（`× 1024²`，四舍五入）。这是**目录声明值**，未与真实字节核验。

**踩坑记录 2**：`s3credentials` 链接的 `rel` 是 `.../metadata#`，而它**也以 `data#` 结尾**。
用 `rel.endswith("data#")` 会把凭证端点误判成数据文件；必须按**路径段**匹配 `rel.endswith("/data#")`。
单元测试 `test_s3credentials_link_is_not_treated_as_a_data_file` 盯住这一点。

**踩坑记录 3**：`inherited: true` 的链接是集合级继承链接（`virtual-directory`、
`search.earthdata.nasa.gov`），**不是文件**，已在解析时排除。

### 1.4 变量元数据

```bash
curl -sS -m 30 -o cmr_variables.json -w "HTTP=%{http_code} bytes=%{size_download}\n" \
  "https://cmr.earthdata.nasa.gov/search/variables.umm_json?concept_id=V2758121925-GES_DISC"
```

实测：`HTTP=200 bytes=870`

```json
{"hits": 1, "took": 0, "items": [{"umm": {
  "Name": "HS/navigation/scLon",
  "LongName": "HS/navigation/scLon",
  "Definition": "HS/navigation/scLon from GPM_2ADPR_07",
  "Units": "degrees",
  "DataType": "float32"
}}]}
```

### 1.5 错误形状

| 场景 | 实测响应 | 本实现的映射 |
|---|---|---|
| 参数非法（`page_size=notanumber`） | `HTTP 400` `{"errors":["page_size must be a number between 0 and 2000"]}` | `MetadataError` |
| concept-id 不存在 | `HTTP 200` + **空** `feed.entry` | 返回空元组（**不是** 404） |
| 5xx / 429 | — | `RetryableTransportError`（429 带 `Retry-After` 时填入 `retry_after_seconds`） |
| 401 / 403 | — | `SourceUnavailableError` |

---

## 2. 认证（Earthdata Login）

两种**真实支持**的方式，本实现在 `providers/earthdata/credentials.py` 里**只检查存在性**：

1. **`EARTHDATA_TOKEN` 环境变量** —— EDL 生成的 Bearer token，下载受保护数据时带
   `Authorization: Bearer $EARTHDATA_TOKEN`。
2. **`~/.netrc`** 中的 `machine urs.earthdata.nasa.gov` 条目（权限必须是 `600`，
   过宽则判定为不可信）。

安全约束（已由单元测试覆盖）：

- 快照对象 `EarthdataCredentialState` **不含任何凭证值**，`to_dict()` / `repr()` 都不输出 token。
- netrc 只做 `machine` 名的 token 匹配，**不读取也不保存** `login`/`password`。

**本机 2026-11-05 实测状态**：`EARTHDATA_TOKEN` 未设置、`~/.netrc` 不存在 →
因此"带凭证下载受保护 granule"记为 **unverified**，代码路径存在但没有被真实验证。

---

## 3. 能力矩阵（逐项如实标注）

| 能力 | 状态 | 证据 / 说明 |
|---|---|---|
| `search`（集合检索） | **verified** | `curl collections.json` HTTP 200 匿名（2026-11-05）；离线样本单测覆盖 |
| `inspect`（集合详情） | **verified** | `curl collections.umm_json` HTTP 200 匿名（2026-11-05） |
| `get_metadata` | **verified** | UMM-C 字段解析，键名空间化为 `cmr.*` |
| `get_variables` | **verified** | `curl variables.umm_json` HTTP 200；GPM_2ADPR 实测 281 个变量 |
| `validate_request` | **verified** | 基于 UMM 声明的时间/空间范围本地判定 |
| `estimate_request` | **verified** | `granules.json` 的 `granule_size`（MB，目录声明值） |
| `submit_task` / `get_task_status` / `retrieve_result` / `cancel_task` | **unsupported** | CMR **没有**作业 API → 抛 `NotImplementedFeatureError` |
| 元数据检索是否需凭证 | **verified：不需要** | 4 个端点均匿名 200 |
| 受保护 granule 的 HTTPS 下载（Bearer） | **unverified** | 本机无 `EARTHDATA_TOKEN` / netrc EDL 条目；未做真实下载 |
| S3 直连（`s3://` + `s3credentials`） | **unsupported** | 需要 Cumulus 临时凭证流程，本实现未验证，**不写假实现** |
| OPeNDAP 子集化（`service#` 链接） | **unverified** | 元数据里能看到链接，但没有实测子集化请求 |
| Harmony 云处理 | **unsupported** | CMR 元数据里有 `service_features.harmony` 声明，但作业端点未验证 |

`capabilities()` 的输出与上表一一对应（`verified` 每项都带日期/端点，
`unverified` 每项都带原因，单元测试 `test_capability_status_is_never_silently_verified` 盯住格式）。

---

## 4. 公开 API（供 MCP 层调用）

```python
from researchdatahub.providers import DatasetFilter, get_provider

with get_provider("earthdata") as provider:          # 也可 EarthdataProvider(...)
    caps = provider.capabilities()                    # ProviderCapabilities（纯内存）
    candidates = provider.search("precipitation", DatasetFilter(provider="GES_DISC", limit=5))
    detail = provider.inspect("C2179081499-GES_DISC")  # DatasetDetail
    variables = provider.get_variables("C2179081499-GES_DISC")   # tuple | None
    metadata = provider.get_metadata("C2179081499-GES_DISC")     # Mapping[str, JsonValue]
    checks = provider.validate_request("C2179081499-GES_DISC",
                                       DatasetFilter(temporal=window))
    estimate = provider.estimate_request("C2179081499-GES_DISC", DatasetFilter(limit=20))
```

- `dataset_id` 接受 **concept-id**（`C2179081499-GES_DISC`）或 **short_name**（`GPM_2ADPR`）。
  short_name 的解析规则：只保留 short_name **精确匹配**的候选，命中多个版本时取
  `version_id` **数值最高**的；最高版本仍并列则抛错并列出全部候选（实测同一 short_name
  可能同时存在 v07 与 v08）。
- `translate_query()` 返回即将发出的真实请求（**不含凭证**），并明确列出被忽略的
  过滤条件（`platform` / `collection` 在 CMR 集合检索里没有实测可用参数）。
- `get_variables()` 在远端没有登记变量时返回 **`None`**（表示"未知"），而不是空元组
  （那会被误读为"确认没有变量"）。

---

## 5. 与 `sources/` 的职责边界（ADR-0008）

| 层 | 包 | 输入 | 输出 | 面向 |
|---|---|---|---|---|
| 目录与能力 | `providers/earthdata/`（本模块） | 检索条件（关键词/时空/提供方） | `DatasetCandidate` / `DatasetDetail` / `ProviderCapabilities` | **Agent**（MCP `search_datasets` 等） |
| 资产发现与执行 | `sources/`（现有 CDS/NCAR 适配器） | 已校验的 `DatasetRequest` | `SourceAsset`（文件级，含 `origin_uri`/`size_bytes`） | 执行层（planner → scheduler → transfer） |

- 本 Provider **不下载字节、不写文件、不绕过 `integrity`**；它只返回目录事实与下载 URL 提示。
- `providers/` **不 import** `sources/`（有 AST 静态检查
  `test_provider_modules_have_no_forbidden_imports` 与运行期检查
  `test_importing_providers_does_not_load_sources` 兜底）。
- **后续桥接方案**（V1 未实现，因写作用域禁止修改 `sources/`）：
  `providers.inspect()` → 构造 `DatasetRequest` → 新增 `sources/earthdata.py`
  （`SourceAdapter` 子类，复用本模块的 `cmr.py` 解析函数产出 `SourceAsset`；granule 的
  `origin_uri` 取 `data_links[0].href`，`size_bytes` 取 `granule_size × 1024²` 并标注
  "catalog-declared"）。届时需要把"带 EDL 凭证的下载"从 unverified 提升为 verified。

---

## 6. 运行测试

```bash
# 离线（默认套件排除 network 标记）
.venv/bin/python -m pytest tests/unit/test_providers -q

# 真实联网（本次未作为验收条件，但已实测通过）
.venv/bin/python -m pytest tests/integration/test_providers_network.py -m network -q
```

`tests/unit/test_providers/fixtures/` 下的样本全部是 2026-11-05 的真实响应快照，
本地 `ThreadingHTTPServer` 回放它们，**没有 mock socket**。
