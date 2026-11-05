# Agent Skills：安装、验证与 MCP 配合

> 面向使用者：把 ResearchDataHub 的四个 Agent Skill 装进 **Claude Code** 与 **OpenAI Codex**，
> 并让它们通过 `rdh-mcp` 这 18 个 MCP 工具真正干活。
> Skill 源码在 [`skills/`](../skills/)，双端规范与限制的调研见
> [`docs/research/agent-skills-landscape.md`](research/agent-skills-landscape.md)。

---

## 1. 四个 Skill 与调用顺序

| Skill | 何时触发 | 用到的工具 | 副作用 |
|---|---|---|---|
| `data-discovery` | "我要下载 ERA5…""有没有能提供 X 的数据集" | `search_datasets` `inspect_dataset` `compare_datasets` `get_provider_capabilities` | 无 |
| `environment-setup` | 采集前检查本机能否跑通 / 缺凭证 | `check_environment` `get_environment_requirements` `validate_environment` | 无 |
| `data-acquisition` | 已确认需求，要估量、生成计划、提交、看进度、取消 | `validate_request` `estimate_acquisition` `create_execution_plan` `get_execution_plan` `submit_acquisition` `get_task_status` `cancel_task` `list_workers` `get_route_status` | **有** |
| `data-validation` | 采集完成，要校验与交付清单 | `verify_dataset` `get_manifest` | 无（可写清单文件） |

```text
data-discovery → environment-setup → data-acquisition → data-validation
```

安全设计（四个 Skill 共同遵守）：

- **凭证零接触**：Agent 只转述凭证**名称**与获取途径，绝不索取/打印/上传密码或 token；
  配置文件由用户自己在终端写。
- **审批两阶段**：`create_execution_plan` 落库并签发**一次性 `approval_token`** → 用户确认 →
  `submit_acquisition` 校验令牌后才执行。令牌不进文件、不回显全文。
- **未验证 ≠ 通过**：`data-validation` 必须逐项列出 `skipped` / `unknown`。

---

## 2. 安装

### 2.1 一键脚本（推荐）

```bash
# macOS / Linux：先看会装到哪里，不写文件
./scripts/install_skills.sh --dry-run

# 正式安装（同时装到 Claude Code 与 Codex）
./scripts/install_skills.sh

# 只装一家 / 改用软链接 / 卸载
./scripts/install_skills.sh --claude-only
./scripts/install_skills.sh --codex-only
./scripts/install_skills.sh --link
./scripts/install_skills.sh --uninstall
```

```powershell
# Windows（PowerShell 5.1+）
.\scripts\install_skills.ps1 -DryRun
.\scripts\install_skills.ps1
.\scripts\install_skills.ps1 -ClaudeOnly
.\scripts\install_skills.ps1 -Uninstall
```

脚本行为：

- 安装前自动跑 `python3 scripts/check_skills_frontmatter.py --check-tools`，**校验失败就不安装**；
- **幂等**：重复执行只覆盖这四个 skill，不碰用户其他 skill；
- 每个已安装目录写 `.rdh-skill.json` 标记，`--uninstall` **只删除带标记的目录**；
- `--dry-run` / `-DryRun` 只打印目标路径与将执行的命令，**不写任何文件**。

### 2.2 目标路径差异

| | Claude Code | Codex |
|---|---|---|
| 目录 | `~/.claude/skills/<name>/SKILL.md` | `~/.codex/skills/<name>/SKILL.md` |
| `CODEX_HOME` 覆盖 | 不适用 | 设置了 `CODEX_HOME` 时用 `$CODEX_HOME/skills/<name>/SKILL.md` |
| 项目级 | `<repo>/.claude/skills/` | —（本仓库不承诺） |
| 调用 | `/data-discovery` | `$data-discovery` |
| 符号链接 | 官方文档明确支持 | 未验证（脚本默认复制） |

### 2.3 手工安装（不用脚本）

```bash
# Claude Code
mkdir -p ~/.claude/skills
cp -R skills/data-discovery ~/.claude/skills/

# Codex
mkdir -p ~/.codex/skills
cp -R skills/data-discovery ~/.codex/skills/
```

> 目录名应与 frontmatter 的 `name` 一致（校验脚本会检查这一点）。

---

## 3. 注册 MCP Server（`rdh-mcp`）

Skill 只描述流程，实际动作由 `rdh-mcp` 的 18 个工具执行。先确认 MCP server 能起来：

```bash
# 安装（开发环境）
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,cds,sci]"
.venv/bin/rdh-mcp --version
.venv/bin/rdh-mcp --print-tools | python3 -c "import json,sys; print(len(json.load(sys.stdin)), 'tools')"   # 期望 18
```

### 3.1 Claude Code

```bash
# stdio 方式（推荐；-- 之后的内容原样交给 server 进程）
claude mcp add --transport stdio --env RDH_ROOT="$HOME/.rdh" rdh-mcp -- rdh-mcp

claude mcp list          # 应显示 ✔ Connected
claude mcp get rdh-mcp
```

- 作用域：默认 `local`（只对当前项目）；加 `--scope user` 让所有项目可用；加 `--scope project` 写入
  `.mcp.json`（团队共享，首次使用需要批准）。
- 若 `rdh-mcp` 不在 `PATH` 上：`claude mcp add --transport stdio rdh-mcp -- /abs/path/.venv/bin/rdh-mcp`。

### 3.2 Codex

```bash
# CLI 方式（会自动写进 ~/.codex/config.toml）
codex mcp add rdh-mcp --env RDH_ROOT="$HOME/.rdh" -- rdh-mcp
codex mcp list
codex mcp get rdh-mcp
```

手工配置等价写法（`~/.codex/config.toml`）：

```toml
[mcp_servers.rdh-mcp]
command = "rdh-mcp"
args = []
enabled = true

[mcp_servers.rdh-mcp.env]
RDH_ROOT = "/Users/you/.rdh"
```

> `env` 表的值必须是字符串；`command` 写可执行文件路径（不在 PATH 时用绝对路径）。

### 3.3 `rdh-mcp` 的启动参数

| 参数 | 作用 |
|---|---|
| `--root` | 状态根目录（默认取 `defaults.storage.root`，通常 `~/.rdh`） |
| `--config` | `defaults.yaml` 路径 |
| `--sources` | `sources.yaml` 路径 |
| `--approval-key` | 审批令牌 HMAC 密钥路径（默认 `$RDH_APPROVAL_KEY_FILE` 或 `<root>/approval.key`） |
| `--log-level` | stderr 日志级别，默认 INFO |
| `--print-tools` | 打印 `tools/list` JSON 后退出（调试用） |
| `--version` | 版本 + 信封 schema 版本 |

> **stdout 只输出 MCP 协议帧**，日志全部走 stderr——不要往 stdout 打任何调试输出。

---

## 4. 验证安装

### 4.1 Skill 侧

```bash
# frontmatter 合法性（YAML 可解析 / 必填字段 / 只用标准字段 / 目录名=name）
python3 scripts/check_skills_frontmatter.py

# 再核对正文里的工具名都在 18 个工具里
python3 scripts/check_skills_frontmatter.py --check-tools
```

期望输出（本机实测 2026-11-05）：

```text
PASS skills/data-acquisition/SKILL.md
PASS skills/data-discovery/SKILL.md
PASS skills/data-validation/SKILL.md
PASS skills/environment-setup/SKILL.md

tool-name check: 18 tools known in MCP catalog
summary: 4/4 skill(s) passed
```

### 4.2 宿主侧

```bash
# Claude Code
claude mcp list          # rdh-mcp 应为 ✔ Connected
# 会话里输入 /data-discovery，应能自动列出四个 skill

# Codex
codex mcp list           # rdh-mcp 应出现在列表中
# 新开一次 codex 会话，输入 $data-discovery（前缀语义未验证，若无效可让 Codex 按描述自动触发）
```

### 4.3 端到端冒烟（不下载数据）

在宿主里说：

> 「帮我查一下配置里有哪些 ERA5 数据集」

Agent 应依次调用 `search_datasets` → `inspect_dataset`，并给出候选表——**不会**触发任何下载。
若要验证完整链路但不消耗配额，可把变量缩到一个、时间缩到一天后再 `estimate_acquisition`。

---

## 5. 完整用例（黄金路径）

> 「我需要 2015—2024 年中国区域 ERA5 小时数据，变量包括云液态水、云冰水、温度和风场，保存到当前项目的 data/raw」

Agent 应解析为：变量 `clwc/ciwc/t/u/v`（气压层）、时间 `2015-01-01 → 2025-01-01`（UTC 闭开、逐时）、
区域 `[73,18,135,54]`（W,S,E,N）、垂直层 `1000/850/500 hPa`、输出 `netcdf → ./data/raw`，然后：

| 步 | 工具 | 说明 |
|---|---|---|
| 1 | `search_datasets` | 找候选数据集（cds / ncar） |
| 2 | `inspect_dataset` | 看覆盖率与可用路线（verified / unverified 如实转述） |
| 3 | `compare_datasets` | 2 个以上候选时逐维度对比 |
| 4 | `get_provider_capabilities` | 看缺什么依赖/凭证（**只有名字**） |
| 5 | — | 向用户确认 8 个字段（关键科学参数缺失必须追问） |
| 6 | `check_environment` | 真实探测依赖/凭证/网络/磁盘 |
| 7 | `get_environment_requirements` | `usable=false` 时给出四要素引导（配什么/为什么/去哪/怎么验证） |
| 8 | `validate_environment` | 复验到 `usable=true` 才继续 |
| 9 | `validate_request` → `estimate_acquisition` → `create_execution_plan` | 规范化 → 估文件数与体积 → 落库 + 拿一次性令牌 |
| 10 | （用户确认）→ `submit_acquisition` → `get_task_status` → `verify_dataset` → `get_manifest` | 执行 → 进度 → 校验 → 交付清单 |

第 9 步的请求体（分段格式全文 + 每段允许的键）见
[`skills/data-acquisition/SKILL.md`](../skills/data-acquisition/SKILL.md) §9。

**交付三件套**：交付文件路径 + Manifest 路径 + 校验结论（通过 / 部分通过 / 未通过 + 未验证项）。

---

## 6. 卸载与回滚

```bash
./scripts/install_skills.sh --uninstall            # 删除带 .rdh-skill.json 标记的四个 skill
claude mcp remove rdh-mcp                          # 移除 Claude Code 的 MCP 配置
codex mcp remove rdh-mcp                           # 移除 Codex 的 MCP 配置
```

安装脚本**不会**改动 `~/.claude.json`、`~/.codex/config.toml` 或任何凭证文件。

---

## 7. 已知限制

1. **Codex 的 `$skill-name` 调用前缀**来自社区资料，未在官方文档正文核对；若前缀无效，Codex 仍可按
   `description` 自动触发 Skill（`description` 已写成"何时用"的句式）。
2. **Codex 侧 `developers.openai.com` 本机 403**，因此 `[mcp_servers]` 的官方字段全集未能核对；
   本文给出的结构来自本机 `~/.codex/config.toml` 与 `codex mcp add --help` 的实测。
3. **`--link`（软链接）** 只对 Claude Code 有文档保证；Codex 默认用复制。
4. 四个 SKILL.md 的 frontmatter **刻意只用标准字段**（`name`/`description`/`metadata`），
   因此不支持 `argument-hint`、`when_to_use`、`context: fork` 等 Claude Code 专属能力。
5. Skill 不做参数替换（不依赖 `$ARGUMENTS`），所有参数由 Agent 在对话中收集后显式传给工具。
