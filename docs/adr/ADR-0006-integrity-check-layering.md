# ADR-0006：完整性校验的分层策略与 checksum 可信度

| 项 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-11-04 |
| 决策者 | 架构负责人（Lead） |
| 影响范围 | `integrity` 模块、`core.models.IntegrityReport`、`manifest.integrity_reports`、交付清单 |

---

## Context

"数据下完了"与"数据可用"之间隔着好几层判断。常见的错误是把低层证据当高层结论：

* 文件存在、大小对 → **不等于**内容正确；
* `xarray.open_dataset()` 成功 → **不等于**文件没被截断，更不等于科学数据正确；
* S3 的 ETag 与本地 MD5 相同 → **不等于**做过端到端校验（multipart 上传的 ETag 根本不是
  内容 MD5）；
* 变量名相同 → **不等于**两个数据集科学等价（单位、网格、垂直层、版本都可能不同）。

同时，用户需要在**不同强度的保证**之间做选择：小样本实验只需大小校验，正式交付需要
SHA-256 + 格式 + 元数据校验。若只有一个布尔 "verified"，用户无法表达也无法理解差异。

---

## Decision

**把校验拆成六个显式层级；每条结果都带层级标签；只有全部 PASSED 才允许 COMPLETED。**

### 1. 六个层级（`CheckLevel`）

| 层级 | 检查 | 能证明 | 不能证明 |
|---|---|---|---|
| `existence` | exists / readable | 有字节、能读 | 任何内容属性 |
| `size` | non_empty / size_match / truncation | 传输完整（预期大小可信时） | 内容未被篡改 |
| `checksum` | checksum_match（SHA-256） | 内容与某已知值一致 | 与远端一致（除非值来自远端） |
| `protocol` | checksum 来源、ETag 误用检测 | 校验依据本身可信 | 数据本身正确 |
| `format` | format_sniff / netcdf_openable | 结构合法、能解析 | 科学语义正确 |
| `scientific` | variable_present / time_coverage / metadata_consistent | 与声明一致 | 科学结论正确 |

**层级不可互相替代。** 报告里的 `level` 字段使"我们到底证明了什么"可被程序与人都读到。

### 2. 四条不可逾越的边界

1. 格式检查**不能**代替科学正确性验证。`netcdf_openable` 通过只说明结构合法。
2. 变量名相同**不能**推断科学等价。等价性判定看
   `DatasetMetadata.scientific_identity()`（产品、版本、单位、时间定义、空间网格、
   垂直层、后处理、是否重网格化）。
3. 缺失的元数据标 `unknown` / `None`，**不推断**。"不知道该数据的单位"必须显式暴露，
   而不是默认成某个常见值。
4. 本系统不做 EDA、不做模型计算、不修改原始数据内容。

### 3. Checksum 可信度分级

```
① 数据源提供的可信 checksum（厂商发布清单）   → 可做端到端校验 → remote_checksum_verified
② 传输协议提供的可信校验信息（服务端确认）     → 视情况 → protocol 层判定
③ 本地计算的 SHA-256                        → 只能自证"从现在起未变" → local_sha256_recorded
```

报告与 `FileRecord.extensions` 中显式区分：

| 标记 | 含义 |
|---|---|
| `remote_checksum_verified` | 远端值被本地重算证实 |
| `local_sha256_recorded` | 本地 SHA-256 已记录 |
| `integrity.checksum_origin` | `remote_trusted` / `local_computed` / `remote_etag` |

**S3 ETag 纪律**：`transfer.s3.verify_etag_as_md5` 默认 `false` 且必须保持 false。
若某记录的 MD5 等于远端 ETag，或来源标为 `remote_etag`，校验直接判 **protocol 级 FAILED**。
宁可多算一次哈希，也不接受把不可靠的值当内容校验。

### 4. 结果语义：四态而非布尔

| 状态 | 含义 | 允许 COMPLETED |
|---|---|---|
| `PASSED` | 所有被要求的检查通过 | ✅ |
| `FAILED` | 至少一项不通过 | ❌ → QUARANTINED |
| `SKIPPED` | 未要求/依赖不可用（如未安装 netCDF 栈） | ❌（**不等于通过**） |
| `UNKNOWN` | 没有证据 | ❌ |

模型层强制（`IntegrityReport` 不变式）：`PASSED` 不能带 failures；`FAILED` 必须至少一条
failure；`permits_completion` 仅 `PASSED` 为真。**"没查"永远不能变成"查过了"**。

### 5. 校验失败的动作

```
IntegrityReport(status=FAILED)
   → storage.quarantine(文件)          # 保留文件，写 .reason.json
   → task: VERIFYING → QUARANTINED     # 终态，不自动复活
   → 交付清单中标记 validation_status=failed + 细节
```

`QUARANTINED` 是终态：修复后需要**重新规划**产生新任务，而不是把旧任务改回 PENDING。
这样"坏数据被交付过"在事实层面上无法被悄悄抹掉。

### 6. 检查链可配置但不可绕过

`defaults.integrity.default_checks` 决定默认检查项；`DatasetRequest.checks` 可按请求覆盖。
但**不能**通过配置让 `RUNNING` 直接进入 `COMPLETED`——状态机物理禁止（`core.state`）。

---

## Consequences

**好处**

* 用户能准确知道"这次交付的保证强度"（大小对齐？哈希对齐？格式可读？语义对齐？）；
* 把"ETag 当 MD5"这类行业常见错误变成**显式失败**而不是静默错误；
* 依赖缺失时是 SKIPPED 而不是虚假 PASSED，运维能一眼看出保证被削弱；
* 失败文件保留 + 报告保留，事故可复盘。

**代价**

* 报告结构比一个布尔值复杂（多一层 level/expected/actual/detail）；
* 每个文件多算一次 SHA-256（大文件有 I/O 成本，用 1 MiB 流式块，内存恒定）；
* `SKIPPED`/`UNKNOWN` 会让"自动放行"更困难——这是刻意的。

---

## Alternatives considered

**A. 单一布尔 `verified`**：无法表达校验强度，也无法解释"为什么这个文件被隔离"。❌

**B. 只要大小对就算完成**：内容可能错位（错误页、截断、错误版本），在科研数据里不可接受。❌

**C. 用 ETag 作为校验和**：multipart/加密场景下完全错误，且失败静默。❌

**D. 校验失败即删除文件**：丢失证据，无法复盘；也无法区分"数据源给错了"与"我们下错了"。❌

**E. 只做格式检查（能 open 就算好）**：把"结构合法"冒充"数据正确"，是最危险的假安全感。❌

---

## 验证方式

* `tests/unit/test_integrity/test_checks.py`：空文件/缺失/大小不符/截断/超大、fail_fast、四态语义。
* `tests/unit/test_integrity/test_checksum_provenance.py`：远端可信值 vs 本地计算值、ETag 当 MD5 必须失败。
* `tests/unit/test_integrity/test_netcdf.py`：正常 NetCDF、损坏 NetCDF、截断 NetCDF、依赖缺席 → SKIPPED。
* `tests/unit/test_integrity/test_metadata.py`：缺失变量、无期望元数据 → SKIPPED、不支持的键 → 失败。
* `tests/unit/test_reporting/test_delivery.py`：`complete` 只在全 PASSED 时成立；失败/跳过/隔离都保留。

## 相关文档

* [数据完整性](../data-integrity.md)：面向使用者的完整说明
* [ADR-0005](ADR-0005-staging-raw-quarantine-storage.md)：隔离区与原子提交
* [数据契约 §6/§9](../data-contract.md)：`IntegrityStatus` 与科学等价性
* 源码：[`integrity/checker.py`](../../src/researchdatahub/integrity/checker.py)、[`core/models.py`](../../src/researchdatahub/core/models.py)
