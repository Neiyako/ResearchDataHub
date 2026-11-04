# 配置参考（Configuration）

本文是 `configs/defaults.yaml` / `configs/sources.yaml` 的**逐字段说明**，
与 `src/researchdatahub/config/models.py` 一一对应。字段名、默认值、约束都来自代码，
改动代码必须同步改本文件（测试 `tests/integration/test_config_defaults_drift.py` 会
锁住"YAML 默认值 = 代码默认值"）。

> 配置模型全部 `frozen=True` + `extra="forbid"`：多写一个未知键会**直接校验失败**，
> 不会被静默忽略。这是刻意的——拼错的 `retries:` 静默走默认值，比报错危险得多。

---

## 0. 加载链路

```
YAML 文本
   │ yaml.safe_load
   ▼
原始 dict
   │ detect_config_kind()          ← 顶层键 defaults: / sources:
   ▼
resolve_env_refs()                  ← 展开 env:VAR 与 ${VAR}；缺失立即报错并点名变量
   ▼
Pydantic 模型（frozen, extra=forbid）
   ▼
AppConfig / SourcesConfig → 注入 planner / scheduler / adapters
```

校验入口：

```bash
rdh config validate                                    # 默认 configs/defaults.yaml + configs/sources.example.yaml
rdh config validate --defaults path/to/defaults.yaml --sources path/to/sources.yaml
```

退出码：`0` 全部通过，`1` 至少一个文件失败，`2` 参数错误。

**凭证规则**：仓库内任何文件都不得出现真实密钥。凭证只允许写成 `env:VAR` 引用，
由 loader 在加载时展开；日志与错误信息只打印变量名，不打印值
（`logging.redact_env` 定义必须打码的变量）。

---

## 1. `defaults:` 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `environment` | `development` / `staging` / `production` | `development` | 仅用于日志与报告标注，不切换行为 |

### 1.1 `defaults.storage`

本地存储布局。数据流转：`staging`（下载中）→ 校验 → `raw`（正式，只读）/ `quarantine`（隔离）。

| 键 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `root` | `~/.rdh` | 具体路径，不能是孤立占位符 | 状态根：Manifest 库、数据区、日志都在其下 |
| `raw_dir` | `raw` | 相对路径 | 正式数据区，**默认禁止覆盖** |
| `staging_dir` | `staging` | 相对路径 | 下载中转区（`.part` 文件在此） |
| `quarantine_dir` | `quarantine` | 相对路径 | 校验失败文件的隔离区（不删除） |
| `temp_dir` | `temp` | 相对路径 | 进程级临时文件 |
| `archive_dir` | `archive` | 相对路径 | 打包/归档产物 |
| `free_space_floor_gb` | `20.0` | `>= 0` | 落盘前最低剩余空间；低于即停止下载 |
| `min_backend` | `local` | 仅 `local` | M1 只支持本地后端 |
| `create_dirs_on_init` | `true` | bool | `rdh db init` 时是否顺带创建缺失目录 |

五个子目录**必须互不相同**（模型校验）：若 `quarantine_dir == raw_dir`，隔离一个坏文件
可能覆盖正式数据。

`root` 三种写法：

```yaml
root: "~/.rdh"                 # 波浪号展开（默认）
root: "env:RDH_HOME"           # 环境变量引用；未设置时校验失败并点名该变量
root: "${RDH_HOME}/storage"    # 内嵌插值
```

绝对路径（如外置 SSD `/Volumes/SSD/rdh`）是允许的：本地大容量存储是合法需求。
但**提交进仓库的配置不得写死某台机器的个人路径**。

### 1.2 `defaults.concurrency`

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `workers` | `4` | 0–64，0=自动 | 传输线程数 |
| `per_source_limit` | `2` | 1–32 | 单数据源并发上限（避免触发远端限流） |
| `discovery_workers` | `2` | 0–16，0=自动 | 发现阶段并发 |

8 GB 内存设备的建议值：`workers: 2`–`4`，`per_source_limit: 2`。

### 1.3 `defaults.retry`

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `max_attempts` | `3` | 1–32 | 单任务最大尝试次数（写入 `AcquisitionTask.max_attempts`） |
| `backoff_seconds` | `5.0` | > 0 | 指数退避基数：第 n 次失败等待 `base * 2^(n-1)` |
| `backoff_max_seconds` | `300.0` | > 0 | 单次退避上限 |
| `jitter` | `0.2` | 0–1 | 均匀抖动比例：实际等待 `= 计算值 * (1 + U(-jitter, +jitter))` |
| `retry_on` | `[timeout, connection_reset, http_5xx, http_429]` | ≤16 项 | 可重试条件；不在列表中的失败直接 `FAILED` |

约束：`backoff_seconds <= backoff_max_seconds`，`retry_on` 不得重复。

**不可重试**（无论 `retry_on` 怎么写）：HTTP 401/403/404、参数错误、无效凭据、
数据契约错误、`PermanentTransportError`。这些重试一万次也不会成功，只会烧掉限流配额。

### 1.4 `defaults.transfer`

| 键 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `timeout_seconds` | `120.0` | > 0 | 单次传输总超时 |
| `connect_timeout_seconds` | `15.0` | > 0, ≤ timeout | 连接超时 |
| `read_timeout_seconds` | `60.0` | > 0, ≤ timeout | 读超时（每次 socket 读，不是总时长） |
| `chunk_size_bytes` | `1048576` | 64 KiB–32 MiB | 流式写入分块；内存占用 ≈ 该值 |
| `max_redirects` | `5` | 0–20 | HTTP 重定向上限 |
| `verify_tls` | `true` | bool | 生产环境不得关闭 |
| `resume_enabled` | `true` | bool | **允许**尝试续传，不等于一定续传（见下） |

断点续传的三重前提（缺一不可，见 `docs/data-integrity.md` §4）：

1. 远端声明支持 Range 且实际返回 `206` + 合法 `Content-Range`；
2. 本地部分文件的 sidecar 记录了与当前远端一致的 `ETag` 或 `Last-Modified`；
3. 远端文件大小与 sidecar 记录的一致。

任一不满足 → **整文件重下**，`TransferResult.resumed = false`。
绝不会出现"简单追加到已有文件末尾"。

#### `defaults.transfer.s3`

| 键 | 默认 | 说明 |
|---|---|---|
| `endpoint_url` | `null` | 空=AWS 默认；测试用 MinIO 时填 `http://127.0.0.1:9000` |
| `region_name` | `null` | AWS region；MinIO 可留空 |
| `addressing_style` | `auto` | `auto` / `virtual` / `path` |
| `part_size_mb` | `64` | 分块下载块大小（5–1024 MiB） |
| `use_ssl` | `true` | 是否用 HTTPS |
| `max_pool_connections` | `8` | 连接池大小 |
| `anonymous` | `false` | 匿名访问公开桶 |
| `verify_etag_as_md5` | `false` | **默认 false 且必须保持 false**：S3 ETag 不等于 MD5（multipart 上传、SSE 加密场景完全不同） |

### 1.5 `defaults.manifest`

| 键 | 默认 | 说明 |
|---|---|---|
| `db_filename` | `manifest.db` | 位于 `storage.root` 下的纯文件名（不允许带路径） |
| `journal_mode` | `wal` | **固定 WAL**，不接受其它值（并发读写的硬前提） |
| `synchronous` | `normal` | `full` 更安全更慢；`normal` 在 WAL 下已能保证一致性 |
| `busy_timeout_ms` | `5000` | 写锁等待上限；超时抛 `ManifestError` |
| `write_retry_attempts` | `5` | `BEGIN IMMEDIATE` 冲突后的重试次数 |

### 1.6 `defaults.planner`

| 键 | 默认 | 说明 |
|---|---|---|
| `preferred_split` | `auto` | 拆分粒度**建议**：`auto` / `file` / `year` / `month` / `request`。数据源能力优先于该建议 |
| `max_tasks_per_plan` | `500` | 单计划任务数上限，防止一个需求被拆成数万个小任务 |
| `allow_partial` | `true` | 部分变量无法满足时是否仍生成计划（未满足变量必须登记进计划） |

### 1.7 `defaults.integrity`

| 键 | 默认 | 说明 |
|---|---|---|
| `default_checks` | `[size_match, checksum_match, netcdf_openable]` | 默认校验链；顺序即执行顺序 |
| `stale_timeout_seconds` | `3600` | 在途任务超过该时长无心跳视为崩溃遗留（≥60） |
| `fail_fast` | `false` | 首个检查失败即终止后续检查 |

`default_checks` 可选项：`size_match` / `checksum_match` / `format_sniff` /
`netcdf_openable` / `variable_present` / `time_coverage` / `metadata_consistent`。

### 1.8 `defaults.logging`

| 键 | 默认 | 说明 |
|---|---|---|
| `level` | `info` | `debug` / `info` / `warning` / `error` |
| `format` | `console` | `console`（人读）/ `jsonl`（机读，便于检索审计） |
| `file` | `null` | 日志文件（相对 `storage.root`）；null=只输出终端 |
| `redact_env` | `[CDSAPI_KEY]` | 写日志前必须打码的环境变量名（必须 UPPER_SNAKE_CASE） |

### 1.9 `defaults.scheduler`

| 键 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `max_workers` | `4` | 1–64 | 调度线程数（与传输线程是两回事） |
| `free_space_floor_gb` | `20.0` | ≥0 | 排队前总量预检阈值 |
| `priority_aging_seconds` | `1800` | ≥0 | 低优先级等待超时后提升优先级，防止饿死 |
| `lease_seconds` | `900` | 60–604800 | 任务租约时长 |
| `heartbeat_interval_seconds` | `60` | 5–3600 | 心跳间隔 |

强制关系：`heartbeat_interval_seconds * 3 <= lease_seconds < integrity.stale_timeout_seconds`。
理由见 [ADR-0004](adr/ADR-0004-task-recovery-lease-heartbeat.md)：租约是抢占保护，
过期判定是崩溃恢复，两条时间线混用会导致"慢任务被第二个 worker 抢走并双写同一文件"。

---

## 2. `sources:` 顶层

`sources.<id>` 声明一个数据源。**声明不等于实现**：`implemented: false` 必须被
`rdh source list` 与计划器如实反映。

| 键 | 必填 | 说明 |
|---|---|---|
| `provider` | ✅ | 机构名，如 `Copernicus CDS` / `NSF NCAR RDA` |
| `display_name` | ✅ | 展示名 |
| `description` | | 说明 |
| `adapter` | ✅ | 数据源适配器 ID（`cds` / `rda` / …），由 `SourceRegistry` 工厂解析 |
| `implemented` | | 该适配器当前是否真的可用；false 时 CLI 与计划器必须拒绝而不是假成功 |
| `transports` | ✅ | 传输绑定列表（见下），至少 1 条 |
| `datasets` | | 该源提供的逻辑数据集 ID |
| `credentials` | | 凭证引用，值必须是 `env:VAR`（loader 展开后只在本进程内存中存在） |
| `endpoint` | | 基础端点（公开地址，可明文） |
| `rate_limit_per_minute` | | 该源每分钟请求上限 |
| `enabled` | | false 时计划器完全忽略该源 |
| `extensions` | | 命名空间化扩展键（必须含 `.`，如 `cds.dataset_name`） |

### 2.1 `transports[]`（传输绑定）

| 键 | 说明 |
|---|---|
| `route` | 访问路径：`s3` / `https` / `globus` / `zarr` / `cds_api` / `local` |
| `adapter` | 传输适配器 ID（`s3` / `https` / …） |
| `implemented` | 该传输是否已实现；计划器只对 `usable_routes` 出计划 |
| `options` | 传输层选项，键必须命名空间化（`s3.part_size_mb`、`https.timeout_seconds`） |

同一数据源的 `route` 不得重复（模型校验）。

**ADR-0002 的分界线**：`adapter` 回答"数据在哪、元数据长什么样"，`transports[].adapter`
回答"字节怎么搬"。源与传输的耦合点**只有** `route` 与 `SourceAsset.origin_uri`，
传输层不得出现 `if source_id == "cds"` 这类分支。

---

## 3. 配置变更的安全检查清单

改配置前逐条确认：

1. 凭证是否仍以 `env:` 引用出现？（`git grep -n "key:" configs/` 不应出现明文）
2. `storage.*_dir` 是否互不重叠？
3. `scheduler.lease_seconds` 与 `integrity.stale_timeout_seconds` 的序关系是否成立？
4. `transfer.verify_tls` 是否仍为 `true`（生产）？
5. `transfer.s3.verify_etag_as_md5` 是否仍为 `false`？
6. `rdh config validate` 是否 PASS（退出码 0）？

## 4. 相关文档

* [架构](architecture.md) §6 配置流
* [数据契约](data-contract.md)
* [任务恢复](recovery.md)
* [数据完整性](data-integrity.md)
* [ADR-0004 租约与心跳](adr/ADR-0004-task-recovery-lease-heartbeat.md)
