#!/usr/bin/env python3
"""验收检查：报告数字必须与 benchmarks/m6/results.json 一致（M6 验收 C3）。

做法：从结果 JSON 计算"应该出现在报告里的字符串"，逐条在报告 Markdown 中检索。
命中即 PASS；缺任何一条即退出码 1（不原谅、不模糊）。这不是物理真实性校验，
只保证"报告没有比数据说得更漂亮"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/m6/results.json"
REPORT = ROOT / "docs/M6-efficiency-benchmark.md"


def spaced(number: int) -> str:
    """把整数按三位空格分组（报告里的写法：1 134 576 716）。"""
    return f"{number:,}".replace(",", " ")


def expected_fragments(run: dict) -> list[tuple[str, str]]:
    """一个 run 在报告里必须出现的片段（键用于日志，值为片段）。"""
    fragments: list[tuple[str, str]] = []
    wall = run.get("wall_seconds") or 0.0
    mode = str(run.get("mode"))
    if run.get("ok"):
        fragments.append((f"{mode}.wall", f"{wall:.1f}"))
    if run.get("bytes_total"):
        fragments.append((f"{mode}.bytes", spaced(int(run["bytes_total"]))))
    validation = run.get("validation") or {}
    if run.get("mode") == "mixed" and validation.get("time_to_first_artifact_seconds"):
        fragments.append(
            ("mixed.first_ready", f"{validation['time_to_first_artifact_seconds']:.1f}")
        )
        fragments.append(("mixed.winner", str(validation.get("winner"))))
    return fragments


def normalize(text: str) -> str:
    """去掉千位空格/逗号，统一比较口径。"""
    return text.replace(" ", "").replace(",", "")


def main() -> int:
    """逐条核对并打印差异。"""
    payload = json.loads(RESULTS.read_text(encoding="utf-8"))
    report = normalize(REPORT.read_text(encoding="utf-8"))
    missing: list[str] = []
    checked = 0
    for run in payload.get("runs") or []:
        for key, fragment in expected_fragments(run):
            checked += 1
            target = {"mixed.first_ready": "先就绪", "mixed.winner": "winner"}.get(key)
            haystack = report
            if target:
                haystack = (
                    "".join(
                        line for line in report.splitlines() if target in line or "混合" in line
                    )
                    or report
                )
            if normalize(fragment) not in normalize(haystack):
                missing.append(f"{key}: 报告缺少 {fragment!r}")
    print(f"[consistency] 检查片段 {checked} 条，缺失 {len(missing)} 条")
    for item in missing:
        print("  MISSING", item)
    print("[consistency]", "PASS" if not missing else "FAIL")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
