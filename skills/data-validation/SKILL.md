---
name: data-validation
description: 在 ResearchDataHub 采集完成后逐文件校验交付物（存在性、大小、格式、SHA-256、文件数、元数据、时间与空间覆盖、请求与交付一致性），生成可审计的交付清单并如实报告结论。当采集任务结束、用户问"下下来的文件完整吗/能不能用"，或需要交付清单与校验报告时使用。
metadata:
  type: workflow
  version: "1.0.0"
  stage: "4/4"
  next-skill: none
---

# data-validation —— 完整性校验与交付清单

> 四段式流程的**最后一段**：`data-discovery` → `environment-setup` → `data-acquisition` → `data-validation`
> 核心口径：**未验证 ≠ 通过**。凡是没真正检查到的项，必须标 `unverified`，不得写进"通过"。

## 1. 何时使用

- `get_task_status` 中相关任务已进入终态（`COMPLETED` / `FAILED` / `QUARANTINED` / `CANCELLED` / `SKIPPED`）；
- 用户问「文件完整吗 / 能用来分析吗 / 给我一份清单」；
- 用户要归档、投稿、给别人复现数据。

**不适用**：任务还在跑（先回 `data-acquisition` 轮询）；只要下载不要校验（用户明确说"我只要文件"时也要**至少**跑一遍 `verify_dataset`，因为这是本项目交付定义的一部分）。

## 2. 前置条件

- 有 `request_id` 或 `plan_id`（来自 `submit_acquisition` 的返回，或 `get_execution_plan`）；
- 文件已从 staging 原子提交到 raw 区（`COMPLETED` 状态才保证这一点）。

## 3. 工具调用序列

```text
① verify_dataset(paths, request_id?)          # 逐文件完整性校验（必填 paths）
② get_manifest(request_id | plan_id, output_path?, verify_checksums?)   # 交付清单
```

### 3.1 `verify_dataset`

| 参数 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `paths` | ✅ | string[] 1–200 | 要校验的文件路径（支持 `~`，相对路径以状态根为基准） |
| `request_id` | — | string | 用该请求的**交付清单**核对大小与 SHA-256 |

返回：

```json
{
  "files": [
    {
      "path": "...", "verified": true, "status": "passed",
      "size_bytes": 123456, "checks": [ ... ], "failures": [],
      "skipped_checks": [ ... ], "expected_source": "delivery_manifest",
      "manifest_issues": [ ... ], "unverified_reason": null
    }
  ],
  "total": 120, "verified": 118, "unverified": 2,
  "request_id": "req-...", "manifest_available": true, "manifest_note": "...",
  "policy": "SKIPPED/UNKNOWN are never counted as verified"
}
```

**读结果的规则**：

- `verified:true` **只**等于 `status == "passed"` 且没有 manifest 问题；
- `status` 为 `skipped` / `unknown` / `missing` / `unavailable` 一律计入 `unverified`；
- `skipped_checks[]` 里出现的检查项（如"没有原始 checksum 可比对"）**必须**原样告诉用户；
- `manifest_issues[]` 非空表示"文件与清单对不上"（大小/SHA-256 不一致），**比 failed 更严重**。

### 3.2 `get_manifest`

| 参数 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `request_id` | 二选一 | string | 请求 ID |
| `plan_id` | 二选一 | string | 计划 ID（内部解析出 request_id） |
| `output_path` | — | string | 把清单写成 JSON 到这个路径；**目标已存在 → `conflict`，不覆盖** |
| `verify_checksums` | — | bool | 写文件/返回前逐文件重算 SHA-256，默认 `false`（只核对大小） |

返回：`delivery_id`、`request_id`、`plan_id`、`created_at`、`tool_version`、`complete`、`total_bytes`、`notes`、
`manifest_path`（未写文件时为 `null`）、`files[]`、`integrity`：

- `files[]` 每项：`file_id`、`task_id`、`dataset_id`、`provider`、`worker`、`route`、`origin_uri`、`path`、
  `size_bytes`、`sha256`、`validation_status`、`verified`、`report_id`、`disk_issues[]`、`retrieved_at`；
- `integrity`：`verified_files`、`unverified_files`、`consistency_issues[]`、`disk_verification`；
- `complete` = 清单完整 **且** 校验 ok。

> 若要"交付一份可归档的清单"，用 `output_path` 指定路径（例如 `./data/raw/delivery-<request_id>.json`），
> 一次成功；若返回 `conflict`，**不要**强行覆盖，改用带时间戳的新文件名并告诉用户。

## 4. 校验维度（八项，逐项给结论）

| # | 维度 | 数据来源 | 未通过时怎么说 |
|---|---|---|---|
| 1 | 文件存在 | `files[].status` = `missing` | 「文件不存在，可能任务未完成或被清理」 |
| 2 | 文件大小 | `size_bytes` vs 清单 | 「大小与清单不一致」 |
| 3 | 格式 | `checks[]` 里的格式嗅探/可打开检查 | 「格式嗅探失败/无法打开」 |
| 4 | SHA-256 | `sha256` vs 清单 | 「校验和不一致 → 内容与清单不是同一份」 |
| 5 | 文件数 | `total` vs 计划 `task_count` / 清单条数 | 「交付 N 个，计划 M 个，差 K 个」 |
| 6 | 元数据 | `validation.metadata` 检查项 | 「元数据缺失/不可读」 |
| 7 | 时间覆盖 | `validation.coverage` | 「时间覆盖不完整（缺某段）」 |
| 8 | 空间覆盖 | `validation.coverage` | 「空间覆盖不完整（区域/网格不符）」 |
| + | 请求与交付一致性 | `manifest_issues[]` / `consistency_issues[]` | 「请求 X，交付 Y，不一致」 |

**任何一项没检查到 → 写 `unverified` 并说明原因，不写"通过"。**

## 5. 输出给用户的模板（交付结论）

```markdown
## 交付结果

**结论：部分通过（118/120 文件通过，2 个未验证）**

| 维度 | 结果 | 证据 |
|---|---|---|
| 文件存在 | ✅ 120/120 | `files[].status` |
| 大小 | ✅ 118/118 有清单的文件一致 | `size_bytes` |
| SHA-256 | ✅ 118 个重算一致 | `sha256` |
| 格式 | ✅ 全部可打开（NetCDF） | `checks[]` |
| 文件数 | ✅ 120 = 计划 120 | `total` |
| 元数据 | ⚠️ 未验证（`validation.metadata` 未启用） | `skipped_checks` |
| 时间覆盖 | ⚠️ 未验证（未做覆盖检查） | `skipped_checks` |
| 空间覆盖 | ⚠️ 未验证（未做覆盖检查） | `skipped_checks` |
| 请求/交付一致性 | ✅ 无 manifest_issues | `integrity.consistency_issues` |

**未验证的 2 个文件**
- `data/raw/era5-2019-03-clwc.nc` — `status: unknown`，原因：该 Provider 不提供远端 checksum
- `data/raw/era5-2021-07-ciwc.nc` — `status: skipped`，原因：格式检查依赖缺失（`netCDF4` 未安装）

**交付文件**
- 目录：`./data/raw`
- 交付清单：`./data/raw/delivery-req-xxxx.json`（`delivery_id`、`plan_id`、每文件 sha256）
- 清单 JSON 路径：`<manifest_path>`

**任务摘要**：plan-7f3a…，120 个任务，COMPLETED 118 / FAILED 2（`transport_error`，重试 3 次耗尽）
```

**结论三态，不许含糊**：

- **通过**：`verified == total` 且 `complete == true`；
- **部分通过**：部分文件通过，其余**逐条**列出文件名 + `status` + 原因；
- **未通过**：0 个文件通过，或存在 `manifest_issues`（内容与清单不符）。

## 6. 失败分支与恢复动作

| 情况 | 错误码 | Agent 动作 |
|---|---|---|
| 参数缺 `request_id`/`plan_id` 或同时给两个 | `invalid_arguments` | 二选一重传 |
| request/plan 无交付文件 | `not_found` | 确认 ID；可能是任务还没 COMPLETED → 回 `get_task_status` |
| `output_path` 已存在 | `conflict` | 换带时间戳的文件名；**不要**覆盖 |
| `verify_dataset` 返回 `status: unavailable` + `unverified_reason` | （在 `files[]` 内，非顶层错误） | 如实转述原因；可能缺 NetCDF 依赖 → 回 `environment-setup` 补依赖后重验 |
| `manifest_issues` 非空 | — | 这是**内容不一致**：告知用户"文件与清单不是同一份"，建议重新采集该文件（`cancel_task` 不适用，需重跑计划） |
| 有文件 `QUARANTINED` | — | 说明已被隔离到 quarantine，**不要**从 raw 目录引用它 |
| 顶层 `integrity_error` | `integrity_error` | 不要当成通过；报告"完整性检查无法进行" |
| `internal_error` | `internal_error` | 停止重试，报告维护者 |

## 7. 禁止事项

1. **禁止把 `skipped` / `unknown` / `missing` 说成"通过"**（MCP 层的 policy 是 `SKIPPED/UNKNOWN are never counted as verified`）。
2. **禁止省略未验证项**——用户看的必须是全表，不是"好消息摘要"。
3. **禁止覆盖已存在的清单文件**（`get_manifest` 会返回 `conflict`，不要绕过它）。
4. **禁止伪造 `sha256` 或 `size_bytes`**；没有值就写"未获取"。
5. 不得把 `FAILED` 任务的文件放进"交付文件"列表。

## 8. 黄金路径中属于本 Skill 的步骤（第 10 步后半）

> 承接 `data-acquisition` 第 10 步：`submit_acquisition` 返回 `task_ids`，`get_task_status` 显示 120 个任务已终态。

| 步 | 工具 | 传什么 | 期望返回 | 用户看到 |
|---|---|---|---|---|
| 10a | `get_task_status` | `{"task_ids": [...]}`（来自 submit 或 `get_execution_plan`） | 每任务 `state` + `file_count` | 采集结束摘要 |
| 10b | `verify_dataset` | `{"paths": ["data/raw/era5-2015-01-clwc.nc", "…"], "request_id": "era5-clouds-2015-2024"}` | `total`/`verified`/`unverified` + 每文件 `status` | 校验维度表 |
| 10c | `get_manifest` | `{"request_id": "era5-clouds-2015-2024", "output_path": "./data/raw/delivery-era5-clouds-2015-2024.json", "verify_checksums": true}` | `complete`、`files[]`、`integrity`、`manifest_path` | **交付清单 + 结论** |

最终交付给用户的三样东西（**缺一不可**）：

1. **交付文件路径**（目录 + 文件数）；
2. **Manifest 路径**（`manifest_path`；若用户没要求落盘，就说明"清单在服务端 `get_manifest` 可随时重取"）；
3. **校验结论**（通过 / 部分通过 / 未通过 + 未验证项清单）；
4. （附加）**任务摘要**（plan_id、任务数、各状态计数、失败原因）。
