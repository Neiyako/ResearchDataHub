#!/usr/bin/env bash
# ResearchDataHub Agent Skills 安装脚本（macOS / Linux）。
#
# 把仓库内 skills/<name>/ 复制到两个 Agent 的个人级 skills 目录：
#   Claude Code : ~/.claude/skills/<name>/SKILL.md
#   Codex       : ~/.codex/skills/<name>/SKILL.md（$CODEX_HOME 存在时用 $CODEX_HOME/skills）
#
# 特性：
#   * 幂等：重复运行只覆盖本脚本管理的四个 skill，不碰其他 skill；
#     每个已安装目录里写 .rdh-skill.json 标记，--uninstall 只删除带标记的目录。
#   * --dry-run 只打印将执行的动作与目标路径，不写任何文件。
#   * 安装前先跑 frontmatter/工具名校验（scripts/check_skills_frontmatter.py），失败即中止。
#
# 用法：
#   ./scripts/install_skills.sh --dry-run
#   ./scripts/install_skills.sh
#   ./scripts/install_skills.sh --claude-only | --codex-only
#   ./scripts/install_skills.sh --link          # 软链接而不是复制（仅对 Claude Code 文档保证支持）
#   ./scripts/install_skills.sh --uninstall [--dry-run]
#
# 退出码：0 成功；1 校验失败或安装失败；2 参数错误/环境不满足。

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SRC="${REPO_ROOT}/skills"
CHECKER="${REPO_ROOT}/scripts/check_skills_frontmatter.py"
MARKER=".rdh-skill.json"

SKILL_NAMES=(data-discovery environment-setup data-acquisition data-validation)

DRY_RUN=0
UNINSTALL=0
LINK=0
TARGET_CLAUDE=1
TARGET_CODEX=1

CLAUDE_SKILLS_DIR="${HOME}/.claude/skills"
if [[ -n "${CODEX_HOME:-}" ]]; then
  CODEX_SKILLS_DIR="${CODEX_HOME}/skills"
else
  CODEX_SKILLS_DIR="${HOME}/.codex/skills"
fi

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log()  { printf '%s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

run() {
  # 在 dry-run 下只回显命令
  if (( DRY_RUN )); then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run)       DRY_RUN=1 ;;
    --uninstall)     UNINSTALL=1 ;;
    --link)          LINK=1 ;;
    --copy)          LINK=0 ;;
    --claude-only)   TARGET_CLAUDE=1; TARGET_CODEX=0 ;;
    --codex-only)    TARGET_CLAUDE=0; TARGET_CODEX=1 ;;
    -h|--help)       usage 0 ;;
    --target-dir)    # 测试用：覆盖两个目标目录（一次给两个：claude codex）
      [[ $# -ge 3 ]] || die "--target-dir needs two arguments: <claude-dir> <codex-dir>"
      CLAUDE_SKILLS_DIR="$2"; CODEX_SKILLS_DIR="$3"; shift 2 ;;
    *) usage 2 ;;
  esac
  shift
done

[[ -d "${SKILLS_SRC}" ]] || die "skills source directory not found: ${SKILLS_SRC}"

targets=()
(( TARGET_CLAUDE )) && targets+=("claude-code|${CLAUDE_SKILLS_DIR}")
(( TARGET_CODEX  )) && targets+=("codex|${CODEX_SKILLS_DIR}")
(( ${#targets[@]} > 0 )) || die "no target selected"

if (( UNINSTALL == 0 )); then
  log "== 安装前校验 =="
  if [[ -f "${CHECKER}" ]] && command -v python3 >/dev/null 2>&1; then
    if (( DRY_RUN )); then
      log "  [dry-run] python3 ${CHECKER} --check-tools"
    else
      python3 "${CHECKER}" --check-tools || die "skill 校验失败，未做任何安装"
    fi
  else
    warn "未找到 ${CHECKER} 或 python3，跳过安装前校验"
  fi
fi

log ""
log "== 目标路径 =="
log "  skills 源目录 : ${SKILLS_SRC}"
for entry in "${targets[@]}"; do
  name="${entry%%|*}"; dir="${entry#*|}"
  log "  ${name} : ${dir}"
done
(( LINK )) && log "  模式 : 软链接（symbolic link）" || log "  模式 : 复制（copy）"
(( DRY_RUN )) && log "  DRY-RUN：不会写入任何文件"
log ""

installed=0
removed=0

for entry in "${targets[@]}"; do
  name="${entry%%|*}"; dir="${entry#*|}"
  log "-- ${name} --"
  run mkdir -p "${dir}"

  for skill in "${SKILL_NAMES[@]}"; do
    src="${SKILLS_SRC}/${skill}"
    dest="${dir}/${skill}"
    [[ -f "${src}/SKILL.md" ]] || die "missing source: ${src}/SKILL.md"

    if (( UNINSTALL )); then
      if [[ -f "${dest}/${MARKER}" || -L "${dest}" ]]; then
        log "  remove ${dest}"
        run rm -rf -- "${dest}"
        removed=$((removed + 1))
      else
        log "  skip   ${dest} (不是本脚本安装的，保留)"
      fi
      continue
    fi

    log "  install ${src} -> ${dest}"
    if (( DRY_RUN )); then
      log "  [dry-run] rm -rf ${dest}; cp -R ${src} ${dest}"
      installed=$((installed + 1))
      continue
    fi

    rm -rf -- "${dest}"
    if (( LINK )); then
      ln -s -- "${src}" "${dest}"
    else
      cp -R -- "${src}" "${dest}"
    fi
    cat >"${dest}/${MARKER}" <<JSON
{
  "managed_by": "researchdatahub/scripts/install_skills.sh",
  "skill": "${skill}",
  "target": "${name}",
  "mode": "$( (( LINK )) && printf 'symlink' || printf 'copy' )",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
    installed=$((installed + 1))
  done
  log ""
done

if (( UNINSTALL )); then
  log "已移除 ${removed} 个由本脚本安装的 skill 目录。"
else
  log "已安装 ${installed} 个 skill 目录。"
  log ""
  log "== 下一步 =="
  log "  1) 确认 rdh-mcp 可用：rdh-mcp --print-tools | head -5"
  log "  2) 注册 MCP server："
  (( TARGET_CLAUDE )) && log "     claude mcp add --transport stdio --env RDH_ROOT=\"\$HOME/.rdh\" rdh-mcp -- rdh-mcp"
  (( TARGET_CODEX ))  && log "     codex mcp add rdh-mcp --env RDH_ROOT=\"\$HOME/.rdh\" -- rdh-mcp"
  log "  3) 重启 Agent（Claude Code 会话 / Codex 进程）后输入 /data-discovery 或 \$data-discovery"
fi
