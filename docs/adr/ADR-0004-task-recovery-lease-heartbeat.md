# ADR-0004：任务恢复策略 —— 租约（lease）+ 心跳（heartbeat）双时间线

| 项 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-11-04 |
| 决策者 | 架构负责人（Lead） |
| 影响范围 | `core.state`、`core.models.AcquisitionTask`、`manifest` 的领取/续约/回收、`scheduler` 的 worker 循环 |

---

## Context

M0 只有一条时间线：`is_stale(task, timeout_seconds=3600)`，参考时间取
`last_transition_at`，其次 `started_at`。它回答了"这个任务是不是卡住了"，但**无法回答两个
必须回答的问题**：

1. **"我还能继续执行这个任务吗？"** —— 两个 worker 同时领到同一个任务时，谁有权继续？
2. **"这个任务该由我接管吗？"** —— 进程崩溃重启后，怎么区分"真死了"和"只是慢"？

一条时间线同时满足这两件事在物理上矛盾：

* 若把阈值调小以快速接管崩溃任务 → 慢任务（大文件、CDN 抖动、服务端排队 40 分钟）会被
  误判为崩溃，被第二个 worker 抢走，**同一文件被并发写两遍**，甚至两个 worker 各自 rename
  到同一个目标路径。
* 若把阈值调大以避免误判 → 崩溃任务要等 1 小时才恢复，违反"任务状态可恢复"的基本要求。

单时间线还有一个更隐蔽的问题：`last_transition_at` 只在**状态变化**时更新。
一个 `RUNNING` 状态可能持续数小时（合法地），期间没有任何转换——
所以它根本不携带"进程还活着"的信息。

---

## Decision

**引入两条独立时间线：短周期租约（lease）用于抢占保护，长周期过期判定（stale）用于崩溃恢复。**

### 1. 数据契约（`core.models.AcquisitionTask`）

新增三个字段（均有默认值 `None`，属于契约 1.x 兼容扩展）：

| 字段 | 语义 |
|---|---|
| `worker_id` | **租约持有者**。SQLite 列名映射为 `tasks.lease_owner`，不额外增加同义字段。 |
| `heartbeat_at` | 持有者最近一次自证存活的时间 |
| `lease_expires_at` | 租约到期时间；`None` 表示无人持有 |

`worker_id` 与 `tasks.lease_owner` 是同一语义的两种表述（Python 侧沿用 M0 已有名字，
避免出现两个字段各写一半），这一点在 `docs/data-contract.md` 中明确记录。

### 2. 时间线常量（`core.state`）

```python
DEFAULT_LEASE_SECONDS = 900        # 15 分钟：抢占保护周期
DEFAULT_STALE_TIMEOUT_SECONDS = 3600  # 1 小时：崩溃判定周期
```

约束：`heartbeat_interval * 3 <= lease_seconds < stale_timeout_seconds`。
配置模型 `config.models.SchedulerConfig` / `IntegrityConfig` 在加载期就强制该关系，
不靠运行期"小心一点"。

### 3. 三个纯函数，职责互斥

| 函数 | 回答的问题 | 判据 | 谁能用 |
|---|---|---|---|
| `transition(..., lease_seconds=)` | 进入 `RUNNING` 时签发租约 | 写入 `heartbeat_at`/`lease_expires_at`；`attempt += 1` | 任何推进状态的调用方 |
| `lease_expired(task, now, grace_seconds)` | 我是否已失去执行权 | `now >= lease_expires_at + grace` | worker 自己（每个写操作前）/ 领取逻辑 |
| `renew_lease(task, now, lease_seconds)` | 续约 | 仅 `RUNNING`/`VERIFYING` 可续 | 心跳线程、长 I/O 之前 |
| `is_stale(task, now, timeout_seconds)` | 进程是否可能已死 | 参考时间优先 `heartbeat_at` → `last_transition_at` → `started_at` | **仅**崩溃恢复流程 |

关键改动：`is_stale` 的参考时间**优先取心跳**。只看 `last_transition_at` 会把一切长任务
误判为崩溃（见 Context）。

### 4. 领取（claim）必须是数据库层的原子操作

应用层"先查再改"一定存在 TOCTOU 窗口。因此领取下沉到 SQL：

```sql
UPDATE tasks SET state='RUNNING', lease_owner=?, heartbeat_at=?, lease_expires_at=?,
                 attempt=attempt+1, started_at=?, last_transition_at=?, updated_at=?
WHERE id IN (SELECT id FROM tasks
             WHERE state='QUEUED'
                OR (state='RETRY_WAIT' AND scheduled_at <= ?)
             ORDER BY priority DESC, scheduled_at ASC, created_at ASC
             LIMIT ?)
  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
RETURNING *;
```

单条语句 + `BEGIN IMMEDIATE`，由 SQLite 的写锁保证互斥；不依赖 Python 侧锁。
`claim_tasks` 返回的任务已经是持久化后的 `RUNNING` 快照，调用方不再二次写状态。

### 5. 回收（release_expired）必须区分两种过期

| 观察 | 判定 | 动作 |
|---|---|---|
| `lease_expired` 且 `now < heartbeat_at + stale_timeout` | 慢任务被误抢（原 worker 可能还活着） | **续租给原持有者**，记录 `lease_renewed` 事件，不接管 |
| `heartbeat_at + stale_timeout <= now` | 进程已死 | 按 `state.recover_stale` 处理：`VERIFYING → RETRY_WAIT`；`RUNNING` 有预算 → `RETRY_WAIT`，无预算 → `FAILED` |
| 终态 / 非在途 | 不在回收范围 | 跳过 |

每次回收写一条 `RecoveryResult` 并追加 `transfer_logs` 事件，使"任务为什么被重新排队"
在事后可审计，而不是黑盒。

### 6. 终态必须释放租约

`transition()` 在进入任何终态时把 `lease_expires_at` 置 `None`。否则恢复流程会把
已完成的任务当成"仍被持有"，永远无法清理。

---

## Consequences

**好处**

* 崩溃恢复的等待时间由 `stale_timeout` 决定（默认 1 小时），而抢占保护的粒度是
  `lease_seconds`（15 分钟）——两者解耦后各自可调，不再互相牵制。
* "同一任务被两个 worker 执行"从"希望不会发生"变成"数据库层不可能发生"。
* 恢复动作有结构化记录，可回答"这个任务为什么重跑了 3 次"。
* 手动 `rdh task retry` 与自动恢复走同一条路径（`recover_stale` / `transition`），
  不存在第二套补丁式逻辑。

**代价**

* `scheduler` 必须有独立心跳线程；没有心跳的长任务会被误判（这是刻意选择：宁可
  误判"慢"也不允许"并发写同一文件"）。
* `heartbeat_interval_seconds` 与 `lease_seconds` 的比值成为运维参数，配错会导致抖动；
  因此配置模型在加载期强制 `heartbeat * 3 <= lease < stale`。
* 单机 SQLite 语义下租约只能防"同库多进程/多线程"，不防"两个数据库文件"（例如用户把
  `~/.rdh` 复制到外置盘再跑一遍）。后者属于部署问题，不在本 ADR 范围。

---

## Alternatives considered

**A. 只增大 `stale_timeout`，不做租约**：无法阻止两个 worker 同时执行；`rdh task run` 与
`rdh task run` 并行（用户很容易这么做）会立刻双写。❌

**B. 用文件锁（`flock`）代替数据库租约**：跨进程可行，但锁的生命周期与进程绑定，
进程崩溃后由 OS 释放——看起来更"自动"。问题是：锁不进入事实来源（ADR-0003），
崩溃恢复时无法从 Manifest 判断"上次是谁在写、写到哪"，且 NFS/外置 SSD 上的 flock
行为不可靠。❌

**C. 单写者队列 + 内存任务表**：状态会随进程消失，违反"进程退出后能从 Manifest 恢复"。❌

**D. `attempt` 作为唯一幂等依据**：`attempt` 只涨不减，无法表达"任务被谁持有"，
也无法在租约到期后安全接管。❌

---

## 验证方式

* `tests/unit/test_lease_recovery.py`：租约签发/边界/宽限/续约/终态释放、
  `is_stale` 优先心跳、两条时间线的序关系、字段 JSON 往返、配置与常量一致性。
* `tests/unit/test_manifest/`（M1）：真实多线程并发 `claim_tasks` 不重复领取；
  `release_expired` 区分"续租"与"接管"；重启后从库恢复。
* `tests/unit/test_scheduler/`（M4）：心跳线程在长传输期间保持租约；进程被杀后
  `release_expired` 能把任务重新排队。

## 相关文档

* [ADR-0003](ADR-0003-sqlite-manifest-source-of-truth.md)：为什么 Manifest 是事实来源
* [`core/state.py`](../../src/researchdatahub/core/state.py)：`lease_expired` / `renew_lease` / `is_stale`
* [`core/models.py`](../../src/researchdatahub/core/models.py)：`AcquisitionTask` 租约字段、`RecoveryResult`
* [`core/ports.py`](../../src/researchdatahub/core/ports.py)：`ManifestRepository.claim_tasks` / `heartbeat` / `release_expired`
