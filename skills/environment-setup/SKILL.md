---
name: environment-setup
description: 在真正开始下载之前，检查并引导用户配置 ResearchDataHub 数据源所需的依赖与凭证（如 CDS 的 CDSAPI_KEY、NCAR/NASA 的认证），全程由用户自己在终端完成配置，Agent 绝不索取或打印任何密码或令牌。当采集前需要"这台机器能不能跑通"的判断、出现 MISSING_CREDENTIAL 类阻断、或用户问"怎么配置 CDS/Earthdata 凭证"时使用。
metadata:
  type: workflow
  version: "1.0.0"
  stage: "2/4"
  next-skill: data-acquisition
---

# environment-setup —— 环境检查与凭证配置引导

> 四段式流程的**第 2 段**：`data-discovery` → `environment-setup` → `data-acquisition` → `data-validation`
> 本段的目标是回答**一个**问题：**"这台机器现在能不能为该 Provider 真正执行采集？"**
> 不行时给出「配什么 / 为什么 / 去哪拿 / 存到哪 / 怎么验证」四要素引导。

## 1. 何时使用

- `data-discovery` 已确认方案，进入采集前；
- 上一次采集报 `source_unavailable` / `not_available` 类错误且与"缺凭证/缺依赖/端点不可达"有关；
- 用户主动问"我要用 CDS/NCAR/NASA，需要准备什么"。

**不适用**：本地文件分析、纯概念问答、用户已明确说"我配好了直接用"（仍要跑一次 `validate_environment` 作为证据）。

## 2. 前置条件

- `rdh-mcp` 已连接；`--root` 指向的状态根已初始化（`rdh db init`）。
- **你（Agent）不能**替用户输入密码、不能读任何认证文件内容、不能把凭证写进任何输出。
  → 本 Skill 的全部动作是"**检查 → 解释 → 引导用户自己做 → 复验**"。

## 3. 工具调用序列

```text
① check_environment(provider, checks?, timeout_seconds?)
② get_environment_requirements(provider)          # 仅当 ① 不可用（usable=false）时
③ （用户自己完成配置）
④ validate_environment(provider, require_network?, checks?, timeout_seconds?)
```

### 3.1 `check_environment`（必填 `provider`）

| 参数 | 类型 | 说明 |
|---|---|---|
| `provider` | string | 如 `cds` / `ncar` |
| `checks` | array of `dependencies` \| `credentials` \| `network` \| `disk` | 只跑指定检查项；省略=全部 |
| `timeout_seconds` | number 0.1–120 | 单次网络/磁盘检查超时，默认 5 s |

返回（关键字段）：`status`（`ok` / `needs_configuration` / `unavailable`）、`usable`（bool）、`summary`、
`report.requirements[]`（每项含 `name` / `kind` / `status` / `required` / `guidance` / `help_url` / `observed`）、
`report.errors[]`。
**会发起真实网络请求**（端点可达性 HEAD），但**不下载数据**。

### 3.2 `get_environment_requirements`（必填 `provider`）

返回 `requirements`（依赖 / 凭证引用 / 端点声明）与 `guide`：

```
guide.provider, guide.summary,
guide.steps[] = { action, why, where, verify }
```

`guide.steps` 就是官方的四要素引导，**直接逐条讲给用户**，不要自己编：
- `action`：要做什么
- `why`：为什么需要
- `where`：去哪拿 / 存到哪（含真实 URL 与文件路径模板）
- `verify`：怎么验证生效（通常是一条可复制的命令）

`requirements[].reference` 只包含**凭证引用**（`kind`: `env` / `file` / `keyring`，`name`: 变量名或路径模板）——
**只有名字，没有值**。请原样转述名字，绝不索取值。

### 3.3 `validate_environment`（必填 `provider`）

| 参数 | 类型 | 说明 |
|---|---|---|
| `provider` | string | 同上 |
| `require_network` | bool | 是否必须联网通过才算可用，默认 `false` |
| `checks` | 同上 | 省略=全部 |
| `timeout_seconds` | number 0.1–120 | 默认 5 s |

返回：`usable`（bool）、`status`、`blocking[]`（每项 `name` / `kind` / `status` / `guidance`）、`report`。
**只有 `usable=true` 才允许进入 `data-acquisition`。**

## 4. 状态语义（必须按此话术对用户解释）

| `status` | 含义 | 你该说什么 |
|---|---|---|
| `ok` | 全部必需要求已就绪 | 「这台机器可以执行该数据源的采集，继续下一步。」 |
| `needs_configuration` | 缺**可修复**的东西（依赖没装 / 凭证没配 / 端点暂时不可达） | 「还差 N 项，按下面 4 条做完就能用」+ 逐条给四要素 |
| `unavailable` | **结构性**不可用（必需端点 5xx、平台不支持），或用户显式跳过必需修复项 | 「当前环境结构性不可用，原因：…；换源或换机器」 |

> 口径来自实现：`unknown` 状态一律降级为 `needs_configuration`（不允许被忽略）；
> 必需端点的 5xx 视为结构性失败 → `unavailable`。

## 5. 输出给用户的模板（怎么配 / 为什么 / 去哪拿 / 存到哪 / 怎么验证）

```markdown
### 还差 2 项才能开始采集（数据源：cds）

**① 缺少凭证 `CDSAPI_KEY`**
- 配什么：环境变量 `CDSAPI_KEY`，或写进 `~/.cdsapirc` 的 `key:` 一行
- 为什么：CDS 的检索与下载接口需要你的个人访问令牌（PAT）
- 去哪拿：https://cds.climate.copernicus.eu/ → 登录 → 用户页面的 API 令牌区
- 存到哪：你自己终端里 `export CDSAPI_KEY="..."`（不要发给我），或按官网写入 `~/.cdsapirc`
- 怎么验证：`rdh config validate`，看到 PASS 即可

**② Python 依赖 `cdsapi` 未安装**
- 配什么：`pip install "researchdatahub[cds]"`
- 为什么：CDS 的异步提交/轮询走 `cdsapi` 客户端
- 存到哪：你当前项目的虚拟环境（`.venv`）
- 怎么验证：`python -c "import cdsapi"` 无报错

配完告诉我「配好了」，我会重新跑一次 validate_environment 复验。
```

**每条必须四要素齐全**（配什么 / 为什么 / 去哪拿或存哪 / 怎么验证），缺一不可。

## 6. 失败分支与恢复动作

| 情况 | Agent 动作 |
|---|---|
| `check_environment` 返回 `not_available`（`data.capability` 指向 `environment` 包） | 说明"环境检查组件未安装"，提示 `pip install -e ".[dev]"` 或确认安装方式；**不要**假装环境没问题 |
| `status=needs_configuration` | 调 `get_environment_requirements`，按 `guide.steps` 逐条输出四要素 |
| `status=unavailable` | 说明结构性原因（看 `report.requirements[].observed` 里的 `status_code`），建议换源或换机器；**不要**反复重试 |
| `validate_environment` 仍 `usable=false` | 只对 `blocking[]` 里的项给出**具体**下一步；不要笼统说"再试一次" |
| 用户说"我把 token 发给你" | **拒绝**：「请不要把令牌发给我，也不需要贴进对话；请在你自己的终端/配置文件里设置，我只做检查」 |
| 用户贴出疑似密钥的字符串 | 不要复述、不要保存、提示其轮换该密钥 |
| 错误码 `invalid_arguments` | 按 hint 修正参数名/取值（schema 顶层禁止未知字段） |
| 错误码 `config_error` | 提示检查 `rdh config validate` 与 `--root` 指向 |

完整错误码表见 `skills/data-acquisition/SKILL.md` §7。

## 7. 禁止事项（本 Skill 的红线）

1. **绝不索取、打印、上传任何密码 / token / API Key / 私钥内容。**
2. **绝不读取认证文件内容**（`~/.cdsapirc`、`~/.netrc`、`~/.config/earthengine/credentials` 等）——只检查**存在性**与**权限**。
3. **绝不替用户执行 `export`/写配置文件**——配置由用户在自己终端完成。
4. 不得把 `needs_configuration` 说成"环境不可用"，也不得把 `unavailable` 说成"配一下就好"。
5. 不得在 `usable=false` 时继续进入 `data-acquisition`。

## 8. 黄金路径中属于本 Skill 的步骤（第 6–8 步）

> 承接 `data-discovery` 第 5 步用户确认后的需求：
> ERA5 气压层 2015-01-01→2025-01-01，`[73,18,135,54]`，变量 `clwc/ciwc/t/u/v`，层 1000/850/500，→ `./data/raw`

| 步 | 工具 | 传什么 | 期望返回 | 用户看到什么 |
|---|---|---|---|---|
| 6 | `check_environment` | `{"provider":"cds","checks":["dependencies","credentials","network","disk"]}` | `status`、`usable`、`requirements[]` | 「环境检查：缺 CDSAPI_KEY 与 cdsapi」 |
| 7 | `get_environment_requirements` | `{"provider":"cds"}`（仅当上一步 `usable=false`） | `guide.steps[]` 四要素 | 「怎么配 / 为什么 / 去哪拿 / 怎么验证」清单 |
| 8 | `validate_environment` | `{"provider":"cds","require_network":true}` | `usable:true`、`blocking:[]` | 「✅ 环境就绪，可以开始采集」 |

若第 8 步仍 `usable=false` → **停在这里**，把 `blocking[]` 逐条转成四要素引导，等用户回话；不要跳步。
