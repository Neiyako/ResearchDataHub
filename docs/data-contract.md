# 数据契约（Data Contract）

本文定义 ResearchDataHub 中**每一个关键概念的边界**。所有定义都以 `src/researchdatahub/core/models.py`
的实际代码为准；本文引用的每个字段、约束、方法，都能在该文件中找到对应实现。

契约版本：`MODELS_VERSION = "1.0"`（见 [`src/researchdatahub/core/models.py:66`](../src/researchdatahub/core/models.py#L66)）。

---

## 0. 这份契约要解决什么问题

科研数据采集的真正难点不是“把文件下载下来”，而是**数据在流转过程中不被悄悄改变身份**。
同一个变量名 `t2m`，在 ERA5 里是 2 米气温（K，瞬时值），在某个再分析产品里可能是日均值
（°C，日平均）。如果系统把二者当成同一个东西，后续科学结论就是错的，而且**错误不会报错**。

因此本契约把“文件字节”和“科学内容”彻底分成两层，并要求每一次流转都保留来源追踪。

---

## 1. 数据集（Dataset） vs 数据资产（Asset）

| | 数据集 Dataset | 数据资产 / 源资产 Asset |
|---|---|---|
| 对应类型 | `DatasetMetadata`（及其所在的 `DatasetRequest`） | `SourceAsset` |
| 回答的问题 | **这是什么科学数据**：产品、变量、单位、时间、空间、垂直层 | **某个源上的哪个文件**：URI、大小、校验和 |
| 是否依赖具体数据源 | 否。`DatasetMetadata` 里没有任何 ERA5/CDS/NCAR 专属字段 | 是。`SourceAsset.source_id` 指向一个具体源 |
| 生命周期 | 稳定，跨源一致 | 随远端组织方式变化（切分方式、命名规则） |
| 数量关系 | 1 个数据集 | N 个源 × M 个文件片段 |

代码依据：

- `DatasetMetadata`（[`models.py:330`](../src/researchdatahub/core/models.py#L330)）的字段全部是科学属性；
  数据源专属信息只能放进 `extensions`，且 key 必须命名空间化（`_namespaced_extensions`，[`models.py:361`](../src/researchdatahub/core/models.py#L361)）。
- `SourceAsset`（[`models.py:451`](../src/researchdatahub/core/models.py#L451)）带有 `file_name`、`size_bytes`、
  `remote_etag`、`time_slice`、`level_slice`——这些是**文件的切分事实**，不是科学定义。
- 同一数据集从两个源获取，会得到两组不同的 `SourceAsset`，但 `DatasetMetadata` 应当一致（否则说明两个源提供的**不是同一份科学数据**）。

**反例（禁止）**：把“ERA5 在 CDS 上分成 24 个 per-day 的 GRIB 文件”这种事实写进 `DatasetMetadata`。
它属于资产层；写进科学元数据会导致“换一个源就判定不等价”。

---

## 2. 用户需求（Request） vs 远端文件（Asset）

- **`DatasetRequest`**（[`models.py:424`](../src/researchdatahub/core/models.py#L424)）表达“我要什么数据”：
  `request_id`、`dataset_id`、内嵌 `metadata`（科学身份）、`preferred_sources`、`max_sources`、
  `checks`（要跑哪些校验）、`preserve_original`、`priority`。
  它**不出现任何 URL**——用户不负责知道文件在哪。
- **`SourceAsset`** 表达“哪里有一个文件”，由 `DataSourceAdapter.discover_assets(request)` 产生。

二者的桥接是 `AcquisitionPlan`（[`models.py:519`](../src/researchdatahub/core/models.py#L519)）：

```
DatasetRequest ──(planner + adapters)──► AcquisitionPlan ──► AcquisitionTask[0..n]
                                                    每个 task 内嵌一个 SourceAsset
```

计划器必须能把“需求中无法被任何源满足的变量”显式写进 `AcquisitionPlan.unmatched_variables`
（[`models.py:526`](../src/researchdatahub/core/models.py#L526)）。**不允许静默丢弃**：
用户请求了 `t2m` 和 `clwc`，只拿到 `t2m` 却报告“完成”，属于缺陷。

---

## 3. 数据源适配器（Source Adapter） vs 传输适配器（Transport Adapter）

这一分界由 ADR-0002 确立，是 M0 最重要的架构约束。

| | `DataSourceAdapter` | `TransportAdapter` |
|---|---|---|
| 职责 | **发现**：目录查询、参数翻译、元数据解析、生成 `SourceAsset` | **搬运**：连接、超时、分块、断点续传、重试、落盘 |
| 知道科学语义吗 | 知道。它懂“ERA5 pressure-levels 的 `t` 参数对应温度” | **不知道**。它只看到一个 URI |
| 能下载字节吗 | 不能 | 只能下载字节，不能解析科学含义 |
| 协议位置 | [`ports.py:55`](../src/researchdatahub/core/ports.py#L55) | [`ports.py:128`](../src/researchdatahub/core/ports.py#L128) |
| 典型实现 | `Era5CdsSource`、`NcarRdaSource` | `S3Transport`、`HttpsTransport`、`CdsApiTransport` |

`TransportAdapter` 的 docstring 明确写了“不得包含任何特化到某个数据源的业务逻辑”
（[`ports.py:129`](../src/researchdatahub/core/ports.py#L129)），并且要求源特定的路径/认证信息
必须由 `SourceAsset.origin_uri` + `access_route` 完全表达。

组合关系：同一个 `S3Transport` 可以服务 NCAR 和 AWS Open Data 两个源；同一个
`Era5CdsSource` 可以选择走 `CDS_API` 或（若已镜像）`S3`。这种正交性就是分层的价值。

---

## 4. 文件（字节） vs 科学内容（Scientific Content）

- `FileRecord`（[`models.py:582`](../src/researchdatahub/core/models.py#L582)）描述**字节事实**：
  `local_path`、`size_bytes`、`checksum`（**必填**）、`content_type`、`retrieved_at`、`is_read_only`。
- `DatasetMetadata` 描述**科学内容**：变量、单位、网格、垂直层、时间定义、后处理。

二者**不可互相替代**：

- 只看 `FileRecord`：知道文件在磁盘上、10 MB、sha256 正确——但不知道里面是不是你要的数据。
- 只看 `DatasetMetadata`：知道这个数据集的科学身份——但不知道本地那份副本有没有下完整、有没有被覆盖。

**一次合格的交付必须同时具备两层证据**。`DeliveryManifest`（[`models.py:640`](../src/researchdatahub/core/models.py#L640)）
正是把两层绑在一起的容器：`dataset_metadata`（科学层）+ `files: list[FileRecord]`（字节层）+ `integrity`（校验证据）。

注意 `FileRecord.checksum` 是**必填**且非 `None`，而 `SourceAsset.checksum` 可为 `None`
（远端不一定提供）。这意味着：远端没给校验和时，本地必须自己算一份——不能留着空。

---

## 5. Manifest（运行时状态库） vs DeliveryManifest（可移植交付产物）

同名但完全不同的两个东西，混用会造成严重后果。

| | Manifest（SQLite 运行时状态库） | `DeliveryManifest` |
|---|---|---|
| 是什么 | 采集过程中的**活状态**：任务状态、事件日志、文件登记 | 一次采集结束后**冻结的交付清单** |
| 协议/实现 | `ManifestRepository`（[`ports.py:339`](../src/researchdatahub/core/ports.py#L339)），SQLite 实现见 ADR-0003 | Pydantic 模型（[`models.py:640`](../src/researchdatahub/core/models.py#L640)） |
| 是否可变 | 持续写入（状态推进、事件追加） | **不可变**（`frozen=True`），生成即定稿 |
| 能否给别人 | 不能。包含运行期临时状态、worker_id、重试记录 | 能。可复制、写进论文附录、用于复现 |
| 生命周期 | 与本次运行/项目绑定 | 永久（归档、引用） |
| 生成方式 | — | `ManifestRepository.build_delivery_manifest(request_id)`（[`ports.py:392`](../src/researchdatahub/core/ports.py#L392)） |

`DeliveryManifest` 的自证约束（[`models.py:666`](../src/researchdatahub/core/models.py#L666)）：

1. `total_bytes` 若不为 0，必须等于所有 `files[].size_bytes` 之和；
2. `complete=True` 时，`integrity` 中**不允许存在任何未 PASSED 的报告**；
3. `complete=True` 时，`total_bytes` 必须非零。

> **实测行为（本文写作时验证）**：因为约束 3 在构造时校验，所以 `complete=True` 的清单必须在
> 构造时直接给出 `total_bytes`；`with_totals()`（[`models.py:677`](../src/researchdatahub/core/models.py#L677)）
> 用于对**已通过校验的对象**补齐总量，不能用来绕过构造期校验。

---

## 6. 校验结果的语义（IntegrityStatus）

`IntegrityStatus`（[`models.py:147`](../src/researchdatahub/core/models.py#L147)）有四个取值，
它们**不是**“通过/不通过”的二值化：

| 取值 | 含义 | 允许进入 `COMPLETED`？ |
|---|---|---|
| `PASSED` | 所有请求的检查都执行了，且全部通过 | ✅ 允许 |
| `FAILED` | 至少一项检查执行了并失败；`failures` 必须非空 | ❌ 不允许。**任务**从 `VERIFYING` 转 `QUARANTINED` |
| `SKIPPED` | 用户显式要求跳过，或该检查对本次数据不适用 | ❌ 不允许（跳过 ≠ 通过） |
| `UNKNOWN` | 校验未能得出结论（依赖缺失、文件不可解析、超时） | ❌ **不允许** |

> **状态去向的精确说法**（容易写错）：
> 校验失败发生在 `VERIFYING` 阶段，因此任务走的是 `VERIFYING → QUARANTINED`。
> 如果任务**已经**以 `FAILED` 结束（终态），事后审计才发现其产物损坏，此时才需要
> `FAILED → QUARANTINED`，且必须显式传 `after_integrity=True`（[`state.py:169`](../src/researchdatahub/core/state.py#L169)）。
> 两条路径不可混淆：前者是常规边，后者是审计专用例外。

**`UNKNOWN` 不等于通过。** 这条规则有两处代码强制：

1. `IntegrityReport.permits_completion`（[`models.py:635`](../src/researchdatahub/core/models.py#L635)）
   实现为 `self.status is IntegrityStatus.PASSED`——只有 `PASSED` 返回 `True`。
2. `DeliveryManifest._check_completeness`（[`models.py:666`](../src/researchdatahub/core/models.py#L666)）
   对 `complete=True` 的清单做全量检查，任何非 `PASSED` 报告都会让构造失败。

实测确认：`IntegrityStatus.UNKNOWN` 构造出的报告 `permits_completion == False`；
把它放进 `complete=True` 的 `DeliveryManifest` 会抛 `ValidationError`。

另外两条内部一致性约束（[`models.py:627`](../src/researchdatahub/core/models.py#L627)）：

- `PASSED` 的报告**不能有** `failures`；
- `FAILED` 的报告**必须列出**至少一条 `failures`。

之所以需要“状态 + 失败列表”两个字段而不是一个布尔值，是因为用户需要知道**哪一项检查失败了**
（`IntegrityCheckKind` 共 7 种：`size_match`、`checksum_match`、`format_sniff`、`netcdf_openable`、
`variable_present`、`time_coverage`、`metadata_consistent`）。

> **M0 现状**：以上只是**契约**。具体的检查算法（尤其是 `netcdf_openable`、`variable_present`、
> `time_coverage`）依赖 `sci` extras（`xarray` + `netCDF4`），在 M1 实现。M0 仓库中不存在
> `IntegrityChecker` 的任何实现类。

---

## 7. 来源追踪规则（Provenance）

**规则：每一级都保留 `source_id` / `access_route` / `origin_uri` / `retrieved_at`，任何一级都不允许丢。**

链路与该规则在各级的落点：

| 层级 | 类型 | 来源字段 |
|---|---|---|
| 数据源 | `SourceMetadata`（[`models.py:392`](../src/researchdatahub/core/models.py#L392)） | `source_id`、`access_route`、`origin_uri`、`retrieved_at`、`provider`、`license`、`citation` |
| 源资产 | `SourceAsset`（[`models.py:451`](../src/researchdatahub/core/models.py#L451)） | `source_id`、`access_route`、`origin_uri`、内嵌 `metadata: SourceMetadata`、`discovered_at` |
| 任务 | `AcquisitionTask`（[`models.py:486`](../src/researchdatahub/core/models.py#L486)） | 通过内嵌 `asset` 继承全部来源字段 |
| 本地文件 | `FileRecord`（[`models.py:582`](../src/researchdatahub/core/models.py#L582)） | `source_id`、`access_route`、`origin_uri`、`retrieved_at`、`remote_etag`、`remote_last_modified` |
| 交付清单 | `DeliveryManifest`（[`models.py:640`](../src/researchdatahub/core/models.py#L640)） | `files[].*` 逐条 + `sources: list[SourceMetadata]` 汇总 |

一致性由 `SourceAsset._consistent_source`（[`models.py:478`](../src/researchdatahub/core/models.py#L478)）强制：
`metadata.source_id` 必须等于 `source_id`，`metadata.origin_uri` 必须等于 `origin_uri`。
这样“资产说自己来自源 A，但元数据说是源 B”这种撕裂状态根本无法构造。

**为什么 `origin_uri` 要一路带到 `FileRecord`**：文件被复制、改名、移动到归档盘之后，
唯一还能回答“这份数据当初从哪来”的就是 `origin_uri`。丢掉它，交付清单就无法被人核查。

**时间字段的硬性要求**：所有 `datetime` 字段都过 `_utc()`（[`models.py:95`](../src/researchdatahub/core/models.py#L95)），
naive（无时区）时间直接拒绝。实测：`TemporalExtent(start=datetime(2020,1,1), ...)` 抛 `ValidationError`。
理由：科研数据的时间戳若时区不明，跨源比对就没有意义。

---

## 8. 模型版本升级策略

### 8.1 实际取值

| 名称 | 取值 | 位置 | 含义 |
|---|---|---|---|
| `MODELS_VERSION` | `"1.0"` | [`models.py:66`](../src/researchdatahub/core/models.py#L66) | 整套数据契约的版本，`rdh version` 期望打印它 |
| `SCHEMA_VERSION` | `"1.0"` | [`models.py:69`](../src/researchdatahub/core/models.py#L69) | 单个模型的默认 `schema_version` 字段值 |
| 每个模型的 `schema_version` | `Literal["1.0"] = "1.0"` | [`models.py:196`](../src/researchdatahub/core/models.py#L196) | 所有继承 `_Contract` 的模型都带这个字段 |
| 包版本 `__version__` | `"0.1.0"` | [`src/researchdatahub/__init__.py`](../src/researchdatahub/__init__.py) | 软件版本，与契约版本**独立演进** |

校验逻辑在 `_check_schema_version`（[`models.py:102`](../src/researchdatahub/core/models.py#L102)）：
值不等于 `MODELS_VERSION` 就直接抛错，错误信息里点名“Run the migration described in docs/data-contract.md”。

实测：传入 `schema_version="2.0"` 会被 Pydantic 拒绝（`Literal["1.0"]` 约束 + validator 双重拦截）。

**注意 `Literal["1.0"]` 的后果**：`1.0` 是写死在类型标注里的。所以 1.x 内的加字段**不需要**改
`schema_version` 的值，但**如果将来要升到 2.0，就必须同时修改 `SCHEMA_VERSION`、`Literal` 标注、
`MODELS_VERSION` 三处**，并补迁移函数。这是一处已知的维护成本，属于有意为之（见下）。

### 8.2 1.x：只允许向前兼容的加法

**允许**：新增字段，且必须给默认值。例如给 `DatasetMetadata` 加一个 `spatial_resolution_m: float | None = None`。

**禁止**：删除字段、重命名字段、收紧已有约束（如把 `max_length=64` 改成 `max_length=32`）。

理由：已落盘的 Manifest 与已交付的 `DeliveryManifest` 都带着旧结构。加字段不破坏它们；
收紧约束会让历史数据在下次读取时直接失败。

### 8.3 2.0：破坏性变更 + 迁移函数

以下任一情况必须升 2.0：

- 删除或重命名字段；
- 改变字段语义（例如把 `size_bytes` 从“字节”改成“MB”）；
- 改变约束使得旧数据不再合法；
- 拆分/合并模型。

升级流程：

1. 把 `MODELS_VERSION` 与 `SCHEMA_VERSION` 改为 `"2.0"`，并把 `_Contract.schema_version` 的
   `Literal` 同步改成 `Literal["2.0"]`；
2. 提供**显式的迁移函数**（例如 `migrate_v1_to_v2(payload: dict) -> dict`），逐字段改写旧 JSON；
3. 迁移必须在**读入阶段**执行，不能靠“容错”糊过去；
4. 迁移前后都要有测试用例：用一份真实的 v1 落盘样本，迁移后构造 v2 模型成功且字段值符合预期；
5. 在本文增加一节“迁移记录”，写明变更点与迁移函数的入口。

### 8.4 `extra="forbid"` 的含义

`_Contract.model_config` 设置 `extra="forbid"`（[`models.py:188`](../src/researchdatahub/core/models.py#L188)，
见 [`models.py:180`](../src/researchdatahub/core/models.py#L180) 的类定义）。实测：多传一个未知字段直接抛
`ValidationError`。

这不只是“严格一点”，它有三个具体作用：

1. **防止上游悄悄塞数据源专属字段**。如果 adapter 往 `DatasetMetadata` 里塞了 `era5_param_id`，
   立刻失败，而不是让核心模型被污染。
2. **让版本错配立刻暴露**。若用旧版代码读新版数据（新加了字段），会立刻报错，而不是静默丢字段
   后继续跑出错误结论——这是科研数据里最危险的一类 bug。
3. **迫使升级走显式迁移**。不能靠“忽略未知字段”蒙混。

代价：1.x 加字段后，**旧版本读新数据会失败**。这是有意接受的：宁可报错，不可静默错。
迁移方向永远是“旧代码 → 迁移函数 → 新代码”，而不是“新代码偷偷兼容旧代码”。

### 8.5 其它不可变（frozen）约定

所有契约模型 `frozen=True`（[`models.py:189`](../src/researchdatahub/core/models.py#L189)）。实测：`md.product = "x"`
抛 `ValidationError`。因此状态变更只能“构造新对象”（`transition()` 用 `model_copy(update=...)` 返回新任务，
见 [`state.py:284`](../src/researchdatahub/core/state.py#L284)），不存在“改了一半没落盘”的中间态。

---

## 9. 科学数据等价性判定（Scientific Equivalence）

这是本契约的核心问题：**两个数据集在什么情况下可以认为是同一份科学数据？**

### 9.1 判定维度

判定必须同时检查以下 9 个维度。任何一项不同，就是**不同数据**，即使文件长得一样、变量同名。

| # | 维度 | 字段 | 为什么必须比对 | 常见陷阱 |
|---|---|---|---|---|
| 1 | 产品 | `product`、`product_type` | 再分析（`reanalysis`）与模式输出（`model-output`）物理意义不同，不能混用 | 都叫“2 米气温”，一个是观测约束的分析场，一个是模式预报 |
| 2 | 变量 | `variables[].name` | 变量集合本身定义了数据集内容 | 只比数量不比名字，`t2m` vs `sst` 会被判等 |
| 3 | 单位 | `variables[].units` | **同名变量不同单位 = 不同数据** | `K` 与 `degC` 数值差 273.15；`m/s` 与 `km/h` 差 3.6 倍 |
| 4 | 时间定义 | `time_definition` | 瞬时值 / 小时均值 / 月平均的统计含义完全不同 | `utc` 与 `utc_hourly` 被误认为等价，导致时间序列不可比 |
| 5 | 空间范围与网格分辨率 | `spatial_extent.{west,east,south,north,grid_degrees}` | 覆盖范围不同 → 空间统计不可比；分辨率不同 → 需重采样 | 0.25° 与 0.5° 的全球场看起来一样，格点数差 4 倍 |
| 6 | 垂直层次 | `vertical_levels.kind`、`vertical_levels.levels` | 等压面 500 hPa 与模式层 60 不是同一层 | 只存数字列表，丢掉 `kind`，导致“500”是 hPa 还是 m 无法区分 |
| 7 | 时间覆盖 | `temporal_coverage.{start,end}` | 区间不同 → 气候统计量不可比 | 起止相差一天，年平均就不同 |
| 8 | 数据版本 | `data_version` | 同一产品不同发布批次（如 ERA5 的 CDS 版本更新）结果会变 | 用“ERA5 就是 ERA5”覆盖掉版本差异，导致复现失败 |
| 9 | 后处理与重网格化 | `post_processing`、`regridded`、`grid.regridded`、`grid.projection`、`grid.source_grid` | 插值、面积平均、重网格化都会改变数值 | 重网格化后的产物被当成原生数据引用 |

### 9.2 `scientific_identity()` 如何实现这个判定

`DatasetMetadata.scientific_identity()`（[`models.py:369`](../src/researchdatahub/core/models.py#L369)）
返回一个**规范化元组**。判定规则：**两个数据集判等 ⇔ 该元组相等**。

```python
def scientific_identity(self) -> tuple[object, ...]:
    return (
        self.product,
        self.product_type,
        self.data_version,
        self.time_definition,
        tuple(sorted((v.name, v.units) for v in self.variables)),
        (self.temporal_coverage.start, self.temporal_coverage.end),
        (
            self.spatial_extent.west, self.spatial_extent.east,
            self.spatial_extent.south, self.spatial_extent.north,
            self.spatial_extent.grid_degrees,
        ),
        (self.vertical_levels.kind.value, tuple(self.vertical_levels.levels)),
        (self.grid.resolution_degrees, self.grid.projection, self.grid.regridded),
        tuple(sorted(self.post_processing)),
        self.regridded,
    )
```

与上面 9 个维度的对应关系：

| 元组位置 | 覆盖维度 |
|---|---|
| 0–1 `product`, `product_type` | 维度 1 产品 |
| 2 `data_version` | 维度 8 数据版本 |
| 3 `time_definition` | 维度 4 时间定义 |
| 4 `sorted((name, units))` | 维度 2 变量 + 维度 3 单位 |
| 5 `(start, end)` | 维度 7 时间覆盖 |
| 6 `(west, east, south, north, grid_degrees)` | 维度 5 空间范围与分辨率 |
| 7 `(kind.value, levels)` | 维度 6 垂直层次 |
| 8 `(resolution_degrees, projection, regridded)` | 维度 9 重网格化（网格侧） |
| 9 `sorted(post_processing)` | 维度 9 后处理 |
| 10 `regridded` | 维度 9 重网格化（顶层标记） |

规范化细节及其理由：

- **`sorted(...)` 而非原序**：变量与后处理的**顺序不影响科学含义**。`[u, t]` 与 `[t, u]` 是同一批数据。
- **变量配对成 `(name, units)` 再排序**：只比名字会漏掉单位差异；只比数量更糟。
- **垂直层保留 `kind.value`**：`(pressure, [500])` 与 `(height, [500])` 必须不等价。
- **`.value` 而非枚举对象**：与 JSON 落盘后的字符串形式对齐，避免“内存里相等、落盘后不等”。

### 9.3 实测验证结果

以下行为在本文写作时用真实代码逐条验证（`PYTHONPATH=src .venv/bin/python`）：

| 场景 | 判等结果 | 说明 |
|---|---|---|
| 两份完全相同构造的 `DatasetMetadata` | `True` | 基线 |
| 变量名相同（`t2m`），单位 `K` vs `degC` | **`False`** | 维度 3 |
| `data_version="unknown"` vs `"era5-v2"` | **`False`** | 维度 8 |
| `time_definition="utc"` vs `"utc_hourly"` | **`False`** | 维度 4 |
| `regridded=False` vs `True` | **`False`** | 维度 9 |
| `post_processing=["b","a"]` vs `["a","b"]` | `True` | 顺序无关 |

**结论：同名变量 ≠ 等价数据。** `t2m` 在 K 与 °C 下是两个不同的数据集，在任何需要“判断是否已有这份数据”
的地方（去重、缓存命中、复用已下载文件），都必须调用 `scientific_identity()`，而不是比对变量名或文件名。

### 9.4 明确不参与判定的字段

- `extensions`：数据源专属信息，同名数据集在不同源上取值天然不同，参与判定会让“同数据不同源”被判不等价。
- `units_normalized`：这只是“是否做过单位归一的标记”，已归一的 K 与未归一的 K 是同一科学数据。
- `VariableSpec.long_name` / `standard_name` / `cell_methods`：描述性文字，不参与判等。
  > ⚠️ 已知局限：`cell_methods`（如 `time: mean`）在 CF 规范中**确实**影响统计含义。
  > 当前 `scientific_identity()` 不包含它；若两个源对同一变量填了不同的 `cell_methods`，可能被判等价。
  > 这是 M0 的已知取舍，缓解手段是 `time_definition` 字段承载主要的时间统计约定。M1 是否纳入，
  > 见 [`docs/development-plan.md`](development-plan.md) 的风险条目；**它尚未实现，不要假定已覆盖**。

### 9.5 `fingerprint()` 不是科学等价判定

`_Contract.fingerprint()`（[`models.py:207`](../src/researchdatahub/core/models.py#L207)）计算
`model_dump_json()` 的 sha256 前 16 位。它的用途是**去重与审计**，不是科学判等：

- 它包含 `extensions`、`units_normalized` 等所有字段，因此两个科学等价、但元数据填写略有差异的对象
  会得到不同指纹；
- 字段顺序或描述性文字变化（如 `long_name` 补了一个字）也会改变指纹。

**规则**：判断“是不是同一份科学数据”用 `scientific_identity()`；判断“这两个序列化结果是否逐字节一致”
用 `fingerprint()`。两者不可互换。

---

## 10. 契约速查表

| 问题 | 用哪个 | 不用哪个 |
|---|---|---|
| 我要的数据是什么？ | `DatasetRequest` + `DatasetMetadata` | 文件名、URL |
| 远端有哪些文件？ | `SourceAsset` | `DatasetMetadata` |
| 该源的元信息？ | `SourceMetadata` | `SourceAsset.file_name` |
| 这次采集要跑几个任务？ | `AcquisitionPlan.tasks` | 临时列表 |
| 任务现在到哪一步了？ | `AcquisitionTask.state`（只能经 `transition()` 改） | 直接赋值（frozen，且语义禁止） |
| 下载成功了吗？ | `TransferResult.status` | 异常是否被吞（禁止） |
| 本地文件是什么？ | `FileRecord` | `TransferResult.destination_path` 单独用 |
| 数据可信吗？ | `IntegrityReport.status == PASSED` | `!= FAILED`（`UNKNOWN` 会漏网） |
| 能给别人的交付物？ | `DeliveryManifest` | SQLite Manifest |
| 这两个数据集是同一份吗？ | `scientific_identity()` | `fingerprint()`、变量名、文件名 |

---

## 11. 相关文档

- 模块接口的完整签名与异常：[`docs/module-boundaries.md`](module-boundaries.md)
- 数据在这些类型之间如何流动：[`docs/architecture.md`](architecture.md)
- 为什么源与传输要分开：[`docs/adr/ADR-0002-source-transport-separation.md`](adr/ADR-0002-source-transport-separation.md)
- 为什么 Manifest 用 SQLite 做事实来源：[`docs/adr/ADR-0003-sqlite-manifest-source-of-truth.md`](adr/ADR-0003-sqlite-manifest-source-of-truth.md)
