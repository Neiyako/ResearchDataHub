# ADR-0013：链路选择只依据实测性能记录，禁止按地域推断

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

指令 §6 要求多节点、多链路调度，并明确两条硬约束：

> "根据**真实测量结果**判断链路表现"
> "**不要仅根据节点所在国家或地区推断下载速度**"

同时要求至少记录：`latency`、`throughput`、`success_rate`、`failure_rate`、
`retry_count`、`transfer_duration`、`transferred_bytes`。

现实中的诱惑是"亚洲节点访问中国数据源所以快"——这是没有证据的因果推断，
一旦写进选路逻辑，就会变成永久的偏见：真实网络状况（CDN 调度、ISP 互联、数据源就近性）与地理标签几乎无关。

## 决策

1. `network/routes/` 维护 `route_stats` 表，**只存实测样本**：

   | 列 | 含义 |
   |---|---|
   | `route_id` / `worker_id` / `provider_id` | 身份 |
   | `samples` | 累计样本数 |
   | `successes` / `failures` / `retry_count` | 计数 |
   | `latency_ms_p50` | 首字节/连接延迟中位数 |
   | `throughput_bps_ewma` | 吞吐指数加权移动平均（新样本权重 0.3） |
   | `transfer_duration_s_ewma` | 单次传输耗时 EWMA |
   | `transferred_bytes_total` | 累计字节 |
   | `last_measured_at` | 最近一次测量时间 |

2. `RouteManager.select()` 的打分（**只用上表字段**）：

   ```
   base        = success_rate                                # 成功率优先（0..1）
   speed       = min(1.0, throughput_bps / target_throughput) # 吞吐相对目标值
   latency_pen = 1 / (1 + latency_ms_p50 / 1000)              # 延迟惩罚，>1s 显著下降
   scale_pen   = 1.0 若 transferred_bytes_total 足以覆盖本次 size_bytes
                 否则 0.85（样本规模不足，置信度打折，而不是猜测）
   score       = base * 0.6 + speed * 0.3 + latency_pen * 0.1，再乘 scale_pen
   ```

3. **冷启动（`samples == 0`）**：不排序、不做伪评分。返回 `basis: "no_measurement"`，
   仅按**可达性**（健康 worker + 声明支持该协议）与用户显式偏好排序，并在结果里明说
   "该链路尚无实测数据，选择依据为可用性而非性能"。第一次真实传输完成后即写入首个样本。
4. **地域字段 `region` 只用于展示和用户显式过滤**（例如用户说"用欧洲节点"），
   **绝不进入打分函数**。代码评审中，`score` 函数出现 `region`/`country` 即为缺陷。
5. 失败与重试也写样本：失败会增加 `failures` 与 `retry_count`，从而自动降低该链路后续得分
   （这满足"失败转移"且不需要额外的黑名单机制）。

## 后果

**正面**

- 选路结论可审计、可复现、可被真实测量纠正；
- 冷启动行为诚实（明说没有数据），不会用地理偏见伪装成"智能调度"；
- 天然支持"实测优于假设"：新部署的远程节点跑几次后自动参与竞争。

**负面**

- 冷启动时可能选到慢链路（第一次必然有代价）。缓解：首个样本写入后立刻生效，
  且调度器对"首次使用某链路"记录 `basis` 以便事后复盘；
- EWMA 需要调参（0.3 权重）；若网络状况长期漂移，需要定期衰减（`last_measured_at` 超过 30 天的样本降权）。
- 样本数少时成功率估计不稳（1/1 = 100%）。缓解：`samples < 3` 时成功率按 `(successes+1)/(samples+2)` 做拉普拉斯平滑。

## 被否决的方案

1. **按 `region` 与数据源所在大洲的距离打分**：无证据支持，且被指令明确禁止。**否决**。
2. **主动对每条链路跑 ping/测速再选路**：需要真实大文件传输才能测吞吐，成本高；
   且会污染数据源的配额与限流。改为"用真实业务传输作为测量"（零额外成本）。**否决**。
3. **纯静态配置优先级（用户在 YAML 里排好顺序）**：无法反映真实网络劣化。保留为用户**显式偏好**（可覆盖），
   但不作为默认打分。**部分采纳**。
