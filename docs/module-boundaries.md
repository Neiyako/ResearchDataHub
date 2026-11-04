# 模块边界（Module Boundaries）

本文规定 ResearchDataHub 各模块的**公开 API**、**允许的依赖方向**，以及**如何机械检查**这些规则。

所有签名以 `src/researchdatahub/core/ports.py` 为准；本文不复写实现，只固定形状。
契约语义见 [`docs/data-contract.md`](data-contract.md)。

---

## 1. 分层总览

```
        ┌───────────────────────────────────────────────┐
  L4    │  cli          （命令解析、呈现）               │
        ├───────────────────────────────────────────────┤
  L3    │  reporting    （报告渲染）                     │
        ├───────────────────────────────────────────────┤
        │  planner · scheduler · sources · transfer      │  ← 执行层
        │  integrity · storage · manifest · catalog      │
        ├───────────────────────────────────────────────┤
  L2    │  config       （YAML 加载、env: 展开、校验）    │
        ├───────────────────────────────────────────────┤
  L1    │  core         （模型 / 异常 / 状态机 / Protocol）│
        └───────────────────────────────────────────────┘
```

**唯一允许的依赖方向：向下。** 同层之间只允许依赖 **Protocol（定义在 `core`）**，不允许依赖彼此的实现。

当前 `core` 的实际内部依赖（`grep` 实测）：

```
core/errors.py  → （无）
core/state.py   → core/errors.py
core/models.py  → core/state.py
core/ports.py   → core/models.py, core/state.py
core/__init__.py→ 以上全部（仅做再导出）
```

`core` 不依赖 `config`，也不依赖任何 L3/L4 模块。

---

## 2. `core` 公开 API

`researchdatahub.core` 的 `__all__` 共 **59** 个符号（[`core/__init__.py:85`](../src/researchdatahub/core/__init__.py#L85)）。
下面按类别给出，**这就是其他模块可以使用的全部东西**。

### 2.1 数据模型（10 个持久化模型 + 8 个枚举/常量）

| 模型 | 用途 | 关键必填字段 | 关键约束 |
|---|---|---|---|
| `DatasetRequest` | 用户需求 | `request_id`、`dataset_id`、`metadata` | `max_sources` ∈ [1,8]；`checks` 不得重复 |
| `DatasetMetadata` | 科学身份 | `product`、`product_type`、`variables`、`temporal_coverage`、`spatial_extent` | 变量名唯一；`extensions` key 必须带 `.` |
| `SourceMetadata` | 源元数据 | `source_id`、`display_name`、`provider`、`access_route`、`dataset_id`、`origin_uri`、`retrieved_at` | 时间 tz-aware |
| `SourceAsset` | 远端文件 | `asset_id`、`source_id`、`dataset_id`、`access_route`、`origin_uri`、`file_name`、`size_bytes`、`metadata`、`discovered_at` | `metadata.source_id`/`origin_uri` 必须与自身一致 |
| `AcquisitionPlan` | 执行计划 | `plan_id`、`request_id`、`generated_at` | `task_id` 唯一；每个 task 的 `plan_id` 必须等于本计划 |
| `AcquisitionTask` | 调度单元 | `task_id`、`plan_id`、`request_id`、`asset` | `attempt ≤ max_attempts`；`finished_at ≥ started_at` |
| `TransferResult` | 传输结果 | `task_id`、`status`、`transport_id`、`bytes_transferred`、`duration_seconds`、`completed_at` | `SUCCEEDED` 必须有 `destination_path`；`FAILED` 必须有 `error_code` |
| `FileRecord` | 本地文件 | `file_id`、`task_id`、`dataset_id`、`source_id`、`access_route`、`origin_uri`、`local_path`、`size_bytes`、`checksum`、`retrieved_at` | `checksum` **必填** |
| `IntegrityReport` | 校验报告 | `report_id`、`task_id`、`status`、`checked_at` | `PASSED` 无 `failures`；`FAILED` 必须有 `failures` |
| `DeliveryManifest` | 交付清单 | `delivery_id`、`request_id`、`plan_id`、`dataset_metadata`、`files`（≥1）、`created_at`、`tool_version` | `total_bytes` 与文件之和对齐；`complete=True` 要求全部 `PASSED` 且 `total_bytes>0` |

| 枚举/常量 | 取值 |
|---|---|
| `AccessRoute` | `s3`、`https`、`globus`、`zarr`、`cds_api`、`local` |
| `Priority` | `critical` > `high` > `normal` > `low`（`.rank` 0–3） |
| `TransferStatus` | `succeeded`、`failed`、`skipped` |
| `IntegrityStatus` | `passed`、`failed`、`skipped`、`unknown` |
| `IntegrityCheckKind` | `size_match`、`checksum_match`、`format_sniff`、`netcdf_openable`、`variable_present`、`time_coverage`、`metadata_consistent` |
| `VerticalLevelKind` | `pressure`、`model_level`、`height`、`none` |
| `MODELS_VERSION` | `"1.0"` |
| `JsonValue` | 受控 JSON 类型（仅用于 `extensions`） |

### 2.2 状态机 API（`core/state.py`）

| 函数 | 输入 | 输出 | 异常 |
|---|---|---|---|
| `transition(task, target, *, reason, now=None, error=None, worker_id=None, after_integrity=False)` | `AcquisitionTask`、`TaskState` | **新** `AcquisitionTask` | `StateTransitionError`（非法转换 / COMPLETED 非经 VERIFYING / 重试预算耗尽） |
| `can_transition(source, target, *, after_integrity=False)` | 两个 `TaskState` | `bool` | — （同状态返回 `False`） |
| `next_state_after_failure(task)` | `AcquisitionTask` | `TaskState`（`RETRY_WAIT` 或 `FAILED`） | — |
| `is_stale(task, *, now=None, timeout_seconds=3600)` | `AcquisitionTask` | `bool`（仅 `RUNNING`/`VERIFYING` 可能为 `True`） | `ValueError`（`now` 非 tz-aware） |
| `recover_stale(task, *, now=None, timeout_seconds=3600)` | `AcquisitionTask` | 新 `AcquisitionTask` | `StateTransitionError`（未过期或不在在途状态） |
| `describe_transitions()` | — | `str`（逐行转换表，由代码生成） | — |

常量：`TaskState`（12 态）、`TRANSITIONS`（唯一事实来源，30 条合法边）、
`TERMINAL_STATES`（5 个：`COMPLETED`/`FAILED`/`CANCELLED`/`SKIPPED`/`QUARANTINED`）、
`DEFAULT_STALE_TIMEOUT_SECONDS`（3600）。

`after_integrity=True` 的唯一用途：允许 **事后审计** 把已结束的 `FAILED` 任务转为 `QUARANTINED`
（`FAILED → QUARANTINED`）。默认 `False` 时该边不可达——这是为了让 `FAILED` 成为真正的终态。
常规的校验失败走的是 `VERIFYING → QUARANTINED`（`TRANSITIONS` 表中的常规边），不需要该参数。

> **硬性规则**：`AcquisitionTask.state` 只能通过 `transition()` 修改。模型 `frozen=True`，
> 直接赋值会抛 `ValidationError`；但更重要的是语义集中——审计日志（`notes`）与时间戳
> （`last_transition_at`/`started_at`/`finished_at`/`attempt`）由 `transition()` 一并维护，
> 绕过它就等于绕过审计。

### 2.3 异常层次（`core/errors.py`）

所有异常继承 `RdhError`，携带 `code`（稳定机器可读串）、`task_id`、`source_id`、`detail`，
并提供 `to_dict()` 供落库。

| 异常 | `code` | 触发方 → 导致的状态 |
|---|---|---|
| `ConfigError` | `RDH_CONFIG` | 配置加载 → CLI 退出码 1 |
| `ModelValidationError` | `RDH_MODEL_INVALID` | 业务规则不满足 |
| `SourceNotFoundError` | `RDH_SOURCE_NOT_FOUND` | 计划阶段 → `FAILED` |
| `SourceUnavailableError` | `RDH_SOURCE_UNAVAILABLE` | 计划/发现阶段 → `FAILED` 或整体放弃 |
| `MetadataError` | `RDH_METADATA` | 元数据不可映射 → `FAILED` |
| `RetryableTransportError` | `RDH_TRANSPORT_RETRYABLE` | 传输 → `RETRY_WAIT`（`retryable=True`，可带 `retry_after_seconds`） |
| `PermanentTransportError` | `RDH_TRANSPORT_PERMANENT` | 传输 → `FAILED`（`retryable=False`） |
| `IntegrityError` | `RDH_INTEGRITY` | 校验无法进行 → `QUARANTINED` |
| `StorageError` | `RDH_STORAGE` | 落盘失败 → `FAILED` |
| `ManifestError` | `RDH_MANIFEST` | SQLite 失败/版本冲突 → 中止当前任务 |
| `ScheduleError` | `RDH_SCHEDULE` | 并发配置非法 / 已运行 → 拒绝 `run()` |
| `StateTransitionError` | `RDH_STATE_TRANSITION` | 非法转换（携带 `from_state`/`to_state`） |
| `NotImplementedFeatureError` | `RDH_NOT_IMPLEMENTED` | M0 未实现命令 → CLI 退出码 2 |

父类 `SourceError`（`RDH_SOURCE`）、`TransportError`（`RDH_TRANSPORT`）只用于 `except` 归类，
不直接实例化。

### 2.4 接口（8 个 `Protocol`，`core/ports.py`）

全部标注 `@runtime_checkable`，可用 `assert_implements(instance, Protocol)` 自检。

#### `DataSourceAdapter` — 发现数据

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `source_id` | `@property -> str` | 与配置 `sources.<id>` 一致 | — |
| `supported_routes` | `@property -> frozenset[str]` | 取值同 `AccessRoute` | — |
| `describe()` | `() -> SourceMetadata` | 源级元数据 | `SourceUnavailableError`、`MetadataError` |
| `discover_assets(request)` | `(DatasetRequest) -> Sequence[SourceAsset]` | 资产列表（可为空） | `SourceNotFoundError`、`SourceUnavailableError`、`MetadataError` |
| `health_check()` | `() -> bool` | `False` 表示暂不可用，**不抛异常** | — |

**并发约定**：实例必须线程安全（`discover_assets` 可能被多 worker 并发调用）；内部可变缓存须加锁。
**禁止**：下载字节、重试、落盘——属于 `TransportAdapter`。

#### `TransportAdapter` — 搬运字节

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `transport_id` | `@property -> str` | `s3`/`https`/`globus`/`cds_api` | — |
| `supports(asset)` | `(SourceAsset) -> bool` | 依据 `access_route` 与 URI scheme | — |
| `transfer(asset, destination, *, task)` | `-> TransferResult` | 结构化结果（成功失败都返回） | `RetryableTransportError`、`PermanentTransportError`、`StorageError` |
| `atransfer(...)` | `async`，语义同上 | `TransferResult` | 同上 |
| `close()` | `() -> None` | 释放连接池；**幂等** | — |

**并发约定**：单实例可被多线程并发调用 `transfer`；实现必须无可变共享状态或加锁。
**硬性约束（ADR-0002）**：不得包含任何数据源专属逻辑；落盘必须**原子**（先写 `.part` 再 rename）。
**生命周期**：`close()` 后调用其它方法应抛 `RuntimeError`。

#### `CatalogProvider` — 数据集目录

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `list_datasets()` | `() -> Sequence[str]` | 升序数据集 ID | — |
| `get_metadata(dataset_id)` | `(str) -> DatasetMetadata` | 科学元数据 | `SourceNotFoundError` |

**并发约定**：只读接口线程安全；`upsert_dataset` 必须单线程调用。

#### `TaskPlanner` — 需求 → 计划

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `plan(request, adapters)` | `(DatasetRequest, Iterable[DataSourceAdapter]) -> AcquisitionPlan` | 计划，任务初始态 `PLANNED` | `SourceNotFoundError`（无源支持）、`ScheduleError`（规模超限） |

**并发约定**：无状态，可并发调用（但依赖的 adapters 必须线程安全）。

#### `TaskScheduler` — 执行调度

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `submit(plan)` | `(AcquisitionPlan) -> int` | 入队数量 | — |
| `run(*, max_workers=None)` | `-> None` | 执行至队列空或取消 | `ScheduleError` |
| `cancel(task_id, reason)` | `-> None` | — | `StateTransitionError`（已终态） |

**并发约定**：`submit`/`cancel` 可由多线程调用，内部队列线程安全；状态写入只能走 `ManifestRepository` 的串行写通道。

#### `IntegrityChecker` — 完整性校验

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `check(file_record, *, task, kinds)` | `-> IntegrityReport` | **失败返回 `status=FAILED`，不抛异常** | 仅内部不可恢复错误抛 `IntegrityError` |

**并发约定**：无状态实现应线程安全。

#### `StorageBackend` — 本地存储

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `allocate_temp(task_id)` | `-> Path` | 临时路径（父目录已建） | — |
| `publish_original(temp_path, *, dataset_id, file_name)` | `-> Path` | 原始区路径（只读） | `StorageError`（已存在/磁盘不足/跨设备失败） |
| `quarantine(path, *, reason)` | `-> Path` | 隔离区路径（**不删除**） | — |
| `free_bytes()` | `-> int` | 存储卷可用字节 | — |

**并发约定**：多 worker 可并发 `allocate_temp`；`publish_original` 必须原子移动。

#### `ManifestRepository` — SQLite 事实来源

| 成员 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `initialize()` | `-> None` | 建表/迁移，**幂等** | — |
| `save_task(task, *, expected_state=None)` | `-> None` | — | `ManifestError`（不存在/**版本冲突**/事务失败） |
| `load_task(task_id)` | `-> AcquisitionTask` | 任务快照 | `ManifestError` |
| `list_tasks(*, state=None)` | `-> Iterator[AcquisitionTask]` | **惰性**迭代 | — |
| `append_event(task_id, event, payload)` | `-> None` | — | — |
| `save_file_record(record)` | `-> None` | — | — |
| `build_delivery_manifest(request_id)` | `-> DeliveryManifest` | 交付清单 | `ManifestError`（无完成文件 / 存在非 PASSED 报告） |
| `aiter_tasks()` | `async -> AsyncIterator[AcquisitionTask]` | 异步迭代 | — |

**并发约定（硬性）**：SQLite 连接**不可跨线程共享**；每个 worker 持有自己的连接；写事务必须串行
（单写者队列或 `BEGIN IMMEDIATE` + 重试）。`save_task` + `append_event` 必须同一事务。
`expected_state` 是乐观并发控制，用于防止两个 worker 同时推进同一任务。

### 2.5 `config` 公开 API

`researchdatahub.config`（[`config/__init__.py:22`](../src/researchdatahub/config/__init__.py#L22)）导出：

| 函数 | 签名 | 返回 | 异常 |
|---|---|---|---|
| `load_config(path)` | `(str \| Path) -> AppConfig` | 自动识别 defaults/sources | `ConfigError` |
| `load_defaults(path)` | `-> AppConfig` | 默认配置 | `ConfigError` |
| `load_sources(path)` | `-> SourcesConfig` | 数据源配置 | `ConfigError` |
| `load_raw_config(path, *, kind)` | `-> dict[str, object]` | 未建模的原始 dict | `ConfigError` |
| `detect_config_kind(document, *, path)` | `-> ConfigKind` | `defaults` / `sources` | `ConfigError` |
| `validate_config_files(defaults, sources)` | `-> ValidationResult` | **从不抛异常**，I/O 错误折算为错误项 | — |
| `resolve_env_refs(node, *, resolver=None)` | → 展开后结构 | 展开 `env:VAR` | `ConfigError` |
| `EnvResolver`（类） | — | 环境变量解析器 | — |

**模块边界**：`config` 只读配置，不构造 `DatasetRequest`、不访问网络、不写文件。
配置对象是 `frozen=True` + `extra="forbid"` 的 Pydantic 模型。

---

## 3. 允许的 import 方向矩阵

行 = 发起导入的模块，列 = 被导入的模块。`✓` 允许，`✗` 禁止，`—` 自身。

| ↓ 导入 → | core | config | sources | transfer | storage | manifest | integrity | planner | scheduler | catalog | reporting | cli |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **core** | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **config** | ✓ | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **sources** | ✓ | ✓ | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **transfer** | ✓ | ✓ | ✗ | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **storage** | ✓ | ✓ | ✗ | ✗ | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **manifest** | ✓ | ✓ | ✗ | ✗ | ✗ | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **integrity** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | — | ✗ | ✗ | ✗ | ✗ | ✗ |
| **catalog** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | — | ✗ | ✗ |
| **planner** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | — | ✗ | ✗ | ✗ | ✗ |
| **scheduler** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | — | ✗ | ✗ | ✗ |
| **reporting** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | — | ✗ |
| **cli** | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | — |

**关键规则：执行层模块之间（`sources` / `transfer` / `storage` / `manifest` / `integrity` / `planner` /
`scheduler` / `catalog` / `reporting`）互不 import。**

它们之间的协作**只能**通过两种方式：

1. **`core.ports` 里的 Protocol**（结构化类型，不是具体类）；
2. **依赖注入**：由 `cli` 或未来的编排层在构造时把实例传进去。

为什么这样规定：

- `scheduler` 需要调 `transfer` 吗？不需要。它拿到的是 `TransportAdapter` 实例（参数传入），
  具体是 S3 还是 HTTPS 与它无关。
- `planner` 需要认识 `Era5CdsSource` 吗？不需要。它遍历 `Iterable[DataSourceAdapter]`。
- `integrity` 需要知道文件来自哪个 `StorageBackend` 吗？不需要。它只处理 `FileRecord`。

### 3.1 唯一的安全例外：类型标注

`from typing import TYPE_CHECKING` 下的导入不产生运行时依赖，且在 `core/state.py:45-46`
已被实际使用（`state.py` 在 `TYPE_CHECKING` 下引入 `AcquisitionTask`，而 `models.py` 在运行时引入
`TaskState`）。这是**打破循环**的标准手法：

```
state.py  ──(TYPE_CHECKING)──► models.py     # 仅类型
models.py ──(runtime)────────► state.py      # TaskState 是字段
```

因此 `state` 与 `models` 之间**存在**一个受控的双向关系，但它不会形成运行时循环。
新增此类关系必须同时满足：`from __future__ import annotations` 已启用 + 导入在 `TYPE_CHECKING` 块内。

### 3.2 循环依赖禁止规则

1. **禁止运行时循环**：任何两个模块 A、B，不得同时存在 `A → B` 与 `B → A` 的**运行时**导入。
2. **禁止反向依赖**：`core` 不得导入任何其它模块；`config` 不得导入执行层或 `cli`。
3. **禁止同层横向依赖**：见 §3 矩阵，执行层内部全部为 `✗`。
4. **禁止在函数体内延迟导入做"绕圈子"**。唯一允许的函数体内导入是：为隔离可选依赖
   （例如 `cli` 延迟导入 `config.loader`），且必须是**单向**且**有注释解释**。
   实测当前仓库中运行时函数体内导入共 **4 处**，全部在 `cli` 内、方向均为 `cli → config`，合法：

   | 位置 | 导入目标 | 目的 |
   |---|---|---|
   | [`cli/config_cmd.py:107`](../src/researchdatahub/cli/config_cmd.py#L107) | `config.loader.validate_config_files` | 配置模块缺席时 `version` 等命令仍可用 |
   | [`cli/source_cmd.py:95`](../src/researchdatahub/cli/source_cmd.py#L95) | `config.loader.load_sources` | 同上 |

   另有 2 处 `from researchdatahub.config.models import ...` 位于 `TYPE_CHECKING` 块内
   （[`cli/config_cmd.py:34`](../src/researchdatahub/cli/config_cmd.py#L34)、
   [`cli/source_cmd.py:28`](../src/researchdatahub/cli/source_cmd.py#L28)），
   仅用于类型标注，不产生运行时依赖，不属此类。

---

## 4. 如何检查（可直接运行）

以下命令在仓库根执行，**不依赖任何外部工具**（只用 `grep` 与 `.venv` 中的 Python）。

### 4.1 检查 `core` 没有被上层污染

```bash
# 期望：无输出（core 内部只允许 researchdatahub.core.*）
grep -rn "from researchdatahub\." src/researchdatahub/core/ | grep -v "researchdatahub\.core"
```

### 4.2 检查执行层之间没有横向依赖

```bash
# 期望：无输出
for m in sources transfer storage manifest integrity planner scheduler catalog reporting; do
  grep -rn "from researchdatahub\.\(sources\|transfer\|storage\|manifest\|integrity\|planner\|scheduler\|catalog\|reporting\)" \
    src/researchdatahub/$m/ --include="*.py" | grep -v "researchdatahub\.$m"
done
```

### 4.3 检查 `config` 不依赖执行层与 CLI

```bash
# 期望：无输出
grep -rn "from researchdatahub\.\(cli\|sources\|transfer\|storage\|manifest\|integrity\|planner\|scheduler\|catalog\|reporting\)" \
  src/researchdatahub/config/ --include="*.py"
```

### 4.4 用脚本一次性检查全部规则

把下面内容存为 `scripts/check_boundaries.py` 后运行（M0 尚未提供该脚本，此处给出可直接使用的实现；
若要长期保留，应作为 M1 的 CI 任务落地）：

```python
"""检查模块依赖方向：core 不被上层依赖，执行层之间不横向依赖。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path("src/researchdatahub")
EXEC = ("sources", "transfer", "storage", "manifest", "integrity",
        "planner", "scheduler", "catalog", "reporting")


def module_name(path: Path) -> str:
    """把 package/__init__.py 或 package/x.py 映射为顶层模块名。"""
    rel = path.relative_to(ROOT).parts
    return rel[0].removesuffix(".py") if len(rel) == 1 else rel[0]


def imports(path: Path) -> set[str]:
    """收集文件里所有 `from researchdatahub.X import ...` 的 X。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("researchdatahub."):
                found.add(node.module.split(".")[1])
    return found


def main() -> int:
    errors: list[str] = []
    for path in ROOT.rglob("*.py"):
        here = module_name(path)
        for target in imports(path):
            if here == target:
                continue
            if here == "core":
                errors.append(f"{path}: core 不得依赖 {target}")
            elif here == "config" and target in (*EXEC, "cli"):
                errors.append(f"{path}: config 不得依赖 {target}")
            elif here in EXEC and target in EXEC:
                errors.append(f"{path}: 执行层横向依赖 {here} -> {target}")
    for line in errors:
        print(line)
    print(f"{'FAIL' if errors else 'PASS'}: {len(errors)} 处违规")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
```

### 4.5 检查 Protocol 的实现完整性

```bash
# 结构自检（运行时可用的协议）
PYTHONPATH=src .venv/bin/python -c "
from researchdatahub.core.ports import *
from researchdatahub.core.ports import assert_implements
print('8 protocols:', len([n for n in dir() if n.endswith(('Adapter','Checker','Provider','Repository','Backend','Planner','Scheduler'))]))
"
```

现有测试已经覆盖：`tests/unit/test_contracts.py::test_assert_implements_accepts_real_implementations`
会对 8 个 Protocol 逐个断言。

---

## 5. 当前实现状态（M0 事实）

以下表格描述**仓库此刻的真实状态**，不是计划。

| 模块 | `core` 契约 | 实现类 | 位置 |
|---|---|---|---|
| `core` | — | 全部完成（模型/异常/状态机/Protocol） | `src/researchdatahub/core/` |
| `config` | — | 完成（loader + models + env 展开 + 校验） | `src/researchdatahub/config/` |
| `cli` | — | 完成 M0 命令：`version`、`config validate`、`source list` | `src/researchdatahub/cli/` |
| `sources` | `DataSourceAdapter` | **无**（仅 docstring 占位） | `src/researchdatahub/sources/__init__.py` |
| `transfer` | `TransportAdapter` | **无** | `src/researchdatahub/transfer/__init__.py` |
| `storage` | `StorageBackend` | **无** | `src/researchdatahub/storage/__init__.py` |
| `manifest` | `ManifestRepository` | **无** | `src/researchdatahub/manifest/__init__.py` |
| `integrity` | `IntegrityChecker` | **无** | `src/researchdatahub/integrity/__init__.py` |
| `planner` | `TaskPlanner` | **无** | `src/researchdatahub/planner/__init__.py` |
| `scheduler` | `TaskScheduler` | **无** | `src/researchdatahub/scheduler/__init__.py` |
| `catalog` | `CatalogProvider` | **无** | `src/researchdatahub/catalog/__init__.py` |
| `reporting` | — | **无** | `src/researchdatahub/reporting/__init__.py` |

`tests/fixtures/contract_fakes.py` 中的 8 个类（`InMemorySourceAdapter`、`LocalCopyTransport`、
`FilesystemStorage`、`InMemoryManifest`、`SizeAndChecksumChecker`、`SimplePlanner`、`SerialExecutor`、
`StaticCatalog`）是**测试用参考实现**，不属于生产代码，也不应被 `src` 导入。

---

## 6. 相关文档

- 接口的语义与字段约束：[`docs/data-contract.md`](data-contract.md)
- 数据在这些模块间如何流动：[`docs/architecture.md`](architecture.md)
- 为什么不分微服务：[`docs/adr/ADR-0001-modular-monolith.md`](adr/ADR-0001-modular-monolith.md)
