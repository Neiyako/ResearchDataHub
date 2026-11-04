# M0 验收报告（Verification Report）

> 本文件记录 M0 的**真实执行命令、退出码与输出摘录**。
> 结论口径只用 `PASS` / `CONDITIONAL_PASS` / `FAIL` / `INSUFFICIENT_EVIDENCE` / `NOT_RUN`。
> 生成时间：2026-11-04。执行环境：macOS 27.0.1 arm64（Apple Silicon M2），Python 3.13.13，仓库根 `/Users/apple/Desktop/ResearchDataHub`。

---

## 0. 结论总览

| # | 验收项（任务书 §12） | 结论 | 证据 |
|---|---|---|---|
| 1 | 项目能够正常安装 | **PASS** | §2.1–§2.2 |
| 2 | `rdh --help` 正常工作 | **PASS** | §2.3 |
| 3 | `rdh version` 正常工作 | **PASS** | §2.3 |
| 4 | `rdh config validate` 能正确验证配置 | **PASS** | §2.4（PASS/FAIL 两条路径均验证）|
| 5 | 核心数据模型可创建、验证、序列化 | **PASS** | §3.1–§3.3 |
| 6 | 状态机阻止非法转换 | **PASS** | §3.4 |
| 7 | 模块接口边界清晰 | **PASS** | §3.5–§3.6 |
| 8 | 文档与代码一致 | **PASS** | §3.7（含行号锚点校验）|
| 9 | 自动化测试全部通过 | **PASS** | §2.5：360 passed |
| 10 | 无真实密钥泄露 | **PASS** | §3.8 |
| 11 | 无伪实现冒充完整功能 | **PASS** | §2.3（未实现命令退出码 2）|

**总体结论：PASS。** 已知限制见 §5，未完成事项见 §6。

---

## 1. 环境

```text
$ python3 --version
Python 3.13.13

$ .venv/bin/python -c "import pydantic; print(pydantic.VERSION)"
2.14.2

$ uname -sm
Darwin arm64
```

虚拟环境：仓库内 `.venv/`（已在 `.gitignore` 中）。

---

## 2. 构建与安装

### 2.1 可编辑安装

```text
$ .venv/bin/pip install -e ".[dev]"
Successfully built researchdatahub
Installing collected packages: researchdatahub
Successfully installed researchdatahub-0.1.0
exit=0
```

### 2.2 元数据可读（证明是真实安装，而非 PYTHONPATH 兜底）

```text
$ .venv/bin/python -c "import importlib.metadata as m; print(m.version('researchdatahub'))"
0.1.0
```

对应测试：`tests/integration/test_cli_install.py::test_installed_package_imports`。

> **过程记录（诚实披露）**：首次 `pip install -e .` **失败**，原因是 `pyproject.toml` 的
> `readme = "README.md"` 指向一个尚不存在的文件，hatchling 在读取 readme 时抛
> `OSError: Readme file does not exist: README.md`。补上 `README.md` 后安装成功。
> 这是一个真实的 M0 早期阻塞，已修复并有集成测试守护。
>
> **第二处过程缺陷**：多次失败/中断的安装把项目 `.venv` 的可编辑安装状态弄坏了——
> `pip install` 退出码 0、`rdh` 也存在，但执行时报 `ModuleNotFoundError`。
> 起初误判为「`.pth` 以下划线开头被 `site` 跳过」，**经重建 venv 实验证伪**
> （同名同内容 `.pth` 在干净 venv 中工作正常）。修法：删掉重建 venv。
> 详见 [`development-plan.md` §7](development-plan.md)。最终以**独立干净 venv** 复验（见 §2.2b）。

### 2.2b 独立干净 venv 复验（排除"只有作者机器能装"）

```text
$ rm -rf /tmp/rdh-final && python3 -m venv /tmp/rdh-final
$ /tmp/rdh-final/bin/pip install -e "/Users/apple/Desktop/ResearchDataHub[dev]"
install exit=0

$ /tmp/rdh-final/bin/rdh version ; echo "exit=$?"
（版本表：name=researchdatahub / version=0.1.0 / models_version=1.0 / python / platform）
exit=0
```

说明：该 venv 位于 `/tmp`，与仓库内 `.venv` 完全独立，证明打包元数据可移植。

### 2.3 CLI 冒烟

```text
$ .venv/bin/rdh --help ; echo "exit=$?"
（显示 version / config / source 三组可用命令 + 「未实现（M1）」面板）
exit=0

$ .venv/bin/rdh version ; echo "exit=$?"
（表格：name=researchdatahub / version=0.1.0 / models_version=1.0 / python / platform）
exit=0

$ .venv/bin/rdh plan ; echo "exit=$?"
错误 [RDH_NOT_IMPLEMENTED] feature 'plan' is not implemented yet (planned for M1)
未实现（planned for M1）
exit=2
```

`fetch`、`status` 同为 `exit=2`。**未实现功能没有一个是退出码 0**。

### 2.4 配置校验（两条路径）

```text
$ CDSAPI_KEY="$CDS_TOKEN" .venv/bin/rdh config validate ; echo "exit=$?"
PASS 配置校验通过：2 个文件，0 条错误。
exit=0

$ env -u CDSAPI_KEY .venv/bin/rdh config validate ; echo "exit=$?"
FAIL 配置校验未通过：2 个文件中 1 个失败，1 条错误。
| configs/sources.example.yaml | environment variable 'CDSAPI_KEY' is not set
  but is referenced at sources.cds.credentials.key in configs/sources.example.yaml |
exit=1
```

语义说明：`defaults.yaml` 使用 `storage.root: "~/.rdh"`，因此**开箱即可通过**；
`sources.example.yaml` 中的唯一秘密是 `credentials.key: env:CDSAPI_KEY`，未提供时如实失败。
这两条路径都有单元测试（`tests/unit/test_cli.py`）与集成测试（`tests/integration/test_cli_install.py`）守护。

### 2.5 测试

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/
360 passed in 1.40s
```

覆盖率：

```text
$ PYTHONPATH=src .venv/bin/python -m pytest --cov=researchdatahub --cov-report=term-missing
TOTAL                                        1237     98    242     51    90%
```

关键模块：`core/models.py` 96%、`core/ports.py` 97%、`core/errors.py` 94%、`core/state.py` 92%、
`config/loader.py` 84%、`config/models.py` 84%、`cli/*` 68%–100%。

---

## 3. 专项验证

### 3.1 数据模型：创建 / 校验 / 序列化

由 `tests/unit/test_models.py`、`tests/unit/test_serialization.py` 覆盖：

- 10 个模型全部 `frozen=True` + `extra="forbid"`；全部携带 `schema_version == "1.0"`。
- JSON 往返：`model_dump_json → json.loads → model_validate` 逐模型断言对象相等且二次序列化字节稳定。
- 时间字段：tz-aware 强制（naive 输入被拒，报 `timezone-aware`）；序列化为 `...Z` ISO8601。
- schema 生成：`model_json_schema()` 全部可生成，且 `additionalProperties: false`。
- 无 `Any` 泄漏：遍历 schema 断言不存在「`type: object` 却无 `properties`/`additionalProperties`」的裸对象；`extensions` 通过 `$ref: #/$defs/JsonValue` 引用有界联合。

### 3.2 非法输入

`tests/unit/test_invalid_input.py`：22 条用例（含 1 条补充的「完整性未通过却标记完成」），
每条断言 `ValidationError` **且错误信息包含目标字段名**，避免"抛了任何异常就算通过"的空转断言。

覆盖：时间倒置、经度越界（`west >= east`）、纬度越界、变量名大写、缺单位、重复变量、
气压层升序、空层列表、扩展键未命名空间、未知顶层字段、空变量列表、naive 时间、
负文件大小、非法 checksum、源 ID 不一致、重试次数超限、`finished_at < started_at`、
成功传输缺路径、失败传输缺错误码、失败报告无 failure 列表、通过报告却有 failures。

### 3.3 科学等价性（数据契约核心）

`DatasetMetadata.scientific_identity()` 判定维度：产品、产品类型、数据版本、时间定义、
变量（名 + 单位）、时间覆盖、空间范围与分辨率、垂直层（类型 + 列表）、网格与重网格化、
后处理列表、`regridded` 标志。

测试断言：**同名变量但单位不同 → 身份不同**；垂直层不同 → 身份不同；完全相同的元数据 → 身份相同。

### 3.4 状态机

- 合法转换：**表驱动参数化**，遍历 `TRANSITIONS` 全部边逐条执行。
- 非法转换：`12 × 12 = 144` 组合中所有非表项**逐条穷举**断言抛 `StateTransitionError`；
  并断言失败的转换**不修改原任务**。
- 5 个终态：任何出边都非法。
- 业务前提：`RETRY_WAIT → QUEUED` 在预算耗尽时被拒；`RUNNING → COMPLETED` 不存在；
  `COMPLETED` 只能自 `VERIFYING`。
- 过期恢复：阈值边界（`timeout-1` 非过期、`timeout` 过期）；无时间戳的在途任务视为过期；
  预算耗尽时 `recover_stale` 转 `FAILED`。

### 3.5 接口契约

`tests/unit/test_contracts.py`：8 个 Protocol 均有 `@runtime_checkable`、非平凡 docstring、
每个成员都有 docstring（空类/纯 `pass` 无法通过）；并发要求在文档中显式出现。

`tests/fixtures/contract_fakes.py` 提供**真实实现**而非 mock：真实 sha256 计算、
真实文件移动、真实乐观并发检查（`save_task(expected_state=...)` 冲突时抛 `ManifestError`）。
这些 fake 用于证明"接口可以被实现"，不是用来掩盖被测逻辑。

### 3.6 依赖方向

```text
$ grep -rn "^from researchdatahub\.\|^import researchdatahub" src/researchdatahub/core/*.py | grep -v "core\."
(无输出)
```

`core` 不依赖任何上层模块 → 无循环依赖。`config → core`、`cli → core/config` 单向。

### 3.7 文档与代码一致

- 文档中的 44 个源码行号锚点（`../src/...#L<n>`）已用脚本逐条校验：文件存在且行号未越界。
- 状态转换表由 `describe_transitions()` 从代码生成，而非手抄。
- 文档中出现的每个命令均在 §2 中被真实执行过。

### 3.8 密钥扫描

```text
$ grep -rn -E "(sk-[A-Za-z0-9]{20,}|CDSAPI_KEY=[^\s\"$]{4,}|password:\s*[^\s\"\${}]{4,}|api_key:\s*[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9]{20,})" \
    --include="*.py" --include="*.yaml" --include="*.md" --include="*.toml" --include="*.json" . | grep -v /.venv/
(无输出)
```

仓库中仅存在 `env:VAR` 形式的变量引用，无真实凭证。

### 3.9 代码质量

```text
$ .venv/bin/ruff format --check src tests
37 files already formatted

$ .venv/bin/ruff check src tests
All checks passed!

$ .venv/bin/python -m mypy src
Success: no issues found in 25 source files

$ grep -rn "TODO\|FIXME\|XXX" --include="*.py" src/
(无输出)
```

`mypy` 为 strict 模式（`strict = true`）。`disable_error_code = ["import-untyped"]` 仅针对
PyYAML 无官方 stubs 这一项，代码中**零** `type: ignore`。

---

## 4. 团队交付与交叉复核

| 交付方 | 范围 | 复核结论 |
|---|---|---|
| lead | `core/`（模型/异常/状态机/接口）、`pyproject.toml`、`LICENSE`、`.gitignore`、`README.md`、全部 `tests/`、本报告 | — |
| arch | `config/`（3 文件 1076 行）、`configs/*.yaml`、`examples/era5-request.yaml` | ruff/format/mypy strict 均 PASS；我独立复跑测试通过 |
| cli-eng | `cli/` 7 文件 + 9 个模块 `__init__.py` docstring | 我独立运行 4 条命令 + 3 条未实现命令，退出码符合约定 |
| doc-writer | `docs/` 6 篇 + 3 篇 ADR | 行号锚点校验通过；状态机表已按代码修正 |

**我推翻/修正过其他成员的判断共 5 处，均有记录：**

1. `configs/defaults.yaml` 的 `storage.root` 由 `env:RDH_HOME` 改为 `~/.rdh` —— 理由：验收要求
   `rdh config validate` 可用，一个刚 clone 的仓库不应因为没设环境变量而失败；env 展开能力
   改由 `CDSAPI_KEY` 覆盖验证。
2. `configs/sources.example.yaml` 的 CDS 端点由 `env:CDSAPI_URL` 改为明文公开地址 ——
   理由：端点是公开信息，只有 key 需要保密，减少一个无意义的环境变量。
3. 状态机 `FAILED -> QUARANTINED` 由常规边改为 `after_integrity=True` 显式放行 ——
   理由：`FAILED` 同时被定义为终态，二者矛盾；测试真实抓到了该矛盾。
4. **证伪 doc-writer 的 `.pth` 根因假设** —— 其结论是「`site` 跳过下划线开头的 `.pth`
   文件」。我重建干净 venv 后，同名同内容的 `.pth` 工作正常，故根因实为旧 venv 损坏。
   已改写 `README.md` 与 `development-plan.md` §7 的错误结论（错的诊断比没有诊断更糟）。
5. **修正 doc-writer 发现的 `defaults.yaml` 注释与代码不符** —— 注释声称"禁止写死绝对路径"，
   但校验器实际允许绝对路径。**不改代码而改注释**：主人有外置 SSD，
   `/Volumes/SSD/rdh` 是合法需求，禁止绝对路径会挡住真实用法；正确规则是
   "默认用可移植路径，但不禁止绝对路径"。

---

## 5. 已知限制与风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | M0 无任何真实适配器/传输实现 | 不能下载数据 | 刻意为之；CLI 对未实现功能退出码 2 |
| R2 | SQLite Manifest 仅有接口，无表结构 | 无法做端到端集成测试 | M1 第一件事落地 schema + 迁移 |
| R3 | `storage.root` 的 `~` 不做展开 | 若 storage 层忘记 `expanduser()` 会建出字面量 `~` 目录 | 已明确分层：展开属于 storage 层，M1 实现时补测试 |
| R4 | 默认值在 YAML 与代码 `Field(default=...)` 中各存一份 | 可能静默分叉 | 已加 `tests/integration/test_config_defaults_drift.py` 锁定 |
| R5 | `sci` extra（xarray/netCDF4）未在 M0 验证 | `netcdf_openable` 等检查无法运行 | M1 引入；不影响 M0 验收 |
| R6 | CLI 覆盖率 68%–82% | 部分错误分支未覆盖 | 已覆盖所有退出码路径；剩余为渲染分支 |
| R7 | Windows 未验证 | 路径/文件锁行为未知 | 任务书列为后续兼容项 |
| R8 | 旧 venv 的可编辑安装可能损坏 | `rdh` 报 `ModuleNotFoundError` | 重建 venv 即修复（已实测）；`test_cli_install.py` 用真实子进程守护；§7 有排查记录 |

---

## 6. 未完成事项（M0 范围内确认无遗漏；以下属于 M1+）

- `rdh plan` / `rdh fetch` / `rdh status` 的实际功能。
- `sources` / `catalog` / `planner` / `scheduler` / `transfer` / `integrity` / `storage` / `manifest` / `reporting` 的实现。
- SQLite schema 与迁移、断点续传、并发调度、真实重试执行。
- Windows 平台验证、CI 流水线。

以上均**未**以"占位实现"形式出现：对应模块只有声明职责的 docstring，没有可被误用的空实现。
