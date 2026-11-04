#!/usr/bin/env python3
"""把 M6 基准的分次运行结果合并为一份 ``results.json`` + ``summary.md``。

分次运行的理由（不是偷懒）：CDS 拒绝"同一数据集并发排队"，且队列等待可长达数十分钟；
分三次跑（纯 NCAR / 纯 CDS / 混合）比一次串行跑更容易在队列波动时保住已完成的部分。
合并只做结构拼接与渲染，不修改任何测量值。

用法::

    PYTHONPATH=src .venv/bin/python scripts/m6_merge_results.py \
        benchmarks/m6/a-ncar/results.json benchmarks/m6/c-mixed/results.json \
        --out benchmarks/m6
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA = "rdh-m6-bench/1"


def load_bench_module() -> ModuleType:
    """按路径加载基准脚本（它不是包内模块），复用其 RunRecord 与渲染函数。"""
    script = Path(__file__).with_name("m6_efficiency_bench.py")
    spec = importlib.util.spec_from_file_location("m6_efficiency_bench", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载基准脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["m6_efficiency_bench"] = module
    spec.loader.exec_module(module)
    return module


def merge(
    inputs: Sequence[Path], out_dir: Path, bench: ModuleType | None = None
) -> tuple[dict[str, Any], str]:
    """拼接多份结果，返回 (merged dict, markdown)。"""
    bench = bench or load_bench_module()
    runs: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for path in inputs:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema") != SCHEMA:
            raise SystemExit(f"{path} 的 schema 不是 {SCHEMA}")
        if not meta:
            meta = {
                key: payload.get(key)
                for key in ("schema", "generated_at", "environment", "workload")
            }
            meta["merged_from"] = []
        meta["merged_from"].append(str(path))
        runs.extend(payload.get("runs") or [])
    records = [bench.RunRecord(**run) for run in runs]
    markdown = bench.render_markdown(records, meta)
    merged = {**meta, "runs": runs}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(markdown + "\n", encoding="utf-8")
    return merged, markdown


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="合并 M6 基准分次结果")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/m6"))
    args = parser.parse_args(argv)
    merged, markdown = merge(args.inputs, args.out)
    print(f"[m6-merge] 合并 {len(merged['runs'])} 次运行 → {args.out / 'results.json'}")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
