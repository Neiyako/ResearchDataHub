# 运维手册（Operations）

面向"每天真的在跑采集"的人。所有命令都已在仓库根实测；输出与退出码是契约的一部分。

---

## 1. 首次部署（5 分钟）

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[sci,cds]"        # sci=NetCDF 检查；cds=CDS 通道
cp configs/sources.example.yaml configs/sources.yaml   # 已被 .gitignore 忽略
export CDSAPI_KEY="<your-token>"   # 只有走 CDS 才需要
rdh config validate                # 必须 PASS（退出码 0）
rdh db init                        # 建库 + 建 raw/staging/quarantine/temp/archive
rdh db status                      # 确认 journal_mode=wal、foreign_keys=on
```

**目录规划**：`storage.root` 指向**外置 SSD** 时请写绝对路径（如
`/Volumes/SSD/rdh`），并确认 `staging` 与 `raw` 在**同一个卷**上能得到原子 rename；
若不在同一卷，提交会走 copy+fsync+rename 路径（正确但更慢，大文件需预留双倍空间）。

## 2. 日常流程

```bash
# a) 写/改需求
$EDITOR my-request.yaml
rdh request validate my-request.yaml        # 离线，先排除语法/范围错误

# b) 规划（联网查目录，不下载）
rdh plan create my-request.yaml
rdh plan show <plan_id>                     # 看清任务数、数据源、未匹配变量

# c) 执行
rdh task run <plan_id> --max-workers 2      # 8GB 机器建议 2
rdh task status                             # 总览
rdh task status <task_id>                   # 租约/心跳/事件

# d) 交付
rdh report generate <request_id>
rdh report show <report_id>
```

### 2.1 循环执行到完成

`task run` 一次跑完所有可领取任务；中途 Ctrl-C / 断电后，**重跑同一条命令**即可：

* 已完成的不会重跑（终态）；
* 传输中断的 `.part` 会按三重自证决定续传或重下；
* 崩溃遗留的 `RUNNING` 由启动时 `release_expired()` 接管。

## 3. 常见故障与处置

| 现象 | 含义 | 处置 |
|---|---|---|
| `环境变量 'CDSAPI_KEY' is not set` | 配置引用了未设置的凭证 | `export CDSAPI_KEY=...`；或把该源 `enabled: false` |
| `Manifest 数据库尚未初始化` | 还没 `db init` | `rdh db init`（幂等，不会清库） |
| `database is locked` | 多进程同时写同一个库（不支持） | 只跑一个写入进程；`db status` 用只读命令 |
| 任务 `RETRY_WAIT` | 可重试失败（超时/5xx/429），等退避 | 等；或 `rdh task retry <id>` 立即重试 |
| 任务 `FAILED` | 不可重试失败（404/403/参数错） | 看 `last_error`；修正需求/权限后重新规划 |
| 任务 `QUARANTINED` | 校验失败，文件在 `quarantine/` | 看 `quarantine/<file>.reason.json`；修复源后重新规划（终态不会自动复活） |
| `needs_attention` 对账条目 | 正式文件与 Manifest 不一致（外部删除/内容不同） | 人工核查 `raw/` 与日志；**不要手改数据库** |
| 磁盘告警 `free space below floor` | 低于 `free_space_floor_gb` | 清理 `staging/`（可安全删 `.part`）或换盘 |
| `resume_identity_unknown` | 无法自证远端资源未变 | 删掉对应 `.part` 让其重下，或按提示人工确认 |

## 4. 崩溃后的恢复检查单

```bash
rdh task reconcile          # 回收过期租约 + 对账文件与 Manifest
rdh db status               # 看各状态计数
rdh task status             # 看具体任务
rdh report generate <request_id>   # 报告会列出失败/未完成/隔离
```

判定规则（详见 [`recovery.md`](recovery.md)）：

* `RUNNING` 但心跳新鲜 → 继续等，不抢；
* 心跳超时 → 自动重排（有重试预算）或判 `FAILED`；
* `raw/` 有文件但 DB 无记录 → `reconcile` 校验后补记；
* DB 说完成但文件不在 → `needs_attention`，**不会静默重建**。

## 5. 空间与性能

| 参数 | 默认 | 8 GB 机器建议 |
|---|---|---|
| `concurrency.workers` | 4 | 2 |
| `scheduler.max_workers` | 4 | 2 |
| `concurrency.per_source_limit` | 2 | 2（避免触发远端限流） |
| `transfer.chunk_size_bytes` | 1 MiB | 1 MiB（内存占用 ≈ 2 块/下载） |
| `transfer.timeout_seconds` | null（不限总时长） | 保持 null，靠 read 超时兜底 |
| `storage.free_space_floor_gb` | 20 | ≥ 单文件最大体积 × 2 |

`staging/` 的占用 ≈ 正在下载的文件大小；`raw/` 是最终体积；跨卷提交会临时占用双倍。

## 6. 备份与迁移

* **要备份的**：`manifest.db`（事实来源）、`raw/`、`deliveries/`、`reports/`。
  `staging/`、`quarantine/` 可备份但非必需。
* **迁移到新盘**：停止写入进程 → 复制上述目录 → 改 `storage.root` → `rdh db status` 验证。
* **不要**在复制后同时从两个位置运行（两套任务状态会各自演进）。

## 7. 安全注意

* 凭证只通过环境变量提供；日志只打印变量名。
* `raw/` 默认 `0444`，请勿手工改动；系统不提供 `--force` 覆盖。
* 需要"重下同一文件"时：先归档/移走旧文件，再重新规划——这是刻意设计的摩擦。

## 8. 相关文档

[架构](architecture.md) · [配置](configuration.md) · [恢复](recovery.md) ·
[完整性](data-integrity.md) · [工程报告](engineering-report.md)
