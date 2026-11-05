# 相似开源项目的"多源统一契约"设计对比

> 调研者：researcher。目的：为本项目（ResearchDataHub）的 M6 Agent Skills 与后续 Provider 扩展提供**可借鉴 / 不可借鉴**的设计参照。
> 每个项目给：**链接 · 原理（它怎么抽象多源）· 值得借鉴 · 不该学（含取舍）**。
> 阅读标注：【已读正文】= 项目文档/README 已读完全文；【未读正文（仅摘要）】= 未核实原文。

本项目的硬约束（来自 `README.md` 与 `docs/adr/ADR-0001-modular-monolith.md`）：**单机、本地优先、`pip install` 即用、不引入 Redis/Kafka/K8s/PostgreSQL**。因此下文"不该学"一栏的标准是：**是否为分布式/托管依赖，或是否为"把数据加载进内存"而非"把数据可靠落盘"**。

---

## 1. intake-esm（ESM Collection Specification）

- 链接：[intake-esm](https://github.com/intake/intake-esm) · [ESM Catalog 规范全文](https://github.com/intake/intake-esm/blob/main/docs/source/reference/esm-catalog-spec.md)（镜像 NCAR [esm-collection-spec](https://github.com/NCAR/esm-collection-spec)）【已读正文】

### 原理

**两文件分离**：一个 **JSON catalog descriptor**（描述"这个目录里有什么、怎么打开"）+ 一个 **CSV catalog**（每行一个数据资产及其元数据）。

```json
{
  "esmcat_version": "0.1.0",
  "id": "sample",
  "description": "This is a very basic sample ESM catalog.",
  "catalog_file": "sample_catalog.csv",
  "attributes": [
    {"column_name": "activity_id", "vocabulary": "https://.../CMIP6_activity_id.json"},
    {"column_name": "source_id",    "vocabulary": "https://.../CMIP6_source_id.json"}
  ],
  "assets": {"column_name": "path", "format": "zarr"}
}
```

```csv
activity_id,source_id,path
CMIP,ACCESS-CM2,gs://pangeo-data/store1.zarr
```

要点（规范原文）：

- descriptor **"inspired by the STAC spec"**，但刻意做成"两文件"（JSON 描述 + CSV 数据），因为**资产数量大时代谢 CSV 比 JSON 便宜**；
- `attributes` 每列声明一个**受控词表 URL**（CMIP6_CVs 等），即"字段名 → 可校验取值"；
- `assets` 声明资产列的**列名 + 格式**（netCDF / Zarr），资产是 **URI 或完整路径**；
- intake-esm 负责在 CSV 上做过滤/聚合（"搜索 → 发现 → 访问"），交给 intake/xarray 打开。

### 值得借鉴

1. **"描述与清单分离"**——与本项目 Manifest 思想一致，但它把**清单做成明文 CSV**（可 diff、可 git 管理、可被 pandas 直接读）；本项目的 SQLite 是事实来源，但**导出的交付清单也应是可读的行式格式**（本项目已有 `rdh report generate` 的 JSON 导出，思路吻合）。
2. **受控词表 URL**：`attributes[].vocabulary` 是一个极轻量的"字段取值来自哪里"的声明——比把枚举写死在代码里更抗漂移。本项目可在 `data-contract.md` 的变量/单位映射上引入"来源 URL"字段。
3. **`assets.format` 显式声明**：避免"看扩展名猜格式"。本项目六层校验里的 `format_sniff` 已有此职能，可在契约层也显式声明。

### 不该学（取舍）

- **它是"加载器"不是"采集器"**：不做下载、不做重试、不做完整性证明——**正好是本项目的差异化**。不要在 Skill 文案里把 intake-esm 当成替代方案。
- **依赖 xarray/dask 的内存式加载**：与项目"先可靠落盘再分析"的取向相反；且 CSV+对象存储的假设（`gs://`）在本项目的 S3 传输尚未端到端验证前不应引入。
- 隐含假设"数据已在云上且路径稳定"——对 CDS 这类**服务端生成**的源完全不适用（这正是本项目 Source ≠ Transport 分离要解决的）。

---

## 2. Pooch（fatiando）

- 链接：[fatiando/pooch](https://github.com/fatiando/pooch) · [文档](https://www.fatiando.org/pooch) · JOSS 论文 DOI `10.21105/joss.01943`【已读正文：README 全文】

### 原理

"**下载 + 缓存 + 哈希校验**"三件套的极小实现：

```python
fname = pooch.retrieve(url=..., known_hash="md5:a7332aa6e69c77d49d7fb54b764caa82")
fname2 = pooch.retrieve(url="doi:10.5281/zenodo.5882430/xxx.csv.xz", known_hash="md5:...")
```

- 缓存目录 `pooch.os_cache("mypackage")`；带版本号的 `base_url` 模板；**"Running this again will not cause a download"**；
- 支持 HTTP/FTP + **Zenodo/figshare 的 DOI 直取**；内置解压/解压缩 post-processor；可自定义 downloader/post-processor；
- 被 SciPy / scikit-image / xarray / MetPy / Satpy / napari 等点名使用（README 的 Projects using Pooch 列表）。

### 值得借鉴

1. **`known_hash` 作为一等公民**：下载前就知道期望哈希 → 这就是本项目"Integrity Before Delivery"的最小可行形态；本项目的优势是把它从"单文件"扩展到"六层 + Manifest 事实来源"。
2. **"存在即跳过"的幂等语义**：本项目的断点续传/租约应保证"重跑不重下"，Pooch 证明了这个默认值是用户要的。
3. **`doi:` 前缀式的 URI 方案**：把"数据来源标识"做成可解析的 URI 方案，很适合本项目 `access_route` / `origin_uri` 的设计——未来加 Zenodo/HydroShare 源不需要改传输层。

### 不该学（取舍）

- **无状态**：没有 Manifest、没有任务、没有断点续传、没有重试分类 → 大文件（本项目实测 1.13 GB）中断后只能重来。Pooch 的定位是"包内示例数据"，不是"科研数据采集"。
- **无多源路由**：它不选源、不做子集/整文件权衡。
- **缓存语义 vs 采集语义**：Pooch 的缓存可被清、可被共享；本项目的 raw 区是**证据**，必须不可变（ADR-0005）。两者不能混为一谈。

---

## 3. earthaccess（NASA）

- 链接：[earthaccess-dev/earthaccess](https://github.com/earthaccess-dev/earthaccess) · [文档](https://earthaccess.readthedocs.io/) · PyPI `earthaccess 0.19.0`（【本机实测 PyPI JSON】）【已读正文：README 全文 + 认证文档】

### 原理

把 **认证 + 搜索 + 访问** 三个 NASA 子系统收进一个 3 行 API：

```python
earthaccess.login()
results = earthaccess.search_data(short_name='ATL06', bounding_box=(-10,20,10,50),
                                  temporal=("1999-02","2019-03"), count=10)
files = earthaccess.download(results, "/tmp/my-download-folder")
```

- 底层组合：`python-cmr`（CMR search API）+ EDL 认证（env var / `.netrc` / 交互）+ `fsspec/s3fs`（云直取）+ 可选 `VirtualiZarr`（把一组 granule 变成虚拟 xarray Dataset）；
- README 明确列出生态分工：`python-cmr` 只封 CMR；`asf_search` 是 SAR 专用；`icepyx` 用 earthaccess 做认证；`earthdatalogin` 是 R 版；`fsspec/s3fs` 是通用层。

### 值得借鉴

1. **"先 login → 再 search → 再 access"的三段式**正是本项目 Skill 编排的骨架（`check_environment` → `search_datasets` → `submit_acquisition`）。可直接在 SKILL.md 里沿用这个心智模型。
2. **凭据查找顺序显式化**（env var → `.netrc` → 交互），且**区分"未登录"与"无权限"**——本项目的 `needs_configuration` vs `not_available` 语义应当照此实现。
3. **薄封装 + 明确上游归属**：它不自造 CMR 客户端，而是依赖 `python-cmr`。本项目已照此做（Source ≠ Transport）。

### 不该学（取舍）

- **只管一个生态（NASA）**：没有跨生态路由（NASA vs CDS vs NCAR），也不解决"同一科学需求在两个源上如何选"。这正是本项目 planner 的价值。
- **不做完整性证明**：`download()` 返回本地路径，不保证"与远端同一份科学数据"；本项目 Manifest + SHA-256 + 六层校验是差异化能力，**不要**为了复用 earthaccess 而弱化它。
- 云直取依赖 `s3fs`/fsspec 与临时凭证，属"托管依赖面"，与本项目"单机 pip install 即用"取向不完全一致——可以借，但**不要**把它作为默认路线。

---

## 4. HyRiver / async-retriever

- 链接：[hyriver/async-retriever](https://github.com/hyriver/async-retriever)（HyRiver 系列的"请求引擎"）· [hyriver/pygeohydro](https://github.com/hyriver/pygeohydro)（应用层）【已读正文：两者 README 全文】

### 原理

HyRiver 是"**一个包一个数据服务**"的家族式结构（PyNHD / Py3DEP / PyGeoHydro / PyDaymet / PyGridMET / PyNLDAS2 …），共享同一个请求引擎：

- `async-retriever` 提供 `retrieve_text` / `retrieve_binary` / `retrieve_json` / `stream_write`，**异步**发送请求；
- **持久缓存**：用 `aiohttp-client-cache` 写进 **SQLite**，默认 `./cache/aiohttp_cache.sqlite`；
- 缓存行为用**环境变量**控制：`HYRIVER_CACHE_NAME`（SQLite 路径）、`HYRIVER_CACHE_EXPIRE`（秒，默认一周）、`HYRIVER_CACHE_DISABLE`；
- 提供 `delete_url_cache(base_url)` 做按 URL 失效。

### 值得借鉴

1. **SQLite 做持久缓存**——与本项目 Manifest 的选型一致（ADR-0003），且证明了"SQLite 足够并发读、轻量"。可作为"为什么不上 Redis"的外部佐证。
2. **按 URL 键缓存 + 显式失效函数**：比"按时间全清"更可控。本项目若加元数据缓存，应沿用"键 = 归一化 URL + 参数"+"显式失效"。
3. **`stream_write`（流式写盘）**：与本项目 HTTP 传输的流式落盘一致，说明这是该场景的标准做法。

### 不该学（取舍）

- **无采集状态机**：它缓存"响应"，不管"任务"；没有租约/心跳/崩溃接管。本项目已有，不要倒退。
- **一个包一个服务的碎片化**：HyRiver 靠"包数量"覆盖多源，扩展是线性的（每加一个源 = 一个包 + 一份测试）。本项目的 Provider 注册表 + 统一契约是更可扩展的形态，**不要**模仿它的包装策略。
- 异步 aiohttp + `nest_asyncio` 的复杂度换取吞吐；本项目用 httpx + 有界并发（默认 2）已满足"单机不把远端打满"的目标。

---

## 5.（补充）HydroShare / hsclient —— 作为"未来源"的参考，非核心

- 链接：[hydroshare/hydroshare](https://github.com/hydroshare/hydroshare)（BSD-3-Clause 平台本体）· [hydroshare/hsclient](https://github.com/hydroshare/hsclient)（面向对象 Python 客户端）【已读正文：hsclient README 全文；平台本体 README 本次未读到（`develop` 分支 README.rst 404）】

### 原理

- 平台侧：HydroShare 是"资源（resource）+ 元数据"的协作网站（标题/摘要/关键词 + RDF 元数据）。
- 客户端侧：`hsclient` 把平台资源映射成 Python 对象：

  ```python
  hs = HydroShare(); hs.sign_in()
  res = hs.create()                      # 建资源（类似"工作目录"）
  res.metadata.title = "..."            # 内存里改元数据
  res.save()                            # 一次性回写
  ```

  即 **"内存对象 → save() 提交"** 的读写分离。

### 值得借鉴

- **元数据对象化 + 显式 save**：本项目 Manifest 的元数据写入可借鉴"先构造、后提交"的两阶段语义（与 `submit_acquisition` 的审批两阶段同构）。
- **`sign_in()` 只负责登录**，不混合业务：与 Skill 里"环境配置与采集分离"的原则一致。

### 不该学（取舍）

- HydroShare 是**发布/共享**导向（写回远端、协作元数据），本项目是**采集/审计**导向（不写回远端）。混用会导致"采集器替用户发布数据"的越权风险——本项目明确不做。
- 平台本体是 Django + 多服务（PostgreSQL/Redis 等，未读正文，不细究）——正是本项目 ADR-0001 明确排除的形态；只把它当**数据源**（通过 API 拿文件），不改造为依赖。

---

## 6. 横向结论：本项目应当"取什么、不取什么"

| 能力 | 外部最佳实践 | 本项目现状 | 行动建议（M6/M7） |
|---|---|---|---|
| 多源抽象 | Provider 注册表 + 统一契约（本项目）vs 一包一服务（HyRiver）vs 单生态（earthaccess） | ✅ 已有 Sources/Registry + `access_route` | 保持；在 Skill 里把 `get_provider_capabilities` 作为"源能力边界"的统一出口 |
| 清单/目录 | ESM：JSON descriptor + CSV catalog | SQLite Manifest + `rdh report` | **导出交付清单时同时给 JSON + 行式（CSV）两种**更利于科研复现（可后续做，非 M6 阻塞项） |
| 完整性 | Pooch `known_hash`；本项目六层 | ✅ 更强 | 在 Skill 的验证流程里强调"未验证项必须标 unverified"（Pooch 的做法是"没有 hash 就不校验"，本项目更严） |
| 认证 | earthaccess 的查找顺序 + CDSE 的 OAuth2 + CDS 的 rc 文件 | 环境检查框架已有 | 环境引导文案按"凭证形态"分类（长期 key / 短期 token / 账号密码） |
| 缓存 | HyRiver 的 SQLite URL 缓存 | 无（有 Manifest） | 若加缓存，复用 SQLite，不引入 Redis |
| 状态机/恢复 | **外部项目普遍没有**（Pooch/intake-esm/earthaccess/HyRiver 都不做） | ✅ 有租约/心跳/崩溃接管 | 这是本项目护城河，Skill 文案要显式告诉 Agent"失败了别重头再来，先 `get_task_status`" |
| 发布/共享 | HydroShare/hsclient | 明确不做 | 保持；不要为了"能上传"引入写回远端的权限 |

---

## 7. 来源清单

**已读正文**
- [intake-esm README](https://github.com/intake/intake-esm) · [ESM Catalog Specification](https://github.com/intake/intake-esm/blob/main/docs/source/reference/esm-catalog-spec.md)
- [Pooch README](https://github.com/fatiando/pooch)
- [earthaccess README](https://github.com/earthaccess-dev/earthaccess) · [认证 how-to](https://earthaccess.readthedocs.io/en/latest/user/howto/authenticate/)
- [async-retriever README](https://github.com/hyriver/async-retriever) · [pygeohydro README](https://github.com/hyriver/pygeohydro)
- [hsclient README](https://github.com/hydroshare/hsclient)

**未读正文（仅摘要）**
- [hydroshare/hydroshare](https://github.com/hydroshare/hydroshare) —— 仓库元数据（BSD-3-Clause、200 stars、默认分支 `develop`）来自 GitHub API，README 正文 404 未读到。
- [pangeo-datastore](https://github.com/pangeo-data/pangeo-datastore) —— README 已读（Intake catalog + Zarr 目录），仅作 intake-esm 生态背景，未作为独立条目展开。

**本机实测**
- PyPI JSON：`earthaccess 0.19.0`、`cdsapi 0.7.7`、`ecmwf-datastores-client 0.6.0`。
