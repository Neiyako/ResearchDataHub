# 数据完整性（Data Integrity）

> **Integrity Before Delivery**：未经必要校验的数据不得被标记为完成，也不得进入交付清单。
> 状态机物理上禁止 `RUNNING → COMPLETED`，必须先经过 `VERIFYING`。

本文说明校验的**层次**、checksum 的**可信来源**、失败后的**隔离**，以及"能打开文件"和
"数据正确"之间的区别——后者**不能**由本系统保证。

---

## 1. 六个层次，不可混为一谈

科研数据最常见的假安全感，是把"文件下完了"当成"数据没问题"。本系统把检查分成六层，
每条报告都带 `level` 字段，**不允许把低层结论冒充高层结论**：

| 层级 | 回答的问题 | 典型检查 | 能证明什么 |
|---|---|---|---|
| `existence` | 文件在不在、可读吗 | `exists` / `readable` | 只是"有字节" |
| `size` | 字节数对不对、是不是空的/截断的 | `non_empty` / `size_match` / `truncation` | 传输完整（若预期大小可信） |
| `checksum` | 内容有没有变 | `checksum_match`（SHA-256） | 内容与某个已知值一致 |
| `protocol` | **校验值的来源可不可信** | checksum 来源标记、ETag 防误用 | 没有拿错东西当校验依据 |
| `format` | 文件结构是否合法 | `format_sniff` / `netcdf_openable` | 能解析，不保证语义 |
| `scientific` | 变量/时间/空间/单位是否符合预期 | `variable_present` / `time_coverage` / `metadata_consistent` | 与**声明**一致，不代表科学正确 |

**必须说清的三条边界**：

1. `netcdf_openable` 通过 ≠ 科学数据正确。文件能被 xarray 打开，只能说明 HDF/NetCDF 结构合法。
2. 变量名相同 ≠ 科学等价。等价性判定看 `DatasetMetadata.scientific_identity()`：
   产品、版本、单位、时间定义、空间网格、垂直层、后处理、是否重网格化（见
   [`data-contract.md` §9](data-contract.md)）。
3. 本系统**不做**科学结论推导、不做 EDA、不修改原始数据内容。

## 2. 检查链与失败语义

`IntegrityChecker.check()` 按 `kinds` 顺序执行（来自 `DatasetRequest.checks` 或
`defaults.integrity.default_checks`），`fail_fast` 控制是否首个失败即停。

| 检查 | 层 | 说明 |
|---|---|---|
| `exists` / `readable` | existence | 文件缺失 → 抛 `IntegrityError`（这是"检查无法进行"，不是"数据不合格"） |
| `non_empty` | size | 0 字节文件必然失败 |
| `size_match` | size | 仅当**预期大小可信**时才比较；远端未声明大小时不得凭空判失败 |
| `truncation` | size | 实际 < 预期 → 截断；实际 > 预期 ≠ 截断（可能是不同版本，单独判不一致） |
| `checksum_match` | checksum | 与期望 checksum 比较；期望值必须带算法前缀（`sha256:`） |
| `format_sniff` | format | 魔数/扩展名一致性 |
| `netcdf_openable` | format | xarray/netCDF4/h5netcdf 真实打开；依赖缺失时记 **SKIPPED**，绝不当 PASSED |
| `variable_present` | scientific | 期望变量是否存在于数据集 |
| `time_coverage` | scientific | 时间轴是否覆盖请求区间 |
| `metadata_consistent` | scientific | 声明的元数据与文件内属性是否一致 |

**结果语义**（`IntegrityReport.status`）：

- `PASSED`：所有被要求的检查通过 → 允许 `COMPLETED`；
- `FAILED`：至少一项不通过 → 文件进 `quarantine`，任务进 `QUARANTINED`（终态，不会自动复活）；
- `SKIPPED`：检查未被要求或依赖不可用 → **不等于通过**；
- `UNKNOWN`：无证据 → **不等于通过**。

模型层强制该语义：`PASSED` 不允许带 failures，`FAILED` 必须至少有一条 failure，
`report.permits_completion` 只在 `PASSED` 时为真（`core/models.py`）。

## 3. Checksum 策略：来源决定可信度

优先级从高到低：

1. **数据源提供的可信 checksum**（例如厂商发布的 `sha256` 清单）→ 可做端到端校验；
2. **传输协议提供的可信校验信息**（例如 S3 提供且服务端确认的校验值）；
3. **本地计算的 SHA-256** → 只能证明"从现在开始内容没变"，不能证明与远端一致。

报告里显式区分三个字段（写在 `IntegrityReport.observed` / `FileRecord.extensions`）：

| 标记 | 含义 |
|---|---|
| `remote_checksum_verified` | 远端声明值被本地重算**证实**（端到端一致） |
| `local_sha256_recorded` | 本地计算的 SHA-256 已记录（可做未来完整性比对） |
| `integrity.checksum_origin` | `remote_trusted` / `local_computed` / `remote_etag` |

### 3.1 ETag 绝不等于 MD5

这是最容易犯的错。S3 的 ETag：

- 单段上传时**通常**是 MD5；
- multipart 上传时是 `"<md5-of-part-md5s>-<part-count>"`，**不是**内容的 MD5；
- 使用 SSE-KMS/SSE-C 加密时也可能不是 MD5。

因此：`transfer.s3.verify_etag_as_md5` 默认 `false` 且必须保持 `false`。
若某个 `FileRecord` 的 MD5 checksum 恰好等于远端 ETag，或显式标了 `checksum_origin=remote_etag`，
完整性检查会直接判 **protocol 级 FAILED** 并说明原因——宁可判失败，也不允许把不可靠的值
当成内容校验。

正确做法：本地算 SHA-256 记录为 `local_sha256_recorded`；如果远端另有可信 SHA-256，
再与之比对得到 `remote_checksum_verified`。

## 4. 断点续传的一致性自证

续传是**唯一**可能产出"长度正确但内容错位"的操作（把新文件的后半段接到旧文件后半段上）。
因此只在三重条件同时成立时才续传：

1. 远端支持 Range，实际返回 `206`，且 `Content-Range` 的起点 == 本地 `.part` 长度；
2. `.part.sidecar` 记录的 `ETag` 或 `Last-Modified` 与当前远端一致；
3. 远端 `Content-Length` 与 sidecar 记录的总大小一致。

任一不满足 → 整文件重下（`resumed=false`）。无法确认资源身份时，可选择
"标记需要人工处理"（`PermanentTransferError`, `error_code=resume_identity_unknown`），
保留 `.part` 而不做任何猜测性拼接。

## 5. 存储提交：staging → raw 的原子性

```
staging/<task>.part            下载中（可续传）
   │ 传输完成、大小/哈希复核
   ▼
staging/<task>.part → raw/<dataset>/<file>    同文件系统：os.replace（原子）
                                               跨文件系统（EXDEV）：copy → fsync → 同目录 rename → 复核 → 删源
   │ 提交日志 <file>.commit.json（pending → committed）
   ▼
raw/（默认 chmod 0444，只读）
   │ 校验失败
   ▼
quarantine/<file>.<ts>           隔离，写 .reason.json，**不删除**
```

规则：

- **正式文件默认禁止覆盖**：目标已存在时比较 size + SHA-256；一致 → `SKIPPED_IDENTICAL`
  （不写、不删源）；不一致 → `StorageError`，要求人工处理，绝不静默覆盖；
- **提交日志**是崩溃恢复的依据（`recover_pending_commits()`），见
  [`recovery.md` §3.2](recovery.md)；
- **路径安全**：文件名经 `sanitize_file_name` 清洗，拒绝 `..`、绝对路径、控制字符、
  超长名；所有路径必须落在配置的存储根内（路径穿越防护）；
- **隔离区不删除文件**：坏文件是证据，删掉就无法查"为什么坏了"。

## 6. 交付清单（DeliveryManifest）

交付物必须自证。`DeliveryManifest` 包含：项目、请求/计划 ID、数据集科学元数据、数据源、
检索时间、文件列表（**相对路径** + 大小 + 算法 + checksum）、时间/空间/变量/垂直层覆盖、
逐文件校验状态与细节、软件版本。

- `complete = true` 只在**所有**纳入文件的校验报告为 `PASSED` 时成立（模型不变式强制）；
- 失败、跳过、隔离的文件都要在报告里出现（`excluded` / report 的 failures/quarantined），
  **不能只报成功数**；
- 相对路径是硬要求：交付清单要能复制给别人、用于论文附录、复现采集，不能绑定某台机器的
  绝对路径。

## 7. 报告

```bash
rdh file verify <path>                 # 单文件校验，输出逐项结果
rdh report generate <request_id>       # 采集报告（请求量/发现数/成功/失败/跳过/总下载量/失败原因）
rdh report show <report_id>
```

报告必须同时包含**失败**与**跳过**记录；只报成功数量的报告视为缺陷。

## 8. 反模式清单

| ❌ 反模式 | 为什么错 | ✅ 正确做法 |
|---|---|---|
| 文件存在就标完成 | 存在≠完整≠正确 | 走完整校验链，`PASSED` 才允许 `COMPLETED` |
| 把 ETag 当 MD5 校验 | multipart/加密场景完全不同 | 本地算 SHA-256；ETag 只作资源身份标识 |
| 校验失败就删除坏文件 | 丢失证据，无法复盘 | 移入 quarantine，写 `.reason.json` |
| 目标已存在就覆盖 | 破坏正式数据且不可逆 | 比较 size+hash：相同跳过，不同报错 |
| 用 `xarray.open_dataset` 成功当"数据正确" | 只证明结构合法 | 明确标注 format 层，科学结论另做 |
| 把 SKIPPED/UNKNOWN 统计进 PASSED | 虚假交付 | 四态严格区分 |
| 跨文件系统假设 rename 原子 | EXDEV 下 rename 失败或行为不同 | copy+fsync+rename+复核，写提交日志 |
| 断点续传只判断"文件长度较小" | 内容可能已错位 | 三重自证（Range/Content-Range/ETag 一致性） |

## 9. 相关文档

- [架构](architecture.md)、[任务恢复](recovery.md)、[配置](configuration.md)
- 契约：[`data-contract.md` §6/§7/§9](data-contract.md)
- 源码：[`integrity/checker.py`](../src/researchdatahub/integrity/checker.py)、
  [`storage/local.py`](../src/researchdatahub/storage/local.py)、
  [`reporting/delivery.py`](../src/researchdatahub/reporting/delivery.py)
