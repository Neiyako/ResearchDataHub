# Route Intelligence（数据集获取路线智能）

> 目标不是“找到一个能下载的 Adapter”，而是先证明**有哪些 route、哪些能力可用、哪条链路值得用**。

## 1. 为什么需要它

现有 `sources` 层可以发现资产，`planner` 可以按需求生成任务，`task run` 可以执行传输；
但三者之间缺少一个**数据集级 route 决策层**。Agent 看到 `cds` Adapter 时，可能直接调用 CDS，
而没有验证：

- NCAR 是否有同数据集的 HTTPS 文件；
- CDS 与 NCAR 的变量/时间/空间/层/格式能力是否匹配；
- endpoint 当前是否可达；
- 小文件基准吞吐与延迟；
- 历史失败情况与 qualification 是否过期；
- 规模是否已经达到“必须 qualification 才能下载”的阈值。

Route Intelligence 把这些判断放在 source adapter 与 acquisition planner 之间，并保留
现有适配器、传输与调度器。

```text
DatasetRequest
  → Route discovery（source × transport × adapter）
  → Capability check（五态）
  → Connectivity probe（低字节量）
  → Benchmark（实测样本）
  → Qualification（DISCOVERED/PROBING/QUALIFIED/DEGRADED/FAILED/EXPIRED）
  → Selection（可解释排序）
  → Planner / Download
  → Integrity verification
  → Route experience registry
```

## 2. 严格区分六件事

| 概念 | 回答的问题 | 代码 |
|---|---|---|
| Dataset capability | 这个数据集在科学上支持哪些变量/时间/空间/层/格式？ | `RouteCapabilities` |
| Source capability | 某个 source 声明提供该数据集和哪些维度的能力？ | `SourceConfig` / adapter probes |
| Transport capability | 这条协议路径当前是否实现、是否可用？ | `RouteCapabilities.transport` |
| Adapter implementation | RDH 有没有实现该 source adapter？ | `AdapterImplementationStatus` |
| Route availability | endpoint / 认证 / 元数据 / 对象当前是否可观察？ | `RouteStatus` + `ProbeResult` |
| Route qualification | 是否已经获准用于默认下载？ | `RouteQualification` |

特别禁止：

```text
UNKNOWN       -> FAIL
NOT_IMPLEMENTED -> DATA_UNAVAILABLE
adapter exists -> dataset只能通过该平台
```

`UNKNOWN` 是“没有足够证据”，`NOT_IMPLEMENTED` 是“RDH 没有实现”，两者都必须原样返回。

## 3. Route 契约

```yaml
route:
  id: ncar-era5-pressure-levels-https
  source: ncar
  dataset: era5-pressure-levels
  transport: https
  adapter: https
  endpoint: https://rda.ucar.edu
  capabilities:
    dataset: PASS
    variables:
      clwc: PASS
      ciwc: PASS
      crwc: PASS
      cswc: PASS
      sp: PASS
    temporal_coverage: PASS
    temporal_resolution: PASS
    spatial_coverage: PASS
    spatial_resolution: PASS
    vertical_levels: PASS
    format: PASS
    subset: PASS
    authentication: PASS
    transport: PASS
  adapter_status: implemented
  status: available
  qualification: qualified
  benchmark:
    probe_size: 1048576
    latency_ms: 120.0
    transfer_time_s: 0.8
    throughput_bps: 1310720
    success: true
    timestamp: "2026-11-18T09:00:00Z"
  last_verified: "2026-11-18T09:00:00Z"
```

未实现的 transport 仍要出现在 discovery 结果中：

```text
transport=NOT_IMPLEMENTED
note="the source/dataset may still be valid through another route"
```

## 4. Python / Agent-facing API

```python
from researchdatahub.routing import RouteIntelligence

rdh = RouteIntelligence(sources=sources_config, adapters=adapters)

plan = rdh.plan(requirement)          # 多条候选 route，不访问下载数据
evaluation = rdh.evaluate(plan)       # 能力 + qualification + 可解释排序
selection = rdh.select(plan, objective="min_time")
acquisition_plan = rdh.materialize(plan, route_id=selection.route.id)
result = rdh.download(
    plan,
    route=selection,
    acquisition_plan=acquisition_plan,
    executor=my_executor,             # 复用现有 scheduler/transport 组合根
)
```

`download()` 不会偷偷回退到 `cds.download()` 之类的平台方法。没有执行器时明确失败；
policy 未通过时返回结构化拒绝原因。

## 5. 选择打分（第一版，可解释优先）

当前 score 是一个透明的工程启发式：

```text
score =
  100
  × capability_factor          # 只有 PASS 才为 1
  × reliability                # 历史成功率；无历史时用保守值
  × (0.5 + 0.3×throughput + 0.1×latency + 0.1×freshness)
  × subset_factor
```

排序默认 `objective="min_time"`：在 eligible routes 中优先选择由 bench 吞吐与延迟估算的
`expected_seconds`；没有 bench 的 route 不会被当成“零成本”。`balanced` 与 `reliability`
是同一批实测字段的替代排序，不是 ML 黑箱。

CDS 没有任何硬编码优先级。Case A 中 CDS=QUALIFIED、NCAR=DISCOVERED 时，CDS 可用，
但 NCAR 仍会出现在 `needs_probe`，结果不会写成“only CDS available”。

## 6. 大规模下载 policy

| 规模 | 策略 |
|---|---|
| small（≤1 GiB） | 已有 `QUALIFIED` route 可直接使用；未知 route 仍会先 probe |
| medium（1–100 GiB） | 优先比较已知 qualified routes；缺少 bench 的 route 不能靠猜速度获胜 |
| large（>100 GiB） | 必须有 `QUALIFIED` 且存在 benchmark；未 qualification 的 route 必须拒绝 |
| adapter 未实现 | 显示 `NOT_IMPLEMENTED`，绝不改成“数据不存在” |
| qualification 过期 | 标记 `EXPIRED`，下一次评估要求 probe |

阈值在 `RouteIntelligence` 构造参数中可覆盖；默认值不写进科学元数据。

## 7. Route experience registry

默认由组合根放到 `~/.rdh/routes.json`（`RouteIntelligence` 本身不会偷偷写用户目录）。
记录：

- `last_verified`
- `success_rate`
- `recent_failures`
- `benchmark`
- qualification 与状态

成功传输会更新经验；连续失败会先降为 `DEGRADED`，达到阈值后变为 `FAILED`。过期 route
不会复用旧的“已验证”结论。

## 8. CLI

```bash
rdh route plan examples/era5-request.yaml --json
rdh route evaluate examples/era5-request.yaml --no-probe
rdh route qualify examples/era5-request.yaml ncar-era5-pressure-levels-https
rdh route select examples/era5-request.yaml
```

`route plan` 不发网络请求；`route evaluate` 默认会调用 adapter health check，且可以注入
source-specific prober 完成 benchmark。`route qualify` 只记录低字节量探活结果；完整基准
仍应通过 `DefaultConnectivityProbe(benchmark=...)` 或项目自己的 probe 注入。默认 benchmark
探针上限为 **8 MiB**，超过会判失败，禁止把 GB 级数据当作探活。

## 9. 当前边界

- 默认 health check 只证明 endpoint 可达，**不会**伪造变量/时间/空间能力；真实 source-specific
  probe 仍需按平台补充。
- `rdh route` 的默认 prober 不下载科研文件；如果 adapter 的 health_check 本身不能证明科学
  能力，结果保持 `UNKNOWN`/`NOT_TESTED`，这是预期行为而不是失败。
- 远程 worker 的 `network.routes.RouteManager` 继续负责“哪个节点搬字节”；本模块负责
  “哪个 source/transport 能取这个数据集”。两者不能混用。

## 10. 本轮验证证据（2026-11-18）

- 静态检查：`ruff check src tests` rc=0；`mypy src/researchdatahub` 136 个源文件 0 错误。
- 测试：1767 个默认测试全通过（30 个 network 测试按项目约定默认 deselected）。
- ERA5 fixture：发现 5 条候选 route（NCAR S3/HTTPS/Globus/Zarr + CDS API），未因 CDS adapter 存在而收敛为单一路线。
- 真实低字节量探测：
  - GDEX `/api/datasets/d633000/groups/` → HTTP 200，3.5 KB，约 3.3 s；
  - CDS process schema → HTTP 200，6.6 KB，约 3.8 s（只读 metadata，不提交 CDS 作业）。
- NCAR 真实 route probe 返回 `PASS`，但 qualification 仍为 `DISCOVERED`：因为该探针只证明 endpoint/metadata/basic request，不提供变量覆盖与 benchmark；这正是“probe 可达 ≠ 可以大规模下载”的预期结果。
