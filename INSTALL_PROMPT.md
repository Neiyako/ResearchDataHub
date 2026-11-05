# 给 Agent 的安装提示词

把下面**整段**复制给 Codex 或 Claude Code，它会自己完成安装与配置。

---

```text
请帮我安装并配置 ResearchDataHub（科研数据获取基础设施）：
仓库 https://github.com/Neiyako/ResearchDataHub

步骤：
1. git clone 到 ~/ResearchDataHub（若已存在则跳过），cd 进去
2. python3 -m venv .venv && .venv/bin/pip install -e ".[mcp,sci]"
3. 把 MCP Server 注册到你自己：
   - Claude Code: claude mcp add researchdatahub -- <仓库绝对路径>/.venv/bin/rdh-mcp
   - Codex: 在 ~/.codex/config.toml 加
       [mcp_servers.researchdatahub]
       command = "<仓库绝对路径>/.venv/bin/rdh-mcp"
4. bash scripts/install_skills.sh
5. 用 `.venv/bin/rdh version` 验证退出码为 0，然后告诉我 MCP 工具是否可见

注意：不要向我索要任何密码或 token；需要凭据时教我自己在终端 export。
```

---

## 装完之后

直接说需求即可，例如：

> 帮我获取 2015—2024 年中国区域的 ERA5 小时数据，包含温度、风场、云液态水和云冰水，
> 保存到当前项目的 data/raw。

Agent 会自动走完：

```
搜索数据源 → 检查数据集能力 → 检查环境 → 引导配置凭据
→ 校验请求 → 生成计划 → 等你确认 → 下载 → 完整性校验
→ 交付到 data/raw + 返回 Manifest
```

## 需要凭据时

Agent **不会**向你索要密码/token。它会告诉你：配什么、为什么、去哪拿、存到哪、怎么验证。
你自己在终端 export，例如：

```bash
# CDS（Copernicus Climate Data Store）
export CDSAPI_KEY="你的令牌"

# NASA Earthdata
export EARTHDATA_TOKEN="你的令牌"

# Copernicus Data Space
export CDSE_CLIENT_ID="..."
export CDSE_CLIENT_SECRET="..."
```

然后让 Agent 重新检查环境即可。

## 手动安装

```bash
git clone https://github.com/Neiyako/ResearchDataHub.git
cd ResearchDataHub
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp,sci]"
.venv/bin/rdh version          # 退出码 0 即成功
bash scripts/install_skills.sh
```

需要 Python 3.12+。
