# ADR-0003：SQLite Manifest 作为事实来源（Source of Truth）

| 项 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-11-04 |
| 决策者 | 架构负责人（arch） |
| 影响范围 | `manifest` 模块、`ManifestRepository` 协议、并发模型、崩溃恢复 |

---

## Context

系统需要回答一个看似简单的问题：**"这次采集完成了吗？"**

有两条路：

1. **扫描文件系统**：看看 `data/era5-pressure-levels/` 下有没有文件、大小对不对；
2. **查状态库**：查 SQLite 里记录的、带校验证据的任务状态。

直觉上第 1 条更"真实"——文件就在那儿。但在科研数据场景里，这个直觉是错的。

### 为什么文件系统不是事实来源

**（a）存在 ≠ 完整**。一个 5 GB 的文件下到 2.3 GB 时进程被杀，文件仍然"存在"。
扫描目录会看到它，但它是废的。

**（b）存在 ≠ 正确**。文件大小对，但内容是 HTML 错误页（某些 CDN 在 403 时返回 200 + 网页）。
只有校验和能发现。

**（c）存在 ≠ 我们需要的那份**。同一个目录下可能有 `era5_clwc_2020.grib` 和
`era5_clwc_2020_v2.grib`。哪一个对应哪次请求？哪些是同一个科学数据集的不同版本？
文件系统不带这些信息。

**（d）不存在 ≠ 没有**。文件被用户手工删了、被移到归档盘了，但数据其实已经采集过、
校验过、记录在案。扫描会得出"没采过"，导致重复下载几百 GB。

**（e）元数据无处安放**。`origin_uri`、`retrieved_at`、`source_id`、`etag`、
`IntegrityReport` 的逐项结果——这些都不是文件属性（xattr 不可靠且不可移植）。
它们必须存在某个地方，而且必须能事务性地更新。

**（f）状态推进一步，审计要同步一步**。`RUNNING → VERIFYING → COMPLETED` 每一跳都要留下
时间戳、原因、worker_id。这些是**关于过程的事实**，文件系统完全不记录。

### 并发压力

一旦用线程池并行传输，多个 worker 会同时想推进状态。SQLite 是一个**单文件、单写者**的数据库。
天真的做法（每个线程一个连接、各自 `commit`）会遇到 `database is locked`；
更糟的做法（共享一个连接）会遇到游标状态错乱。

---

## Decision

**SQLite 中的 Manifest 是唯一的事实来源。它记录"我们确认完成了什么"，而不是"磁盘上有什么"。**

具体规则：

### 1. 状态推进必须落库，且带审计

每次 `transition()` 产生的 `notes`（[`state.py:284`](../../src/researchdatahub/core/state.py#L284)，格式
`"<ISO8601> FROM->TO: reason"`）、`last_transition_at`、`attempt`、`worker_id`、`last_error`
都随任务一起持久化。没有落库的状态推进视为未发生。

### 2. 完成需要证据链

`COMPLETED` 只能从 `VERIFYING` 到达（`state.py:132`，且 `state.py:260` 二次强制），
且 `IntegrityReport.status` 必须是 `PASSED`（`models.py:635` 的 `permits_completion`）。

**"文件存在"不是证据，"校验通过"才是证据。** 校验报告本身也落库。

### 3. 交付清单从库生成，不从目录生成

`ManifestRepository.build_delivery_manifest(request_id)`（`ports.py:392`）的语义是：
**从已完成的文件记录**生成 `DeliveryManifest`，且若存在未通过的报告则抛 `ManifestError`。

绝不"扫描目录然后拼一个清单"。

### 4. 并发写入：每个 worker 独立连接 + 单写者

`ports.py:342-343` 定下两条硬性规则：

> SQLite 连接不可跨线程共享。每个 worker 持有自己的连接；
> 写事务必须串行化（单写者队列或 `BEGIN IMMEDIATE` + 重试）。

**采用"每个 worker 独立连接 + 单写者队列"：**

```
worker-1 ──┐
worker-2 ──┤   投递 (save_task, event) ──►  writer thread（唯一持有写连接）
worker-3 ──┤                                     │
worker-4 ──┘                                     ▼
                                            BEGIN IMMEDIATE
                                            UPDATE task ...
                                            INSERT event ...
                                            COMMIT
```

- **读**：每个 worker 用只读连接（`mode=ro` 或独立连接），可并发；
- **写**：只有一个线程持有可写连接，队列串行处理；
- **优点**：绝不会 `database is locked`，且写入天然满足"状态与事件同事务"（`ports.py:347`）。

### 5. 乐观并发控制

`save_task(task, *, expected_state=...)`（`ports.py:354`）：

```sql
UPDATE tasks SET state = ?, last_transition_at = ?, ...
WHERE task_id = ? AND state = ?     -- expected_state
```

若受影响行数为 0，说明状态已被别的 worker 改掉 → 抛 `ManifestError`。
这防止两个 worker 同时把同一任务推进到不同终态。

### 6. WAL 模式

启用 `PRAGMA journal_mode=WAL`：读不阻塞写、写不阻塞读。这是"每 worker 独立读连接"
能高效工作的前提。

### 7. 文件系统仍是**数据**的家，只是不是**事实**的家

原始区里的文件是数据本体，SQLite 记录的是**关于这些文件的事实**（路径、大小、校验和、来源）。
两者通过 `FileRecord` 关联：`FileRecord.local_path` + `FileRecord.checksum`。

---

## Consequences

### 正面

1. **完成的判定有证据链**：`COMPLETED` 一定伴随一份 `PASSED` 的 `IntegrityReport` 和一个 `FileRecord`。
   任何人复核时都能追溯"凭什么说它完成了"。
2. **不会重复下载**：查库就知道某请求下的文件是否已校验通过，不需要扫描、不需要猜测。
3. **崩溃后可精确恢复**：`is_stale()` 依赖 `last_transition_at`——这个字段只有落库才有意义。
   扫描目录无法回答"这个任务是从什么时候开始 RUNNING 的"。
4. **并发安全且简单**：单写者队列把并发问题收敛到一个点，其余代码可以当单线程写。
5. **交付清单自证**：`DeliveryManifest` 由库生成，能保证 `complete=True` 时每一份文件都有
   `PASSED` 报告（`models.py:666` 的构造期校验）。
6. **零运维**：SQLite 是文件，备份就是复制。没有服务要起。

### 负面（如实记录）

1. **写入串行化限制扩展性**：理论写入上限是所有写操作排队。
   - 实测预期不构成瓶颈：每个任务约 4–6 次写（`QUEUED→RUNNING`、`RUNNING→VERIFYING`、
   文件登记、`VERIFYING→COMPLETED`），而单任务传输耗时以分钟计。
   - 若未来写入 QPS 持续超过数百，需重新评估（见"何时重估"）。
2. **SQLite 不适合网络文件系统**：把 `storage.root` 放在 NFS 上会破坏锁语义。
   - 缓解：文档明确要求本地卷；M1 的 storage 初始化应检测并警告。
3. **多进程共享需要额外设计**：如果同时跑两个 `rdh` 进程指向同一个库，单写者队列管不到跨进程。
   - 缓解：WAL + `BEGIN IMMEDIATE` 能保证单条写事务不冲突；重试逻辑需覆盖 `SQLITE_BUSY`。
     当前**不支持**多进程并发写，M0 未实现。
4. **库和文件可能不一致**：用户手工删了文件，库里仍记着 `COMPLETED`。
   - 缓解：这是**可检测的**不一致——报告阶段可以校验 `FileRecord.local_path` 是否存在并要求重校验。
     而"扫描目录"方案连这种检测都不存在（它无法知道"这个文件原本对应哪个请求"）。
5. **多一份状态要同步**：`FileRecord.local_path` 与磁盘实际位置必须一致。
   - 缓解：`publish_original` 返回最终路径，由 storage 与 manifest 在同一次事务前后保证。

---

## Alternatives considered

### 方案 A：扫描文件系统判断完成

**为什么拒绝**：见 Context（a）–（f）。核心是：**文件系统不携带"校验通过"这个事实**。
一个 2.3 GB 的截断文件和 5 GB 的完整文件在目录列表里同样"存在"。
用目录扫描做完成判定，等于把"数据是否正确"这个最重要的判断交给最不可靠的信号。

### 方案 B：JSON 文件记录状态（一个任务一个 JSON）

**为什么拒绝**：

- 没有事务：状态文件写了一半就崩，会得到损坏的 JSON；
- 没有原子性跨文件：任务状态、事件日志、文件记录是三个文件，无法一起提交；
- 并发写同一个 JSON 需要外部锁，等价于自己实现一个更差的数据库；
- 查询能力弱：`list_tasks(state=FAILED)` 要遍历所有文件。

### 方案 C：PostgreSQL

**为什么拒绝**：见 [ADR-0001](ADR-0001-modular-monolith.md)。对"个人工作站跑一次采集"的场景，
强制部署数据库服务器是过高的门槛，而负载远未到需要它。`ManifestRepository` 是 Protocol，
未来可加 PostgreSQL 实现而不改上层。

### 方案 D：内嵌键值库（如 LevelDB / RocksDB）

**为什么拒绝**：

- 没有 SQL 查询能力，`list_tasks(state=...)`、按 `request_id` 聚合都要手写索引；
- 没有成熟的事务语义与并发文档；
- 生态与可观测工具（`sqlite3` CLI、任何 DB 浏览器）都不如 SQLite；
- 对本场景的数据量（万级行）没有任何性能优势。

### 方案 E：每个 worker 共享一个 SQLite 连接（`check_same_thread=False`）

**为什么拒绝**：

- SQLite 的 Python 绑定在有未提交事务时共享连接会互相干扰；
- 游标状态在并发调用下未定义；
- 即使不崩溃，也会出现"某 worker 的 `save_task` 落到了另一个 worker 的事务里"这种
  极难排查的问题。
- `ports.py:342` 把这条写成了硬性禁止。

### 方案 F：不加 `expected_state`，后写覆盖先写

**为什么拒绝**：两个 worker 同时推进同一任务时，最终状态取决于提交顺序——这是
**不确定性**。科研数据系统不能接受"结果有时成功有时失败，取决于线程调度"。

---

## 何时应该重新评估

1. 需要多台机器共享同一个采集状态库；
2. 写入 QPS 成为实测瓶颈（持续高于数百次/秒）；
3. 单次采集的任务数达到数万级，SQLite 的索引与 VACUUM 成为负担。

---

## 验证方式

1. **无共享连接**：`grep -rn "check_same_thread" src/` 应当只出现在明确的单线程封装中，
   且该封装只在单写者线程使用。
2. **乐观并发有效**：`tests/fixtures/contract_fakes.py` 的 `InMemoryManifest` 已实现
   `expected_state` 冲突检测，并由
   `tests/unit/test_contracts.py::test_manifest_repository_optimistic_concurrency` 覆盖。
3. **完成必须有证据**：`DeliveryManifest` 在 `complete=True` + 非 `PASSED` 报告时构造失败
   （M0 实测确认）。

---

## 相关文档

- 数据流中的位置：[`docs/architecture.md` §5](../architecture.md)
- `ManifestRepository` 完整签名：[`docs/module-boundaries.md` §2.4](../module-boundaries.md)
- Manifest 与 DeliveryManifest 的区别：[`docs/data-contract.md` §5](../data-contract.md)
