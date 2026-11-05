# ADR-0012：版本化数据请求契约 v1.2（坐标顺序 / 日期边界 / 时区 / 频率 / 原生变量映射）

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

指令 §10 要求建立独立、版本化的 Data Request Schema，并明确规定：

- 经纬度坐标顺序；
- 日期边界语义；
- 时区；
- 时间分辨率语义；
- 变量语义；
- Provider 原生变量映射；
- 数据格式；
- 目标存储语义。

并强调："统一变量名称只是语义标识，不是数据源真实变量 ID。Provider 必须负责真实映射。"

现状：`planner/request.py` 已实现一个成熟的 YAML 加载器（923 行，含 `文件:行` 错误定位、
`extra="forbid"` 未知字段拒绝、`units_catalog` 单位来源标注），但它有缺口：

1. 没有顶层 `schema_version` 的**强制与校验**（契约版本化只在 Pydantic 模型的 `schema_version` 字段）；
2. `temporal.end` 的"日期型→次日零点"语义只写在 docstring，**没有机读的显式声明字段**；
3. 变量只有单一名字，**没有"语义变量名 → Provider 原生变量 ID"的显式映射槽位**；
4. `frequency` 支持 `hourly/daily/monthly`，但没有独立的枚举契约（散落在 `_FREQUENCY_STEP_SECONDS` 字典）。

## 决策

新增 `src/researchdatahub/planner/request_v12.py`，定义**显式、可校验、可序列化**的请求契约 v1.2，
与既有 `request.py` 加载器**共存**（不替换，`request.py` 继续服务 CLI 与既有 979 个测试）：

```
schema_version: "1.2"
request_id: "request_001"            # 可选，缺省由 Core 生成
dataset:
  provider: auto                     # auto | cds | ncar | earthdata | copernicus | gee
  dataset_id: auto_discover          # auto_discover 时由 M3 search 决定
  product_family: ERA5               # 可选，人类可读
selection:
  variables:                         # 语义变量名（CF 风格 snake_case），不是平台变量 ID
    - name: cloud_liquid_water
      units: kg m**-2               # 语义单位；Provider 映射时必须校验真实单位能否转换
      provider_native:               # 可选，显式映射槽；缺省由 Provider 负责映射
        cds: clwc
  temporal:
    start: "2015-01-01"
    end: "2024-12-31"
    frequency: hourly                # 枚举见下
    end_semantics: exclusive_next_day # 显式声明 end 的解释规则
    time_zone: UTC                   # 仅允许 UTC（见下）
  spatial:
    type: bounding_box
    bounds: [73, 18, 135, 54]        # 严格顺序 W, S, E, N（西, 南, 东, 北）
    grid_degrees: null
  vertical:
    kind: none | pressure | model_level | height
    levels: []
output:
  format: netcdf                     # 只声明格式，不做转换
  destination: local | remote_worker | object_store
  path: "./data/raw"
execution:
  mode: automatic | preview_only
  max_retries: 3
validation:
  checksum: true
  metadata: true
  coverage: true
```

### 语义规定（不可推断，必须机读）

| 项 | 规定 | 理由 |
|---|---|---|
| **坐标顺序** | `bounds` 固定为 `[west, south, east, north]`，即 `[-180,180]` 经度在前、`[-90,90]` 纬度在后 | 与 CDS `area=[N,W,S,E]` **不同**；这一对是最常见的事故来源，必须在契约里钉死并在 Provider 翻译时显式转序 |
| **日期边界** | 闭开区间 `[start, end)`；`end` 为纯日期时解释为**次日 UTC 00:00**，因此 `2015-01-01`..`2024-12-31` **包含** 2024-12-31 全天 | 若按闭区间解释，最后一天的数据会静默丢失 |
| **时区** | 只接受 UTC；带偏移的时间立即规整为 UTC；`time_zone` 字段非 `UTC` 直接拒绝 | 科研数据时间定义必须显式，且不接受本地时区 |
| **频率** | 枚举 `hourly / 3_hourly / 6_hourly / daily / monthly`；`frequency` 与区间长度必须相容（例如 `hourly` 不允许小于 1 小时的区间） | 防止"要逐时但要了一分钟区间"这类错误 |
| **变量** | `name` 是**语义名**（CF 风格），不是平台变量 ID；两者用 `provider_native` 显式桥接，未提供时由 Provider 内置映射表负责，**映射失败必须报错，不得静默丢弃** | 指令 §10 "统一变量名称只是语义标识" |
| **单位** | 变量带 `units`；Provider 若返回不同但可转换的单位（如 `K`↔`degC`），必须在返回的计划里显式标注转换，不得静默改数 | 静默改单位是数据事故 |
| **格式** | `format` 只声明期望的交付格式，**不承诺转换**；Provider 不支持该格式时必须显式报 `format_unsupported`，绝不偷偷换成另一个格式 | 指令 §17.5 |
| **存储** | `destination=local` 时 `path` 是相对调用方 cwd 或绝对路径；**默认禁止静默覆盖**（见 ADR-0015 待补），冲突返回 `conflict` 由用户决定 | 指令 §13 |

### 与既有 `request.py` 的关系

`request_v12.py` 负责**契约定义与校验**（纯 Pydantic，无 IO、无网络）；
`request.py` 负责**加载既有 YAML 格式**并保持向后兼容。
两者最终都产出 `core.models.DatasetRequest`（同一个执行契约对象），
`request_v12.render_to_dataset_request()` 是唯一转换入口，并有 round-trip 测试。

## 后果

**正面**

- 坐标顺序、日期边界、时区这三类"静默错数据"风险被机读契约挡住；
- 变量语义名与平台原生 ID 有显式桥接槽位，Provider 映射可测试、可审计；
- 版本化后可以演进（v1.3 加字段不破坏 v1.2 的读取），符合指令 §10。

**负面**

- 仓库里有两个请求加载器（`request.py` 与 `request_v12.py`），需要文档明确各自适用场景；
- Provider 必须实现语义名→原生 ID 映射表，否则 `variables` 全部报错（这是刻意的：宁可报错不可猜测）。

## 被否决的方案

1. **直接把 `request.py` 重写成 v1.2**：会破坏 979 个测试中依赖既有 YAML 格式的用例，
   且既有加载器的 `文件:行` 错误定位质量很高，重写风险大于收益。**否决**。
2. **沿用 `bounds: [minx, miny, maxx, maxy]` 但文档里写清楚**：仅靠文档约束不足，
   Provider 翻译时仍需人工核对。改为在 schema 校验层增加 `coordinate_order: "W,S,E,N"` 常量并在测试中断言转序。**采纳并加强**。
3. **允许局部时区**：会导致跨数据集拼接时的 1 小时错位。**否决**。
