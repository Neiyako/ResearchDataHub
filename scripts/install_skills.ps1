<#
.SYNOPSIS
    ResearchDataHub Agent Skills 安装脚本（Windows / PowerShell 5.1+）。

.DESCRIPTION
    把仓库内 skills/<name>/ 复制到两个 Agent 的个人级 skills 目录：
      Claude Code : %USERPROFILE%\.claude\skills\<name>\SKILL.md
      Codex       : %USERPROFILE%\.codex\skills\<name>\SKILL.md（设置 CODEX_HOME 时用 $env:CODEX_HOME\skills）

    特性：
      * 幂等：只覆盖本脚本管理的四个 skill，不碰其他 skill；每个已安装目录写 .rdh-skill.json 标记，
        -Uninstall 只删除带标记的目录。
      * -DryRun 只打印将执行的动作与目标路径，不写任何文件。
      * 安装前跑 frontmatter/工具名校验（scripts/check_skills_frontmatter.py），失败即中止。

.PARAMETER DryRun
    只打印，不写文件。

.PARAMETER Uninstall
    卸载本脚本安装的 skill 目录（只删带 .rdh-skill.json 标记的）。

.PARAMETER ClaudeOnly
    只安装到 Claude Code 目录。

.PARAMETER CodexOnly
    只安装到 Codex 目录。

.EXAMPLE
    .\scripts\install_skills.ps1 -DryRun

.EXAMPLE
    .\scripts\install_skills.ps1

.EXAMPLE
    .\scripts\install_skills.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$ClaudeOnly,
    [switch]$CodexOnly,
    [string]$ClaudeDir,
    [string]$CodexDir
)

$ErrorActionPreference = 'Stop'

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$SkillsSrc  = Join-Path $RepoRoot 'skills'
$Checker    = Join-Path $RepoRoot 'scripts/check_skills_frontmatter.py'
$Marker     = '.rdh-skill.json'
$SkillNames = @('data-discovery', 'environment-setup', 'data-acquisition', 'data-validation')

function Write-Step([string]$Text) { Write-Host $Text }
function Write-Warn([string]$Text) { Write-Warning $Text }

if (-not $ClaudeDir) {
    $ClaudeDir = Join-Path $env:USERPROFILE '.claude\skills'
}
if (-not $CodexDir) {
    if ($env:CODEX_HOME) {
        $CodexDir = Join-Path $env:CODEX_HOME 'skills'
    } else {
        $CodexDir = Join-Path $env:USERPROFILE '.codex\skills'
    }
}

if (-not (Test-Path $SkillsSrc)) {
    Write-Error "skills source directory not found: $SkillsSrc"
    exit 2
}

$targets = @()
if (-not $CodexOnly)  { $targets += [pscustomobject]@{ Name = 'claude-code'; Dir = $ClaudeDir } }
if (-not $ClaudeOnly) { $targets += [pscustomobject]@{ Name = 'codex';       Dir = $CodexDir } }
if ($targets.Count -eq 0) {
    Write-Error 'no target selected'
    exit 2
}

function Invoke-Action {
    param([scriptblock]$Action, [string]$Description)
    if ($DryRun) {
        Write-Host "  [dry-run] $Description"
        return
    }
    & $Action
}

# --------------------------------------------------------------------------- #
# 安装前校验
# --------------------------------------------------------------------------- #
if (-not $Uninstall) {
    Write-Step '== 安装前校验 =='
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if ($python -and (Test-Path $Checker)) {
        if ($DryRun) {
            Write-Host "  [dry-run] python $Checker --check-tools"
        } else {
            & $python.Source $Checker --check-tools
            if ($LASTEXITCODE -ne 0) {
                Write-Error 'skill 校验失败，未做任何安装'
                exit 1
            }
        }
    } else {
        Write-Warn "未找到 python 或 $Checker，跳过安装前校验"
    }
}

Write-Step ''
Write-Step '== 目标路径 =='
Write-Step "  skills 源目录 : $SkillsSrc"
foreach ($t in $targets) { Write-Step "  $($t.Name) : $($t.Dir)" }
if ($DryRun) { Write-Step '  DRY-RUN：不会写入任何文件' }
Write-Step ''

$installed = 0
$removed = 0

foreach ($t in $targets) {
    Write-Step "-- $($t.Name) --"
    Invoke-Action -Description "mkdir -p $($t.Dir)" -Action { New-Item -ItemType Directory -Force -Path $t.Dir | Out-Null }

    foreach ($skill in $SkillNames) {
        $src  = Join-Path $SkillsSrc $skill
        $dest = Join-Path $t.Dir $skill
        $srcSkill = Join-Path $src 'SKILL.md'

        if (-not (Test-Path $srcSkill)) {
            Write-Error "missing source: $srcSkill"
            exit 1
        }

        if ($Uninstall) {
            $hasMarker = Test-Path (Join-Path $dest $Marker)
            if ($hasMarker) {
                Write-Step "  remove $dest"
                Invoke-Action -Description "Remove-Item -Recurse -Force $dest" -Action {
                    Remove-Item -Recurse -Force -Path $dest
                }
                $removed++
            } else {
                Write-Step "  skip   $dest (不是本脚本安装的，保留)"
            }
            continue
        }

        Write-Step "  install $src -> $dest"
        if ($DryRun) {
            Write-Step "  [dry-run] Remove-Item -Recurse -Force $dest; Copy-Item -Recurse $src $dest"
            $installed++
            continue
        }

        if (Test-Path $dest) { Remove-Item -Recurse -Force -Path $dest }
        Copy-Item -Recurse -Path $src -Destination $dest
        $payload = [ordered]@{
            managed_by   = 'researchdatahub/scripts/install_skills.ps1'
            skill        = $skill
            target       = $t.Name
            mode         = 'copy'
            installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        $payload | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 -Path (Join-Path $dest $Marker)
        $installed++
    }
    Write-Step ''
}

if ($Uninstall) {
    Write-Step "已移除 $removed 个由本脚本安装的 skill 目录。"
} else {
    Write-Step "已安装 $installed 个 skill 目录。"
    Write-Step ''
    Write-Step '== 下一步 =='
    Write-Step '  1) 确认 rdh-mcp 可用：rdh-mcp --print-tools | Select-Object -First 5'
    Write-Step '  2) 注册 MCP server：'
    if (-not $CodexOnly) {
        Write-Step '     claude mcp add --transport stdio --env RDH_ROOT="$HOME/.rdh" rdh-mcp -- rdh-mcp'
    }
    if (-not $ClaudeOnly) {
        Write-Step '     codex mcp add rdh-mcp --env "RDH_ROOT=$env:USERPROFILE\.rdh" -- rdh-mcp'
    }
    Write-Step '  3) 重启 Agent 后输入 /data-discovery（Claude Code）或 $data-discovery（Codex）'
}
exit 0
