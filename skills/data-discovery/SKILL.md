---
name: data-discovery
description: 把用户用自然语言描述的科学数据需求解析成结构化需求，并在 ResearchDataHub 中搜索、检视、比较候选数据集，产出对比表让用户确认。当用户提到要下载/采集 ERA5、再分析、云水、温度、风场等气候或地球科学数据，或问"有没有某个数据集""哪几个数据集能满足这个需求"时使用。
metadata:
  type: workflow
  version: "1.0.0"
  stage: "1/4"
  next-skill: environment-setup
---

# data-discovery —— 需求解析与候选数据集确认

> 本 Skill 是 ResearchDataHub 四段式采集流程的**第 1 段**：
> `data-discovery` → `environment-setup` → `data-acquisition` → `data-validation`
> 本段**只读**：只用 `search_datasets` / `inspect_dataset` / `compare_datasets` /
> `get_provider_capabilities` 四个工具，不落库、不下载。

## 1. 何时使用

**触发条件**（任一命中即应主动使用）：

- 用户说"我要/我需要/帮我下载 …… 数据"，且数据属于气候、气象、地球科学（ERA5、CRA-40、CMIP、
  再分析、卫星等）；
- 用户问"有没有能提供 X 变量的数据集""哪个数据集更合适"；
- 用户给了时间范围/区域/变量但没指定数据集，或指定了但想比较替代源。

**不适用**（转交别的 Skill 或拒绝）：

- 用户只要**解释概念**（"什么是云水路径"）→ 直接回答，不启动流程；
- 用户要**分析已有本地文件** → 不是采集需求；
- 用户要求**伪造/爬取绕过授权**的数据 → 拒绝并说明原因。

## 2. 前置条件

- MCP server `rdh-mcp` 已由宿主 Agent 连接（见 `docs/skills.md` 的安装章节）。
- 工作目录可写（后续阶段会把数据写到用户指定的 `output.path`）。
- **不要求**任何数据源凭证：本段只用只读工具，凭证问题留到 `environment-setup`。

## 3. 结构化需求：8 个字段（缺关键项必须追问）

把用户的话解析成下表。**标 ★ 的是关键科学参数——缺失时必须追问，禁止猜测、禁止用默认值蒙混。**

| # | 字段 | 契约位置（分段格式；MCP `request` 参数实际接受的形状） | 说明 | 缺失处理 |
|---|---|---|---|---|
| 1 | ★ 数据集/产品 | `dataset.name`（+ `dataset.provider` / `dataset.product_family`） | 如 `reanalysis-era5-pressure-levels`；不确定时先用 `search_datasets` 找候选 | 允许先留空让 Agent 走发现流程，但**候选表必须交用户确认** |
| 2 | ★ 变量 | `variables[].name` + `units` + `long_name` | **`variables[].name` 就是规划与路由使用的变量标识**；`units` 强烈建议显式写（不写则依赖平台默认值，属猜测） | 缺变量 → 必须问；变量名有歧义（"温度"=2m 气温还是气压层气温？）→ 必须问 |
| 3 | ★ 时间范围 | `temporal.start` / `.end` / `.frequency` | 三者**全必填**；只接受 UTC；**闭开区间 `[start, end)`**；`end` 为纯日期时展开到次日 00:00（含该日全天） | 缺起止时间 → 必须问；只说"近几年"→ 追问具体年份 |
| 4 | ★ 空间范围 | `spatial.bbox.{west,east,south,north,grid_degrees}` 或 `spatial.type: global` | **不要**写 `bounds: [W,S,E,N]` 数组（加载器会拒绝）；写 bbox 时四个方向字段缺一不可 | 缺区域 → 必须问；笼统说"中国区域"→ 必须确认到具体边界（如 `west:73, south:18, east:135, north:54`） |
| 5 | 垂直层 | `vertical.kind` / `.levels` / `.units` | `pressure` 的 `levels` 单位 hPa 且**严格递减**；不给 `vertical` 段 = 单层/地表 | 变量本身区分层（如气压层温度）→ 必须问层数 |
| 6 | 时间分辨率 | `temporal.frequency` | `hourly` / `daily` / `monthly` …（与区间长度是否匹配会被校验） | 缺省**不得**臆测；ERA5 原生小时，降采样需求要用户明确 |
| 7 | ★ 输出格式与路径 | `storage`（目前只支持 `preserve_original`）+ 项目的默认 staging/raw 目录 | 采集产物落 `data/raw`；原始文件默认保留不改写 | 用户给的路径若不在项目内，追问一次确认 |
| 8 | 校验要求 | `validation.{verify_size, calculate_sha256, netcdf_openable}` | **至少开一项**；全 `false` 会让校验状态成 `UNKNOWN`、任务永远无法 `COMPLETED` | 默认三项全开（推荐） |

> 补充：
> - `request.name` 是 `request_id`，建议填一个可读 slug（如 `era5-clouds-2015-2024`）；
> - `request.preferred_sources` / `max_sources` 表达"优先用哪个源"，对应本 Skill 的候选比较结论；
> - 单位权威映射可放在 `validate_request` 的 `units_catalog` 参数里，**不要**在需求里猜单位。

### 3.1 追问的写法（必须一次问完、不接受"你看着办"）

一次最多问 3 个问题，按"缺了就无法规划"排序；每个问题给**一个推荐值和理由**，让用户能一句话拍板：

```text
在继续之前需要确认 3 件事（其余我按推荐值处理）：
1) 变量的层：你要的是地表 2 m 气温，还是气压层气温？推荐：气压层 1000/850/500 hPa（云水通常按层分析）。
2) "中国区域"的具体边界：推荐 [73, 18, 135, 54]（西部含帕米尔、南部含曾母暗沙以北）。
3) 时间窗口：你说"2015—2024"，我按 [2015-01-01, 2025-01-01) 处理（含 2024-12-31 全天），对吗？
```

**禁止**：把不确定项写成"假设：……"然后直接往下推——采集是昂贵且可审计的动作，宁可多问一句。

## 4. 工具调用序列

```text
① search_datasets(query?, variables?, provider?, limit?)
② inspect_dataset(dataset_id, provider?)
③ compare_datasets(dataset_ids[2..8], dimensions?)      # 有 2 个以上候选时
④ get_provider_capabilities(provider?)                   # 用户关心"哪个源更快/更全"时
```

### 4.1 `search_datasets`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string ≤256 | 否 | 模糊关键词，匹配 Provider 名与逻辑数据集 ID；省略=不筛 |
| `variables` | string[] ≤64 | 否 | 要求包含的变量名（语义名，如 `["t","clwc"]`） |
| `provider` | string | 否 | 限定 Provider（`ncar` / `cds` …） |
| `limit` | int 1..200 | 否 | 默认 50 |

返回：`provider`、`dataset_id`、可访问路线、已实现状态。
**只读、不访问远端**——它回答的是"**配置里声明了哪些**"，不是"远端有没有数据"。

### 4.2 `inspect_dataset`（必填 `dataset_id`，可选 `provider`）

返回"哪些 Provider 提供它、可用访问路线、该 Provider 适配器的能力边界（verified / unverified）"。
读它时**必须向用户如实转述 unverified 项**，不得把"未验证"翻译成"支持"。

### 4.3 `compare_datasets`（必填 `dataset_ids` 2–8 个；可选 `dimensions`）

`dimensions` 取值：`variables` / `temporal_coverage` / `spatial_extent` / `vertical_levels` / `providers` / `grid`。

### 4.4 `get_provider_capabilities`（可选 `provider`）

返回每个适配器的**可用路线、verified/unverified 能力、缺失依赖名、缺失凭证名**。
**它只返回凭证名称，绝不返回值**——不要向用户索要值，也不要展示任何疑似密钥的字符串。

## 5. 输出给用户的模板（候选对比表 + 确认请求）

```markdown
## 需求解析结果

| 字段 | 值 |
|---|---|
| 数据集 | era5-pressure-levels（CDS）/ ERA5（NCAR） |
| 变量 | 云液态水 clwc、云冰水 ciwc、温度 t、风 u/v |
| 时间 | 2015-01-01 → 2025-01-01（UTC，闭开区间，逐时） |
| 区域 | [73, 18, 135, 54]（W,S,E,N） |
| 垂直层 | 气压 1000/850/500 hPa |
| 输出 | netcdf → ./data/raw |

## 候选数据集对比

| dataset_id | Provider | 变量覆盖 | 时间覆盖 | 空间覆盖 | 垂直层 | 路线 | 实测状态 |
|---|---|---|---|---|---|---|---|
| reanalysis-era5-pressure-levels | cds | ✅ clwc/ciwc/t/u/v | 1940→近实时 | 全球 0.25° | pressure | cds_api | 已验证（真实下载） |
| ds633.0 | ncar | ✅（整文件） | … | 全球 0.25° | pressure | https | 已验证（整文件） |

**建议**：小区域子集走 CDS（服务端生成、体积小）；需要整月全域原始文件走 NCAR。
**请确认**：用下面这个方案继续吗？（确认后我进入 environment-setup，检查凭证与依赖）
```

## 6. 失败分支与恢复动作（错误码 → Agent 该做什么）

| 错误码 | 含义 | Agent 动作 |
|---|---|---|
| `invalid_arguments` | 参数名/取值不符合 schema（顶层未知字段一律拒绝） | 按 `error.hint` 修正参数名与类型后重试；**不要**重试同一份参数 |
| `not_found` | 数据集/Provider 不存在 | 回到 `search_datasets` 重新发现，把结果给用户重选 |
| `not_available` | 依赖的可选包/组件未注入（如 `environment` / `providers` / `network` 包） | 看 `data.capability`；告诉用户"该能力当前不可用"，**不要**编造结果 |
| `config_error` | 配置文件或状态根有问题 | 提示用户检查 `rdh config validate` 与 `--root` 指向 |
| `source_not_found` | 数据源未在配置中声明 | 列出已声明 Provider，让用户二选一：换源 或 补配置 |
| `internal_error` | 服务端内部错误 | 不要重试；告知用户查看 stderr 日志并向维护者报告 |

其余错误码见 `data-acquisition` 的完整错误码表。

## 7. 禁止事项

1. **禁止猜测关键科学参数**（变量、时间、区域、层）——缺失就追问。
2. **禁止把 `unverified` 说成 `supported`**：`inspect_dataset` / `get_provider_capabilities` 里的
   unverified 项必须原样转述。
3. **禁止索要、打印、上传任何密码/token/API Key**；本 Skill 不需要任何凭证。
4. **禁止在本段调用 `submit_acquisition`** —— 那是 `data-acquisition` 的职责。
5. 禁止把 CDS 的 `[N,W,S,E]` 顺序写进 `spatial.bounds`。

## 8. 黄金路径（真实需求，跨 4 个 Skill，本文只覆盖到第 2 步）

> 用户原话："我需要 2015—2024 年中国区域 ERA5 小时数据，变量包括云液态水、云冰水、温度和风场，保存到当前项目的 data/raw"

| 步 | 工具 | 传什么 | 期望返回 | 用户看到什么 |
|---|---|---|---|---|
| 1 | `search_datasets` | `{"query":"era5","variables":["clwc","ciwc","t"]}` | 候选数据集清单（cds: `reanalysis-era5-pressure-levels`；ncar: `ds633.0` 等） | 「找到 2 个候选源」 |
| 2 | `inspect_dataset` | `{"dataset_id":"reanalysis-era5-pressure-levels"}` | 变量/时间/空间覆盖、路线 `cds_api`、verified 标记 | 候选对比表 |
| 3 | `compare_datasets` | `{"dataset_ids":["reanalysis-era5-pressure-levels","ds633.0"],"dimensions":["variables","temporal_coverage","vertical_levels","providers"]}` | 逐维度对比 | 对比表 + 建议 |
| 4 | `get_provider_capabilities` | `{"provider":"cds"}` | 路线可用性、缺失依赖/凭证**名称** | 「CDS 需要 CDSAPI_KEY，稍后引导你配置」 |
| 5 | — | 向用户确认 8 个字段 + 方案 | 用户回"确认" | 确认框 |

确认后转入 `environment-setup`（第 6–8 步）与 `data-acquisition`（第 9–10 步）。
完整的 10 步调用序列见 `skills/data-acquisition/SKILL.md` §9。
