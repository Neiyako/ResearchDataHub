# ResearchDataHub（RDH）

ResearchDataHub，简称 RDH，是一个帮助科研人员获取公开科研数据的工具。

它把“找到合适的数据、填写下载条件、下载文件、检查文件、整理结果”串成一条可重复的流程。你可以直接用自然语言告诉 AI 助手想要什么数据，也可以在终端里逐步执行。

RDH 适合气象、气候、遥感和地球科学数据。它不会替你做数据分析、画图或建立模型。它负责把数据可靠地取回来，并留下足够的记录，让你知道数据来自哪里、是否完整、什么时候取得。

## 一句话理解

你告诉 RDH：

> 获取 2015—2024 年中国区域的 ERA5 小时温度和风场数据，保存到当前项目的 `data/raw`。

RDH 会依次完成：

1. 找到可能提供这份数据的平台。
2. 检查平台是否真的有你要的变量、时间和区域。
3. 检查本机的网络、磁盘和所需账号配置。
4. 把一次大需求拆成多个可恢复的小任务。
5. 下载文件，并在中断后继续下载或安全重试。
6. 检查文件大小、校验和、格式以及可识别的科学信息。
7. 将确认完整的文件交付到你指定的目录。
8. 生成清单和报告，记录来源、文件、校验结果和失败原因。

## RDH 解决什么问题

直接下载科研数据时，常见问题包括：

- 平台名称相似，容易选错数据集。
- 日期、变量名和区域写法因平台而不同。
- 下载中断后只能从头开始。
- 文件下载完成了，却没有确认内容是否完整。
- 同一份数据过一段时间后无法说明来源和取得时间。
- 一个文件失败后，人工很难找出究竟缺了什么。

RDH 将这些步骤固定下来。它不会把“文件存在”当作“数据正确”，也不会把无法验证的结果标记为完成。

## 适合谁使用

- 需要批量获取 ERA5、NCAR 等科研数据的学生和研究人员。
- 希望让 AI 助手代为执行数据获取流程的人。
- 需要保留数据来源、下载记录和完整性证据的课题组。
- 想把一次数据获取过程重复运行，或迁移到另一台电脑的人。

你不需要先了解 RDH 的内部结构。只需要准备数据需求和相应平台要求的账号或令牌。

## 推荐用法：让 AI 助手操作

RDH 可以通过 MCP 接入 Codex、Claude Code 等 AI 编程助手。安装后，你可以直接描述需求。AI 助手会在需要时询问你确认范围、账号配置和保存位置。

### 安装

需要 Python 3.12 或更高版本。下面的 Python 命令适用于 macOS、Linux 和 Windows PowerShell。

```bash
git clone https://github.com/Neiyako/ResearchDataHub.git
cd ResearchDataHub
python -m venv .venv
python -m pip install -e ".[mcp,sci]"
rdh version
```

这里的两个可选组件用途如下：

- `mcp` 让 AI 助手能够调用 RDH。
- `sci` 用于读取 NetCDF 等科研文件的基本信息。

需要使用 Copernicus CDS 时，再安装：

```bash
python -m pip install -e ".[cds]"
```

### 接入 Codex 或 Claude Code

将 `.venv/bin/rdh-mcp` 注册为 MCP 服务。具体配置方式取决于你使用的 AI 助手。注册完成后，在对话中直接提出需求，例如：

> 获取 2015 年中国区域的 ERA5 小时 2 米气温，保存到当前项目的 `data/raw`。先检查数据源和我的配置，不要立即下载。

RDH 会先给出数据源、任务数量和需要的配置。涉及真实下载时，建议先查看计划，再确认执行。

## 不使用 AI 时的用法

RDH 也可以单独在终端运行。典型流程如下：

```bash
# 复制数据源配置模板
cp configs/sources.example.yaml configs/sources.yaml

# 检查配置
.venv/bin/rdh config validate

# 检查需求文件
.venv/bin/rdh request validate examples/era5-request.yaml

# 查看可用路线和数据源
.venv/bin/rdh route plan examples/era5-request.yaml
.venv/bin/rdh route evaluate examples/era5-request.yaml --no-probe
.venv/bin/rdh route select examples/era5-request.yaml

# 生成下载计划
.venv/bin/rdh plan create examples/era5-request.yaml

# 执行计划
.venv/bin/rdh task run <plan_id> --max-tasks 1 --max-workers 1

# 生成结果报告
.venv/bin/rdh report generate <request_id>
```

第一次使用时，建议先用小范围、短时间的数据验证配置，再扩大到正式任务。

## 一次获取任务是怎样完成的

### 1. 说明需求

需求可以通过自然语言告诉 AI，也可以写成 YAML 文件。需求通常包括：

- 数据集或产品名称。
- 变量，例如温度、降水、风速。
- 时间范围。
- 地理范围。
- 空间或时间分辨率。
- 输出格式。
- 保存位置。

RDH 会检查日期边界、坐标顺序、变量名称和单位等容易出错的内容。平台内部的变量名称由对应的数据源适配，不要求你记住每个平台的缩写。

### 2. 查找并比较数据源

RDH 会先查看官方数据入口，再考虑经过验证的其他入口。Copernicus CDS 可以作为回退路径，但不会因为习惯而自动优先使用它。

选择数据源时，RDH 会比较：

- 是否提供同一数据集和版本。
- 是否包含请求的变量、时间和区域。
- 是否满足格式和分辨率要求。
- 当前连接是否成功。
- 过去的成功情况和实测速度。

速度不能抵消数据不一致或来源不明。每次选择都会记录原因。

### 3. 检查本机环境

RDH 会检查网络连通性、代理环境、可用磁盘空间、Python 依赖和凭据引用。凭据只从环境变量或用户配置中读取，RDH 不会把密码、令牌写入报告或长期记录。

缺少账号配置时，系统会告诉你需要配置什么，以及如何重新检查。它不会伪造“已连接”或“已下载”。

### 4. 生成并执行计划

一项需求可能对应很多文件。RDH 会按照日期、变量或数据源允许的自然范围拆分任务，并记录每个任务的状态。

多个相互独立的任务可以同时执行。并行数量受到本机资源和数据源限制，避免为了追求速度而压垮电脑或触发平台限制。

### 5. 处理中断和失败

下载时，RDH 会：

- 使用临时文件，避免半个文件被误认为正式结果。
- 对支持分段下载的文件记录字节范围。
- 检查远端文件是否在下载过程中发生变化。
- 对超时、临时网络错误和平台限流进行有限重试。
- 在程序退出、断电或网络短暂中断后恢复可安全恢复的任务。
- 将无法恢复的文件标记为失败或隔离，不报告为成功。

每次实际响应的大小默认不能超过 400 MiB。超过限制时，RDH 会停止该次响应并报告原因。

### 6. 验证和交付

下载结束后，RDH 会检查文件是否存在、是否为空、大小是否符合预期、校验和是否一致，以及文件格式是否可读取。对支持的科研格式，还会检查变量、维度和时间信息。

只有通过要求的检查，文件才会进入正式数据目录。验证失败的文件会进入隔离目录，并保留失败记录，方便后续排查。

## 你会得到什么

一次成功的获取任务通常会产生：

- 正式数据文件，默认交付到用户指定的目录，例如 `data/raw`。
- `Delivery Manifest`，记录数据集、文件、大小、校验和、来源和取得时间。
- 下载任务和重试记录。
- 完整性检查报告。
- 失败项、缺失项和需要人工处理的事项。

RDH 默认不覆盖已有正式文件。目标位置已有同名文件时，会先报告冲突，再由你决定如何处理。

## 目前支持的来源

| 数据来源 | 当前能力 | 使用条件 |
|---|---|---|
| Copernicus CDS | ERA5 等 CDS 数据集的查询、规划和下载 | 通常需要 CDS 个人访问令牌 |
| NSF NCAR GDEX / RDA | 目录查询、文件发现和下载 | 公开资源可直接使用，受平台规则约束 |
| NASA Earthdata | CMR 目录查询和能力声明 | 真实受保护资源下载需要 Earthdata 账号 |
| Copernicus Data Space | STAC/OData 目录查询和 OAuth2 配置 | 真实下载需要 CDSE 账号 |

Google Earth Engine、FTP、SFTP、Globus、Zarr 以及其他未列出的平台目前不能当作已支持来源。RDH 会明确报告未实现或未验证，不会把预留接口说成可用功能。

## 数据安全和可追溯性

- 凭据通过环境变量或受控配置引用提供，不写入普通日志。
- 预签名地址中的令牌和签名会在日志中隐藏。
- 下载中的文件放在临时区域，验证通过后才原子提交。
- 原始数据默认保留为只读文件。
- 损坏或不完整的文件进入隔离区，不会被静默删除。
- SQLite 数据库保存任务状态、事件和文件登记，是运行过程的记录来源。

## 常用命令

```bash
rdh version                         # 查看版本
rdh config validate                 # 检查配置
rdh source list                     # 查看数据源
rdh db init                         # 初始化本地数据库和目录
rdh db status                       # 查看数据库状态
rdh request validate <request.yaml> # 检查需求文件
rdh plan create <request.yaml>      # 创建下载计划
rdh plan show <plan_id>             # 查看计划
rdh task run <plan_id>              # 执行计划
rdh task status                     # 查看任务状态
rdh task reconcile                  # 回收过期任务并对账
rdh file verify <path>              # 检查文件
rdh report generate <request_id>    # 生成报告
```

命令遇到未实现能力时会返回失败，不会假装成功。网络测试默认不作为普通离线测试的一部分，真实平台验证需要相应的网络和账号条件。

## 数据保存位置

具体位置由 `configs/defaults.yaml` 中的 `storage.root` 决定。常见目录包括：

- `raw/`：通过验证的原始数据。
- `staging/`：正在下载的临时文件。
- `quarantine/`：验证失败或不完整的文件。
- `reports/`：报告和交付清单。
- `manifest.db`：任务和文件登记数据库。

建议把 `raw`、`staging` 和数据库放在有足够空间的本地磁盘或外置 SSD 上。跨磁盘交付可能需要额外临时空间。

## 常见情况

### 缺少凭据

看到 `environment variable ... is not set` 时，说明当前请求需要的平台账号还没有配置。按平台说明设置环境变量，再运行：

```bash
rdh config validate
```

### 任务停在 `RETRY_WAIT`

通常表示超时、临时网络错误或平台限流。等待退避时间后重新运行任务即可。

### 任务为 `FAILED`

查看任务详情和报告中的错误原因。常见原因是权限、文件不存在或请求条件超出数据源能力。修正需求或配置后，重新生成计划。

### 任务为 `QUARANTINED`

文件已经下载到本地，但没有通过完整性检查。它不会被当作正式数据使用。查看隔离目录中的原因文件和报告。

### 程序中途退出

重新执行同一计划。已完成任务不会重复下载，未完成任务会根据远端文件身份决定续传或重新开始。

## 项目边界

RDH 负责数据发现、获取、恢复、验证、记录和交付。

以下工作不属于 RDH：

- 数据清洗和特征工程。
- 统计分析、建模和机器学习。
- 绘图和论文写作。
- 替用户申请平台账号或绕过访问限制。
- 把未经验证的文件标记为正式结果。

你可以把 RDH 交付的数据交给 xarray、MATLAB、R、Python 或其他科研工具继续处理。

## 进一步阅读

- [运维手册](docs/operations.md)
- [配置说明](docs/configuration.md)
- [数据完整性检查](docs/data-integrity.md)
- [故障恢复](docs/recovery.md)
- [数据源说明](docs/providers/)
- [MCP 接入说明](docs/mcp.md)
- [平台支持](docs/platform-support.md)
- [当前状态审计](docs/architecture/RDH_CURRENT_STATE_AUDIT.md)
- [最终升级报告](docs/reports/RDH_FINAL_UPGRADE_REPORT.md)

## 许可证

RDH 使用 MIT 许可证，详见 [LICENSE](LICENSE)。
