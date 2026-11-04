# 测试策略（Testing Strategy）

本文说明 ResearchDataHub 的测试分层、每层测什么/不测什么、**可复制的真实命令**、
覆盖率目标与现状，以及几条反模式约束。

所有命令均已在仓库根实测，结果与本文一致。

---

## 1. 分层总览

```
tests/
├── conftest.py                  共享装配：fixture 构造器（从 tests/fixtures/*.json 读）
├── fixtures/
│   ├── contract_fakes.py        8 个 Protocol 的参考实现（真算法，非 mock）
│   ├── era5_request.json        DatasetRequest 样例
│   ├── source_metadata.json     SourceMetadata 样例
│   └── invalid/                 故意非法的输入样例
├── unit/                        M1–M5 单元测试（约 900 个）
│   ├── test_models.py / test_state_machine*.py / test_contracts.py    M0 契约与状态机
│   ├── test_lease_recovery.py   15   租约/心跳双时间线
│   ├── test_config.py / test_serialization.py / test_invalid_input.py
│   ├── test_cli*.py             48   CLI 命令面（db/request/plan/task/file/report）
│   ├── test_manifest/           82   SQLite schema/迁移/CRUD/并发领取/租约/交付
│   ├── test_transfer/          104   真实 HTTP 服务器（Range/超时/中断/限速/错误码）
│   ├── test_sources/            82   CDS/NCAR/Registry/解耦（+11 network）
│   ├── test_planner/            39   YAML→请求、源匹配、拆分、去重
│   ├── test_scheduler/          24   领取/并发/失败分类/心跳/恢复/控制
│   ├── test_storage/            98   原子提交/EXDEV/覆盖保护/路径安全/恢复
│   ├── test_integrity/          58   六层检查/ETag 防护/NetCDF/check_path
│   └── test_reporting/          37   交付清单/报告
└── integration/
    ├── test_cli_install.py       8   真实子进程
    ├── test_config_defaults_drift.py  5
    ├── test_e2e_pipeline.py      5   端到端 + 4 个故障注入
    └── test_{sources_,transfer_}*_network.py  network 标记（默认不跑）

| 层 | 速度 | 依赖 | 目的 |
|---|---|---|---|
| unit | 毫秒–秒级 | 无外网；本地 HTTP 服务器（真实 socket，非 mock） | 契约、状态机、配置、SQLite、传输、校验、规划、调度 |
| integration | 秒级 | 真实 `python -m` 子进程 / 临时 SQLite / tmp 存储 | 安装后命令可用性、端到端链路、故障注入 |
| network | 分钟级 | 外网 + 凭证 | 真实数据源连通性（**默认不跑**，`-m network` 显式触发） |

**默认套件离线可重复**：`pyproject.toml` 的 `addopts` 含 `-m 'not network'`。
理由：把外网抖动算进 CI 红/绿会让"测试通过"失去意义（本次开发中就出现过一次
network 用例偶发失败，默认套件仍保持稳定）。

---

## 2. 单元测试层

### 2.1 测什么

| 测试文件 | 覆盖对象 | 典型断言 |
|---|---|---|
| `test_models.py` | 10 个 Pydantic 模型 | frozen、`schema_version`、`scientific_identity()`、必填字段与约束 |
| `test_state_machine.py` | `TRANSITIONS`、`transition()` | 表驱动遍历合法边、终态不可变、`attempt` 自增、过期恢复 |
| `test_state_machine_illegal.py` | 非法转换矩阵 | 144 组合中非法的部分必须抛 `StateTransitionError` |
| `test_contracts.py` | 8 个 Protocol | `assert_implements` 通过、方法齐全、docstring 含并发说明、fake 真能工作 |
| `test_config.py` | `config` 包 | `env:` 展开、缺失变量报错、`extra="forbid"`、校验结果聚合 |
| `test_cli.py` | CLI 命令 | 退出码语义（0/1/2）、未实现命令拒绝、输出文案 |
| `test_serialization.py` | JSON round-trip | `to_json_dict()` → `model_validate()` 不丢字段 |
| `test_invalid_input.py` | 非法输入 | 空串、超长 ID、naive datetime、区间倒置等必须拒绝 |

### 2.2 不测什么

- **不测外部服务**。单元层不联网，不需要 `CDSAPI_KEY`。
- **不测私有方法**。只测公开 API；私有函数的正确性通过公开行为间接验证。
- **不测第三方库**。不验证 pydantic 自己的行为，只验证**我们的约束是否生效**。
- **不重复测 `TRANSITIONS` 数据本身**。它由 `describe_transitions()` 生成，测的是
  `transition()` 是否遵守这张表。

### 2.3 参数化穷举

状态机的非法组合用 `pytest.mark.parametrize` 穷举，而不是抽样。
实际实现见 [`tests/unit/test_state_machine_illegal.py`](../tests/unit/test_state_machine_illegal.py)：

```python
ALL_PAIRS = [(s, t) for s in TaskState for t in TaskState]
ILLEGAL_PAIRS = [pair for pair in ALL_PAIRS if pair[1] not in TRANSITIONS[pair[0]]]

@pytest.mark.parametrize(("source", "target"), ILLEGAL_PAIRS,
                         ids=[f"{s.value}->{t.value}" for s, t in ILLEGAL_PAIRS])
def test_illegal_transition_raises(source: TaskState, target: TaskState) -> None: ...
```

配套的三条结构性断言：

| 测试 | 断言 |
|---|---|
| `test_matrix_is_fully_partitioned` | 合法边 + 非法边 == 144，不存在"未定义"的空洞 |
| `test_can_transition_matches_table_exactly` | 逐对比较 `can_transition()` 与 `TRANSITIONS`，且自环恒为 `False` |
| `test_terminal_states_allow_nothing` | 5 个终态的所有出边都非法 |

理由：状态机只有 144 组，全遍历成本极低，而漏掉一条非法边的后果是**静默的非法状态**。

---

## 3. 集成测试层

### 3.1 测什么

| 测试文件 | 测什么 | 为什么必须集成 |
|---|---|---|
| `test_cli_install.py` | `python -m researchdatahub.cli.main --help` 等真实子进程调用 | CLI 在导入期才暴露循环导入、入口点配置错误 |
| `test_config_defaults_drift.py` | `configs/defaults.yaml` 的键与 `config/models.py` 的 schema 是否一致 | 配置与代码脱节只有跑真实文件才会暴露 |

集成测试的原则：**通过真实进程边界调用**，而不是在进程内调函数。这样能抓到
`pyproject.toml` 入口点、`__main__` 块、`sys.exit` 码这类只在子进程里才显现的问题。

### 3.2 不测什么

- 不访问网络（真实源连通性属于 network 层）。
- 不下载数据（M0 没有传输实现）。

---

## 4. Network 测试层（默认不跑）

标记：`@pytest.mark.network`。`pyproject.toml` 已声明该 marker
（`tests` 目录中 `addopts = "-q --strict-markers --strict-config"`，未声明的 marker 会导致报错）。

```bash
# 默认：不跑网络测试
PYTHONPATH=src .venv/bin/python -m pytest

# 显式运行（需要真实凭证与外网）
CDSAPI_KEY="..." PYTHONPATH=src .venv/bin/python -m pytest -m network
```

**为什么默认不跑**：

1. CI 环境通常无外网或需要单独的凭证注入；
2. 远端服务不稳定会让 CI 假红，侵蚀"红灯可信度"；
3. CDS 请求可能排队数分钟到数小时，不适合放进快速反馈环。

**当前状态**：M0 没有任何 network 测试（没有真实适配器可测）。该层在 M1 随
`CDSAPIAdapter` / `RDAAdapter` 一起引入。

---

## 5. 真实命令（均已实测）

以下命令在仓库根 `/Users/apple/Desktop/ResearchDataHub` 执行。

### 5.1 跑测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest
```

实测输出（M1–M5 完成后）：

```text
955 passed, 14 deselected, 1 warning        # 默认套件（离线）
10 passed, 4 skipped                        # -m network（真实 CDS/NCAR；S3/CDS凭证 NOT RUN）
```

默认套件约 69 秒（含 104 项真实 HTTP 传输测试与 NetCDF 检查）。

> 为什么需要 `PYTHONPATH=src`：本仓库用 `src/` 布局，测试通过 `pyproject.toml` 的
> `pythonpath = ["src"]` 找到包。直接 `pytest` 若已执行过 editable 安装也能工作，
> 但显式设置 `PYTHONPATH` 让测试**不依赖安装状态**，是更稳的写法。

### 5.2 覆盖率

```bash
PYTHONPATH=src .venv/bin/python -m pytest --cov=researchdatahub --cov-report=term-missing
```

要看某个子包：

```bash
PYTHONPATH=src .venv/bin/python -m pytest --cov=researchdatahub.core --cov-report=term-missing
```

### 5.3 只跑某一层

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/unit
PYTHONPATH=src .venv/bin/python -m pytest tests/integration
PYTHONPATH=src .venv/bin/python -m pytest -o addopts="" -q -m network   # 真实外网（需凭证）
```

> 加 `-o addopts=""` 才能看到 summary 行：`pyproject.toml` 的 `-q` 会把统计压掉。

### 5.4 静态检查

```bash
.venv/bin/ruff format src tests --check
.venv/bin/ruff check src tests
.venv/bin/python -m mypy src
```

### 5.5 模块边界检查

见 [`docs/module-boundaries.md` §4](module-boundaries.md)，其中给出 3 条 `grep` 与 1 个
可直接运行的脚本。

---

## 6. 覆盖率目标与现状

| 范围 | 目标 | 实测（M1–M5 完成后） |
|---|---|---|
| `researchdatahub.core` | **≥ 85%** | **≥ 95%** |
| 整体 `researchdatahub` | **≥ 70%** | **85%**（8819 statements / 1137 miss / 2416 branches） |
| 关键执行层 | 尽量高 | manifest 98%、storage 84%、integrity 88%、reporting 96%、planner 88% |

覆盖率配置在 `pyproject.toml`：

```toml
[tool.coverage.run]
source = ["researchdatahub"]
branch = true                    # 统计分支覆盖，不只是行覆盖

[tool.coverage.report]
exclude_lines = ["pragma: no cover", "if TYPE_CHECKING:",
                 "raise NotImplementedError", "\\.\\.\\."]
```

**为什么 core 的目标更高（85%）**：core 是全部模块的依赖，它的缺陷会扩散到所有地方。
执行层同样有覆盖率要求，但**以行为为准**：像 `transfer/base.py` 中不可达的防御分支、
`reporting` 的渲染分支，覆盖率数字不等于质量。

**为什么用分支覆盖**：状态机的价值恰恰在"哪些分支不走"——例如
`RUNNING → COMPLETED` 必须不可达。只看行覆盖会漏掉这类断言。

**覆盖率是下限，不是目标**。为了刷高覆盖率去写"调一下不报错"的测试是反模式，
见 §8。当前 85% 是**没有被刻意追求**的结果：测试是围绕行为与事故场景写的。

---

## 7. Fixture 约定

1. **样例数据放在 `tests/fixtures/` 的文件里**，不在测试函数内内联大字典。
   理由：fixture 可被文档、手工实验复用；schema 演进时只改一处。
2. **构造器在 `conftest.py`**：`make_metadata()` / `make_task()` 等接受 `**overrides`，
   让测试只写**与本用例相关的差异**。
3. **时间固定**：`FIXED_NOW` 提供稳定的 UTC 时间戳，避免"测试在午夜跑失败"这类偶发问题。
4. **`contract_fakes.py` 是参考实现，不是 mock**：

| Fake | 实现的 Protocol | 真实性 |
|---|---|---|
| `InMemorySourceAdapter` | `DataSourceAdapter` | 真的按 request 过滤资产 |
| `LocalCopyTransport` | `TransportAdapter` | 真的复制字节并计算校验和 |
| `FilesystemStorage` | `StorageBackend` | 真的做原子移动与只读设置 |
| `InMemoryManifest` | `ManifestRepository` | 真的实现 `expected_state` 冲突检测 |
| `SizeAndChecksumChecker` | `IntegrityChecker` | 真的比对大小与校验和 |
| `SimplePlanner` | `TaskPlanner` | 真的做源匹配与去重 |
| `SerialExecutor` | `TaskScheduler` | 真的按优先级串行执行 |
| `StaticCatalog` | `CatalogProvider` | 真的返回元数据/抛 NotFound |

---

## 8. 反模式：不要让 mock 掩盖缺陷

这是本项目最重要的一条测试纪律。

### 8.1 问题

用 `unittest.mock.MagicMock` 替代核心逻辑时，测试会变成：

```python
# ❌ 反模式：测试只验证"调用发生过"，不验证"结果对不对"
def test_transfer(mocker):
    transport = mocker.MagicMock()
    transport.transfer.return_value = TransferResult(status="succeeded", ...)
    scheduler.run()
    transport.transfer.assert_called_once()     # 断言了什么？只断言"被调用了"
```

这段测试在**传输代码完全错误**时依然通过——因为返回值是被 mock 硬编码的。

### 8.2 本项目的做法

**用实现真实算法的 fake，不用 mock 返回值。**

`tests/fixtures/contract_fakes.py` 里的 `LocalCopyTransport`：

```python
# ✅ 真的复制字节、真的算 sha256
class LocalCopyTransport:
    def transfer(self, asset, destination, *, task):
        shutil.copyfile(asset.origin_uri, destination)   # 真 I/O
        return TransferResult(checksum=Checksum.of_file(str(destination)), ...)  # 真校验
```

因此 `test_transport_writes_real_bytes_and_checksum` 断言的是"落盘的字节确实对了"，
而不是"某个方法被调过了"。

### 8.3 具体规则

1. **禁止 mock `core` 的任何东西**（模型、状态机、异常）。它们是纯逻辑，直接用真实的。
2. **禁止 mock 返回值来"制造成功"**。要制造成功，就让 fake 真的做对。
3. **允许 mock 的只有**：
   - 外部网络客户端（`boto3.client`、`httpx.Client`、`cdsapi.Client`）；
   - 系统时间与随机数（为了确定性）；
   - 故意注入故障（模拟 5xx、连接重置）——此时 mock 的目的是**制造失败**，不是制造成功。
4. **断言行为而非调用次数**：断言"文件内容正确"而不是"copyfile 被调了 1 次"。

### 8.4 自查

```bash
# 检查是否有测试对 core 用了 mock
grep -rn "MagicMock\|mocker.patch" tests/ --include="*.py" | grep -i "core\|transition\|models"
# 期望：无输出
```

---

## 9. 测试与状态机变更的联动

状态机是变更最敏感的部分。任何 `TRANSITIONS` 改动必须同步做三件事：

1. 跑 `PYTHONPATH=src .venv/bin/python -c "from researchdatahub.core import describe_transitions; print(describe_transitions())"`，
   把输出与文档中的表逐行比对；
2. 检查 `test_state_machine.py` 的终态断言是否仍成立；
3. 检查本文档与 [`docs/architecture.md` §7.2](architecture.md) 中的转换表是否已更新。

> **历史案例**：`FAILED → QUARANTINED` 曾被定义为常规边，而 `FAILED` 同时在
> `TERMINAL_STATES` 中——自相矛盾。`test_terminal_states_are_immutable` 在穷举终态出边时
> 真实地抓到了它。修复方式是把该边改为需要 `after_integrity=True` 的审计专用路径。
> 这说明**穷举型断言比抽样断言更能发现设计矛盾**。

---

## 10. CI 建议

M0 尚未配置 CI。以下是 M1 落地时的建议（**当前未实现**，不是已有能力）。

```yaml
# 建议的 .github/workflows/ci.yml（M1）
jobs:
  test:
    strategy:
      matrix:
        python: ["3.12", "3.13"]
        os: [ubuntu-latest, macos-latest]
    steps:
      - run: python -m pip install -e ".[dev]"
      - run: ruff format --check src tests
      - run: ruff check src tests
      - run: mypy src
      - run: python -m pytest --cov=researchdatahub --cov-report=term-missing
      - run: python -m pytest tests/integration
```

要点：

1. **不跑 network**：CI 默认 `pytest`，不带 `-m network`。
2. **macOS 必须进矩阵**：目标用户主力平台是 Apple Silicon，只测 Linux 会漏掉
   `netCDF4`/`h5netcdf` 的 wheel 可用性问题。
3. **静态检查与测试分开报**：一条命令失败时能立刻定位是风格问题还是行为问题。
4. **覆盖率作为信息而非硬门槛**：先报告，观察稳定后再设为 `--cov-fail-under`。
   直接设门槛会诱导写无意义测试。
5. **边界检查脚本入 CI**：把 [`docs/module-boundaries.md` §4.4](module-boundaries.md) 的脚本
   放到 `scripts/check_boundaries.py` 并在 CI 中运行，防止依赖方向漂移。

---

## 11. 已知的测试盲区（M1–M5 后）

如实列出当前**没有测到**的东西，避免产生虚假信心：

| 盲区 | 原因 | 何时补 |
|---|---|---|
| 真实 ERA5 产物下载与校验 | 需 CDS 许可 / RDA 数据集权限 | M6（第一优先） |
| CDS 有凭证的 submit→poll→download 全链路 | 无 CDSAPI_KEY | M6 |
| S3 端到端（真实桶/MinIO） | 无 S3 环境；测试 3 项 skip，**未用 mock 冒充** | M6 |
| S3 multipart 并行下载 | 功能未实现 | 视需求 |
| 大文件（1–10 GB）RSS 与吞吐 | 需要真实数据与机器时间 | M6 |
| 长时间稳定性（数小时连续采集） | 未做 | M6 |
| 多进程同时写同一个库 | 明确不支持（单写者语义） | 不计划 |
| Windows 平台 | 不在支持范围 | 不计划 |
| NetCDF 科学语义正确性 | 超出系统职责（只校验结构与声明一致） | 不做 |

**关于"偶发失败"的经验**：network 用例曾出现一次失败（外网 502），随后重跑通过。
这正是把 network 从默认套件中排除的原因——默认套件的红/绿必须只反映代码。

---

## 12. 相关文档

- 状态机定义：[`docs/architecture.md` §7](architecture.md)
- 模块边界检查：[`docs/module-boundaries.md` §4](module-boundaries.md)
- M1 任务与验收标准：[`docs/development-plan.md`](development-plan.md)
