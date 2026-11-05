# 科学数据平台访问方式核对（2026-11-05 实机探测 + 官方文档）

> 调研者：researcher。本文只写"能核对到的事实"，不写宣传语。
> 每条给 **来源等级** + **本机探测结果**（探测时间 2026-11-05，macOS，中国大陆网络环境）。
> 探测方式：`curl -m N -o /dev/null -w "%{http_code}"`（不下载数据、不发认证请求、不使用 `cdsapi`）。

## 0. 阅读标注

| 标注 | 含义 |
|---|---|
| **【已读正文】** | 官方文档正文已抓取并读完 |
| **【未读正文（仅摘要）】** | 只拿到摘要/二手页面/镜像 |
| **【本机实测 YYYY-MM-DD】** | 本次真实发请求得到的状态码/字段 |
| **【未验证】** | 没证据，不要当事实用 |

---

## 1. 全网可达性总览（本机实测）

| 端点 | 状态码 | 判定 |
|---|---|---|
| `cds.climate.copernicus.eu/api/retrieve/v1/processes/reanalysis-era5-single-levels` | **200** | 可用（含完整 process description） |
| `cds.climate.copernicus.eu/api/retrieve/v1/openapi.json` | **200** | 可用（14 条路径，见 §2） |
| `cds.climate.copernicus.eu/api/catalogue/v1/collections?limit=1` | 200（但 20 s 内未下完 576 KB） | 可用，**慢** |
| `gdex.ucar.edu/` | **200** | 可用 |
| `gdex.ucar.edu/api/schema/` | **200**（仓库代码注释记录的实测日期 2026-11-04） | 可用 |
| `gdex.ucar.edu/api/v1/`、`/datasets`、`/datasets?format=json` | 404 / 301 | **不存在**（GDEX 不用 JSON:API 的 `/api/v1/`） |
| `cmr.earthdata.nasa.gov/search/collections.json` | **200** | 可用 |
| `cmr.earthdata.nasa.gov/search/granules.json` | **200** | 可用 |
| `urs.earthdata.nasa.gov/`（EDL 登录页） | **200** | 可用 |
| `catalogue.dataspace.copernicus.eu/stac/collections` | **200** | 可用 |
| `catalogue.dataspace.copernicus.eu/stac/search`（POST） | **200** | 可用（返回 GeoJSON FeatureCollection，含 `next` 链接走 `stac.dataspace.copernicus.eu/v1/search`） |
| `identity.dataspace.copernicus.eu/.../openid-connect/token` | **405**（GET 不被允许） | 端点存在（正确用法是 POST） |
| `eodata.dataspace.copernicus.eu` / `s3.waw3-1.cloudferro.com/eodata/` | **403** | 存在但需签名（匿名 S3 列表被拒，符合预期） |
| `earthengine.googleapis.com` | **000（连接超时）** | **本机不可达** |
| `earthengine-highvolume.googleapis.com` | **000** | 本机不可达 |
| `code.earthengine.google.com` | **000** | 本机不可达 |
| `oauth2.googleapis.com`、`accounts.google.com` | **000** | 本机不可达（**整个 googleapis/accounts 域都不通**，不是只有 GEE） |

> 关键区分：GEE 相关的**主机**（`earthengine*.googleapis.com`、`code.earthengine.google.com`）和 **Google 统一认证主机**（`oauth2.googleapis.com`、`accounts.google.com`）在本机**同时**超时。这说明超时是**网络层对 Google 认证/API 域的可达性问题**，不是 GEE 服务端故障，也**不能**据此说 GEE 不可用。同一环境下 `github.com`、`githubusercontent.com`、`ecmwf`、`nasa`、`copernicus` 全部可达。

---

## 2. CDS / ERA5（Copernicus Climate Data Store）

### 2.1 认证

- 新版 REST 面向"个人访问令牌（PAT）"：`url: https://cds.climate.copernicus.eu/api` + `key: <PERSONAL-ACCESS-TOKEN>`，放在 `~/.cdsapirc`（旧式 `cdsapi` 客户端读取）。【已读正文：[ecmwf/cdsapi README](https://github.com/ecmwf/cdsapi)，其中 `cat ~/.cdsapirc` / `key: <PERSONAL-ACCESS-TOKEN>` 为原文】
- 老账号的 **username/password 已不再是推荐方式**（新版 API 用 PAT）；`cdsapi` 的 README 现在以 `url`/`key` 两行为准——**具体是否仍接受 email/password，本文【未验证】**（未做任何认证请求，遵守"不得用 cdsapi 抓取"的纪律）。

### 2.2 客户端：`cdsapi` 与 `ecmwf-datastores-client`

- `cdsapi` 最新版本 **0.7.7**，`requires_dist = ['ecmwf-datastores-client>=0.4.0', 'requests>=2.5.0', 'tqdm']`（【本机实测 2026-11-05：PyPI JSON API `pypi.org/pypi/cdsapi/json`】）。
  → **`cdsapi` 现在是 `ecmwf-datastores-client` 的一层薄包装**；官方论坛亦以"New cdsapi version 0.7.7 released and new API package for advanced users"为题发布（【未读正文（仅摘要）】[ECMWF forum](https://forum.ecmwf.int/t/new-cdsapi-version-0-7-7-released-and-new-api-package-for-advanced-users-ready-to-be-used/12806)）。
- `ecmwf-datastores-client` 当前版本 **0.6.0**（发布于 **2026-11-04**），Apache-2.0，ECMWF "Graduated" 成熟度。【已读正文：[文档首页](https://ecmwf.github.io/ecmwf-datastores-client/) + [README](https://ecmwf.github.io/ecmwf-datastores-client/README.html)；【本机实测】GitHub Releases API 返回 `0.6.0 2026-11-04T13:12:02Z`】
- **配置优先级（原文）**：① 构造 `Client(...)` 的关键字参数 → ② 环境变量 `ECMWF_DATASTORES_URL` / `ECMWF_DATASTORES_KEY` → ③ `~/.ecmwfdatastoresrc`（或 `ECMWF_DATASTORES_RC_FILE` 指定的文件）。文件格式：

  ```yaml
  url: https://cds.climate.copernicus.eu/api
  key: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  ```

  【已读正文：同上 README §Configuration】
- **API 形状（原文示例，可直接照抄）**：

  ```python
  from ecmwf.datastores import Client
  client = Client()
  client.check_authentication()
  client.retrieve(collection_id, request, target="target_1.grib")      # 阻塞
  remote = client.submit(collection_id, request)                        # 非阻塞
  remote.download("target_2.grib")
  results = client.submit_and_wait_on_results(collection_id, request)
  client.download_results(remote.request_id, "target_4.grib")
  client.get_collections(sortby="update")      # 分页：collections.next
  client.get_collection(collection_id)         # 元数据：bbox/begin_datetime/end_datetime
  client.apply_constraints(collection_id, constraints)   # 校验日期/变量是否存在
  client.get_jobs(sortby="-created", status="successful")
  client.get_remote(request_id)                # .status / .results_ready / .get_receipt()
  ```

  【已读正文：同上 README §Quick Start】

### 2.3 REST 端点（直接可用，取自 OpenAPI）

`GET https://cds.climate.copernicus.eu/api/retrieve/v1/openapi.json` → **200**，14 条路径（【本机实测】）：

```
/                                        GET
/conformance                             GET
/processes                               GET
/processes/{process_id}                  GET
/processes/{process_id}/execute          POST
/processes/{process_id}/execution        POST
/jobs                                    GET
/jobs/{job_id}                           GET, DELETE
/jobs/{job_id}/results                   GET
/jobs/{job_id}/receipt                   GET
/processes/{process_id}/constraints      POST
/processes/{process_id}/costing          POST
/processes/{process_id}/api-request      POST
/jobs/delete                             POST
```

这是 OGC API - Processes 风格：**提交作业 = `POST /processes/{id}/execution`**，然后轮询 `/jobs/{job_id}`，结果在 `/jobs/{job_id}/results`。

**ERA5 single-levels 的真实输入字段**（【本机实测】：`GET /processes/reanalysis-era5-single-levels` 返回的 `inputs` 键）：

```text
product_type, variable, year, month, day, time, area, data_format, download_format
```

> 注意两点：
> 1. **压力层数据集是另一个 collection**：`reanalysis-era5-pressure-levels`（额外有 `pressure_level`）。本项目黄金路径里的"云液态水/云冰水/温度/风场"要么走 pressure-levels（`clwc`/`ciwc`/`t`/`u`/`v`，需 `pressure_level`），要么走 single-levels 的积分量（`tclw`/`tciw`），**两者科学含义不同，不能混用**。
> 2. `area` 的坐标顺序 CDS 是 `[north, west, south, east]`，与本项目契约 v1.2 的 `[west, south, east, north]` **相反**——本仓库 `src/researchdatahub/planner/request_v12.py` 的 `to_cds_area()` 就是唯一正确实现（**本项目已知事实**，非外部来源）。

### 2.4 对本项目的含义

- CDS 是"请求-生成-下载"异步模型：小范围小变量 30 s 级；**数据集级队列拥塞时数分钟到数十分钟不启动**（本项目 M6 基准实测，见 `docs/M6-efficiency-benchmark.md`）。
- 规划器应优先用 `apply_constraints` 语义做**前置校验**（避免提交必然失败的作业），本项目对应 `validate_request` / `estimate_acquisition`。

---

## 3. NSF NCAR GDEX（原 RDA）

### 3.1 地址与 API

- 旧域名 `https://rda.ucar.edu` **301** 到 `https://gdex.ucar.edu`（GDEX = Geoscience Data Exchange）。【本项目源码注释 `src/researchdatahub/sources/ncar.py` 记录的实测；【本机实测】`gdex.ucar.edu/` → 200】
- **RDA Datasets API**：OpenAPI 3.0.3，schema 在 `https://gdex.ucar.edu/api/schema/`（**200**，本仓库代码注释记录的实测日期 2026-11-04）。数据集列表：`/api/datasets/`；分组：`/groups/`；文件清单：`/webfiles/{gindex}/`。
- **不要**假设它提供 JSON:API 风格的 `/api/v1/datasets`：本次实测 `/api/v1/` → **404**、`/api/v1/datasets?limit=2` → **301**（重定向到别处，未跟随）。【本机实测 2026-11-05】
- **数据文件主机**：`https://data.gdex.ucar.edu/{dsid}/{wfile}`（本仓库代码注释记录 HEAD 200；本次未重复探测该主机）。

### 3.2 是否需要 Globus

- **不需要**：`/api/datasets/` + `/webfiles/{gindex}/` 给出文件路径后，直接从 `data.gdex.ucar.edu` 做 HTTP 下载即可（本项目 M6 基准已真实下载 1.13 GB 文件）。
- Globus 在 GDEX 上是**可选的加速/大批量通道**，不是前置条件。**官方是否强制 Globus 访问某些受限数据集，本文【未验证】**（GDEX 用户协议页面未抓取）。
- ⚠️ 注意本项目 README 已明确：**Globus 传输在本项目是"明确未实现"**，不要在 Skill 文案里暗示可用。

### 3.3 特性与本项目策略的关系

- GDEX 的 webfiles API **不提供 checksum**（本仓库 `ncar.py` 注释：`checksums: GDEX webfiles API exposes none`）→ 交付校验只能到"大小 + 格式可打开"，SHA-256 需要自算并存进 Manifest。
- 交付形态是**整文件**（月度/全域 NetCDF/NetCDF4），子集需要本地裁剪 → 小区域小变量应走 CDS。

---

## 4. NASA Earthdata（CMR + EDL）

### 4.1 CMR 搜索 API

- 端点：`https://cmr.earthdata.nasa.gov/search/{collections|granules|variables|services}.json`（XML 也可）——【本机实测 2026-11-05】`collections.json?keyword=MERRA-2&page_size=1` → **200**；`granules.json?short_name=GPM_3IMERGHH&page_size=1` → **200**。
- 常用参数（本项目生态的代表实现 `earthaccess` 的公开 API 映射，【已读正文】[earthaccess README](https://github.com/earthaccess-dev/earthaccess)）：`short_name=`, `bounding_box=(west, south, east, north)`, `temporal=("YYYY-MM", "YYYY-MM")` 或时间区间, `count=`；底层由 [python-cmr](https://github.com/nasa/python_cmr) 封装 CMR search API。
- `concept_id=C...-PROVIDER` 形态的 collection id 可直接用于 `/search/collections.json?concept_id=...`（【本机实测】200）。

> **未读正文（仅摘要）**：CMR 官方 API 文档站（`cmr.earthdata.nasa.gov/search/site/docs`、wiki 导出 PDF）本次未取到正文。**完整参数表建议以 `earthaccess`/`python-cmr` 的源码为准**（它们是被 NASA 官方教程采用的封装）。

### 4.2 EDL 认证（三种文件 + token）

【已读正文：[NASA GES DISC 官方教程 `How to Generate Earthdata Prerequisite Files`](https://github.com/nasa/gesdisc-tutorials/blob/main/notebooks/How_to_Generate_Earthdata_Prerequisite_Files.ipynb)】

- **前置**：需要免费的 Earthdata Login 账号（`urs.earthdata.nasa.gov`），并启用相应应用授权。
- **`.netrc`（默认方式）**：

  ```text
  machine urs.earthdata.nasa.gov login <uid> password <password>
  ```

  权限 `chmod 0600 ~/.netrc`。
- **`.edl_token`（令牌方式，可替代 `.netrc`）**：从 EDL 网站或 User Tokens API 生成；**有效期 60 天**（原文："Earthdata Login tokens are only valid for 60 days"），需定期刷新。
- **`.urs_cookies` + `.dodsrc`（OPeNDAP/云端子集下载需要）**：

  ```text
  HTTP.NETRC=<HomeDir>/.netrc
  HTTP.COOKIEJAR=<HomeDir>/.urs_cookies
  ```

  Windows 上还需把 `.dodsrc` 复制到**工作目录**。
- `earthaccess.login()` 的凭据查找顺序（【已读正文】[earthaccess 认证文档](https://earthaccess.readthedocs.io/en/latest/user/howto/authenticate/)）：① 环境变量 `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` → ② `~/.netrc` → ③ 交互提示。

### 4.3 受保护资源怎么下载

- **直接 HTTP 下载**（`https://data.gesdisc.earthdata.nasa.gov/...` 等 DAAC 文件服务）：EDL 重定向认证，`.netrc` 即可；`requests` 需 `trust_env` 才能用 `.netrc`（**未验证**，属客户端行为）。
- **Harmony / OPeNDAP 子集**：需要 token + `.dodsrc`/`.urs_cookies` 组合。
- **S3 直取（cloud-hosted 数据）**：`earthaccess` 会自动取临时 S3 凭证（README 的 "Direct S3 access" 章节，【已读正文】见 [文档导航](https://github.com/earthaccess-dev/earthaccess)）。

### 4.4 对本项目的含义

- 凭证形态与 CDS 不同：CDS 是 **`~/.cdsapirc` 里的单个 key**；Earthdata 是 **`.netrc` 明文账号密码**或 **60 天短期 token** → 环境引导必须区分"长期凭证 vs 短期 token"，并在 `check_environment` 里把"token 过期"报成 `needs_configuration` 而不是 `unavailable`。
- 本项目 `docs/data-integrity.md` 的六层校验中"协议/格式"层对 HDF5/GRIB 需要相应依赖——Earthdata 侧多为 HDF5（**未验证**具体产品格式，按产品定）。

---

## 5. Copernicus Data Space Ecosystem（CDSE）

三个 API 各司其职（【已读正文】：[OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html)、[STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)、[S3](https://documentation.dataspace.copernicus.eu/APIs/S3.html) 的导航与正文；Sentinel Hub [Authentication](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html) 全页已读）：

| API | 能做什么 | 认证 | 本机实测 |
|---|---|---|---|
| **STAC** | **搜索/发现**产品（GeoJSON FeatureCollection，分页 `next` link，隐含端点 `https://stac.dataspace.copernicus.eu/v1/search`） | 匿名可搜 | `POST /stac/search` → **200**；`GET /stac/collections` → **200** |
| **OData** | **下载**产品（`https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter=Name eq '...'` → 取 `Id`/`S3Path`） | Bearer token | 未直接测（需 token） |
| **S3 (eodata)** | **高性能/并行取对象**，S3 兼容协议 | access/secret key（账号里生成，可设过期日）+ 临时凭证 API | `eodata.dataspace.copernicus.eu` → **403**（需签名，符合预期） |

### 5.1 STAC（发现）

- STAC 端点 `POST https://catalogue.dataspace.copernicus.eu/stac/search`，body 与标准 STAC `/search` 一致：`bbox` / `datetime`（`"2024-01-01T00:00:00Z/2024-01-02T00:00:00Z"`）/ `collections`（如 `sentinel-2-l2a`）/ `limit`。【本机实测：返回 FeatureCollection，`links[].rel="next"` 指向 `https://stac.dataspace.copernicus.eu/v1/search`】
- 用法：**只做发现，不下载**——把 items 的资产 URL 与 `S3Path` 交给下载层。

### 5.2 认证：两种 grant

- **用户账号 password grant**（【已读正文】[S3 文档页](https://documentation.dataspace.copernicus.eu/APIs/S3.html) 的示例脚本原文）：

  ```python
  auth_data = {"client_id": "cdse-public", "grant_type": "password",
               "username": ..., "password": ...}
  requests.post("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token", data=auth_data)
  ```

- **OAuth2 client credentials**（【已读正文】[Sentinel Hub Authentication](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html) 原文）：需在 Dashboard 的 "User Settings → OAuth clients" **注册 OAuth client**，拿到 client ID + secret（secret 只显示一次），然后：

  ```bash
  curl --request POST \
    --url https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token \
    --header 'content-type: application/x-www-form-urlencoded' \
    --data 'grant_type=client_credentials&client_id=<your client id>' \
    --data-urlencode 'client_secret=<your client secret>'
  ```

  文档明确：**token 有有效期（JWT 的 `exp`），不要每个请求都换 token**（token 端点是限流的，滥用会 429）。

### 5.3 S3 eodata

- 默认端点 `https://eodata.dataspace.copernicus.eu/`（GSLB 按 DNS 位置分流到 CloudFerro 或 OpenTelekom Cloud）；强制 OTC：`https://eodata.ams.dataspace.copernicus.eu/`。bucket 名 **`eodata`**。【已读正文：同上 S3 页】
- 路径形状：`s3://eodata/Sentinel-1/SAR/SLC/2016/12/28/<PRODUCT>.SAFE/...`（原文示例）。
- 凭据：在网页生成 access/secret（可设过期日，secret **只显示一次**）；或调用临时凭证 API `POST https://s3-keys-manager.cloudferro.com/api/user/credentials`（Bearer token）拿临时 `access_id`/`secret`。【已读正文：同上 S3 页 Python 示例】
- 计费/配额提醒（原文）：`s3cmd get --recursive` 下载一个含 100 个文件的 `.SAFE` 产品，**按 100 次请求计**。

### 5.4 对本项目的含义

- CDSE 与 CDS 是**两套不同的认证域**（CDSE = `identity.dataspace.copernicus.eu` + S3 key；CDS = `cds.climate.copernicus.eu` + PAT）。环境引导文档不能把两者混写。
- S3 路线适合纳入本项目已有的 `s3` transport（`ctransfer/s3.py`），但需支持 endpoint override（eodata 不是 AWS）。

---

## 6. Google Earth Engine（GEE）——重点核查

### 6.1 本机不可达的**真实范围**

【本机实测 2026-11-05，均 20 s 超时、HTTP 000】：

```text
https://earthengine.googleapis.com                    → 000（超时）
https://earthengine-highvolume.googleapis.com          → 000（超时）
https://code.earthengine.google.com                    → 000（超时）
https://oauth2.googleapis.com                          → 000（超时）
https://accounts.google.com                             → 000（超时）
```

对照：`github.com`、`raw.githubusercontent.com`、`gdex.ucar.edu`、`cmr.earthdata.nasa.gov`、`catalogue.dataspace.copernicus.eu`、`cds.climate.copernicus.eu` 全部 **2xx/3xx**。

**结论（如实）**：这台机器所在网络对 **Google 认证域（`oauth2`/`accounts`）与 GEE API 域**同时不可达。因此：
1. 这是**网络可达性问题**，不是 GEE 服务中断；
2. **不能**据此判断 GEE 是否收费/需审批；
3. 在本机**无法**完成任何 GEE 端到端验证（登录、初始化、导出都依赖上述主机）→ 本项目若接入 GEE，**验收只能是 NOT_RUN**。

### 6.2 Python API 依赖哪些端点

【已读正文：[Earth Engine Python 安装指南（镜像全文）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/python_install.md)、[认证与初始化](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/auth.md)】

- **认证**：`ee.Authenticate()` → 拿 OAuth 凭证存到 **`~/.config/earthengine/credentials`**；`ee.Initialize(project='my-project')` 用该凭证初始化。
- **auth_mode 自动选择（原文顺序）**：Colab → `colab`；其他 Jupyter → `notebook`；有浏览器且没装 gcloud → `localhost`（默认端口 8085）；否则 `gcloud`。
- **认证依赖的端点**：OAuth 流程走 Google 账号体系（`accounts.google.com` / `oauth2.googleapis.com` 及 Google 自家登录页）；`auth_mode=notebook` 要打开 "Notebook Authenticator 页面"；`auth_mode=gcloud` 等价于 `gcloud auth application-default login`（需要装 gcloud）。
- **执行依赖的端点**：所有计算请求打到 **`earthengine.googleapis.com`**（文档原文错误信息里直接出现该主机：*"The earthengine.googleapis.com API requires a quota project"*）；并行/高吞吐路径另有 `earthengine-highvolume.googleapis.com`（**后者本文未读正文，属社区常见做法，标注【未验证】**）。
- **服务账号**：`ee.Initialize(credentials=...)` 可传服务账号密钥，用于无人值守；服务账号需其父项目已注册 GEE 且启用 Earth Engine API。
- **CLI**：`earthengine authenticate`（等价 `ee.Authenticate()`）、`earthengine set_project {project}`、`earthengine` 命令行每次调用都会初始化。

### 6.3 是否收费 / 需审批

【已读正文：[Earth Engine access（镜像全文）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/access.md)、[非商业用途层级](https://developers.google.cn/earth-engine/guides/noncommercial_tiers)（中文镜像，仅导航）/ [pricing（未读正文）]】

原文要点（access.md）：

- *"Earth Engine access … is registered for **commercial** or **noncommercial** use"*；
- 注册入口 `https://console.cloud.google.com/earth-engine`，**注册后立即启用**；
- **非商业（免费）**：需要填写 Cloud 控制台的注册问卷（registration questionnaire）确认资格；**"If you register a noncommercial project, no billing configuration is required"**（非商业项目不需要计费配置）；
- **2025-04-16 之前注册的项目**必须在 **2025-09-26** 前补填并完成验证，否则**访问被暂停**；非商业项目**需要周期性重新验证**；
- **商业用途**：注册后选择套餐并绑计费账户（`cloud.google.com/earth-engine/pricing`，**未读正文**）。

【已读正文：[Earth Engine quotas（镜像全文）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/usage.md)】：

- 每项目默认 **平均并发批处理任务 2 个**；非商业用户并发上限就是默认值（除非获批配额提升）；
- **只有已注册付费 GEE 访问的项目**才能设置任务优先级（priority）；
- 每天 EECU-time 默认不限，但可设上限；
- 超配额会返回 **HTTP 429**，客户端库自动指数退避重试最多 5 次。

**如实结论**：

> GEE **对非商业用途免费，但必须"注册 + 填问卷验证"，并周期性复验**；商业用途付费（套餐价格本文未核对）。它不是"注册即可随便跑"的服务，也不是"必须付费"。
> **在本机当前网络环境下，GEE 完全不可用**（认证域与 API 域均超时），任何"接入 GEE"的实现都无法在此机器上验证。

### 6.4 导出（Export）生命周期

【已读正文：python_install.md §Exporting data + [processing_environments（镜像全文）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/processing_environments.md)】

```python
task = ee.batch.Export.image.toDrive(image=..., description="mock_export",
                                     folder="gdrive_folder", fileNamePrefix="mock_export",
                                     scale=..., region=...)
task.start()          # 提交
task.status()         # {'state': ..., 'id': ..., 'task_type': 'EXPORT_IMAGE'}
```

- **状态机（原文列表）**：`UNSUBMITTED`（客户端待提交）→ `READY`（服务端排队）→ `RUNNING` → `COMPLETED` / `FAILED`；取消路径 `CANCEL_REQUESTED`（**不保证真的取消**）→ `CANCELLED`。
- 任务进入队列后按**优先级 + 提交时间**排序，被分配给 batch processor，worker 数由服务端决定、**用户不可配置**。
- Python API 启动的任务也会出现在 Code Editor 的 Tasks 面板。
- **CLI 导出**：`earthengine export` 子命令的完整参数**本文未读正文**（[未验证]）；文档中确认存在的等价入口是模块化的 `ee.batch.Export.*`。

### 6.5 对本项目的含义

- 若把 GEE 作为 Provider：**只能在有 Google 网络出口的机器上验收**；本机必须显式报 `not_available`（网络不可达），而不是假装成功。本项目的 `check_environment` 已有 "端点可达" 检查项，天然能表达这一点。
- GEE 的交付形态是 **Drive / Cloud Storage 导出 + 异步 Task**，与本项目"直接 HTTP 拉文件"的传输模型不同 → 需要新的 transport/轮询语义，**不建议在 M6 期间做**。

---

## 7. 五个平台的横向对照（供 Skill 文案使用）

| 维度 | CDS/ERA5 | NCAR GDEX | NASA Earthdata | CDSE | GEE |
|---|---|---|---|---|---|
| 发现 | `GET /processes/{id}` + catalogue API | `/api/datasets/`、`/groups/`、`/webfiles/{gindex}/` | CMR `/search/collections.json` | STAC `/stac/search` | GEE 资产目录（客户端 API） |
| 认证 | `~/.cdsapirc`（PAT） | 无需（公开数据） | `.netrc` 或 60 天 EDL token | OAuth2（password / client_credentials）或 S3 key | Google OAuth / 服务账号 |
| 交付形态 | 服务端生成子集文件 | **整文件** | 单 granule 文件 / 子集服务 | 单产品文件（.SAFE 目录） | **导出任务**到 Drive/GCS |
| 异步 | 作业队列（分钟~小时） | 无（直接 GET） | 无（直接 GET） | 无（直接 GET / S3） | Task 队列 |
| 校验和 | 结果 receipt（**未读正文**） | **无** | 产品自带（视产品） | MD5（视产品） | 导出文件由用户自校验 |
| 本机可达 | ✅ | ✅ | ✅ | ✅ | ❌（网络） |

---

## 8. 来源清单

**已读正文**
- [ecmwf/cdsapi README](https://github.com/ecmwf/cdsapi)
- [ecmwf-datastores-client 文档](https://ecmwf.github.io/ecmwf-datastores-client/README.html)（安装/配置优先级/API 示例）
- [NASA GES DISC · How to Generate Earthdata Prerequisite Files](https://github.com/nasa/gesdisc-tutorials/blob/main/notebooks/How_to_Generate_Earthdata_Prerequisite_Files.ipynb)（`.netrc`/`.edl_token`/`.dodsrc`/`.urs_cookies`）
- [earthaccess README](https://github.com/earthaccess-dev/earthaccess) + [认证文档](https://earthaccess.readthedocs.io/en/latest/user/howto/authenticate/)（CMR 搜索参数映射、EDL 查找顺序）
- [CDSE · Access to EO data via S3](https://documentation.dataspace.copernicus.eu/APIs/S3.html)
- [CDSE · Sentinel Hub Authentication](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html)（OAuth2 client credentials 全流程）
- [GEE · Python install（镜像）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/python_install.md)
- [GEE · Authentication and Initialization（镜像）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/auth.md)
- [GEE · Earth Engine access（镜像）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/access.md)
- [GEE · Earth Engine quotas（镜像）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/usage.md)
- [GEE · Processing environments / task states（镜像）](https://github.com/wybert/earthengine-doc-md/blob/main/docs/guides/processing_environments.md)
- [openai/codex · docs/config.md](https://github.com/openai/codex/blob/main/docs/config.md)（已改为跳官网）

**未读正文（仅摘要）**
- [https://developers.google.com/earth-engine/guides/noncommercial_tiers](https://developers.google.com/earth-engine/guides/noncommercial_tiers) —— 本机 fetch 报 `TypeError: fetch failed`（Google 域不可达），仅拿到搜索摘要。
- CMR 官方 API 文档 —— 未取到正文，参数以 `earthaccess`/`python-cmr` 封装为准。
- CDS 官方 how-to（`cds.climate.copernicus.eu/how-to-api`）—— 本机抓取超时。
- GDEX 用户协议 / Globus 强制范围 —— 未抓取。
- GEE `earthengine export` CLI 参数 —— 未取到正文。

**本机实测（2026-11-05）**
- 全部端点状态码见 §1；
- CDS `retrieve/v1/openapi.json` 的 14 条路径与 ERA5 process `inputs` 字段；
- PyPI `cdsapi` 0.7.7 依赖、`ecmwf-datastores-client` 0.6.0 发布日；
- CDSE STAC `POST /stac/search` 返回结构与 `next` link。

---

## 9. 仍然"未验证"的清单（不要在这些点上撒谎）

1. CDS 是否仍接受 email/password 认证（本文未做认证请求）。
2. GDEX 是否有任何数据强依赖 Globus。
3. CMR 的完整参数表（`page_num`、`token`、`provider`、`processing_level` 等）。
4. GEE `earthengine export` 子命令的参数与 `earthengine-highvolume.googleapis.com` 的官方用途。
5. GEE 商业套餐价格。
6. Earthdata 各产品是否都提供 MD5/SHA 校验。
