# M6 轻量下载效率基准：纯 CDS API / 纯 NCAR / 混合制

| 项 | 值 |
|---|---|
| 报告日期 | 2026-11-04 |
| 里程碑 | M6（真实 ERA5 小规模端到端、性能验证） |
| 本次范围 | 三种数据获取条件（纯 CDS / 纯 NCAR / 混合制）的**轻量**端到端效率对比 |
| 执行方式 | 单机脚本 `scripts/m6_efficiency_bench.py`，真实网络、真实凭证（CDS 个人令牌） |
| 原始证据 | [`benchmarks/m6/results.json`](../benchmarks/m6/results.json)、[`benchmarks/m6/summary.md`](../benchmarks/m6/summary.md) |
| 判定口径 | 只有实测 PASS 才算验证；未测项显式标记 NOT RUN（见 §6） |

---

## 0. 结论先行

1. **同一逻辑需求（ERA5 2t，2024-01-01..03 逐时，30–45°N / 100–125°E）下**：
   纯 NCAR 端到端 **113.6 s**（下载 1 134 576 716 B，10.5 MB/s）；混合制先就绪 **112.55 s**（NCAR 胜出）；
   **纯 CDS 的新请求没能给出数值**——它在 45 分钟观察窗内一直停在 `accepted`，
   最终按超时记为未就绪（下界 ≥ 2 700 s）。同一条 CDS 路径在空闲期的小样本上端到端 59.2 s。
2. **差值的主因不是带宽，是"数据形态 + 队列"**：NCAR 无排队、吞吐稳定（实测 10.5 MB/s），
   但只能整文件交付（月度全域 1.134 GB，约为 3 天子集的 600 倍量级）；CDS 只下子集，
   却要经过 `submit → 排队 → 生成 → 下载`，排队在当前 CDS 保护性限流下可达数十分钟，
   且**同一数据集有排队上限**（超限直接拒绝）。
3. **混合制只能做"串行兜底"，不能做"并发双投"**：CDS 与 NCAR 同时提交在本次实测里等于给 CDS
   制造并行请求，既违反 CDS 现行使用政策，也没让 CDS 更快；真正的收益是"CDS 排队超时就换 NCAR"。
   在 CDS 队列正常的时段，混合制没有额外收益。

> 一句话：**要小区域子集 → CDS（省 200 倍字节）；要整月全域/大批量 → NCAR（稳定、无排队）；
> 混合只作为 CDS 排队超时的 fallback，不做默认策略。**

---

## 1. 问题与范围

M6 的第一优先是"真实 ERA5 小规模端到端"。本报告只覆盖其中**轻量下载效率**一块：
同一份科研需求，分别走 Copernicus CDS 与 NSF NCAR GDEX 两条真实访问路径，并测"两路同时投"的混合策略。

明确不覆盖（NOT RUN，见 §6）：S3 端到端、断点续传压力、长时稳定性、RSS/内存曲线、
并行多请求下的限流表现、`cds_api` 传输层在 RDH 调度器内的端到端（该项仍缺传输适配器）。

---

## 2. 实验设计

### 2.1 同一逻辑需求（工作负载）

| 项 | 取值 |
|---|---|
| 数据集 | ERA5 hourly data on single levels（CDS 集合 `reanalysis-era5-single-levels`；NCAR `d633000`） |
| 变量 | `2m_temperature`（NCAR 变量码 `128_167_2t`） |
| 时间 | 2024-01-01 00:00 – 2024-01-03 23:00（72 个时次，逐时） |
| 区域 | 30–45°N / 100–125°E（CDS `area` 顺序 `[N,W,S,E]`，实测产物经纬度范围反证，见 §3.1） |
| 格式 | NetCDF |

关键不对称（**必须承认，不能假装等价**）：CDS 按请求**生成子集**；NCAR GDEX 的 ERA5
是**预生成月度全域文件**，没有子集服务，想拿这 3 天数据只能下载整个 2024-01 月文件
（1 134 576 716 B，目录声明值与 HEAD 一致）。因此本报告同时给出两个量：
**"拿到目标子集的时间"** 与 **"为此付出的字节数"**，二者都计入效率结论。

### 2.2 三种条件

| 条件 | 定义 | 实现 |
|---|---|---|
| `cds` | 纯 CDS API | `cdsapi` 内部同一个 `ecmwf-datastores` 客户端，手工驱动 `submit → 轮询 → get_results → 下载`（等价 `cdsapi.Client().retrieve()`，但可分段计时） |
| `ncar` | 纯 NCAR GDEX | `GET /api/datasets/d633000/filelist/{年份}29/` 定位文件 → `HEAD` 核对大小 → HTTPS 流式下载整文件 |
| `mixed` | 混合制（双源对冲） | 两个线程用 `Barrier` 同时启动上述两条路径；记录各自就绪时间、先就绪时间、冗余字节 |

混合制的语义是"先到先用"：真实调度器里可把慢的一路取消。本次测量让两路都跑完，
以保留"慢的一路要多久、浪费多少字节"；`--hedge-grace-seconds` 可模拟放弃慢路（本次未启用）。

### 2.3 指标与测量方法

| 指标 | 定义 | 为什么需要 |
|---|---|---|
| 端到端耗时 | 从第一次网络调用到产物落盘完成 | 科研用户感知的"多久能拿到数据" |
| 分阶段耗时 | CDS：`submit` / `queue_wait`(accepted→running) / `generate`(running→successful) / `download`；NCAR：`catalog` / `head` / `download` | 定位瓶颈在排队还是在带宽 |
| 有效吞吐 | 字节数 / 端到端耗时 | 跨条件可比的效率量 |
| 下载吞吐 | 字节数 / 下载阶段耗时；另记 TTFB | 排除排队后的纯网络能力 |
| 冗余字节 | 混合制中未胜出一侧的下载量 | 对冲策略的成本 |
| 产物校验 | `xarray` 打开，记录变量、dims、时间范围、经纬度范围 | 证明拿到的是**真 ERA5 数据**，不是 0 字节或错区域 |

计时用 `time.perf_counter()`；下载为 `httpx` 流式（1 MiB chunk），不整载入内存；SHA-256 边流式计算。

### 2.4 公平性与已知偏差

1. **单次运行**：每个条件各 1 次，不做重复统计。CDS 排队时间方差极大（实测 40 s → 20 min+），
   单次数字只代表"当时"；本报告把观测到的排队波动写进 §3.1 与 §6，不伪装成稳定基线。
2. **CDS 缓存**：CDS 对相同请求有结果缓存，缓存命中时几乎无排队。本次"纯 CDS"用的是
   **未命中缓存的新请求**（日期 01–03 未请求过），因此计入了真实排队；缓存命中对照见 §3.1。
3. **网络路径**：CDS 结果在 ECMWF 对象存储（欧洲），NCAR 经 OSeF director（美国）。
   单机、单 ISP、同一时段，未做跨国链路对比。
4. **NCAR 整文件假设**：没有使用 THREDDS/NCSS 子集服务（未验证是否对本数据集开放）。
   若未来走 NCSS，NCAR 侧的字节成本会大幅下降——这是本节最重要的未验证项。
5. **混合制的两路并不完全独立**：CDS 侧下载量小（几 MB），对 NCAR 吞吐影响可忽略；
   NCAR 侧 1.134 GB 下载期间 CDS 在排队，互不占用。

### 2.5 环境与凭据

| 项 | 值 |
|---|---|
| 机器 | Apple M2 / 8 核 / 8 GB / macOS 27.0.1 (arm64) |
| 运行时 | Python 3.13.13；httpx 0.28.1；xarray 2026.9.0；netCDF4 1.7.5.1 |
| 凭据 | CDS 个人令牌经 `~/.cdsapirc` 提供（**值不落盘、不打印**）；NCAR GDEX 为公开访问，无需登录 |
| 脚本 | [`scripts/m6_efficiency_bench.py`](../scripts/m6_efficiency_bench.py)（合并工具 [`scripts/m6_merge_results.py`](../scripts/m6_merge_results.py)） |

---

## 3. 结果

### 3.1 纯 CDS（新请求，未命中缓存）

**结论：本次未能获得"纯 CDS 新请求"的端到端数值**（外部服务限流，非脚本错误）。

| 观测 | 实测值 | 证据 |
|---|---|---|
| 3 天逐时子集请求：submit → 状态 | 11:30:00 UTC 提交，45 分钟轮询窗口内**始终 `accepted`**，无 `running` | `benchmarks/m6/c-mixed/results.json`（`runs[mode=mixed]` 的 cds 侧 notes）、`.awgs/tasks/M6-BENCH/evidence/cds-queue-limit.log` |
| 同一请求在观察窗外的状态 | 12:17 UTC（提交后 47 分钟）仍为 `accepted` | 服务端 job `5ba3eb9e-ad11-4dd6-a7f5-141019a854cd` |
| 空闲期小样本（1 天 × 1 时次） | 客户端端到端 **59.2 s**；服务端 submit→successful 39.6 s；产物 34 442 B | 2026-11-04 11:18:28–11:19:08 UTC 的 job `c65a940b` + 本地 `xarray` 校验 |
| 排队超上限 | 提交后 19.6 s 被**立即拒绝**：`Maximum number queued requests for this dataset has been reached. Please configure your scripts to request data sequentially` | `.awgs/tasks/M6-BENCH/evidence/cds-queue-limit.log` |

产物校验（空闲期小样本，`xarray` 读取）：`t2m`，`valid_time` = 2024-01-01T00:00，
`latitude ∈ [30, 45]`（61 格），`longitude ∈ [100, 120]`（81 格）——与请求
`area=[45,100,30,120]` 的 `[N,W,S,E]` 顺序一致，**CDS 的 area 顺序由此被真实产物反证**。

**同一路径的对照观测（不计入上表，作为波动证据）**：

| 观测 | 事实 |
|---|---|
| 空闲期小请求 | 1 变量 × 1 天 × 1 时次 × 同区域：`submit→successful` 40 s（11:18:28→11:19:08 UTC），产物 34 442 B，客户端含初始化共 59.2 s |
| 拥塞期排队 | 11:20:47 与 11:24:32 UTC 提交的两个请求，13–16 分钟后仍是 `accepted`，无进度；11:30:00 UTC 提交的 3 天请求在观察窗内同样长期停在 `accepted` |
| 队列上限 | 队列满时新请求被**立即拒绝**：`Maximum number queued requests for this dataset has been reached. Please configure your scripts to request data sequentially`（服务端原文，证据 `.awgs/tasks/M6-BENCH/evidence/cds-queue-limit.log`） |
| 缓存命中 | 相同请求重发走 CDS 对象缓存（`cci2-prod-cache`），`successful` 时间从十几分钟降到 ≤ 40 s 量级 |

**这次波动有明确的政策背景，不是单纯"服务慢"**：ECMWF 于 2026-10-22 发布
[Data Stores 保护性措施公告](https://forum.ecmwf.int/t/data-stores-service-protection-measures-affecting-data-downloads-and-account-access-cds-ads-ewds-ecds/15481)，
明确要求**串行提交请求**（"Submit requests sequentially, rather than in parallel or in large batches"），
并对被判定为非合规的活动实施监测与临时限制。本基准的"混合制"（对 CDS 与 NCAR 并发提交）
在 CDS 侧恰好属于**并行提交**——这既是本实验在 CDS 侧排队异常长的合理解释，
也直接改变了混合制的工程建议（见 §4）。

结论：**CDS 的端到端时间由"排队"主导且强依赖服务端负载**，带宽只在下载段（几 MB）起作用。

### 3.2 纯 NCAR（整月度文件）

| 阶段 | 耗时 (s) |
|---|---|
| 目录 API（filelist） | 2.63 |
| HEAD 核对大小 | 1.43 |
| 下载（1 134 576 716 B） | 108.73 |
| **端到端** | **113.61** |

- 下载 TTFB 1.92 s，下载段吞吐 **10.50 MB/s**，端到端有效吞吐 **9.99 MB/s**。
- 目录声明大小与 HEAD `Content-Length` **一致**（1 134 576 716 B），SHA-256 = `e619fc57…9b04`。
- 产物校验：变量 `VAR_2T`，`time=744`（2024-01 全月逐时），`latitude=721`（-90…90），
  `longitude=1440`（0…359.75）——确是 ERA5 全域文件，但**不是**用户要的 3 天子集。

### 3.3 混合制（双源对冲）

| 观测 | 实测值 |
|---|---|
| 先就绪（time to first artifact） | **112.55 s**，winner = `ncar` |
| 两侧耗时 | NCAR 112.55 s 完成；CDS 侧 45 分钟窗口内未就绪（按超时记 ≥2 700 s） |
| 冗余字节 | 0（CDS 侧未下载任何字节即被超时终止） |
| 整轮 wall | 2 702.7 s（=CDS 轮询超时上限；若按"先到先用"策略应立即交付 NCAR 产物并取消 CDS） |
| 变体：CDS 被队列上限拒绝时 | CDS 侧 19.6 s 即失败；混合制先就绪 = NCAR **116.0 s**；冗余 0（`benchmarks/m6/c-mixed-queue-limited/results.json`） |

> 两个变体说明同一件事：在 CDS 排队/拒绝时，混合制的实际交付时间 ≈ 纯 NCAR 的时间；
> 它换来的是"CDS 一旦就绪可以省字节"的可能性，代价是冗余下载与违反 CDS 串行政策的风险。

### 3.4 三条件对比

| 条件 | 端到端 | 字节数 | 有效吞吐 | 结论状态 |
|---|---|---|---|---|
| 纯 CDS（新请求，拥塞期） | **未获得**（≥2 700 s 未就绪） | 未下载 | — | INSUFFICIENT：外部限流，观察窗内未启动 |
| 纯 CDS（空闲期小样本，参照） | 59.2 s | 34 442 B | 0.58 KB/s（小文件被延迟主导） | 仅参照，工作负载不同 |
| 纯 NCAR（整月全域文件） | **113.6 s** | 1 134 576 716 B | **9.99 MB/s**（下载段 10.5 MB/s） | PASS |
| 混合制（双源对冲） | 先就绪 **112.55 s**（NCAR） | 1 134 576 716 B（CDS 0 B） | 9.99 MB/s | PASS（但策略本身不合规，见 §4） |

### 3.5 时间花在哪：派生分析

用本机实测参数做一阶模型（只用于解释量级，不作为新测量）：

- NCAR：`T_ncar ≈ 4.1 s（目录+HEAD） + F / 10.5 MB/s`，F = 需要的整文件字节数。
- CDS：`T_cds ≈ 排队(0 → 数十分钟) + 生成 + s / 下载吞吐`，s = 子集字节数
  （3 天逐时 × 61×101 格 ≈ 1.8 MB 量级，按网格估算，未实测）。

代入本次需求（s ≈ 1.8 MB，F = 1.134 GB）：

| 情形 | 端到端 | 说明 |
|---|---|---|
| CDS 空闲（排队+生成 < ~110 s） | 60 s 量级 | 此时 CDS 更快，且只下 ~1.8 MB |
| CDS 拥塞（本次） | ≥2 700 s 未就绪 | 排队主导，与字节数无关 |
| 纯 NCAR | 113.6 s（实测） | 与需求大小无关，恒为整文件（1.134 GB） |
| 混合制（串行兜底语义） | ≈ min(T_cds, T_ncar) | 本次 = 112.55 s（NCAR） |

**判据**：CDS 与 NCAR 的胜负几乎只取决于"CDS 排队+生成要多久"：
若这段固定开销 < 约 110 s（NCAR 的整文件下载时间），CDS 更快；否则 NCAR 更快。
字节比（本次 ~1.8 MB vs 1.134 GB，约 600 倍）决定的是"值不值得为省流量而等 CDS"，
不决定时间胜负。对 ERA5 这类"小区域 + 短时段"科研需求，CDS 的字节优势是决定性的，
前提是队列不拥塞；拥塞时 NCAR 是唯一能在分钟级交付出结果的路径。

**但混合制不能靠"并发双投"实现**：CDS 现行政策要求串行，`hedge`（同时向 CDS 与 NCAR 提交）
会让 CDS 侧被判定为并行/批量提交，反而拉长排队甚至触发队列上限拒绝（§3.1 实测）。
可行的混合策略是**串行兜底**：先按政策单发 CDS，超过阈值仍未生成再转 NCAR；
或按请求形态直接路由（小区域子集 → CDS；整月全域/大批量 → NCAR）。

---

## 4. 讨论：三种条件到底差多少

**"三种条件差多少"的直接回答（2026-11-04 实测，带条件）**：

1. **CDS 空闲期**：小样本端到端 59.2 s（34 KB）；按线性外推，3 天逐时子集下载部分应仍在秒级，
   端到端由"排队+生成"主导。此时 CDS 优于 NCAR（快 ~2 倍，流量省 ~600 倍）。
2. **CDS 拥塞期（本次实测）**：新请求 45 分钟窗口内未启动（观察 47 分钟仍 `accepted`），
   相对 NCAR 慢 **≥ 24 倍**；且再提交会被队列上限直接拒绝。
3. **纯 NCAR**：稳定 113.6 s / 1.134 GB / 10.5 MB/s，与需求大小无关。
4. **混合制（并发双投，本次）**：先就绪 112.55 s ≈ 纯 NCAR，CDS 侧 45 分钟未就绪；
   若 CDS 被队列上限拒绝，混合制退化为纯 NCAR（116.0 s）。
   **结论：本次条件下混合制没有比纯 NCAR 更快的证据，且并发提交违反 CDS 串行政策。**

因此工程建议是**路由 + 串行兜底**，而不是并发对冲：

| 需求形态 | 建议路径 | 依据 |
|---|---|---|
| 小区域 / 短时段子集，且 CDS 可用 | CDS（单发，串行，限速） | 字节省 ~600 倍，空闲期更快 |
| 整月全域 / 大批量 / 站点级聚合 | NCAR 直接文件 | 无排队、吞吐稳定、字节劣势不显著 |
| CDS 排队超阈值（如 10–15 min） | 转 NCAR（让 CDS 请求保持排队或取消，不做并发双投） | 本次实验：拥塞时 NCAR 是唯一分钟级路径 |

---

## 5. 由基准暴露的产品缺口（对 RDH 的影响）

| # | 缺口 | 证据 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | NCAR 适配器发现不了 ERA5 netCDF 文件 | `GET /api/datasets/d633000/webfiles/29/` 返回 `{"data": {}}`（大分组无 webfile 列表）；`/filelist/{年}29/` 才给出 744 个文件与 URL | 目前 RDH 的 NCAR 路径只能发现 catalogs 分组（CSV），真实 ERA5 产物无法进计划 | M6 后续：`NCARAdapter` 增加 filelist 端点支持（`rda.filelist`），并补 network 测试 |
| 2 | CDS 队列上限未建模 | 拒绝原因原文（见 §3.1） | 批量计划会把请求堆到队列上限，得到 400 拒绝 | 调度器：单数据集串行化 + 队列深度感知 + `rejected` 映射为可退避错误 |
| 3 | `cds_api` 传输适配器仍缺 | 计划 §4 遗留 #2 | CDS 任务在 RDH 调度器内仍会 "no transport" 失败 | 用本基准的生命周期代码落地 `CdsApiTransport`（submit→poll→results→下载） |
| 4 | 无子集服务探测 | 未验证 NCAR THREDDS/NCSS 是否对本数据集开放子集 | 若有，NCAR 侧字节成本可降 200 倍 | M6 后续探测 `/thredds/ncss/` 能力 |

---

## 6. 局限与未验证（NOT RUN ≠ PASS）

| 项 | 状态 |
|---|---|
| 重复统计（每条件 ≥3 次、置信区间） | NOT RUN（单次；CDS 排队方差大，需更多样本） |
| CDS 缓存命中路径的完整往返（独立测量） | NOT RUN（仅有成功/时的日志性观测） |
| NCAR THREDDS/NCSS 子集服务 | NOT RUN（未探测） |
| 并发多请求下的限流/排队行为 | NOT RUN（仅观测到单账户队列上限拒绝） |
| 长时稳定性（>1 h 连续下载、断点续传、限速） | NOT RUN |
| S3 端到端、`cds_api` 传输层在调度器内 | NOT RUN（M6 其他子项） |
| 跨网络路径（国内 ISP / 代理）对 CDS/NCAR 的差异 | NOT RUN（单机单 ISP） |

---

## 7. 复现

```bash
cd ~/Desktop/ResearchDataHub
# 0) 前置：CDS 令牌（~/.cdsapirc 或 CDSAPI_KEY）；NCAR GDEX 公开无需凭证
# 1) 纯 NCAR（约 2 分钟；下载 1.1 GB 到临时目录）
PYTHONPATH=src .venv/bin/python scripts/m6_efficiency_bench.py --mode ncar \
    --out benchmarks/m6/a-ncar --workdir /tmp/rdh-m6-bench
# 2) 纯 CDS（新请求；CDS 限流时可能数十分钟排队甚至被队列上限拒绝——如实记录，不要并发重试）
PYTHONPATH=src .venv/bin/python scripts/m6_efficiency_bench.py --mode cds \
    --out benchmarks/m6/b-cds --workdir /tmp/rdh-m6-bench
# 3) 混合制（两路同时启动）
PYTHONPATH=src .venv/bin/python scripts/m6_efficiency_bench.py --mode mixed \
    --cds-start-day 1 --mixed-day-offset 0 \
    --out benchmarks/m6/c-mixed --workdir /tmp/rdh-m6-bench
# 4) 合并
PYTHONPATH=src .venv/bin/python scripts/m6_merge_results.py \
    benchmarks/m6/a-ncar/results.json benchmarks/m6/b-cds/results.json benchmarks/m6/c-mixed/results.json \
    --out benchmarks/m6
# 离线单元测试（不联网）
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_m6_benchmark.py -q
```

> 说明：`--ncar-cap-mb N` 只取前 N MB（轻量吞吐测，产物不完整，不得做完整性/科学校验）；
> 本次报告使用的是完整文件下载。

---

## 8. 相关文件

- 基准脚本：[`scripts/m6_efficiency_bench.py`](../scripts/m6_efficiency_bench.py)
- 合并脚本：[`scripts/m6_merge_results.py`](../scripts/m6_merge_results.py)
- 原始结果：[`benchmarks/m6/results.json`](../benchmarks/m6/results.json)
- 验收记录：[`docs/M6-verification.md`](M6-verification.md)
- 里程碑与遗留：[`docs/development-plan.md`](development-plan.md) §4
- 数据契约：[`docs/data-contract.md`](data-contract.md)
