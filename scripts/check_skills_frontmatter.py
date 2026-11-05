#!/usr/bin/env python3
"""校验 ``skills/*/SKILL.md`` 的 frontmatter，并（可选）核对正文里的工具名。

两道检查，任一失败退出码 1：

1. **frontmatter 合法性**（默认开启）
   * 文件第一行必须是 ``---``（Agent Skills 标准：frontmatter 只能出现在文件开头）；
   * YAML 必须可解析且是 mapping；
   * ``name`` / ``description`` 非空，``name`` 符合标准约束（小写字母/数字/连字符，≤64）；
   * ``description`` ≤1024 字符；
   * 只允许标准 6 字段（``name`` ``description`` ``license`` ``compatibility`` ``metadata``
     ``allowed-tools``）——超出即失败，因为 Claude Code 专属字段会让 claude.ai 上传硬报错、
     且 Codex 校验器不接受 ``disable-model-invocation``。

2. **工具名对齐**（``--check-tools`` 开启）
   * 从 ``src/researchdatahub/interfaces/mcp/schemas/catalog.py`` 读出 ``TOOL_CATALOG`` 里的
     工具名（不 import 包，只用正则，避免依赖安装状态）；
   * 扫描每个 SKILL.md 正文里形如 ``tool_name(...)`` / ``` `tool_name` ``` 的标识符，
     若某个标识符是"疑似工具名"（在 MCP 工具名字母表里的蛇形标识）但不在 18 个工具里，判失败。

用法::

    python3 scripts/check_skills_frontmatter.py                  # 只查 frontmatter
    python3 scripts/check_skills_frontmatter.py --check-tools    # 额外核对工具名
    python3 scripts/check_skills_frontmatter.py --strict-text    # 禁止 metadata（最严）

退出码：0 = 全部通过；1 = 有失败；2 = 环境问题（找不到 skills/ 或 catalog.py）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
SKILLS_DIR: Final = ROOT / "skills"
CATALOG: Final = ROOT / "src/researchdatahub/interfaces/mcp/schemas/catalog.py"

STANDARD_FIELDS: Final = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
REQUIRED_FIELDS: Final = ("name", "description")
NAME_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX: Final = 64
DESCRIPTION_MAX: Final = 1024

_CATALOG_NAME: Final = re.compile(r'ToolSpec\(\s*name="([a-zA-Z0-9_]+)"', re.S)
_FENCE: Final = re.compile(r"^```.*?^```", re.S | re.M)
_SNAKE: Final = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

PARAMETER_NAMES: Final = frozenset(
    {
        # 工具参数名 / 返回字段名（不是工具名），出现在正文里属正常
        "approval_token",
        "approval_ttl_seconds",
        "request_path",
        "units_catalog",
        "split_granularity",
        "max_workers",
        "max_tasks",
        "task_ids",
        "dataset_ids",
        "output_path",
        "require_network",
        "timeout_seconds",
        "schema_version",
        "provider_native",
        "unmatched_variables",
        "verify_checksums",
        "access_route",
        "origin_uri",
        "heartbeat_at",
        "lease_expires_at",
        "latest_report_status",
        "max_attempts",
        "last_error",
        "file_count",
        "file_name",
        "source_id",
        "delivery_id",
        "file_id",
        "disk_issues",
        "validation_status",
        "retrieved_at",
        "skipped_checks",
        "manifest_issues",
        "unverified_reason",
        "expected_source",
        "product_family",
        "side_effects_performed",
        "next_skill",
        "tool_version",
        "total_bytes",
        "report_id",
        "blocked_by",
        # 请求契约里的段/字段名（不是工具名）
        "verify_size",
        "calculate_sha256",
        "netcdf_openable",
        "preserve_original",
        "preferred_sources",
        "max_sources",
        "grid_degrees",
        "long_name",
        "standard_name",
        "cell_methods",
        "dataset_note",
    }
)
"""参数名/返回字段名白名单：这些**不是**工具名，正文里出现是正常的。"""


def _minimal_yaml(text: str) -> dict[str, object]:
    """解析 frontmatter 的最小子集（无第三方依赖）。

    支持：``key: value``、``key:`` 后跟缩进的子映射（只做"是否非空"判定）、
    带引号的 value、以及 ``|`` / ``>`` 块标量（本轮 skills 未使用，遇到即报未支持）。
    """
    result: dict[str, object] = {}
    depth_marker = "  "
    current_parent: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(depth_marker):
            if current_parent is None:
                raise ValueError(f"unexpected indented line without parent: {raw!r}")
            result[current_parent] = result[current_parent] or {}
            continue
        if ":" not in raw:
            raise ValueError(f"not a 'key: value' line: {raw!r}")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"empty key in line: {raw!r}")
        if value in {"|", ">", "|-", ">-", "|+", ">+"}:
            raise ValueError(f"block scalars are not supported by this checker: {raw!r}")
        current_parent = None
        if value == "":
            current_parent = key
            result[key] = {}
        else:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            result[key] = value
    return result


def _try_yaml(text: str) -> tuple[dict[str, object] | None, str | None]:
    """优先用 PyYAML（若环境有），否则退回最小解析器。"""
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        try:
            return _minimal_yaml(text), None
        except ValueError as exc:
            return None, str(exc)
    try:
        loaded = yaml.safe_load(text)
    except Exception as exc:
        return None, f"YAML parse error: {exc}"
    if not isinstance(loaded, dict):
        return None, "frontmatter must be a mapping"
    return {str(k): v for k, v in loaded.items()}, None


def catalog_tool_names() -> set[str]:
    """从 catalog.py 里抠出 18 个工具名（不 import，避免依赖安装状态）。"""
    text = CATALOG.read_text(encoding="utf-8")
    return set(_CATALOG_NAME.findall(text))


def _frontmatter_and_body(path: Path) -> tuple[str, str, str | None]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return "", text, "file must start with '---' (frontmatter on first line)"
    end = text.find("\n---", 3)
    if end == -1:
        return "", text, "frontmatter is not closed by '---'"
    front = text[3:end].lstrip("\n")
    body = text[end + 4 :]
    return front, body, None


def check_frontmatter(path: Path, *, strict_text: bool) -> list[str]:
    """返回该文件的失败列表（空列表 = 通过）。"""
    problems: list[str] = []
    front, _body, error = _frontmatter_and_body(path)
    if error is not None:
        return [error]
    data, error = _try_yaml(front)
    if error is not None or data is None:
        return [error or "frontmatter parse failed"]

    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing or empty required field: {field}")

    unknown = sorted(set(data) - STANDARD_FIELDS)
    if unknown:
        problems.append(
            f"non-standard frontmatter field(s): {unknown}; "
            f"allowed: {sorted(STANDARD_FIELDS)} (Claude-Code-only fields break "
            "claude.ai upload and Codex validation)"
        )

    name = data.get("name")
    if isinstance(name, str) and name.strip():
        if len(name) > NAME_MAX:
            problems.append(f"name is {len(name)} chars (max {NAME_MAX})")
        if not NAME_PATTERN.match(name):
            problems.append(f"name {name!r} must be lowercase letters/digits/hyphens only")
        if path.parent.name != name:
            problems.append(
                f"directory name {path.parent.name!r} must equal frontmatter name {name!r}"
            )

    description = data.get("description")
    if isinstance(description, str) and len(description) > DESCRIPTION_MAX:
        problems.append(f"description is {len(description)} chars (max {DESCRIPTION_MAX})")

    if strict_text and "metadata" in data:
        problems.append("metadata is not allowed under --strict-text")
    return problems


def check_tool_names(path: Path, tools: set[str]) -> list[str]:
    """正文里出现的疑似工具名，必须属于 18 个工具（代码块内不检查）。"""
    _front, body, error = _frontmatter_and_body(path)
    if error is not None:
        return [error]
    plain = _FENCE.sub("", body)
    problems: list[str] = []
    seen: set[str] = set()
    for token in _SNAKE.findall(plain):
        if token in seen:
            continue
        seen.add(token)
        if token in tools or token in PARAMETER_NAMES:
            continue
        # 只对"看起来像 MCP 工具调用"的标识符较真：以动词开头且带下划线
        if re.match(
            r"^(search|inspect|compare|get|check|validate|estimate|create|submit|cancel|list|verify)_",
            token,
        ):
            problems.append(f"unknown tool-like identifier in body: {token!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """校验 ``skills/*/SKILL.md`` 的 frontmatter 与工具名，返回进程退出码。"""
    parser = argparse.ArgumentParser(description="validate skills/*/SKILL.md")
    parser.add_argument("--check-tools", action="store_true", help="核对正文里的工具名")
    parser.add_argument("--strict-text", action="store_true", help="禁止 metadata 字段")
    args = parser.parse_args(argv)

    if not SKILLS_DIR.is_dir():
        print(f"[ERROR] skills directory not found: {SKILLS_DIR}", file=sys.stderr)
        return 2
    files = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    if not files:
        print(f"[ERROR] no skills/*/SKILL.md found under {SKILLS_DIR}", file=sys.stderr)
        return 2

    tools: set[str] = set()
    if args.check_tools:
        if not CATALOG.is_file():
            print(f"[ERROR] catalog not found: {CATALOG}", file=sys.stderr)
            return 2
        tools = catalog_tool_names()
        if len(tools) != 18:
            print(
                f"[ERROR] expected 18 tools in catalog, parsed {len(tools)}: {sorted(tools)}",
                file=sys.stderr,
            )
            return 2

    failures = 0
    for path in files:
        rel = path.relative_to(ROOT)
        problems = check_frontmatter(path, strict_text=args.strict_text)
        if args.check_tools:
            problems += check_tool_names(path, tools)
        if problems:
            failures += 1
            print(f"FAIL {rel}")
            for item in problems:
                print(f"  - {item}")
        else:
            print(f"PASS {rel}")

    print()
    if args.check_tools:
        print(f"tool-name check: {len(tools)} tools known in MCP catalog")
    print(f"summary: {len(files) - failures}/{len(files)} skill(s) passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
