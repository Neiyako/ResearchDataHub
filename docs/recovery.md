# 任务恢复（Recovery）

本文回答一个问题：**进程在任何时刻被杀掉，重启后系统如何知道哪些任务该重做、哪些该接管、
哪些不能碰？**

原则：**恢复不是"把所有 RUNNING 重置为 PENDING"**。那样做的后果是：一个已经下载了 4.8 GB
的任务被从头再下一遍，而磁盘上还留着半个文件。恢复必须基于**事实**（Manifest 中的状态 +
租约 + 心跳 + 文件系统证据），而不是基于"看起来像没跑完"。

实现依据：[ADR-0004](adr/ADR-0004-task-recovery-lease-heartbeat.md)（双时间线）、
[ADR-0003](adr/ADR-0003-sqlite-manifest-source-of-truth.md)（Manifest 是事实来源）。

---

## 1. 两条时间线

| 时间线 | 常量（默认） | 回答的问题 | 判据函数 |
|---|---|---|---|
| 租约 `lease` | 900 s | "我是否还拥有这个任务的执行权？" | `core.state.lease_expired` |
| 心跳 `heartbeat` | 60 s 间隔 | "持有者还活着吗？" | — |
| 过期 `stale` | 3600 s | "持有它的进程是不是死了？" | `core.state.is_stale` |

约束（配置加载期强制，不是运行期自觉）：

```
heartbeat_interval_seconds * 3 <= lease_seconds < integrity.stale_timeout_seconds
```

- 租约必须**远短于**过期阈值，否则慢任务会被误判崩溃并被第二个 worker 抢走；
- 心跳必须**远短于**租约，否则活着的 worker 会在两次心跳之间失去租约；
- 三个参数都在 `configs/defaults.yaml` 的 `defaults.scheduler` / `defaults.integrity` 中，
  配错直接启动失败。

## 2. 进入与退出在途状态时发生了什么

```
QUEUED ──claim_tasks()──► RUNNING
                            │  heartbeat_at      = now
                            │  lease_expires_at  = now + lease_seconds
                            │  worker_id         = <claim 方>
                            │  attempt           += 1
                            │  + transfer_logs 事件
                            ▼
                    周期性 heartbeat()
                            │  心跳线程每 60 s 续约；返回 False 表示已失去租约
                            ▼
                  终态（COMPLETED/FAILED/CANCELLED/SKIPPED/QUARANTINED）
                            │  lease_expires_at = NULL（必须释放）
                            ▼
```

终态不释放租约 → 恢复流程会把已完成任务当成"仍被持有"，永远无法清理。因此
`core.state.transition()` 在进入任何终态时把 `lease_expires_at` 置 `None`。

## 3. 启动时的恢复流程

进程启动（`rdh task run` 或任何会持有任务的动作）时执行：

```python
repo.release_expired()      # 1. 回收过期在途任务
storage.recover_pending_commits()  # 2. 对账"文件已提交但数据库未更新"
```

### 3.1 `release_expired()` 的三分支决策

对每个 `RUNNING` / `VERIFYING` 任务（在 `BEGIN IMMEDIATE` 事务内处理）：

| 观察 | 判定 | 动作 | 审计 |
|---|---|---|---|
| 租约未过期 | 正常在途 | 不动 | — |
| 租约过期，但 `heartbeat_at + stale_timeout` 未到 | **慢任务**，持有者可能还活着 | **续租给原持有者**，不接管 | `RecoveryResult(previous==new, reason="lease expired but heartbeat fresh")` |
| `heartbeat_at + stale_timeout` 已到 | **进程已死** | 按状态机接管（见下） | `RecoveryResult` + `transfer_logs` 事件 |

接管时的目标状态由 `core.state.recover_stale()` 决定：

| 当前状态 | 重试预算 | 目标状态 | 说明 |
|---|---|---|---|
| `VERIFYING` | — | `RETRY_WAIT` | 传输可能已完成，重做校验即可 |
| `RUNNING` | `attempt < max_attempts` | `RETRY_WAIT` | 重新排队，等待退避到期 |
| `RUNNING` | 预算耗尽 | `FAILED` | 不再无限重试 |

为什么"租约过期但心跳新鲜"要续租而不是接管：租约过期可能只是 worker 在长时间
`fsync`/校验中没来得及续约；此时接管会让**同一文件被两个进程同时写**，比"晚 15 分钟
才恢复"危险得多。

### 3.2 文件系统对账（M5）

Manifest 说"提交完成"但文件不在、或者文件已经在 `raw/` 但数据库还没记——这两种不一致
由 M5 的提交日志处理。`LocalStorageBackend.recover_pending_commits()` 的决策表：

| 正式文件存在 | 源文件（staging）存在 | 动作 |
|---|---|---|
| 是，大小/哈希一致 | 任意 | 清理源文件 → `FINALIZED` |
| 是，但内容不一致 | 任意 | `NEEDS_ATTENTION`（**绝不删除**正式文件） |
| 否 | 是 | 重做移动 → `RETRIED` |
| 否 | 否 | `ABORTED`（没有数据可救） |

已标记 `COMMITTED` 但正式文件缺失 → `NEEDS_ATTENTION`：说明文件在提交后被外部删除，
必须人工介入，不能静默重建（那会掩盖真实的数据丢失）。

`storage.scan_orphans(known_raw_paths=...)` 提供只读体检：`raw` 孤儿、待提交目标、
`staging` 孤儿、临时文件、`.rdh-partial-*` 残留。

### 3.3 传输层的续传（M2）

任务被重新排队后，`staging` 里可能还留着 `.part` 文件。是否续传由传输层判断，
**满足全部三个条件才续传**：

1. 远端支持 Range，且实际返回 `206` 且 `Content-Range` 起点等于本地已有长度；
2. 本地 `.part.sidecar` 记录的 `ETag` 或 `Last-Modified` 与当前远端**一致**；
3. 远端大小与 sidecar 记录一致。

任一不满足 → 整文件重下，`TransferResult.resumed = false`。绝不把"追加到已有文件末尾"
当作续传——那会产出一个长度正确但内容错位的文件，而校验和会诚实地告诉你是坏的，
代价是一次无用的 GB 级重下。

## 4. 状态机是唯一入口

所有状态变化必须经过 `core.state.transition()`（`manifest` 层会拒绝绕过状态机的直接写入，
测试 `test_save_task_rejects_illegal_state_bypassing_state_machine` 用真实 SQLite 验证）。
恢复路径不是"另一套逻辑"，它复用同一状态机：

```
release_expired → recover_stale(task) → transition(task, RETRY_WAIT/FAILED, reason="...")
```

因此手动 `rdh task retry` 与自动恢复的行为一致，不存在"自动恢复能走通、手动重试走不通"
的分叉。

## 5. 崩溃窗口清单

| 崩溃时刻 | 遗留现象 | 恢复动作 |
|---|---|---|
| 下载中 | `staging/*.part` + sidecar | 任务被重新排队；下次传输按三重自证决定续传或重下 |
| 下载完、校验前 | `staging` 完整文件、任务 `VERIFYING` | 心跳超时后 → `RETRY_WAIT` → 重做校验（不重新下载） |
| 校验中 | `VERIFYING` | 同上 |
| 文件已移到 `raw`、DB 未更新 | `raw` 有文件、`*.commit.json` 为 `pending` | `recover_pending_commits()` 校验后 `FINALIZED` 并补记 `FileRecord` |
| DB 已更新、文件提交失败 | 任务 `COMPLETED` 但 `raw` 无文件 | 不得静默重建；`db status`/报告暴露不一致，人工核查 |
| 两个进程同时启动 | 同一任务被两个 worker 想执行 | `claim_tasks` 的 SQL 级原子性 + 租约，只有一个成功 |

最后一行是这套机制存在的主要理由：**并发不是假设，而是必须被数据库层物理阻止的事情**。

## 6. 运维命令

```bash
rdh db status                 # 各状态任务数、校验失败数、可交付请求
rdh task status               # 任务列表与当前状态（M4）
rdh task retry <task_id>      # 手动重试（受状态机与重试预算约束）
rdh task pause/resume/cancel  # 人工干预，全部走同一状态机
```

恢复动作都会在 `transfer_logs` 留下事件、在 `RecoveryResult` 留下结构化记录，
因此"这个任务为什么重跑了三次"有据可查，而不是靠猜。

## 7. 已知限制

- SQLite 租约只能防**同一数据库文件**上的并发；若用户把 `~/.rdh` 复制到另一块盘同时运行，
  两个库互不可见（属于部署问题，不在 ADR-0004 范围内）。
- 未实现"多机分布式调度"：本项目定位是单机 local-first。
- 外部进程删除 `raw/` 文件无法被阻止，只能被检测（`NEEDS_ATTENTION`）。

## 8. 相关文档

- [ADR-0004 租约与心跳](adr/ADR-0004-task-recovery-lease-heartbeat.md)
- [数据完整性](data-integrity.md)
- [架构](architecture.md)
- [配置](configuration.md)
- 源码：[`core/state.py`](../src/researchdatahub/core/state.py)、
  [`manifest/repository.py`](../src/researchdatahub/manifest/repository.py)、
  [`storage/local.py`](../src/researchdatahub/storage/local.py)、
  [`transfer/base.py`](../src/researchdatahub/transfer/base.py)
