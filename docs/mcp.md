# MCP 接口层（M5）：面向 AI Coding Agent 的唯一正式执行接口

> 项目定位：ResearchDataHub 是**面向 AI Coding Agent 的科研数据获取基础设施**。
> Core（`planner` / `scheduler` / `manifest` / `integrity` / `reporting` / `transfer`）**不依赖任何 LLM**，
> MCP Server 是它们对外暴露的唯一正式执行接口；业务逻辑一行都不在 MCP handler 里。

| 项目 | 值 |
| --- | --- |
| 协议 | MCP over stdio（JSON-RPC 2.0，官方 Python SDK `mcp>=1.2`） |
| 工具数 | 18（只读 12、只计算 2、写状态 2、需审批 1、可写文件 1） |
| 信封版本 | `meta.schema_version = "1.0"` |
| 代码位置 | `src/researchdatahub/interfaces/mcp/` |
| 启动 | `rdh-mcp` 或 `python -m researchdatahub.interfaces.mcp.server --root <状态根>` |

---

## 1. 启动方式

### 1.1 `rdh-mcp`（安装后可用，stdio）

```bash
pip install -e '.[mcp]'      # 装 MCP SDK（可选依赖，缺席时服务器报结构化错误而不是崩溃）
rdh profile add rdh --command rdh-mcp   # 以 Claude Code 为例，把本服务注册为 MCP server
```

### 1.2 `python -m`（开发态，支持 `--root`）

```bash
# 先初始化 Manifest（MCP 不替你建库：没有库会明确报 config_error）
python -m researchdatahub.cli.main db init --root ~/.rdh
# 启动
PYTHONPATH=src python -m researchdatahub.interfaces.mcp.server --root ~/.rdh
```

### 1.3 全部命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--root` | `defaults.storage.root`（`~/.rdh`） | 状态根：Manifest 库、数据区、审批密钥都在其下 |
| `--config` | `configs/defaults.yaml` | 全局配置路径 |
| `--sources` | `configs/sources.yaml` → 回落 `sources.example.yaml` | 数据源声明 |
| `--approval-key` | `$RDH_APPROVAL_KEY_FILE` → `<root>/approval.key` | 审批令牌 HMAC 密钥路径 |
| `--log-level` | `INFO` | **日志只写 stderr** |
| `--print-tools` | — | 把 `tools/list` 的 JSON 打到 stdout 后退出（调试用） |
| `--version` | — | 版本与信封版本 |

**stdout 纪律**：stdout 只允许 MCP 协议帧。日志走 `stderr`；SDK 的 stdio 传输在服务期间接管
fd 1，并把进程内**任何**杂散 `print` 导向 stderr（我们*不*自己重定向——见下文"踩坑记录"）。

---

## 2. 返回信封（所有工具统一）

```json
{
  "ok": true,
  "data": { "...": "工具特有内容" },
  "error": null,
  "meta": { "tool": "create_execution_plan", "schema_version": "1.0", "duration_ms": 3.412 }
}
```

失败时：

```json
{
  "ok": false,
  "data": {},
  "error": {
    "code": "approval_invalid",
    "message": "approval token rejected: approval token has expired",
    "hint": "重新调用 create_execution_plan 拿新的 approval_token，并在过期前提交。",
    "retryable": false,
    "detail": { "plan_id": "plan-...", "reason": "approval token has expired", "side_effects_performed": false }
  },
  "meta": { "tool": "submit_acquisition", "schema_version": "1.0", "duration_ms": 1.02 }
}
```

要点：

* **业务失败也是正常响应**（`ok=false`），不是协议错误 —— Agent 可以按 `error.code` 分支；
* 未知异常一律 `internal_error`，message 固定文案，**不含堆栈、不含 `str(exc)`**（可能泄露密钥/路径）；
* `error.detail` 里键名含 `token`/`secret`/`password`/`api_key`/`credential`/`signature` 等片段的值一律替换为 `"[redacted]"`。

---

## 3. 18 个工具

**副作用等级**（同时写进 `tools/list` 的 `annotations`）：

| 等级 | 含义 | 工具 |
| --- | --- | --- |
| `read_only` | 无副作用，可任意/并行调用 | 12 个 |
| `computes` | 只计算不落库 | `validate_request`、`estimate_acquisition` |
| `mutates_state` | 写 Manifest / SQLite / 文件系统 | `create_execution_plan`、`cancel_task`、`get_manifest` |
| `requires_approval` | 必须携带审批令牌才执行 | `submit_acquisition` |

### 3.1 目录与能力（只读）

| 工具 | 入参摘要 | 说明 |
| --- | --- | --- |
| `search_datasets` | `query?` string · `variables?` string[] · `provider?` string · `limit?` int(1–200, 默认 50) | 搜索**配置中已声明**的逻辑数据集；不访问远端 |
| `inspect_dataset` | `dataset_id` **必填** string · `provider?` string | 单数据集详情 + 每个 Provider 的适配器能力边界 |
| `compare_datasets` | `dataset_ids` **必填** string[2–8] · `dimensions?` enum[]（`variables`/`temporal_coverage`/`spatial_extent`/`vertical_levels`/`providers`/`grid`） | 多维比较；配置层没有权威值的维度如实返回 `null` |
| `get_provider_capabilities` | `provider?` string | 路线、已实现/未实现、能力边界、可选包可用性；**绝不返回凭证值** |
| `get_route_status` | `dataset_id?` string · `probe?` bool（默认 false） · `timeout_seconds?` number | 路线可用性；`probe=true` 时对 https 端点做真实 HEAD |

### 3.2 环境（只读；接 `researchdatahub.environment`，M2）

| 工具 | 入参摘要 | 说明 |
| --- | --- | --- |
| `get_environment_requirements` | `provider` **必填** string | 只读声明：依赖 / 凭据引用 / 端点 + 可操作配置步骤 |
| `check_environment` | `provider` **必填** · `checks?` enum[]（`dependencies`/`credentials`/`network`/`disk`） · `timeout_seconds?` number | **真实探测**（只读 HEAD/GET，不下载数据） |
| `validate_environment` | `provider` **必填** · `require_network?` bool · `checks?` enum[] · `timeout_seconds?` number | 折叠成 `usable` + `blocking[]`；`require_network=true` 时联网未通过即不可用 |

判定口径：只有 M2 的 `OverallStatus.ok` 才算可用；`needs_configuration` / `unavailable` 都不是通过。

### 3.3 规划（`validate_request` / `estimate_acquisition` 不落库）

| 工具 | 入参摘要 | 说明 |
| --- | --- | --- |
| `validate_request` | `request?` object 或 `request_path?` string（二选一） · `units_catalog?` object | 校验并规范化需求，返回 `warnings` 与可满足性预判 |
| `estimate_acquisition` | 同上 + `split_granularity?` enum（`auto`/`file`/`year`/`month`/`request`） | 任务数、按源/按路线分布、总字节；**不落库** |
| `create_execution_plan` | 同上 + `approval_ttl_seconds?` int(60–86400，默认 3600) | **落库**请求+计划+任务，返回 `plan_id` 与一次性 `approval_token` |
| `get_execution_plan` | `plan_id` **必填** string | 读计划、任务状态与审批状态（SQLite，跨会话可查） |

`request` 支持两种形态：分段格式（`request`/`dataset`/`variables`/`temporal`/`spatial`/`vertical`/`storage`/`validation`）
与扁平 `DatasetRequest` 格式。解析、单位处理、未知字段拒绝全部由 `planner.request` 完成，MCP 不重复实现。

### 3.4 执行与任务控制

| 工具 | 入参摘要 | 说明 |
| --- | --- | --- |
| `submit_acquisition` | `plan_id` **必填** · `approval_token` **必填** · `max_workers?` int(1–8) · `max_tasks?` int | **唯一真正执行入口**；令牌校验通过后把任务推进到 `QUEUED`；带 `max_workers` 时同步执行 |
| `get_task_status` | `task_ids` **必填** string[] | 批量查状态、尝试次数、租约/心跳、最近事件、文件数、最新校验结论 |
| `cancel_task` | `task_ids` **必填** string[] · `reason?` string | 任意非终态 → `CANCELLED`（经状态机 + 乐观并发）；终态逐条返回 `skipped` |
| `list_workers` | `status?` enum（`healthy`/`degraded`/`offline`） · `limit?` int | 接 `researchdatahub.network` 的 `WorkerRegistry`（`<状态根>/workers.db`） |

### 3.5 校验与交付

| 工具 | 入参摘要 | 说明 |
| --- | --- | --- |
| `verify_dataset` | `paths` **必填** string[] · `request_id?` string | 逐文件 `verified` / `status` / `unverified_reason`；**`SKIPPED`/`UNKNOWN` 绝不算通过** |
| `get_manifest` | `request_id?` 或 `plan_id?`（二选一） · `output_path?` string · `verify_checksums?` bool | 交付清单（路径/大小/sha256/provider/worker/route/校验结论）；写文件时**目标已存在返回 `conflict`，不覆盖** |

---

## 4. 错误码表

`code` 是稳定契约（全小写下划线），与 Core 的 `RDH_*` 内部码分属两个命名空间，
映射见 `interfaces/mcp/errors.py::CORE_CODE_TO_MCP_CODE`。

| code | 触发场景 | retryable | Core 对应 |
| --- | --- | --- | --- |
| `invalid_arguments` | 参数缺失/类型错/未知字段/枚举越界；需求 YAML 校验失败 | ✗ | `RDH_REQUEST` |
| `not_found` | plan / task / dataset / provider / 可交付请求不存在 | ✗ | —（接口层） |
| `conflict` | `get_manifest` 的 `output_path` 已存在 | ✗ | —（接口层） |
| `approval_required` | 审批子系统未装配，拒绝执行 | ✗ | —（接口层） |
| `approval_invalid` | 令牌缺失/伪造/过期/已被消费/与计划绑定的指纹不符 | ✗ | —（接口层） |
| `invalid_state` | 当前状态不允许该动作（如执行所需后端未装配） | ✗ | `RDH_STATE_TRANSITION` |
| `not_available` | 可选包（environment/network）不可用 | ✗ | —（接口层） |
| `not_implemented` | 声明了但未实现（服务端 bug） | ✗ | `RDH_NOT_IMPLEMENTED` |
| `config_error` | 配置文件缺失/校验失败、Manifest 库未初始化 | ✗ | `RDH_CONFIG` |
| `model_invalid` | 数据契约校验失败 | ✗ | `RDH_MODEL_INVALID` |
| `source_not_found` | 没有源能满足需求 | ✗ | `RDH_SOURCE_NOT_FOUND` |
| `source_unavailable` | 数据源维护/配额/认证失败 | ✓ | `RDH_SOURCE` / `RDH_SOURCE_UNAVAILABLE` |
| `metadata_error` | 远端元数据与声明不一致 | ✗ | `RDH_METADATA` |
| `storage_error` | 磁盘满、权限、原子移动失败 | ✗ | `RDH_STORAGE` |
| `manifest_error` | SQLite 操作失败（含并发写冲突） | ✓ | `RDH_MANIFEST` |
| `schedule_error` | 计划规模超限、调度约束不满足 | ✗ | `RDH_SCHEDULE` |
| `integrity_error` | 完整性检查**无法进行**（缺失/不可读/目录） | ✗ | `RDH_INTEGRITY` |
| `transport_error` | 传输失败（可重试性取自 Core 的 `retryable`） | ✓/✗ | `RDH_TRANSPORT*` |
| `internal_error` | 未预期异常；文案固定、不含堆栈 | ✗ | 未登记的任何码 |

> 未登记进映射表的 Core 错误码会落到 `internal_error` —— 这是刻意的"安全失败"，
> 并且有单测（`tests/unit/test_mcp/test_errors.py`）守护"每个 Core 错误类都有映射"。

---

## 5. 副作用授权：审批令牌设计

### 5.1 为什么需要两阶段

MCP 面向的是**会误操作**的 AI Agent：一次 `submit_acquisition` 就会真的消耗带宽与磁盘。
因此设计成 `create_execution_plan`（出具令牌）→ `submit_acquisition`（出示令牌执行）两步，
让"规划"与"执行"之间有一次显式确认。

### 5.2 令牌格式

```
v1.<plan_id>.<expires_at_epoch>.<fingerprint_hex>.<nonce_hex>.<hmac_hex>
```

| 段 | 作用 |
| --- | --- |
| `v1` | 令牌版本；换算法时整体失效，无需猜测 |
| `plan_id` | 绑定的计划 |
| `expires_at_epoch` | 过期时刻（UTC epoch 秒），**参与签名** → 改大无效 |
| `fingerprint_hex` | 计划指纹：`sha256(规范化 JSON(model_dump))` 前 16 hex，**键排序** → 对 SQLite 往返不敏感，对内容敏感 |
| `nonce_hex` | 每次签发 16 字节随机数，**参与签名** → 同一计划重复签发必得不同令牌，旧令牌立即失效 |
| `hmac_hex` | `HMAC-SHA256(key, "v1\n<plan_id>\n<expires>\n<fingerprint>\n<nonce>")` |

验签用 `hmac.compare_digest`（常数时间），避免计时侧信道。

### 5.3 密钥

| 项 | 值 |
| --- | --- |
| 默认位置 | `<状态根>/approval.key`，默认即 `~/.rdh/approval.key` |
| 覆盖 | `--approval-key` 参数 > 环境变量 `RDH_APPROVAL_KEY_FILE` > 默认 |
| 文件权限 | **0600**；父目录 **0700**；启动时发现更宽自动收窄并记 WARNING |
| 生成 | 缺失时 `secrets.token_hex(32)`，以 `O_CREAT|O_EXCL|0600` **原子创建**（并发首启不会互相覆盖） |
| 过短 | < 32 字节直接拒绝启动（不静默补全） |
| 外泄防护 | 模块**没有任何**函数返回密钥内容；日志只打印路径；`repr()` 不含密钥 |

### 5.4 校验链（`submit_acquisition`）

按顺序，任一步失败都**不产生任何副作用**（不入队、不改状态、不消费令牌）：

1. `plan_id` 存在 → 否则 `not_found`；
2. 计划**已提交过**（任一任务非 `PLANNED`）→ 幂等返回既有任务组（不再次消费令牌）；
3. 取库中计划指纹，与令牌绑定指纹比对；
4. 审批记录存在且 `status=approved`（非 `consumed`/`expired`/`revoked`）；
5. HMAC 验签通过；
6. 未过期（`expires_at > now`）；
7. 记录里的 `token_digest`（令牌 sha256）与出示令牌一致；
8. **原子消费**：`UPDATE ... WHERE status='approved' AND remaining_uses>0 AND token_digest=?`
   —— 并发提交只有一个能 `rowcount==1`；
9. 全部任务经状态机 `PLANNED → QUEUED`，逐任务独立事务 + 乐观并发（`expected_state`）。

任一步失败 → `approval_invalid`，`detail.side_effects_performed = false`。

### 5.5 防重放与防重复提交

| 威胁 | 防护 |
| --- | --- |
| 令牌被重放 | 一次性：消费后 `status=consumed`，再次出示 → `approval_invalid` |
| 并发重放 | 第 8 步条件更新原子竞争，只有一个赢家 |
| 令牌跨计划使用 | `plan_id` 直接写在令牌里且参与签名 |
| 计划签发后被改 | 指纹绑定；任何字段（含 `extensions`）变化 → 旧令牌失效 |
| 重复提交同一计划 | 第 2 步幂等返回既有任务组（`duplicate=true`，`queued_tasks=0`） |
| 篡改过期时间 | `expires_at` 参与 HMAC |

### 5.6 审批记录的存放

`mcp_approvals` 表由 `interfaces/mcp/auth.py` 用 `CREATE TABLE IF NOT EXISTS` **自建**，
位于**同一个 Manifest SQLite 文件**：与任务状态共享 WAL 事务语义，但**不占用 Manifest 迁移编号**
（`core/models.py` 的契约是只读 SSOT，M5 无权给它加"审批状态"字段；审批是接口层概念）。

```sql
CREATE TABLE mcp_approvals (
    plan_id        TEXT PRIMARY KEY,
    fingerprint    TEXT NOT NULL,
    status         TEXT NOT NULL,          -- approved | consumed | expired | revoked
    issued_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    remaining_uses INTEGER NOT NULL DEFAULT 1,
    token_digest   TEXT NOT NULL,          -- sha256(令牌)，不可反推令牌
    consumed_at    TEXT,
    task_group_id  TEXT,
    note           TEXT
);
```

**令牌本身从不落库**：只在 `create_execution_plan` 的响应里出现一次；库里只存 sha256 摘要。

---

## 6. 依赖状态（三个并行包）

| 包 | 状态 | MCP 侧接法 |
| --- | --- | --- |
| `researchdatahub.environment`（M2） | ✅ 已落地 | `get_environment_requirements` / `check_environment` / `validate_environment` 直接调用 `get_requirements` / `validate_environment`；`usable` 以 `OverallStatus.ok` 为唯一通过口径 |
| `researchdatahub.network`（M4） | ✅ 已落地 | `list_workers` 用 `WorkerRegistry(WorkerStore(<状态根>/workers.db))`；`get_route_status` 展示配置层路线与适配器支持情况 |
| `researchdatahub.providers`（M3） | ⏳ 并行开发中 | 能力探测（`capabilities.py`）会报告 `available`；尚未提供被 MCP 工具直接调用的入口，工具层不臆造其 API |

探测实现为**可选注入 + 能力探测**：`importlib` 逐候选模块（包顶层 → 已知子模块）检查入口符号，
**不执行**任何检查逻辑、不在模块顶层 import；失败降级为结构化 `not_available`（`data.capability` 指明缺哪个）。

---

## 7. 安全不变式（可验证）

1. **不泄露凭证**：返回值与日志都不含令牌/密码；`get_provider_capabilities` 只报缺失**键名**。
2. **不冒充成功**：`SKIPPED`/`UNKNOWN` 校验结论永不标记 `verified=true`；可选能力缺失返回 `not_available` 而不是空列表。
3. **失败无副作用**：令牌任何一步校验失败都不入队、不改状态。
4. **不覆盖交付证据**：`get_manifest` 写文件用 `os.link` 原子"不覆盖"提交，冲突返回 `conflict`。
5. **状态在库里**：任务状态只从 SQLite 读，服务重启/换会话结果不变（无内存态）。

---

## 8. 踩坑记录（值得写进文档的真故障）

| 现象 | 根因 | 修法 |
| --- | --- | --- |
| `python -m ...server` 静默退出 | 模块缺少 `if __name__ == "__main__"` 守卫 | 补守卫；集成测试用 `-m` 启动 |
| 握手成功但 `tools/list` 永远等不到 | 在 SDK 之前 `os.dup2(2,1)`，与 SDK 的 fd 私有化冲突，协议帧写进 stderr | **不自己重定向**，交给 SDK 的 `stdio_server` |
| 刚签发的令牌被判"计划已改动" | `fingerprint()` 直接哈希 `model_dump_json`，JSON 键序经 SQLite 往返会变 | 改用键排序的规范化 JSON 计算指纹 |
| 重复提交报 `approval_invalid` | 幂等判断放在验签之后，而令牌已被上一次消费 | 幂等判断提前到验签之前 |
| 重新签发令牌后旧令牌仍可用 | 同秒同输入的 HMAC 相同 | 令牌加入随机 nonce |

---

## 9. 测试与验收

```bash
# 单元（信封/错误码/schema/审批/工具/服务器）
.venv/bin/python -m pytest tests/unit/test_mcp -q
# 集成（真子进程 + stdio JSON-RPC + 本地 HTTP 文件源，不碰 cdsapi）
.venv/bin/python -m pytest tests/integration/test_mcp_server.py -q
# 静态检查
.venv/bin/ruff check src/researchdatahub/interfaces
.venv/bin/ruff format --check src/researchdatahub/interfaces
.venv/bin/mypy src/researchdatahub/interfaces
```

集成测试断言：`tools/list` 恰好 18 个工具名 → `create_execution_plan` 拿到 `plan_id`+`approval_token`
→ 无 token 被拒（`invalid_arguments`）→ 伪造 token 被拒（`approval_invalid` + `side_effects_performed=false`）
→ 合法 token 入队 → `get_task_status` 读到 `QUEUED` 及之后的状态，且**状态确实落在 SQLite**。
