# 平台支持

RDH 的核心功能使用 Python 标准库和跨平台依赖实现，目标平台包括 macOS、Linux 和 Windows。

## 安装

### macOS 和 Linux

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,mcp,sci]"
rdh version
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,mcp,sci]"
rdh version
```

Windows 使用 `scripts/install_skills.ps1`，macOS 和 Linux 使用 `scripts/install_skills.sh`。两者执行相同的安装检查，并且都支持 dry-run 和卸载。

## 运行时约定

- 路径由 `pathlib` 处理，不要求固定盘符或用户目录。
- 临时文件应使用系统临时目录，不依赖 `/tmp`。
- Python 命令使用 `python -m ...`，避免假定虚拟环境脚本目录名称。
- 文件提交使用跨平台的原子替换和必要的同步操作。
- 凭据通过环境变量或用户配置引用，不写入项目文件。

## CI 验证

GitHub Actions 在 Ubuntu、Windows 和 macOS 上运行 Python 3.12 与 3.13 的静态检查和离线测试。真实数据源测试仍需要单独的网络和账号条件。
