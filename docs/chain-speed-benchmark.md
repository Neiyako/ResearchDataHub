# 全链路下载速度基准报告

> 测量时间：2026-11-06 00:30–01:40（Asia/Shanghai）
> 环境：macOS 27.0.1 arm64 / Python 3.13.13 / 家用宽带
> 脚本：[`scripts/chain_speed_bench.py`](../scripts/chain_speed_bench.py)
> 原始数据：[`benchmarks/speed/{earthdata,gee,ncar}/results.json`](../benchmarks/speed/)

---

## 0. 一句话结论

**在"能下载"这件事上三者（基线/框架）差别不大；差距出在"下载失败之后"。**

- **NCAR（大文件、CDN 波动大）**：RDH **100% 成功**，朴素脚本 **88%** —— RDH 靠重试/续传赢回 12 个百分点，且中位耗时反而更快（14.2s vs 15.8s）。
- **Earthdata（小文件、受保护资源）**：两者都 100%，但 RDH 中位慢 **+2.5s**（3.50→6.04），这是六层校验 + 原子提交 + 交付的固定成本。
- **GEE（云端导出任务）**：两者都 100%，RDH 慢 **+1.7s**（46.6→48.3），成本同样来自校验与交付；**瓶颈是 GEE 自己的排队（中位 46s），框架开销只占 3.7%**。

**结论要诚实**：本项目带来的不是"下载更快"，而是**"下载更不容易失败、失败后不用重头再来、成功的数据经过验证"**。在小文件上它比朴素脚本慢，这是可接受且应当说明的代价。

---

## 1. 测量方法

### 1.1 两条对比路径

| 路径 | 含义 |
|---|---|
| `baseline` | **用户自己写下载脚本**的真实形态：单次 httpx 流式请求，**不重试、不续传、不校验、不交付**。这是"没有本项目"的基准线。 |
| `rdh` | **ResearchDataHub 完整链路**：请求契约 → 计划器（源匹配/拆分/去重）→ SQLite Manifest 落库 → 调度器（原子领取 + 租约/心跳）→ 传输（流式 + `.part` 断点续传 + 重试分类）→ 六层完整性校验 → staging→raw 原子提交 → `delivery` 交付到用户目录。 |

两条路径下载**同一个远端对象**、写到**同一块盘**，因此差值反映框架本身的开销/收益，
而不是"挑了个更快的数据源"。

### 1.2 停止规则（按需求）

同一时间窗口内**响应率达到 80% 即停止**；未响应的直接记 `FAILED`。

本报告中的多平台对比采用 `--stop-rate 1.0`（**关闭提前停止**）以取得固定样本量做统计，
这是为得到可比的分位数；80% 规则的单独验证见 §4。

### 1.3 响应率口径

| 状态 | 含义 |
|---|---|
| `SUCCESS` | 文件落盘且字节完整 |
| `FAILED` | HTTP 错误、连接中断、字节不符、校验失败 |
| `TIMEOUT` | 超过单样本超时（计入 `FAILED`，单独统计） |
| `NOT_RUN` | **需要凭据而未配置**，或平台未接入 —— 不计入响应率分母，单独列出 |

> `NOT_RUN ≠ 失败 ≠ 通过`。本报告没有任何平台被静默算作通过。

---

## 2. 测量结果

| 链路 | 路径 | 尝试 | 成功 | 响应率 | 中位耗时 | P95 | 中位吞吐 |
|---|---|---:|---:|---:|---:|---:|---:|
| **Earthdata**（0.17 MB 受保护 granule） | baseline | 8 | 8 | **100%** | 3.50 s | 7.62 s | 0.05 MB/s |
| | **rdh** | 8 | 8 | **100%** | 6.04 s | 6.77 s | 0.03 MB/s |
| **GEE**（云端裁剪→Export→Drive，0.9 MB GeoTIFF） | baseline | 5 | 5 | **100%** | 46.56 s | 75.34 s | 0.02 MB/s |
| | **rdh** | 5 | 5 | **100%** | 48.28 s | 85.49 s | 0.02 MB/s |
| **NCAR GDEX**（58.3 MB NetCDF 整文件） | baseline | 8 | 7 | **88%** | 15.75 s | 20.34 s | 3.53 MB/s |
| | **rdh** | 8 | 8 | **100%** | 14.18 s | 19.60 s | 3.92 MB/s |

### 2.1 逐链路解读

#### Earthdata：小文件上框架是净开销

- 数据量仅 176 KB，**网络传输只占几百毫秒**，剩下的全是往返延迟与框架流程。
- RDH 的 +2.54 s = 计划器装配 + Manifest 落库（SQLite WAL 事务）+ 六层校验（含 SHA-256 全文件扫描）
  + staging→raw 原子提交 + 交付复制 + 完整性报告写库。
- **P95 反而更低**（6.77 vs 7.62），说明框架的耗时是**稳定的固定成本**，而基线的高分位由网络抖动主导。
- 判定：**小文件场景 RDH 慢 1.7×，但这 2.5s 换来的是"文件真的完整、有校验记录、有 Manifest"**。

#### GEE：瓶颈在云端排队，不在框架

- GEE 的正确用法是**云端筛选 + Export 任务**，数据从不经过"HTTP 文件下载"：
  `Image.clip(region)` → `Export.image.toDrive` → 轮询任务状态 → Drive API 取回。
- 中位 46.6 s 中，**Export 任务排队与计算占绝大部分**（实测任务在 READY→RUNNING→COMPLETED 间约 30–45 s）。
- RDH 的 +1.72 s 仅占 3.7%：它做的是下载后的校验/提交/交付，**没有重复云端计算**。
- 判定：**GEE 场景框架开销可忽略**；真正的效率来自"不重复导出"（幂等任务 ID + 结果缓存，尚未实现）。

#### NCAR：框架赢在失败恢复（最有价值的证据）

- 基线 8 次中失败 1 次（`RemoteProtocolError: peer closed connection`，耗时 110 s 后放弃）。
  NCAR CDN 实测存在**间歇性断连**：同一 URL 连续请求时，出现过 324 s 的极端慢速与随机断连。
- RDH 8/8 全部成功：**重试分类 + `.part` 三重自证续传**把断连变成了"从断点继续"，而不是"从头再来"。
- 中位耗时 RDH 略快（14.18 vs 15.75），原因之一是 RDH 的失败重试发生在**同一次样本内**且能续传，
  而基线那次失败直接浪费了 110 s。
- 判定：**这正是项目要解决的场景** —— 不是让下载更快，而是让下载**不白费**。

---

## 3. 框架开销的构成（Earthdata 实测拆解）

以 Earthdata 单样本（176 KB）为例，RDH 比基线多出的约 2.5 s 大致分布：

| 阶段 | 说明 | 是否可优化 |
|---|---|---|
| 计划器 + 装配 | 源匹配、任务拆分、SQLite 落库 | 可（连接复用/批量事务） |
| 六层完整性校验 | 存在/大小/SHA-256/格式/NetCDF/科学语义 | **不可省**（这是项目的核心价值） |
| staging→raw 原子提交 | `os.replace` + fsync | 不可省（避免半截文件被当成正式数据） |
| 交付到用户目录 | copy + fsync + 原子 rename | 可（大文件时可改为硬链接/移动，见 ADR-0015） |
| Manifest 写库 | 任务状态、事件、文件记录、完整性报告 | 部分可优化 |

**对小文件（<1 MB），这些固定成本占主导；对 >100 MB 的文件（如 NCAR 58 MB），它被摊薄到可忽略**
—— 这正是 NCAR 两组数字接近甚至反超的原因。

---

## 4. 80% 停止规则验证

按需求实现的停止逻辑（`--stop-rate 0.8`）：

- 每完成一个样本即计算 `成功/尝试`；
- **达到 0.80 立即停止**，不再发起新样本；
- 未响应的样本记 `FAILED`，超时记 `TIMEOUT` 并同时计入 `FAILED`。

实测（Earthdata，`--stop-rate 0.8` 默认）：

```
baseline  attempted=1 succeeded=1 rate=100%   → 首个样本成功即达阈值，立即停止
rdh       attempted=1 succeeded=1 rate=100%   → 同上
```

规则按预期工作：**首个样本成功时响应率 100% ≥ 80%，立刻停止**，不浪费后续请求。

---

## 5. 本轮修复的真实缺陷（测速过程中暴露）

这些缺陷是跑基准时才暴露的，**都已修复并有回归测试**：

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | **签名 URL 写进日志** | CloudFront/S3 预签名地址的 `Signature`/`X-Amz-Credential`/`token` 被原样写入 `transfer_logs` 落盘 —— **凭据泄露到磁盘**，且超长导致 `TransferLogEvent` 校验崩溃 | 新增 `redact_url()`：签名类查询参数替换为 `REDACTED`，超长截断；7 个安全用例锁定 |
| 2 | **认证头无法透传** | `HttpTransportAdapter` 不支持 `extra_headers` → RDH **无法下载任何需要 Bearer token 的科研数据**（Earthdata 全部受保护资源） | 增加 `extra_headers` 参数，文档明确"值不得写入日志/Manifest" |
| 3 | **跨域重定向丢凭据** | EDL 要求 303 到 CloudFront 签名地址；httpx 按设计丢弃跨域 Authorization（**行为正确**），但 RDH 因此 401 | 改为手动跟随重定向：**只在主机不变时携带凭据**，跨域立即降级 |
| 4 | **规划器改写签名 URL** | 已完成 submit→poll→results 的资产被改写成 `cds://` 模板 → 传输层拿不到可下载地址 → 任务必然失败 | 保留真实结果 URL，只对"未提交的模板资产"生成请求单元 |

---

## 6. 未验证 / 未测项（如实登记）

| 项 | 状态 | 原因 |
|---|---|---|
| **Copernicus Data Space** | `NOT_RUN` | 需 `CDSE_CLIENT_ID` + `CDSE_CLIENT_SECRET`（OAuth2 client credentials） |
| **CDS（Copernicus Climate Data Store）** | `NOT_RUN` | 按 [ADR-0014](decisions/ADR-0014-no-cdsapi-live-tests.md) 默认禁止 cdsapi 取数；且需要 CDS 排队时间 |
| **GEE 大数据量导出** | `未测` | 本次只测 0.9 MB GeoTIFF；GB 级导出涉及 Drive 配额与更长排队 |
| **超大文件（>1 GB）** | `未测` | 会稀释框架开销，理论上 RDH 相对更有优势，但需单独验证 |
| **断网/断连的受控注入** | `部分覆盖` | NCAR 真实出现 1 次断连并被 RDH 重试恢复；M7 场景 E 已在本地做确定性断连注入验证 |

---

## 7. 复跑方法

```bash
# Earthdata（需 EARTHDATA_TOKEN）
set -a && . ~/.rdh/env && set +a
PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py \
    --mode earthdata --samples 8 --stop-rate 1.0 --out benchmarks/speed

# GEE（需 earthengine-api + 凭据 + project）
PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py \
    --mode gee --gee-project <YOUR_PROJECT_ID> --samples 5 --out benchmarks/speed

# NCAR（公开匿名，需具体文件 URL）
PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py \
    --mode ncar --url "<GDEX_FILE_URL>" --samples 8 --out benchmarks/speed

# 本地离线冒烟（不联网，验证基准逻辑与 80% 停止规则）
PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py --mode local --out /tmp/speed
```

---

## 8. 结论

**这个项目的速度优势不在"平均更快"，而在三件事：**

1. **失败恢复**：NCAR 上 88% → 100%，靠重试与断点续传；这是最可复现、最有价值的差异。
2. **数据可信**：每个交付文件都过六层校验、有 SHA-256、有 Manifest 可回溯；朴素脚本"下完了"≠"下对了"。
3. **固定成本可预期**：小文件慢 2–3 s（稳定、P95 更低），大文件被摊薄到可忽略。

**不应该宣称的**：本项目不会让网络变快。GEE 的 46 秒是云端排队，Earthdata 的 3.5 秒是网络往返，
这些框架都无法改善；框架只能保证**这些时间不被浪费、结果可信**。
