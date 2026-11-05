# 环境配置系统（M2）使用说明

> 模块：`src/researchdatahub/environment/`
> 契约版本：`schema_version = "1.0"`（`EnvironmentReport`）
> 定位：自然语言需求 → 数据源发现 → 数据集匹配 → **环境配置引导** → 请求构建/验证 → 链路选择 → 下载 → 完整性验证 → 交付

## 1. 这个模块解决什么问题

Agent 在真正下载之前必须先回答四个问题，本模块就是这四个问题的唯一实现：

| 问题 | 谁回答 | 入口 |
| --- | --- | --- |
| 这个 Provider **要求什么** | `ProviderRequirementsRegistry` | `get_requirements("cds")` |
| 当前环境**满足了吗** | 依赖 / 凭据 / 网络 / 磁盘 四个 checker | `ConfigurationValidator.validate(...)` |
| **整体结论**是什么 | 状态推导（唯一实现） | `EnvironmentReport.status` |
| 用户**该怎么做** | 可操作引导（中文，含真实 URL） | `get_environment_requirements("cds")` |

覆盖五个 Provider：`cds`（Copernicus CDS）、`ncar`（NSF NCAR RDA / GDEX）、
`nasa_earthdata`（NASA GES DISC）、`copernicus_datasphere`（Copernicus Data Space）、
`gee`（Google Earth Engine）。

**不在范围内**：数据分析、EDA、机器学习、可视化。本模块只回答「能不能拿到数据」。

## 2. 安全约定（硬约束）

1. 凭据**只以引用形式**存在：`env:VAR`、`file:/path`、`keyring:SERVICE/USERNAME`。
   模型 `CredentialReference` 只有 `kind` / `name` / `service` 三个字段，**结构上没有装 secret 的位置**。
2. 任何返回值 / 日志 / 报告 / manifest 里**不得出现 secret 明文**
   （`tests/unit/test_environment/test_credentials.py` 与 `test_validation.py` 用 sentinel 字符串断言）。
3. **不要**把 Personal Access Token / API key **粘贴到聊天窗口或 issue**。所有令牌只写进本机
   环境变量或 `chmod 600` 的凭据文件。
4. 网络探测只做只读 `HEAD`/`GET`，不携带认证头，不提交检索请求；
   **绝不调用 cdsapi**（cdsapi 的每个调用都会触发真实检索、排队并消耗配额）。

## 3. 快速开始（人类可读 CLI 用法）

无需 `rdh` 子命令，直接用 Python 一行命令。全部示例都在项目根目录、使用 `.venv/bin/python`。

### 3.1 查看整体状态

```bash
.venv/bin/python -c "
from researchdatahub.environment import validate_environment
r = validate_environment('cds', check_network=False)
print(r.status.value)
print(r.summary)
"
```

输出示例（本机真实结果，缺少 `CDSAPI_KEY`）：

```
needs_configuration
缺少 1 项必需配置：key@环境变量 CDSAPI_KEY；未做网络探测（check_network=False）；下一步：按 guidance 逐条修复，然后重新执行 validate_environment('cds', check_network=True)。
```

### 3.2 打印逐条明细（推荐给人看）

```bash
.venv/bin/python -c "
from researchdatahub.environment import validate_environment
r = validate_environment('ncar', check_network=False)
for q in r.requirements:
    print(f'{q.kind.value:10} {q.name:45} {q.status.value:8} required={q.required}')
    if q.guidance:
        print('   →', q.guidance)
"
```

### 3.3 输出稳定 JSON（给 Agent / MCP / 脚本消费）

```bash
.venv/bin/python -c "
import json
from researchdatahub.environment import validate_environment
print(json.dumps(validate_environment('gee').to_json_dict(), ensure_ascii=False, indent=2))
"
```

报告形状：

```jsonc
{
  "schema_version": "1.0",
  "provider": "cds",
  "display_name": "Copernicus Climate Data Store (CDS)",
  "status": "ok" | "needs_configuration" | "unavailable",
  "requirements": [
    {
      "name": "httpx",                  // 依赖模块名 / 凭据键@来源 / 端点名 / 存储路径
      "kind": "dependency" | "credential" | "network" | "storage",
      "status": "present" | "missing" | "optional" | "unknown",
      "required": true,
      "guidance": "",                   // 中文可操作修复说明（missing 时非空）
      "help_url": "https://www.python-httpx.org/",
      "observed": { "package": "httpx", "importable": true }
    }
  ],
  "summary": "…",                       // status=ok 时为空串
  "checked_at": "2026-11-05T22:55:10.123456+00:00",
  "checks_run": ["dependencies", "credentials", "network", "storage"],
  "duration_ms": 12.4,
  "errors": []
}
```

### 3.4 打印配置引导（就是「我该怎么做」）

```bash
.venv/bin/python -c "
from researchdatahub.environment import render_environment_guide
print(render_environment_guide('nasa_earthdata'))
"
```

每条引导含四要素：**要做什么 / 为什么 / 去哪拿或存哪（真实 URL + 文件路径）/ 怎么验证**。

### 3.5 列出五个 Provider 的要求清单

```bash
.venv/bin/python -c "
from researchdatahub.environment import PROVIDER_REQUIREMENTS_REGISTRY as reg
for entry in reg.all():
    deps = [f'{d.module}{\"\" if d.required else \"?\"}' for d in entry.dependencies]
    creds = [f'{c.key}@{c.reference.name}{\"\" if c.required else \"?\"}' for c in entry.credentials]
    print(f'{entry.provider:24} 依赖={deps}')
    print(f'{\"\":24} 凭据={creds}')
"
```

真实输出：

```
cds                      依赖=['httpx', 'netCDF4', 'xarray', 'cdsapi', 'keyring?']
                         凭据=['key@CDSAPI_KEY', 'key@~/.cdsapirc', 'url@CDSAPI_URL?', 'url@~/.cdsapirc?']
ncar                     依赖=['httpx', 'netCDF4', 'xarray', 'boto3?', 'keyring?']
                         凭据=['api_key@RDA_API_KEY?', 'api_key@~/.netrc?']
nasa_earthdata           依赖=['httpx', 'netCDF4', 'xarray', 'keyring?']
                         凭据=['token@EARTHDATA_TOKEN', 'netrc_login@~/.netrc?']
copernicus_datasphere    依赖=['httpx', 'boto3?', 'netCDF4', 'keyring?']
                         凭据=['access_token@CDSE_TOKEN', 'netrc_login@username?']
gee                      依赖=['httpx', 'ee']
                         凭据=['credentials@~/.config/earthengine/credentials', 'project@EE_PROJECT?']
```

（`?` 后缀表示可选项。凭据只打印「变量名 / 文件路径 / keyring 用户名」，永远不含值。）

### 3.6 机器可读退出码（给 CI / Agent 用；本机缺凭据时返回 1）

```bash
.venv/bin/python -c "
import sys
from researchdatahub.environment import validate_environment
r = validate_environment('cds')
sys.exit(0 if r.ok else 1)
"
echo "exit=$?"
```

## 4. 公开 API

### 4.1 顶层（`from researchdatahub.environment import ...`）

| 名称 | 签名 / 值 | 说明 |
| --- | --- | --- |
| `validate_environment` | `(provider, *, storage_root="~/.rdh", free_space_floor_gb=20.0, check_network=True, timeout=None, environ=None, sources_path=None, validator=None) -> EnvironmentReport` | **配置完成后的再验证**；与检查共用同一实现，默认开网络探测 |
| `render_environment_guide` | `(provider: str) -> str` | Markdown 文本引导（`validation.render_guide` 的别名） |
| `get_environment_requirements` | `(provider: str) -> ConfigurationGuide` | 结构化引导：`.steps[i].action / why / where / verify` |
| `get_requirements` | `(provider: str) -> ProviderRequirements` | 静态要求声明（依赖 / 凭据 / 端点） |
| `ConfigurationValidator` | `(*, registry=None, dependency_checker=None, credential_checker=None, network_checker=None, sources_path=None, redirect_probe_failure=False, clock=None)` | 组合器；`.validate(provider, *, storage_root=None, free_space_floor_gb=20.0, check_network=False, timeout=None) -> EnvironmentReport`，`.validate_many(providers=None, **kwargs) -> list[EnvironmentReport]` |
| `derive_overall_status` | `(requirements: list[RequirementResult]) -> OverallStatus` | **纯函数**状态推导（MCP/CLI 应复用它，别各写一份） |
| `EnvironmentReport` | Pydantic 模型 | 见 §3.3 的 JSON 形状；`.ok` / `.blocking_requirements` / `.by_kind` / `.to_json_dict()` |
| `RequirementResult` | Pydantic 模型 | 单条结果；`.blocking`（required 且 missing）、`.unresolved` |
| `ProviderRequirementsRegistry` | `()`；`.get()` / `.require()` / `.all()` / `.provider_ids` | 只读登记表 |
| `PROVIDER_IDS` | `("cds", "ncar", "nasa_earthdata", "copernicus_datasphere", "gee")` | 固定顺序 |
| `PROVIDER_REQUIREMENTS_REGISTRY` | `ProviderRequirementsRegistry` 实例 | 默认实例（无状态，可共享） |
| `DependencyChecker` | `(probe=None)`；`.check(list[DependencyRequirement])` | `find_spec` 探针，**不 import** 模块 |
| `CredentialChecker` | `(environ=None, *, keyring_getter=None, keyring_available=None, redact_values=())`；`.check_reference()` / `.check_credentials()` / `.any_present()` | **永不返回值** |
| `NetworkChecker` | `(*, timeout=5.0, client=None, follow_redirects=True, clock=None)`；`.check()` / `.check_one()` / `.check_url()` | 只读探测；`client` 可注入 `MockTransport` |
| `DiskSpaceChecker` | `(root, *, free_space_floor_gb=20.0, probe_write=True)`；`.check()` | 写探测 + 剩余空间；**不创建目录** |
| `CredentialReference` / `CredentialRequirement` / `DependencyRequirement` / `EndpointRequirement` / `ProviderRequirements` / `ConfigurationGuide` / `GuidanceStep` | Pydantic 模型 | 见各模块 docstring |
| `parse_credential_reference` | `(text: str) -> CredentialReference` | 解析 `env:` / `file:` / `keyring:` |
| `load_credential_references` | `(path) -> dict[str, dict[str, CredentialReference]]` | 只读 `configs/sources*.yaml` 的 `credentials:`（**不展开** `env:`） |
| `assert_report_has_no_secret` | `(report, secrets) -> None` | 兜底断言；命中即 `AssertionError` |
| `redact_secrets` | `(text: str, secrets) -> str` | 替换成 `***REDACTED***` |
| `ENVIRONMENT_SCHEMA_VERSION` | `"1.0"` | 报告契约版本 |

### 4.2 子模块（供 MCP / CLI 层细粒度使用）

```python
from researchdatahub.environment.dependencies import DependencyChecker
from researchdatahub.environment.credentials import CredentialChecker, load_credential_references
from researchdatahub.environment.network import NetworkChecker, DEFAULT_TIMEOUT_SECONDS
from researchdatahub.environment.storage import DiskSpaceChecker
from researchdatahub.environment.validation import ConfigurationValidator, derive_overall_status
from researchdatahub.environment.registry import get_environment_requirements
```

### 4.3 给 MCP 层接线的最小提示

```python
from researchdatahub.environment import validate_environment, render_environment_guide

def mcp_environment_check(provider: str) -> dict:
    report = validate_environment(provider)          # 默认含网络探测
    payload = report.to_json_dict()
    if not report.ok:
        payload["guide_markdown"] = render_environment_guide(report.provider)
    return payload        # 报告 JSON 里保证没有 secret
```

## 5. 状态判定表（`EnvironmentReport.status`）

| 条件 | status |
| --- | --- |
| 全部 `required` 项为 `present` / `optional` | `ok` |
| 有 `required` 项 `missing`，且原因是缺包 / 缺凭据 / 连接失败 / 磁盘不足 | `needs_configuration` |
| 有 `required` 项 `unknown`（无法判定，如探针异常、YAML 读不了） | `needs_configuration`（`unknown` **绝不等于通过**） |
| 必需端点在网络探测中返回 **5xx**（500/501/502/503/504）——本机配置修不了 | `unavailable` |
| provider 未登记 | `unavailable`（`errors` 里说明，**不抛异常**） |

单条状态映射：

| 检查 | `present` | `missing` | `optional` | `unknown` |
| --- | --- | --- | --- | --- |
| 依赖 | `find_spec` 找得到 | 必需包未装 | 可选包未装 | 探针抛异常（父包损坏） |
| 凭据 | 变量非空 / 文件非空且含标记 / keyring 有值 | 必需凭据缺失或为空 | 可选凭据缺失 | — |
| 网络 | HTTP < 400；**401/403/404/405 也算可达** | 5xx / 429 / 连接失败 / 超时 | 可选端点失败 | `redirect_probe_failure=True` 时的连接失败 |
| 磁盘 | 目录存在、可写、剩余 ≥ 下限 | 目录不存在 / 不是目录 / 不可写 / 空间不足 | — | `disk_usage` 读取失败 |

## 6. 五个 Provider 的要求（摘要）

| Provider | 必需依赖 | 凭据（`?` = 可选） | 端点 |
| --- | --- | --- | --- |
| `cds` | httpx, xarray, netCDF4, cdsapi | `CDSAPI_KEY`（env）、`~/.cdsapirc`（文件，必须含 `key` 行）；`CDSAPI_URL`? | `cds.climate.copernicus.eu/api/retrieve/v1/` |
| `ncar` | httpx, xarray, netCDF4 | `RDA_API_KEY`?、`~/.netrc`?（公有数据集**不需要**凭据） | rda.ucar.edu 门户 / Datasets API / data.rda.ucar.edu |
| `nasa_earthdata` | httpx, xarray, netCDF4 | `EARTHDATA_TOKEN`（env）、`~/.netrc`? | URS 登录 / GES DISC OPeNDAP |
| `copernicus_datasphere` | httpx, netCDF4 | `CDSE_TOKEN`（env）、keyring `researchdatahub/username`? | Data Space 门户 / OData 目录 |
| `gee` | httpx, earthengine-api（导入名 `ee`） | `~/.config/earthengine/credentials`（文件）、`EE_PROJECT`? | earthengine.googleapis.com |

> CDS 的 `~/.cdsapirc` 之所以是**必需**项：RDH 通过官方 `cdsapi` 客户端发请求，而 cdsapi
> 只读这个文件；`CDSAPI_KEY` 环境变量是辅助来源。获取页面：<https://cds.climate.copernicus.eu/how-to-api>

## 7. 与其它模块的边界

* **不修改** `config` / `core` / `sources` / `planner` / `scheduler` / `cli` 等模块。
* 只**读** `configs/sources*.yaml` 的 `credentials:`（用 `yaml.safe_load`，不走 `config.loader`
  的 `env:` 展开——展开会把真实值带进内存，与「只检查存在性」相悖）。
* `free_space_floor_gb` 默认值与 `config.models.StorageConfig.free_space_floor_gb` 一致（20.0），
  由 `tests/unit/test_environment/test_storage.py::test_default_floor_matches_config_default` 守住。

## 7.5 文档内示例的自检结果（本机真实运行）

本文档里每个 `bash` 示例块都被逐块执行过（脚本 `python - <<'PY'` 抽取全部块并逐个运行）。
除 §3.6 的退出码示例在本机会得到 `exit=1`（因为本机没有配置 `CDSAPI_KEY`，这正是它的用途）外，
其余示例退出码均为 0。`ruff` / `ruff format --check` / `mypy strict` / `pytest` 四条验收命令见 §8。

## 8. 测试

```bash
.venv/bin/python -m pytest tests/unit/test_environment -q
.venv/bin/python -m ruff check src/researchdatahub/environment tests/unit/test_environment
.venv/bin/python -m ruff format --check src/researchdatahub/environment tests/unit/test_environment
.venv/bin/python -m mypy src/researchdatahub/environment
```

测试套件**全程离线**：

* 网络用例一律注入 `httpx.MockTransport`，或连本地 `http.server`（`127.0.0.1`）；
  `conftest.py` 里的 `no_network` 夹具会把任何意外真实请求变成 `AssertionError`（附 URL）。
* 凭据用例注入假 `keyring_getter`，**不接触系统钥匙串**。
* **任何测试都不 import 或调用 cdsapi 的发请求方法。**

## 9. 已知限制

* 依赖检查用 `find_spec`，只证明「已安装」，不证明「能 import 且 ABI 正常」
  （例如 C 扩展版本不匹配）。真正的可用性由首次真实请求确认。
* `copernicus_datasphere` 的 S3 兼容路径需要先用 OAuth 换取 STS 临时凭证，**尚未实现**；
  报告只检查 boto3 与端点是否就绪。
* `nasa_earthdata` / `copernicus_datasphere` 尚未在 `configs/sources.yaml` 里声明
  （该文件由 M3 负责人维护），因此 `source_id=None`，不参与 `sources.yaml` 凭据核对。
* 网络探测结果受本机代理 / VPN 影响；`trust_env=True` 会读取 `HTTP(S)_PROXY`。
  在禁止出网的环境里请用 `redirect_probe_failure=True` 把连接失败降级为 `unknown`。
