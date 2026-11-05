# Agent Skills 与 MCP 接入：Codex / Claude Code 现状调研

> 调研时间：2026-11-05（本机实测 + 官方文档）
> 调研者：researcher（ResearchDataHub 调研员）
> 用途：为 M6「Agent Skills ×4 + 安装脚本 + 文档」提供可复用的事实依据。

## 0. 阅读标注约定

本文每条结论都标注证据等级，**不要**把未标注的推断当成事实：

| 标注 | 含义 |
|---|---|
| **【已读正文】** | 官方文档页面已完整抓取并读过正文（链接可直接点开核对） |
| **【未读正文（仅摘要）】** | 只拿到搜索摘要或二手页面，**未核实原文** |
| **【本机实测】** | 在本机（macOS，`codex-cli 0.147.0`）用命令/二进制字符串实测得到的结论 |

冲突时以「已读正文 > 本机实测 > 未读正文」为准；本机实测与文档不一致的地方，文中单独标出。

---

## 1. 结论先行（可直接影响实现的 8 条）

1. **双端共用同一份 `SKILL.md` 是可行的**，因为 Codex 明确"遵循 Agent Skills 开放标准"，两端的目录约定都是 `<skill-name>/SKILL.md`，frontmatter 都是 YAML。【已读正文：[agentskills.io 规范](https://agentskills.io/specification)、[Codex skills](https://developers.openai.com/codex/skills/)（经 openai/skills README 与客户端展示页引用）】
2. **`allowed-tools` 在标准里是"实验性"字段**，值格式为**空格分隔的字符串**；Claude Code 额外接受"逗号分隔字符串或 YAML 列表"，并把它解释为"调用该 skill 的那一轮免权限提示"，**不会**限制可用工具集合。【已读正文：[agentskills.io/specification](https://agentskills.io/specification)、[Claude Code skills](https://code.claude.com/docs/en/skills)】
3. **标准 frontmatter 只有 6 个字段**：`name`、`description`（必填）、`license`、`compatibility`、`metadata`、`allowed-tools`（可选）。`name` ≤ 64 字符、仅小写字母/数字/连字符；`description` ≤ 1024 字符（Claude Code 侧在 skill 列表里与 `when_to_use` 合并后截断到 1536 字符）。【已读正文：[specification](https://agentskills.io/specification)、[Claude Code skills #frontmatter-reference](https://code.claude.com/docs/en/skills)】
4. **Claude Code 的 frontmatter 超出标准很多**：`when_to_use`、`argument-hint`、`arguments`、`disable-model-invocation`、`user-invocable`、`disallowed-tools`、`model`、`effort`、`context`、`agent`、`background`、`hooks`、`paths`、`shell`、`metadata`、`license`、`compatibility`。**但这些字段一旦进入 claude.ai 上传 / Skills API / `package_skill.py` 打包路径，会直接硬报错**（`Unexpected key(s) in SKILL.md frontmatter`）。→ 若要"同一份 SKILL.md 双端复用"，**只用标准 6 字段**最稳。【已读正文：同上】
5. **Codex 的 skill 安装位置**是 `$CODEX_HOME/skills`（`CODEX_HOME` 未设置时即 `~/.codex/skills`）；Claude Code 的**个人级**位置是 `~/.claude/skills/<skill-name>/SKILL.md`，项目级是 `.claude/skills/...`。【已读正文：[Claude Code skills #where-skills-live](https://code.claude.com/docs/en/skills)；【本机实测】`~/.codex/skills/` 已存在 17 个 skill 目录 + codex 二进制内文案 `$CODEX_HOME/skills (or ~/.codex/skills when CODEX_HOME is unset)`】
6. **`claude mcp add` 的真实形态**（本地 stdio）：`claude mcp add [options] <name> -- <command> [args...]`；`--` 之后的内容原样交给 server 进程，`--env KEY=VALUE` 可重复。示例：`claude mcp add --env AIRTABLE_API_KEY=xxx --transport stdio airtable -- npx -y airtable-mcp-server`。【已读正文：[Claude Code MCP](https://code.claude.com/docs/en/mcp)】
7. **Codex 的 MCP 配置**是 `~/.codex/config.toml` 里的 `[mcp_servers.<name>]` 表，键名 `command` / `args` / `env` / `cwd` / `enabled` / `startup_timeout_sec`（本机 config.toml 实测字段）。CLI 侧有等价命令 `codex mcp add <NAME> [--env K=V] -- <COMMAND>...` 与 `codex mcp add <NAME> --url <URL>`。【本机实测：`codex mcp add --help`、`~/.codex/config.toml`；【未读正文（仅摘要）】官方 config-reference 页 `developers.openai.com/codex/config-reference` 本机 403 无法读取】
8. **Codex 也支持 stdio 之外的 streamable HTTP**（`--url`），插件包用 `.mcp.json` 描述 server（`{"mcpServers": {"<name>": {"type": "http", "url": ...}}}`）。**stdio 的 `command`/`args` 只在本机 config.toml 实测到，未在官方网页正文核对**——实现时请以 `codex mcp add` 写入结果为准（见 §4.2）。【本机实测：[openai/plugins](https://github.com/openai/plugins) 的 `plugins/notion/.mcp.json`】

---

## 2. Agent Skills 开放标准（两端共同底座）

### 2.1 目录结构

```text
skill-name/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 指令
├── scripts/          # 可选：可执行脚本
├── references/       # 可选：按需加载的参考文档
├── assets/           # 可选：模板等资源
└── ...               # 任意其他文件
```

【已读正文：[agentskills.io/specification](https://agentskills.io/specification)、[agentskills.io/home](https://agentskills.io/home)（"Progressive disclosure" 三阶段：发现=只读 name+description；激活=整读 SKILL.md；执行=按需读附加文件）】

### 2.2 frontmatter 字段（标准）

| 字段 | 必需 | 约束（原文要点） |
|---|---|---|
| `name` | ✅ | ≤64 字符；仅小写字母、数字、连字符；不能以连字符开头/结尾 |
| `description` | ✅ | ≤1024 字符；非空；说明"做什么 + 何时用" |
| `license` | — | 许可证名或指向仓库内许可证文件的引用 |
| `compatibility` | — | ≤500 字符；环境要求（目标产品、系统依赖、网络访问等） |
| `metadata` | — | 任意 string→string 映射 |
| `allowed-tools` | — | **空格分隔**的预授权工具字符串（**标注为实验性**） |

【已读正文：[agentskills.io/specification](https://agentskills.io/specification)】

> 注意：`description` 的实际长度上限在两端不同——标准 1024，Claude Code 会把 `description` + `when_to_use` 合并后在"skill 列表"里截断到 1536 字符（**列表展示**上限，非 frontmatter 校验上限）。【已读正文：[Claude Code skills](https://code.claude.com/docs/en/skills)】

### 2.3 标准之外的"本地扩展"

`argument-hint`、`when_to_use`、`disable-model-invocation`、`context: fork`、`model` 等字段在标准里**不存在**。跨端复用写到这些字段会带来两种后果：

- Claude Code：照常工作（它接受全部字段）；
- Claude Code 的 claude.ai 上传 / Skills API / `package_skill.py`：**硬报错**（不是忽略），错误样例：

```text
Unexpected key(s) in SKILL.md frontmatter: argument-hint.
Allowed properties are: allowed-tools, compatibility, description, license, metadata, name
```

【已读正文：同上 §"Using skill frontmatter outside Claude Code"】

**对本项目的直接含义**：四个 SKILL.md 的 frontmatter 只写 `name` / `description`（+ 可选 `allowed-tools`、`metadata`、`license`、`compatibility`）。**不要**写 `argument-hint`、`when_to_use`、`disable-model-invocation`。

---

## 3. Claude Code 侧：路径、调用、MCP

### 3.1 Skill 加载位置（官方表）

| 位置 | 路径 | 生效范围 |
|---|---|---|
| Enterprise | 受管设置目录下 `.claude/skills/<name>/SKILL.md` | 组织内所有用户 |
| **Personal** | **`~/.claude/skills/<name>/SKILL.md`** | 本机所有项目（不含 Cowork/云端会话） |
| Project | `.claude/skills/<name>/SKILL.md` | 该仓库会话（需提交入库才共享） |
| Nested | `<subdir>/.claude/skills/<name>/SKILL.md` | 子目录会话 |
| Additional dir | `--add-dir` 目录内 `.claude/skills/...` | 该会话 |
| Plugin | `<plugin>/skills/<name>/SKILL.md` | 插件启用处（`/plugin-name:skill-name`） |

【已读正文：[Claude Code skills #where-skills-live](https://code.claude.com/docs/en/skills)】

其他实现相关细节（同页已读）：

- **目录可以是符号链接**（enterprise/personal/project 三处的 `<skill-name>` 条目）：Claude Code 从目标目录读 `SKILL.md`，多个位置指向同一目标也只加载一次。→ 安装脚本可以安全用 symlink。
- **保留名**：`synced`（任意大小写）、`anthropic-skills`（或以 `anthropic-skills:` 开头）不能用作 skill 目录名。
- 名字里的**零宽空格 / `<` 等字符会导致该 skill 不加载**。
- frontmatter 只在**文件第一行就是 `---`** 时才被解析；YAML 解析失败时 skill 仍加载但"所有字段为空"（不报错，静默降级）。
- 调用方式：自动（按 `description` 匹配）或主动 `/skill-name`。Claude Code 里 `.claude/commands/*.md` 已被合并进 skills 体系。

### 3.2 `allowed-tools` 的真实语义

> "Tools Claude can use **without asking permission during the turn that invokes this skill**. The grant clears when you send your next message. … It does **not restrict** which tools are available: every tool remains callable."
> —— 【已读正文：[Claude Code skills #pre-approve-tools-for-a-skill](https://code.claude.com/docs/en/skills)】

即：`allowed-tools` 是**免提示授权**，不是沙箱。语法支持 glob 式匹配，例如 `Bash(gh *)`、`Read Grep`、`Bash(${CLAUDE_SKILL_DIR}/scripts/render.sh *)`。**对被检查进仓库的 skill**，官方也提醒要审计它的 `allowed-tools`（因为 project skill 在未信任目录的 `-p` 运行里同样生效）。

### 3.3 MCP server 注册（`claude mcp add`）

真实参数形态（官方原文示例，【已读正文：[Claude Code MCP](https://code.claude.com/docs/en/mcp)】）：

```bash
# 1) 本地 stdio（本项目 rdh-mcp 走这条）
claude mcp add [options] <name> -- <command> [args...]

# 官方示例
claude mcp add --env AIRTABLE_API_KEY=YOUR_KEY --transport stdio airtable \
  -- npx -y airtable-mcp-server

# 2) 远程 HTTP
claude mcp add --transport http <name> <url>
claude mcp add --transport http secure-api https://api.example.com/mcp \
  --header "Authorization: Bearer your-token"

# 3) 远程 SSE（已弃用）
claude mcp add --transport sse <name> <url>
```

要点（同页已读）：

- `--` 是**硬分隔符**：之后的一切都交给 server 进程，Claude Code 不再解析（否则 `--port` 之类会被误当自己的选项）。
- `--env` 可重复；**若 `--env` 后紧跟 server 名，CLI 会把名字当成又一对 `KEY=VALUE` 而报错**——必须把 `--transport stdio` 之类选项夹在两者之间。
- 作用域三档：`local`（默认，写 `~/.claude.json` 的项目条目）/ `project`（`.mcp.json`，可提交入库）/ `user`（`~/.claude.json`，全项目）。
- 项目级 `.mcp.json` 的 server 需要**人工批准**（`⏸ Pending approval`），克隆的仓库不能自我批准（workspace trust 门槛）。
- JSON 入口 `claude mcp add-json <name> '<json>'`；`.mcp.json` 支持 `${VAR}` 与 `${VAR:-default}` 展开；**凭据类变量（`ANTHROPIC_API_KEY` 等）在远端 server 的 `url`/`headers` 里会被读成空串**。
- stdio server 退出信号：macOS/Linux 先 `SIGINT`，100 ms 后 `SIGTERM`，再 400 ms 后 `SIGKILL`；Windows 先关 stdin，300 ms 后结束进程树。
- 校验：`claude mcp list` 会显示 `✔ Connected` / `! Needs authentication` / `✘ Failed to connect`。

### 3.4 对本项目的建议写法

```bash
# Claude Code（user 级，所有项目可用）
claude mcp add --transport stdio --env RDH_ROOT="$HOME/.rdh" rdh-mcp -- rdh-mcp
```

`rdh-mcp` 的实际入口与参数见本仓库 `pyproject.toml` 的 `[project.scripts]` 与 `src/researchdatahub/interfaces/mcp/server.py`（`--root` / `--config` / `--sources` / `--approval-key` / `--log-level` / `--print-tools` / `--version`）。

---

## 4. Codex 侧：路径、调用、MCP

### 4.1 Skill 安装位置与调用

- **安装位置**：`$CODEX_HOME/skills`，`CODEX_HOME` 未设置时 `~/.codex/skills`。**本机实测**：`~/.codex/skills/` 已存在（17 个 skill 目录），且 `codex` 二进制内含文案 *"place it in `$CODEX_HOME/skills` (or `~/.codex/skills` when `CODEX_HOME` is unset) so Codex can discover it automatically"*、【只读引用】`strings` 提取。
- **调用方式**：`$skill-name`（Codex 的 skill 触发前缀是 `$`，不是 `/`）。【未读正文（仅摘要）】来自 openai/skills README 的示例 `$skill-installer gh-address-comments`（该 README **已读正文**：[openai/skills](https://github.com/openai/skills)），前缀语义来自客户端展示页引用；**未在官方 skills 正文页核对**。
- **内置安装器**：`$skill-installer`（可从 `openai/skills` 或任意 GitHub 目录安装）。【已读正文：[openai/skills README](https://github.com/openai/skills)】
- **历史仓库已弃用**：`openai/skills` README 顶部明确写 *"This repository is deprecated. For current Codex skill and plugin examples, use the [OpenAI Plugins repository]"*，并指向 `developers.openai.com/codex/plugins/build`。【已读正文：同上】

> ⚠️ **取证限制**：`developers.openai.com` 本机**直连返回 403（"This request was blocked"）**，`r.jina.ai` 代理超时。因此 Codex 官方 skills 页面（`https://developers.openai.com/codex/skills/`）**未能读取正文**。本文所有 Codex 侧"官网"结论均标注来源等级。

### 4.2 MCP 配置：`~/.codex/config.toml` 的 `[mcp_servers]`

**本机实测**（`~/.codex/config.toml`，`codex-cli 0.147.0`）真实结构：

```toml
[mcp_servers]

[mcp_servers.computer-use]
command = "./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
args = ["mcp"]
cwd = "."
enabled = false

[mcp_servers.node_repl]
args = []
command = "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS = "1000"
CODEX_HOME = "/Users/apple/.codex"
```

由此确认（**本机实测**）：
- 表名：`[mcp_servers.<name>]`；子表 `[mcp_servers.<name>.env]` 放环境变量（**值必须是字符串**）；
- 已验证出现的键：`command`（string）、`args`（array of string）、`env`（table）、`cwd`（string）、`enabled`（bool）、`startup_timeout_sec`（int）。

**本项目建议写入（stdio）**：

```toml
[mcp_servers.rdh-mcp]
command = "rdh-mcp"
args = []
enabled = true

[mcp_servers.rdh-mcp.env]
RDH_ROOT = "/Users/apple/.rdh"
```

> `rdh-mcp` 必须在 `PATH` 上（`pip install -e .` 后由 `[project.scripts]` 生成）。若不在 PATH，用绝对路径，例如 `command = "/Users/apple/Desktop/ResearchDataHub/.venv/bin/rdh-mcp"`。
> 本机实测提醒：`args = []` 是合法的空数组；若把命令参数写在 `args` 里，务必保证 `command` 本身是**可执行文件路径**，而不是带空格的整条命令。

**CLI 等价写法**（**本机实测**：`codex mcp add --help`）：

```bash
codex mcp add rdh-mcp --env RDH_ROOT="$HOME/.rdh" -- /path/to/.venv/bin/rdh-mcp
codex mcp add --url https://example.com/mcp remote-name   # streamable HTTP
codex mcp list / codex mcp get rdh-mcp / codex mcp remove rdh-mcp / codex mcp login|logout
```

`codex mcp add` 的帮助文本确认：`--env <KEY=VALUE>` 仅对 stdio server 有效；`--url <URL>` 是 streamable HTTP；`--bearer-token-env-var`、`--oauth-client-id`、`--oauth-resource` 用于 HTTP server。

**官方文档状态**：`~/.codex` 仓库内 `docs/config.md`、`docs/skills.md`、`docs/example-config.md` 现在都只剩一句话跳转到 `developers.openai.com`（**已读正文**：[openai/codex docs/config.md](https://github.com/openai/codex/blob/main/docs/config.md)），而该站点本机 403。因此 **`[mcp_servers]` 的官方字段全集本文无法给出**；上表只列本机实测出现过的键。

### 4.3 Skill 的目录与 frontmatter（Codex 侧）

- Codex 二进制内（本机实测，`strings codex`）有两条互相印证的约束：
  1. 插件/技能校验器里 `allowed_properties = {"name", "description", "license", "allowed-tools", "metadata"}`，以及错误文案 `Unexpected key(s) in SKILL.md frontmatter: ...`——**与 Agent Skills 标准的字段集合一致**（区别：该处未列 `compatibility`，可能是校验器未跟进标准，**未验证**）。
  2. 校验器要求 `SKILL.md` 以 YAML frontmatter 开头、`name`/`description` 非空，且 `disable-model-invocation` **必须为 false**（出现即报错）——说明 Codex 的 skill 校验器**不接受** Claude Code 特有的 `disable-model-invocation: true`。【本机实测：`strings` 提取的 Python 内嵌脚本】
- `$CODEX_HOME/skills` 下的目录若不以 `SKILL.md` 为入口会被报 `skill <name> is missing SKILL.md`。

> **实现含义**：四个 SKILL.md 的 frontmatter 用标准 6 字段即可同时通过两端校验；**只写 `name` + `description`** 是最保险的交集。

---

## 5. 同一份 SKILL.md 双端复用的已知限制（汇总）

| # | 限制 | 证据 |
|---|---|---|
| L1 | Claude Code 专属 frontmatter（`argument-hint` / `when_to_use` / `context` / `agent` / `hooks` / `paths` / `shell` / `disallowed-tools` / `user-invocable` / `model` / `effort` / `background` / `arguments`）在 claude.ai 上传与 Skills API 路径**硬报错** | 【已读正文】[Claude Code skills](https://code.claude.com/docs/en/skills) |
| L2 | Codex 的校验器把 `disable-model-invocation` 视为非法（要求其为 false） | 【本机实测】`strings codex` 内嵌校验脚本 |
| L3 | 正文里的 Claude Code 专属语法（`` !`cmd` `` 动态注入、` ```! ` 块、`${CLAUDE_SKILL_DIR}`）在 Codex 侧不会展开，会以字面文本进入上下文 | 【已读正文】[Claude Code skills #inject-dynamic-context](https://code.claude.com/docs/en/skills)；Codex 侧无对应实现（**未验证**，官网 403） |
| L4 | `allowed-tools` 语法两端解释器不同：Claude Code 支持 `Bash(gh *)` 这类**工具+参数模式**；标准只规定"空格分隔字符串"，Codex 的实际解析**未验证** | 【已读正文】specification + Claude Code skills |
| L5 | 触发前缀不同：Claude Code `/skill-name`，Codex `$skill-name`；文档里若写"输入 `/xxx` 调用"会误导 Codex 用户 | 【已读正文】Claude Code；【未读正文（仅摘要）】Codex |
| L6 | 安装位置不同（`~/.claude/skills/` vs `~/.codex/skills/` 或 `$CODEX_HOME/skills`），且 Codex 支持 `CODEX_HOME` 重定向 | §3.1、§4.1 |
| L7 | 变量替换差异：`$ARGUMENTS` / `$1` 在 Claude Code 有明确语义；Codex 对 `$` 前缀的处理未验证，**正文里不要依赖参数替换** | 【已读正文】Claude Code skills #available-string-substitutions |
| L8 | 长度约束：`name`≤64、`description`≤1024 是标准的硬约束；Claude Code 列表另截断到 1536（含 `when_to_use`） | 【已读正文】specification / Claude Code skills |
| L9 | Claude Code **不报错地忽略**未知 frontmatter 字段（"a field name must match the table exactly … ignores a field it doesn't recognize without reporting an error"）——拼错字段会**静默失效** | 【已读正文】[Claude Code skills #frontmatter-reference](https://code.claude.com/docs/en/skills) |

**落地建议（M6 直接照做）**：

1. frontmatter 只用 `name` + `description`（必要时加 `metadata`），别用两端不一致的字段；
2. 正文**不要**用 `!` 动态注入、不要用 `${CLAUDE_SKILL_DIR}`、不要依赖 `$ARGUMENTS`；
3. 需要"Claude Code 免提示"的场合，在 `docs/skills.md` 里给**安装后另行配置权限**的说明（例如把 MCP 工具加进 allow 规则），而不是把参数塞进 `allowed-tools`；
4. 安装脚本按平台分流复制到 `~/.claude/skills/` 与 `~/.codex/skills/`（Claude Code 支持符号链接，Codex 侧未验证 symlink 行为 → **默认复制，`--link` 需人工验证**）。

---

## 6. 对 ResearchDataHub 的具体落地参数

| 项 | Claude Code | Codex |
|---|---|---|
| Skill 目录 | `~/.claude/skills/<name>/SKILL.md`（或项目 `.claude/skills/`） | `~/.codex/skills/<name>/SKILL.md`（或 `$CODEX_HOME/skills`） |
| 调用 | `/data-discovery` | `$data-discovery`（**未验证**） |
| MCP 注册 | `claude mcp add --transport stdio --env RDH_ROOT=$HOME/.rdh rdh-mcp -- rdh-mcp` | `codex mcp add rdh-mcp --env RDH_ROOT=$HOME/.rdh -- rdh-mcp`（等价于 `[mcp_servers.rdh-mcp]`） |
| 配置文件 | `~/.claude.json`（local/user）/ `.mcp.json`（project） | `~/.codex/config.toml` |
| 查看状态 | `claude mcp list`、`claude mcp get rdh-mcp` | `codex mcp list`、`codex mcp get rdh-mcp` |
| 前置条件 | `rdh-mcp` 在 PATH 上；项目级 `.mcp.json` 需批准 | 同左；`[mcp_servers.*.env]` 值必须是字符串 |

---

## 7. 来源清单（按可信度）

**已读正文**
- [agentskills.io · Overview](https://agentskills.io/home)
- [agentskills.io · Specification](https://agentskills.io/specification)（frontmatter 全表、"progressive disclosure"、`allowed-tools` 标为实验性）
- [agentskills.io · Client Showcase](https://agentskills.io/clients)（点名 CLI 列表，含 "ChatGPT & Codex" → `https://developers.openai.com/codex/skills/`）
- [GitHub · agentskills/agentskills README](https://github.com/agentskills/agentskills)
- [Claude Code · Extend Claude with skills](https://code.claude.com/docs/en/skills)（位置表、frontmatter 全表、`allowed-tools` 语义、标准 6 字段与硬报错、symlink、保留名）
- [Claude Code · Connect to tools via MCP](https://code.claude.com/docs/en/mcp)（`claude mcp add` 语法与 `--` 分隔、`--env`、scopes、`.mcp.json` 批准、stdio 退出信号）
- [GitHub · openai/skills README](https://github.com/openai/skills)（已弃用声明、`$skill-installer`）
- [GitHub · openai/plugins README](https://github.com/openai/plugins)（插件包含 `skills/`、`.mcp.json`）
- [GitHub · openai/codex docs/config.md](https://github.com/openai/codex/blob/main/docs/config.md)、[docs/skills.md](https://github.com/openai/codex/blob/main/docs/skills.md)（均已改为跳转官网，正文无内容）

**未读正文（仅摘要）**
- `https://developers.openai.com/codex/skills/`、`/codex/config-reference`、`/codex/mcp` —— 本机直连 **403**（"This request was blocked"），代理 `r.jina.ai` 超时。**结论：Codex MCP 配置的官方字段全集本文未能核对。**

**本机实测**
- `codex-cli 0.147.0`：`codex mcp add --help`、`~/.codex/config.toml` 结构、`strings $(which codex)` 中的 skill 校验器文案、`~/.codex/skills/` 存在性。
- 未安装 `claude` CLI（`command not found`），Claude Code 侧全部依据官方文档正文。

---

## 8. 待办 / 未验证项（交给实现者）

1. Codex 侧 `$skill-name` 触发前缀与 symlink 支持——需在装了 Codex 的机器上实跑一次验证。
2. Codex 校验器是否接受 `compatibility` 字段（标准里有，本机校验器清单里没有）——**未验证**。
3. `codex mcp add` 写入后 `~/.codex/config.toml` 的确切产物（是否补 `startup_timeout_sec` 默认值）——建议安装脚本后用 `codex mcp get rdh-mcp` 截图/贴输出确认。
4. `developers.openai.com` 需要可访问网络后重读 `/codex/skills`、`/codex/config-reference` 补全官方字段表。
