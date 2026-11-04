# ADR-0002：数据源适配器与传输适配器分离

| 项 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-11-04 |
| 决策者 | 架构负责人（arch） |
| 影响范围 | `sources` / `transfer` 两个模块、`SourceAsset` 模型、配置结构 |

---

## Context

同样叫"下载一个 ERA5 文件"，不同数据源的实际过程差异极大。举两个真实例子：

### 例子 A：NCAR RDA

NSF NCAR Research Data Archive 提供 **静态文件目录**。给定数据集（如 `ds633.0`）和文件，
文件有稳定的路径与命名：

- 通过 **S3**：`s3://rda.ucar.edu/datasets/ds633.0/...`，公开桶，无需认证；
- 通过 **HTTPS**：`https://rda.ucar.edu/data/ds633.0/...`，需要 cookie/token；
- 通过 **Globus**：端点 `rda#datashare`，需要 Globus 授权与传输任务；
- 通过 **Zarr**：分块对象存储，读取的是 chunk 而不是整个文件。

**关键点**：NCAR 侧的复杂度在"**如何列出文件、如何拼路径、哪些数据集需要注册**"，
而字节的搬运本身是标准协议（S3 GET / HTTP GET / Globus transfer）。

### 例子 B：Copernicus CDS

CDS **不是静态文件目录**。它的流程是：

1. 客户端提交一个**请求对象**：数据集名 + 变量列表 + 压力层 + 区域 + 时间段 + 输出格式；
2. CDS 把请求放进服务端队列，**排队生成**（可能几分钟到几小时）；
3. 客户端轮询请求状态；
4. 生成完成后，得到一个**有时效性的下载 URL**；
5. 从这个 URL 下载 zip/GRIB 文件。

**关键点**：CDS 侧没有"文件路径"这个东西。远端文件是**请求的产物**，
它的 URL 是临时的、不可预测的、会过期的。所谓的"下载"之前有一整套"请求—排队—轮询"逻辑。

### 冲突

如果按"每个源一个 worker"来组织代码，那么 S3 worker 里必须写：

```python
# ❌ 反例：把数据源逻辑写进传输层
def transfer(asset, dest):
    if asset.source_id == "ncar":
        url = f"https://rda.ucar.edu/data/{asset.dataset_id}/{asset.file_name}"
        key = url.replace("https://rda.ucar.edu/", "")
    elif asset.source_id == "cds":
        # CDS 压根没有静态路径：要提交请求、轮询、等结果
        req = cdsapi.Client(key=..., url=...).retrieve(...)  # 逻辑完全不同
    elif asset.source_id == "aws_open_data":
        ...
```

这段代码有四个问题：

1. **每加一个源就要改传输模块**，传输模块成了"源知识的垃圾场"；
2. **CDS 的分支根本无法复用任何 S3 代码**，因为流程不同（请求 vs GET）；
3. **传输模块需要读配置里的凭证**（`CDSAPI_KEY`），把安全边界扩散了；
4. **无法测试**：测 S3 传输要先有 NCAR 的路径知识。

---

## Decision

**把"发现"与"搬运"拆成两个独立的 Protocol，二者通过 `AccessRoute` 与 `origin_uri` 连接。**

### 1. `DataSourceAdapter`（`ports.py:55`）负责"数据在哪"

- 目录查询、参数翻译（科学需求 → 远端请求参数）、元数据解析；
- 产出 `SourceAsset`：包含 `origin_uri`、`access_route`、`size_bytes`、可选的 `checksum`、
  以及完整的 `SourceMetadata`（provider、license、citation、retrieved_at）；
- **不下载任何字节**。

### 2. `TransportAdapter`（`ports.py:128`）负责"怎么搬"

- 连接、超时、分块、断点续传、重试策略执行、原子落盘；
- 输入只有一个 `SourceAsset` 和一个目标路径，**它不知道数据源是谁**；
- 通过 `supports(asset)` 判断自己能否处理（依据 `access_route` 与 URI scheme）。

### 3. 连接点：`AccessRoute` + `origin_uri`

`AccessRoute`（`models.py:114`）有六个取值：`s3`、`https`、`globus`、`zarr`、`cds_api`、`local`。

**源特定的信息必须被压缩进 `origin_uri` 与 `access_route`，而不是传进传输实现。**

`ports.py:132-135` 把这条写成了硬性约束：

> 不得包含任何特化到某个数据源的业务逻辑（例如「NCAR 的 S3 路径规则」）。
> 源特定的路径/认证/前缀必须由 `SourceAsset.origin_uri` 与 `access_route` 完全表达。

### 4. 组合关系因此变成正交的

| 源 | `s3` | `https` | `globus` | `zarr` | `cds_api` |
|---|---|---|---|---|---|
| NCAR RDA | ✅ | ✅ | ✅ | ✅ | — |
| CDS | — | — | — | — | ✅ |
| AWS Open Data | ✅ | ✅ | — | ✅ | — |

`S3Transport` 一份实现同时服务 NCAR 和 AWS Open Data；新增一个 S3 数据源**不需要改传输代码**。

### 5. CDS 的"异步生成"如何落在契约内

CDS 看似打破了"传输就是 GET"的假设。处理方式是：

**"提交请求 → 轮询 → 拿到临时 URL"属于发现阶段**，由 `CdsApiSource.discover_assets()` 完成。
它返回的 `SourceAsset.origin_uri` 是**已经生成好的临时下载 URL**，`access_route=cds_api`。
随后 `CdsApiTransport` 只做一件事：从那个 URL 把字节取回来（带过期重试）。

如果 URL 在下载过程中过期（CDS 常见），传输层抛 `RetryableTransportError`，
状态机进入 `RETRY_WAIT`；重试前调度器会**重新走一次发现**以获得新 URL。

> **注意**：上面描述的是契约允许的形态，`CdsApiSource` 与 `CdsApiTransport` 在 M0 **尚未实现**，
> 计划在 M1 交付（见 [`docs/development-plan.md`](../development-plan.md)）。

---

## Consequences

### 正面

1. **传输实现可在源之间复用**：一个 `S3Transport` 服务所有 S3 源，代码量与源数量解耦。
2. **新增源不改传输层**：新增数据源 = 新增一个 `DataSourceAdapter` 实现 + 一条配置；
   `transfer/` 目录一行不动。
3. **测试可独立**：`TransportAdapter` 可以用本地文件系统实现来测（`tests/fixtures/contract_fakes.py`
   里的 `LocalCopyTransport` 就是），不需要网络，也不需要任何源知识。
4. **凭证边界清晰**：只有 `DataSourceAdapter` 需要读源凭证；`TransportAdapter` 只在必要时
   （如 S3 预签名）接触。
5. **`access_route` 成为可观测的调度维度**：`source list` 能直接展示"这个源支持哪几条路径"
   （实测 `rdh source list` 输出 `ncar → globus, https, s3, zarr`），调度器据此选择可用路径。
6. **一处失败可降级**：NCAR 的 S3 不可用时，同一源可以换 HTTPS 路径重试，因为二者都是
   同一个 `SourceAsset` 的不同 route 表达。

### 负面

1. **`SourceAsset` 承载的信息变多**：它必须足够完整，让传输层不需要回查源。
   代价是 `origin_uri`、`checksum`、`content_type` 等字段都要在发现阶段填好。
   - 缓解：`SourceAsset._consistent_source`（`models.py:478`）强制元数据一致性，
     遗漏会在构造时暴露。
2. **CDS 这类"非静态"源需要额外设计**：临时 URL 的过期语义必须由契约表达（重试重新发现）。
   - 代价是调度器要支持"重试前重新发现"这一动作。M1 需要明确实现。
3. **可能出现"两层都以为对方负责"的空隙**：例如"CDS 请求失败了谁报错"。
   - 缓解：契约明确——**发现阶段的错误抛 `SourceUnavailableError`，传输阶段的错误抛
     `RetryableTransportError`/`PermanentTransportError`**，二者在异常层次上是分开的
     （`errors.py:140-205`）。
4. **多一跳间接**：读代码时要跳两次（发现 → 传输）才能理解完整流程。
   - 缓解：`AccessRoute` 枚举只有 6 个值，`supports()` 让映射一目了然。

---

## Alternatives considered

### 方案 A：每个数据源一个完整的 worker 类（"源即 worker"）

```python
class NcarWorker:
    def run(self, request): discover + download + verify + save
class CdsWorker:
    def run(self, request): discover + submit + poll + download + verify + save
```

**为什么拒绝**：

- **重复实现**：校验、重试、断点续传、原子落盘在每个 worker 里各写一遍，行为会逐渐分叉；
- **无法测试**：测 CDS 的校验逻辑要先 mock 掉 CDS 的整个请求流程；
- **横向耦合**：每个 worker 同时依赖 `sources`、`transfer`、`integrity`、`storage`、`manifest`
  五个模块，完全破坏依赖矩阵；
- **加一条访问路径就要复制整个 worker**：NCAR 从 S3 改成 Globus，等于重写一个 worker。

### 方案 B：一个统一的 `Transport` 接口，用 `if source_id == ...` 分支处理差异

**为什么拒绝**：见 Context 中的反例代码。本质上它把"源知识"塞进了"传输实现"，
导致传输模块的每一个改动都牵连所有源，且违反开闭原则。

### 方案 C：让传输层回查数据源（`transport.transfer(asset, source_adapter)`）

**为什么拒绝**：

- 传输层会依赖 `DataSourceAdapter`，形成 `transfer → sources` 的横向依赖，违反依赖矩阵；
- 传输过程中重新查询会改变"下载的是哪个版本"的语义（远端可能已更新）；
- 断点续传需要"同一个文件"，回查可能得到不同的 URI。

### 方案 D：把 `AccessRoute` 做成开放式字符串

**为什么拒绝**：枚举有 6 个固定值，能让 `supports()` 的实现变成一次集合查找，
也让配置校验（`SourceConfig.routes`）能在加载期发现拼写错误。开放字符串会把错误推迟到运行时。

---

## 验证方式（如何确认实现没有跑偏）

1. **代码检查**：`transfer/` 目录下不得出现任何源 ID 字面量：
   ```bash
   # 期望无输出
   grep -rnE "ncar|cds|pangeo|aws" src/researchdatahub/transfer/ --include="*.py" -i
   ```
2. **契约检查**：`TransportAdapter.supports(asset)` 必须只依据 `asset.access_route` 与
   `asset.origin_uri` 的 scheme 判断。
3. **依赖检查**：`transfer` 不得 import `sources`（见 [`docs/module-boundaries.md` §4.2](../module-boundaries.md)）。
4. **测试检查**：`TransportAdapter` 的测试必须能在**完全离线**的情况下通过。

---

## 相关文档

- 字段语义：[`docs/data-contract.md` §3](../data-contract.md)
- 模块 API：`TransportAdapter` / `DataSourceAdapter` 见 [`docs/module-boundaries.md` §2.4](../module-boundaries.md)
- 数据流中的位置：[`docs/architecture.md` §1](../architecture.md)
