# ADR-0005：Staging / Raw / Quarantine 存储布局与原子提交

| 项 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-11-04 |
| 决策者 | 架构负责人（Lead） |
| 影响范围 | `config.models.StorageConfig`、`storage` 模块、`scheduler` 编排、交付路径 |

---

## Context

下载一个 5 GB 的再分析文件，可能在任何时刻被打断：网断了、磁盘满了、进程被 Ctrl-C、
机器休眠。系统必须保证：

1. **正式数据区里永远不出现半个文件**——半个文件比没有文件更危险，因为它看起来像数据；
2. **失败的文件不被删除**——它是"为什么失败"的证据，删掉就无法复盘；
3. **已交付的文件不被覆盖**——这是科研可复现性的底线；
4. **崩溃后能判断"文件到底提交成功没有"**——文件系统本身不提供这个答案。

同时，用户的数据可能放在**外置 SSD** 上，而 `staging` 与 `raw` 可能**不在同一个文件系统**
（例如 `staging` 在本地盘、`raw` 在外置盘）。`os.rename` 跨挂载点会抛 `EXDEV`，
而"`rename` 是原子的"这个常用假设在这里不成立。

---

## Decision

**用四个物理隔离的目录表达文件的生命周期状态，提交动作以"提交日志 + 原子替换"实现可恢复性。**

### 1. 目录布局（全部由配置决定，不硬编码）

```
<storage.root>/
├── staging/      下载中与待校验（*.part、*.part.json sidecar）
├── raw/          正式数据区（chmod 0444；同名文件默认禁止覆盖）
│   └── <dataset>/<file>
├── quarantine/   校验失败/不一致的文件（保留，写 .reason.json）
├── temp/         进程级临时文件（如校验时的中间拷贝）
├── archive/      打包/归档产物（不属于交付数据本身）
└── manifest.db   SQLite 事实来源（ADR-0003）
```

配置字段：`storage.root` + `raw_dir` / `staging_dir` / `quarantine_dir` / `temp_dir` /
`archive_dir`。约束（配置模型加载期强制）：

* 全部为**相对**路径，不得以 `/`、`~`、`env:` 开头；
* 五个子目录**必须互不相同**——若 `quarantine_dir == raw_dir`，隔离一个坏文件可能覆盖正式数据；
* `raw/` 下的 `*.commit.json` 是提交日志，**不是交付数据**；扫描交付文件时必须排除。

### 2. 文件生命周期

```
下载中        staging/<task>.part + .part.json(etag/last_modified/total/sha256 进度)
   │ 大小校验 + 哈希计算（integrity 层）
   ├─ 失败 → quarantine/<file>.<ts> + <file>.reason.json（原文件保留）
   ▼
提交          raw/<dataset>/<file>
```

### 3. 原子提交的两种情形

**同文件系统**：

```
os.replace(staging_part, raw_target)     # 原子
```

崩溃只可能发生在 `replace` 之前（目标不存在）或之后（目标完整）。没有"半个目标文件"。

**跨文件系统（`OSError(errno=EXDEV)`）**：

```
copy(staging_part → raw/<file>.rdh-partial-<rand>)   # 同目录内
fsync(临时文件)
os.replace(临时文件, raw_target)                     # 同目录内，仍是原子
复核：size + sha256 与源一致
删除 staging 源
```

关键点：**先把数据搬进目标文件系统、再在目标目录内做 `replace`**，所以"跨文件系统"只影响
拷贝这一步，最终可见性仍然是原子的。若拷贝中途失败，只会留下 `.rdh-partial-*` 垃圾文件，
正式路径仍然不存在，由 `scan_orphans()` 识别清理。

### 4. 提交日志（可恢复性）

`raw/<file>.commit.json`，`O_EXCL` 创建（同时充当**同一目标**的串行锁）：

| 字段 | 说明 |
|---|---|
| `status` | `pending` → `committed` / `aborted` / `needs_attention` |
| `source_path` | staging 源路径 |
| `target_path` | 正式目标路径 |
| `size_bytes` / `sha256` | 提交前记录的期望值 |
| `task_id` / `created_at` / `updated_at` | 审计 |

恢复决策表（`recover_pending_commits()`）：

| 目标存在 | 源存在 | 动作 |
|---|---|---|
| 是，size+hash 一致 | 任意 | 清理源 → `FINALIZED` |
| 是，但内容不一致 | 任意 | `NEEDS_ATTENTION`（**绝不删除目标**） |
| 否 | 是 | 重做提交 → `RETRIED` |
| 否 | 否 | `ABORTED`（无数据可救） |

已 `committed` 但目标缺失 → `NEEDS_ATTENTION`：说明文件在提交后被外部删除。**不自动重建**
——自动重建会掩盖一次真实的数据丢失事故。

### 5. 覆盖策略：默认禁止

目标已存在时：

| 情况 | 动作 |
|---|---|
| size 与 SHA-256 都相同 | `SKIPPED_IDENTICAL`：不写、不删源、**不碰目标**，任务记为跳过（不是失败） |
| 任一不同 | 抛 `StorageError`，人工处理（不同版本可能是合法的不同数据，系统无权替用户决定） |

绝不静默覆盖。用户在 `raw/` 里的任何文件都视为**用户资产**，不是本系统的缓存。

### 6. 路径安全

* 文件名经 `sanitize_file_name` 清洗：拒绝 `/`、`..`、控制字符、保留名、超长；
* 所有解析后的路径必须 `Path.is_relative_to(root)`，否则抛 `StorageError`（路径穿越）；
* 目标为符号链接时拒绝操作（`NEEDS_ATTENTION`），避免把写入导向未知位置。

---

## Consequences

**好处**

* "正式区里不会出现半个文件"成为结构性质，不依赖每个调用方自觉；
* 崩溃恢复有确定性决策表，不靠"看着像没完成就重做"；
* 失败文件保留，事后能查"为什么坏"；
* 跨文件系统场景被显式处理，外置 SSD 用户不会遇到难以解释的提交失败。

**代价**

* 每份文件多一个 `.commit.json`（交付/统计时必须排除，`scan_orphans` 已处理）；
* 跨文件系统提交要多一次完整拷贝（无法避免：`rename` 不能跨挂载点）；
* 同名不同内容的文件需要人工介入——这是刻意的，不提供 `--overwrite` 开关。

---

## Alternatives considered

**A. 直接下载到 `raw/`，失败再删**：失败清理逻辑崩溃时会在正式区留下坏文件；且"下载中"的
文件会被后续扫描误认为已交付数据。❌

**B. 只用 `.part` 后缀 + rename，不做提交日志**：崩溃点在 `rename` 与"数据库更新"之间时，
无法判断目标文件是本次提交产生的还是上一次遗留的。❌

**C. 用数据库事务包住文件移动**：文件系统操作不在 SQLite 事务里，两者无法真正原子；
伪装成原子只会让恢复逻辑更不可靠。❌

**D. `shutil.move` 一把梭**：`move` 跨设备时内部 copy+unlink，没有 fsync 与复核，
崩溃可能丢失数据；且失败后源文件可能已被部分删除。❌

**E. 允许覆盖（加 `--force`）**：科研数据被静默替换是不可接受的；需要替换时应由用户先显式
移走旧文件，或在新的 request/plan 下产生新路径。❌

---

## 验证方式

* `tests/unit/test_storage/test_atomic_commit.py`：同文件系统 `os.replace`、跨设备 copy+fsync+rename+复核、
  拷贝失败留下可恢复状态、内容篡改被检出。
* `tests/unit/test_storage/test_publish.py`：目标已存在且相同 → `SKIPPED_IDENTICAL` 且不触碰目标；
  不同 → 拒绝覆盖。
* `tests/unit/test_storage/test_recovery.py`：决策表四个分支 + 损坏日志 + 已 committed 但目标缺失。
* `tests/unit/test_storage/test_path_safety.py`：路径穿越、非法文件名、符号链接。
* `tests/unit/test_storage/test_quarantine_archive.py`：隔离保留文件并写 reason。

## 相关文档

* [ADR-0003](ADR-0003-sqlite-manifest-source-of-truth.md)：Manifest 是事实来源
* [ADR-0004](ADR-0004-task-recovery-lease-heartbeat.md)：任务恢复
* [数据完整性](../data-integrity.md)、[任务恢复](../recovery.md)、[配置](../configuration.md)
* 源码：[`storage/local.py`](../../src/researchdatahub/storage/local.py)、[`storage/naming.py`](../../src/researchdatahub/storage/naming.py)
