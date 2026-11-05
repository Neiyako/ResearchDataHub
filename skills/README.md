# ResearchDataHub Agent Skills

四个面向 Coding Agent 的**官方 Skill**，把 ResearchDataHub 的采集流程封装成 Agent 可直接执行的工作流。
同一份 `SKILL.md` **同时适用于 OpenAI Codex 与 Claude Code**（都是 Agent Skills 开放标准格式）。

## 四个 Skill 与调用顺序

| # | Skill | 职责 | 关键工具 | 是否产生副作用 |
|---|---|---|---|---|
| 1 | [`data-discovery`](./data-discovery/SKILL.md) | 自然语言需求 → 8 字段结构化需求；搜索/检视/比较候选数据集 | `search_datasets` `inspect_dataset` `compare_datasets` `get_provider_capabilities` | 否（只读） |
| 2 | [`environment-setup`](./environment-setup/SKILL.md) | 检查并引导用户**自己**配置依赖与凭证；复验可用性 | `check_environment` `get_environment_requirements` `validate_environment` | 否（只读探测） |
| 3 | [`data-acquisition`](./data-acquisition/SKILL.md) | 估量 → 计划 → 用户确认 → 提交 → 轮询 / 取消 | `validate_request` `estimate_acquisition` `create_execution_plan` `get_execution_plan` `submit_acquisition` `get_task_status` `cancel_task` `list_workers` `get_route_status` | **是**（写库、入队、下载） |
| 4 | [`data-validation`](./data-validation/SKILL.md) | 逐文件完整性校验 + 交付清单 + 三种结论 | `verify_dataset` `get_manifest` | 否（`get_manifest` 可写清单文件） |

```text
data-discovery ──→ environment-setup ──→ data-acquisition ──→ data-validation
   (只读)              (只读探测)          (两阶段确认执行)        (只读校验)
```

主干序列（黄金路径）：`search_datasets` → `inspect_dataset` → `compare_datasets` → `get_provider_capabilities`
→ `check_environment` → `get_environment_requirements` → `validate_environment` →
`validate_request` → `estimate_acquisition` → `create_execution_plan` →（用户确认）→
`submit_acquisition` → `get_task_status` → `verify_dataset` → `get_manifest`
（`list_workers` / `get_route_status` 为可选支线；`cancel_task` / `get_execution_plan` 为恢复分支）。

## 安装路径：两家的差异

| 项目 | Claude Code | Codex |
|---|---|---|
| **个人级 Skill 目录** | `~/.claude/skills/<skill-name>/SKILL.md` | `~/.codex/skills/<skill-name>/SKILL.md`（或 `$CODEX_HOME/skills/...`） |
| **项目级 Skill 目录** | `<repo>/.claude/skills/<skill-name>/SKILL.md` | 未在官方文档正文核对（本文档不承诺） |
| **调用方式** | `/data-discovery` | `$data-discovery`（**未验证**，见下方"已知限制"） |
| **MCP 注册命令** | `claude mcp add --transport stdio --env RDH_ROOT="$HOME/.rdh" rdh-mcp -- rdh-mcp` | `codex mcp add rdh-mcp --env RDH_ROOT="$HOME/.rdh" -- rdh-mcp` |
| **MCP 配置文件** | `~/.claude.json`（local / user 作用域）或项目 `.mcp.json`（需批准） | `~/.codex/config.toml` 的 `[mcp_servers.rdh-mcp]` |
| **查看 MCP 状态** | `claude mcp list` / `claude mcp get rdh-mcp` | `codex mcp list` / `codex mcp get rdh-mcp` |
| **命令名** | `rdh-mcp`（`pip install` 后由 `[project.scripts]` 生成） | 同左 |

安装脚本会自动把四个目录复制到两个位置：

```bash
# macOS / Linux
./scripts/install_skills.sh --dry-run     # 只打印将安装到哪里
./scripts/install_skills.sh               # 安装到 ~/.claude/skills 与 ~/.codex/skills
./scripts/install_skills.sh --claude-only # 只装 Claude Code
./scripts/install_skills.sh --uninstall   # 卸载（只删本脚本装的）
```

```powershell
# Windows
.\scripts\install_skills.ps1 -DryRun
.\scripts\install_skills.ps1
.\scripts\install_skills.ps1 -Uninstall
```

## 同一份 SKILL.md 双端复用的已知限制

调研细节见 [`docs/research/agent-skills-landscape.md`](../docs/research/agent-skills-landscape.md)，此处只列必然影响的几条：

1. **frontmatter 只用标准字段**：本仓库四个 SKILL.md 只用 `name` + `description` + `metadata`（标准 6 字段的子集），
   因此同时通过 Claude Code 的 frontmatter 校验与 Codex 的校验器。
   **不要**添加 `argument-hint` / `when_to_use` / `context` / `disable-model-invocation` 等 Claude Code 专属字段——
   它们在 claude.ai 上传与 Skills API 打包路径会**硬报错**；`disable-model-invocation` 在 Codex 校验器里甚至被要求为 `false`。
2. **正文不使用 Claude Code 专属语法**：没有 `!` 动态命令注入、没有 ` ```! ` 块、没有 `${CLAUDE_SKILL_DIR}`。
3. **调用前缀不同**：Claude Code `/name`，Codex `$name`。正文里统一写"调用 `<skill-name>`"，不写死斜杠前缀。
4. **`allowed-tools` 不用于限制工具**：Claude Code 把它解释为"调用那一轮免权限提示"，且标准标注为实验性；
   本仓库不依赖它，需要免提示请在宿主的权限设置里配置。
5. **参数替换不可移植**：正文不依赖 `$ARGUMENTS` / `$1`。

## 与 MCP Server 的配合

Skills 只**描述流程**，真正的动作全部由 `rdh-mcp` 的 18 个工具完成。启动方式：

```bash
# 前台启动（调试用，stdout 只输出协议帧，日志走 stderr）
rdh-mcp --root ~/.rdh --log-level INFO

# 打印工具清单（不提供服务），用于确认 18 个工具都在
rdh-mcp --print-tools | python3 -m json.tool | grep '"name"'

# 打印版本与信封 schema 版本
rdh-mcp --version
```

宿主侧注册（推荐 stdio + `--env`）：

```bash
# Claude Code
claude mcp add --transport stdio --env RDH_ROOT="$HOME/.rdh" rdh-mcp -- rdh-mcp
claude mcp list            # 应显示 ✔ Connected

# Codex
codex mcp add rdh-mcp --env RDH_ROOT="$HOME/.rdh" -- rdh-mcp
codex mcp list
```

Codex 若手工写 `~/.codex/config.toml`：

```toml
[mcp_servers.rdh-mcp]
command = "rdh-mcp"
args = []
enabled = true

[mcp_servers.rdh-mcp.env]
RDH_ROOT = "/Users/you/.rdh"
```

> `env` 表的值**必须是字符串**。若 `rdh-mcp` 不在 `PATH` 上，`command` 写绝对路径
> （例如 `<repo>/.venv/bin/rdh-mcp`）。

## 安全边界（四个 Skill 共同遵守）

- **绝不索取、打印、上传任何密码 / token / API Key**；`environment-setup` 只转述凭证的**名称**与获取途径。
- **审批两阶段**：`create_execution_plan` 落库并签发一次性 `approval_token`，用户确认后 `submit_acquisition` 才执行；
  令牌不落文件、不回显全文。
- **未验证 ≠ 通过**：`data-validation` 必须如实列出 `skipped` / `unknown` 项。
- **不静默改需求**：不支持的变量/参数通过 `unmatched_variables` 显式上报。

## 校验与回归

```bash
# frontmatter 合法性（YAML 可解析 + name/description 非空 + 仅标准字段）
python3 scripts/check_skills_frontmatter.py

# Skill 正文提到的工具名必须是 MCP 层的 18 个工具之一
python3 scripts/check_skills_frontmatter.py --check-tools
```

（这两个校验也内置在 `scripts/install_skills.sh --dry-run` 的输出里，便于安装前先发现拼写错误。）
