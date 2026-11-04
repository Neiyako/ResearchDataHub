# M6 子项 1 验收记录：轻量下载效率基准

| 项 | 值 |
|---|---|
| 验收对象 | [`docs/M6-efficiency-benchmark.md`](M6-efficiency-benchmark.md)、[`scripts/m6_efficiency_bench.py`](../scripts/m6_efficiency_bench.py)、[`benchmarks/m6/results.json`](../benchmarks/m6/results.json) |
| 验收标准 | 冻结于 2026-11-04T11:36:52Z（`entry_hash = 30d003cd…b94210`），原文见 `.awgs/tasks/M6-BENCH/criteria.json` |
| 判定方式 | adversarial-eval 五态判定 + `verify_verdict.py` 门禁（verdict 文件 `.awgs/tasks/M6-BENCH/verdict.yaml`） |
| 角色分离 | 执行、验收由同一会话在不同阶段完成（共享盲区，见 §F）；无独立第三方复核 |

---

## A. 最终判定

**`INSUFFICIENT_EVIDENCE`**

一句话依据：「纯 NCAR」与「混合制」两项有可复核的真实实测（113.61 s / 先就绪 112.55 s），但「纯 CDS」新请求在 45 分钟观察窗内未就绪（服务端全程 `accepted`，47 分钟时仍未启动），三条件效率差的核心一问拿不到 CDS 侧数值，因此按先冻结的标准只能判**证据不足**，而不是 PASS。这不是说报告/脚本不可信，而是说「CDS 拥塞期多慢」这个数字本次没有测到，只有下界（≥2 700 s）。

---

## B. 核心依据（决定判定的证据）

验收标准 C1–C7（冻结于验收前，`locked_before_execution=true`）：

| # | 硬性标准 | 结果 | 证据 |
|---|---|---|---|
| C1 | 三条件真实执行：`results.json` 含 cds/ncar/mixed 三条 run，`ok=true`、字节数 > 0、SHA-256 非空；NCAR 字节数 == GDEX 清单声明值 | **未满足（1/3）** | `benchmarks/m6/results.json`；`results.json` 含 ncar/mixed 两条 `ok=true`；**无 cds 成功 run**（45 min 窗口内未就绪，见 `.awgs/tasks/M6-BENCH/evidence/benchmark-runs.log`） |
| C2 | CDS 产物科学正确：xarray 可开，含 2t，时间覆盖 2024-01-01..03 逐时，纬度 30–45、经度 100–125 | **未满足** | 3 天 CDS 产物未获得；空闲期小样本 `t2m / lat 30–45 / lon 100–120` 正确（仅 1 个时次），证明的是 area 顺序而非时间覆盖 |
| C3 | 报告数字与原始 JSON 一致（逐项，误差 ≤ 0.01） | **满足** | `.awgs/tasks/M6-BENCH/evidence/report-consistency.log` |
| C4 | 混合制语义正确：`first_ready==min(两侧)`、`wasted==败者字节`、winner 与实测一致 | **满足（修正后）** | `results.json` `runs[mode=mixed].validation` |
| C5 | 离线质量门：pytest 0 failed；ruff check/format 通过；mypy strict Success | **满足** | `evidence/pytest.log`（`PYTEST_EXIT=0`）、`evidence/static-checks.log` |
| C6 | 未验证项显式 NOT RUN；报告无免结论措辞（过 hedge 黑名单） | **满足** | `verify_verdict.py` 退出码 0；报告 §6 |
| C7 | 异常路径有真实证据：CDS 队列上限拒绝原文与排队时间戳，可复核 | **满足** | `evidence/cds-queue-limit.log` |

---

## C. 致命与高优先级问题

**F1 · MAJOR · 纯 CDS 端到端未测得（外因）**
- 位置：`docs/M6-efficiency-benchmark.md` §3.1；`benchmarks/m6/c-mixed/results.json`
- 证据：job `5ba3eb9e-ad11-4dd6-a7f5-141019a854cd` 于 2026-11-04T11:30:00Z 提交，至 12:17Z（47 分钟）仍为 `accepted`；
  harness 45 分钟窗口因超时终止。同路径小样本 39.6 s（服务端）/59.2 s（客户端）可作参照。
- 影响："三种条件差多少"的 CDS 侧只有下界（≥2 700 s），无法给出确定值 → 本项验收判证据不足。
- 复现：`--mode cds --cds-days 3` 重跑；或在 CDS 非拥塞时段/缓存命中时重跑。

**F5 · MAJOR · 混合制"并发双投"与 CDS 现行政策冲突**
- 位置：`docs/M6-efficiency-benchmark.md` §3.1/§4；ECMWF 公告（2026-10-22）
- 证据：公告要求 "Submit requests sequentially, rather than in parallel or in large batches"；
  本次混合制同时向 CDS+NCAR 提交，CDS 侧随后出现 45 分钟排队与队列上限拒绝。
- 影响：不能把"并发对冲"作为产品策略；应改为"串行 CDS + 超时转 NCAR"。
- 复现：对同一账号并发/快速连续提交 CDS 请求，观察队列与 `rejected`。

---

## D. 其他问题

**F2 · MINOR · 单次测量，无置信区间**
- 证据：每个条件仅 1 次；CDS 排队方差大（40 s ↔ >47 min）。
- 影响：数字只能代表 2026-11-04 20:00 前后该 ISP/该 CDS 负载时段。
- 复现：`--mode ...` 重复跑并汇总。

**F3 · MINOR · 混合制 winner 字段曾被脚本误判**
- 证据：旧版 `run_mixed` 只按 `wall_seconds` 排序，CDS 失败侧 `wall=0` 被当成赢家（首版记录 winner=cds、wasted=1.13 GB）。
- 处理：已修复（仅成功侧参与排序；失败路径写真实耗时），并按同一 run 的实测字段重算；
  原始记录保留在 `benchmarks/m6/c-mixed-raw/`，修正说明写在 `results.json.corrections`。
- 影响：修复前若直接引用会得到错误的"混合制赢家"。

**F6 · INFO · 一个时间炸弹测试被本次验收暴露并修复**
- 位置：`tests/unit/test_scheduler/test_run_pipeline.py::test_lease_loss_stops_worker_without_writing_state`
- 证据：该用例用固定 `FIXED_NOW = 2026-11-04T12:00Z` 取消任务，而调度器线程用真实时钟写 `started_at`；
  真实时间越过 12:00Z 后 `finished_at < started_at`，持久化任务违反数据契约 → 用例在 12:20Z 实测失败（12:00Z 前通过）。
- 处理：取消动作改用真实时钟；同类固定时钟用法已核对（其余用例的写入时间也用 FIXED_NOW，不受影响）。
- 复现：`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_scheduler/test_run_pipeline.py -q`。

**F4 · INFO · NCAR 适配器无法发现 ERA5 netCDF 文件**
- 证据：`GET /api/datasets/d633000/webfiles/29/` 返回 `{"data": {}}`；`/filelist/{年}29/` 才给出 744 个文件与真实 URL。
- 影响：RDH 的 NCAR 路径目前只能发现 catalogs 分组（CSV），真实 ERA5 产物进不了计划。
- 复现：对比上述两个端点的响应。

---

## E. 检查与测试记录

| # | 类型 | 命令 | 结果 |
|---|---|---|---|
| T1 | executed | `PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" -m "not network" -q` | `PYTEST_EXIT=0`；**967 passed / 14 deselected / 0 failed** |
| T2 | executed | `.venv/bin/ruff check src tests` | `RUFF_CHECK_EXIT=0`（All checks passed） |
| T3 | executed | `.venv/bin/ruff format --check src tests` | `RUFF_FORMAT_EXIT=0`（137 files already formatted） |
| T4 | executed | `.venv/bin/python -m mypy src` | `MYPY_EXIT=0`（Success: no issues found in 64 source files） |
| T5 | executed | `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_m6_benchmark.py -o addopts="" -q` | 12 passed |
| T6 | executed | 三次真实网络基准运行（命令见报告 §7） | ncar PASS 113.61 s / 1 134 576 716 B；mixed 整体 PASS，先就绪 112.55 s（winner=ncar）；cds 未就绪（45 min 超时）；另有队列上限拒绝变体（19.6 s 被拒） |
| T7 | executed | 报告 ↔ JSON 一致性脚本 | PASS（6 条片段、0 缺失；见 `evidence/report-consistency.log`） |
| T8 | static | 报告措辞检查（hedge 黑名单） | `verify_verdict.py` 退出码 0 |
| T9 | not_run | 重复统计（每条件 ≥3 次、置信区间） | NOT RUN：单次测量，见 §F |
| T10 | not_run | NCAR THREDDS/NCSS 子集服务探测 | NOT RUN：不在本次范围 |
| T11 | not_run | `cds_api` 传输适配器在调度器内的端到端 | NOT RUN：本次只用脚本层生命周期，RDH 传输层仍未实现（报告 §5） |

---

## F. 剩余风险与局限

1. **单次测量**：每个条件 1 次；CDS 排队方差极大（本次同时观测到 40 s 与 >20 min），
   `ncar` 的 113.6 s 也只代表该时段、该 ISP、该 OSeF 节点。
2. **工作负载不对称**：NCAR 侧无子集能力，必须整文件下载（1.134 GB）；"同样时间拿到同样数据"这一强断言不成立，
   报告用"拿到目标子集的时间 + 字节成本"两个维度同时表述。
3. **CDS 服务状态**：测量期间 CDS 处于保护性限流（新请求排队、队列上限拒绝），
   报告中 CDS 数字带明示的时间背景；不能外推为"CDS 常年如此"。
4. **角色分离不足**：执行者与验收者是同一模型、同一会话，共享盲区；
   `verify_verdict.py` 只校验结构与措辞，不校验物理真实性；NCAR 侧用字节数/SHA-256/目录声明交叉印证以降低风险。
5. **未做**：断点续传、并发、长时稳定性、跨网络路径（见 §E T9–T11）。

---

**会话结束时状态**：CDS 请求 `5ba3eb9e-ad11-4dd6-a7f5-141019a854cd` 仍在服务端排队
（2026-11-04T12:17Z 仍 `accepted`）；本地观察进程已停止，未见结果，故「纯 CDS」判为证据不足。

## G. 后续行动

1. 用本基准的生命周期代码实现 `CdsApiTransport`，把 CDS 接进 RDH 调度器（报告 §5 缺口 3）。
2. `NCARAdapter` 增加 GDEX filelist 端点支持（缺口 1），并补 network 测试；顺带探测 THREDDS/NCSS 子集能力（缺口 4）。
3. 调度器：单数据集串行化 + 队列深度感知，把 CDS `rejected` 映射为可退避错误（缺口 2）。
4. 补重复测量（每条件 ≥3 次）与不同时段对照，把"效率差"从单点升级为区间。
