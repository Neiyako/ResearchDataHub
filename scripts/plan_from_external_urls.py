#!/usr/bin/env python3
"""从「已存在的产物 URL」建立 RDH 采集计划（恢复/重放工具）。

什么时候用
----------
CDS 的服务端生成阶段可能长时间排队（保护性限流），而**产物其实已经生成过**
（例如上一次成功 job 的结果 URL、或本机已有真实产的本地 HTTP 服务）。
本工具不做「发现」，直接把若干已知文件 URL 变成 RDH 计划里的 https 资产，
之后照常走 ``rdh task run``（传输 → 完整性校验 → staging→raw 原子提交）与
``rdh report generate``（交付清单）——**只替换了发现/提交阶段，传输与校验仍是真的**。

诚实边界
--------
* 计划的 ``extensions`` 会写 ``replay.external_assets=true`` 与每个 URL 的来源说明，
  交付清单/报告里能看出「这不是一次新的远端 submit」。
* URL 必须使用 http(s)；asset 的 ``size_bytes`` 与（可选）``checksum`` 由调用方提供，
  传输层会按声明值做大小/校验和核对，声明错了会 FAILED 而不是静默通过。

用法::

    python scripts/plan_from_external_urls.py \
        --request examples/era5-clwc-ciwc-tp-2024-01-01.yaml \
        --asset "canary_clwc_ciwc.nc|http://127.0.0.1:8765/canary_clwc_ciwc.nc|45855|<sha256>" \
        --note "replay of a real CDS product downloaded 2026-11-03"
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from researchdatahub.cli._services import load_context, open_repository  # noqa: E402
from researchdatahub.core.models import (  # noqa: E402
    AccessRoute,
    Checksum,
    DatasetRequest,
    SourceAsset,
    SourceMetadata,
)
from researchdatahub.planner.request import load_request_detailed  # noqa: E402
from researchdatahub.planner.service import create_plan, plan_overview  # noqa: E402


class _StaticAdapter:
    """把一组既有 URL 当作「发现结果」的只读适配器（无网络发现）。"""

    def __init__(self, source_id: str, assets: Sequence[SourceAsset]) -> None:
        self._source_id = source_id
        self._assets = tuple(assets)

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def supported_routes(self) -> frozenset[str]:
        return frozenset({"https"})

    def describe(self) -> SourceMetadata:
        return self._assets[0].metadata

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        return self._assets

    def health_check(self) -> bool:
        return True


def _parse_asset(spec: str) -> tuple[str, str, int, str | None]:
    parts = spec.split("|")
    if len(parts) < 3:
        raise SystemExit(
            f"--asset 需要 '名称|URL|字节数[|sha256]' 四段中的至少三段，收到: {spec!r}"
        )
    name, url, size = parts[0].strip(), parts[1].strip(), parts[2].strip()
    checksum = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"asset URL 必须是 http(s): {url!r}")
    try:
        size_bytes = int(size)
    except ValueError as exc:
        raise SystemExit(f"asset 字节数不是整数: {size!r}") from exc
    return name, url, size_bytes, checksum


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数、建立重放计划并打印 plan/request id。"""
    parser = argparse.ArgumentParser(description="从既有产物 URL 建立 RDH 采集计划")
    parser.add_argument("--request", required=True, help="需求 YAML（复用其 request_id/元数据）")
    parser.add_argument(
        "--asset", action="append", required=True, help="名称|URL|字节数[|sha256]，可重复"
    )
    parser.add_argument("--source-id", default="ncar", help="资产归属的数据源 id（默认 ncar）")
    parser.add_argument("--note", default="", help="写进计划扩展的来源说明（审计用）")
    args = parser.parse_args(argv)

    loaded = load_request_detailed(args.request)
    request = loaded.request
    assets: list[SourceAsset] = []
    for spec in args.asset:
        name, url, size_bytes, checksum = _parse_asset(spec)
        metadata = SourceMetadata(
            source_id=args.source_id,
            display_name=f"external replay ({args.source_id})",
            provider="replay",
            access_route=AccessRoute.HTTPS,
            dataset_id=request.dataset_id,
            origin_uri=url,
            retrieved_at=datetime.now(UTC),
            extensions={
                "replay.external_assets": True,
                "replay.note": args.note or "external asset replayed into RDH pipeline",
            },
        )
        assets.append(
            SourceAsset(
                asset_id=f"asset-replay-{abs(hash((name, url))) % 10**10}",
                source_id=args.source_id,
                dataset_id=request.dataset_id,
                access_route=AccessRoute.HTTPS,
                origin_uri=url,
                file_name=name,
                size_bytes=size_bytes,
                checksum=Checksum(value=f"sha256:{checksum}") if checksum else None,
                metadata=metadata,
                discovered_at=datetime.now(UTC),
            )
        )

    context = load_context()
    with open_repository(context) as repository:
        plan = create_plan(
            repo=repository,
            request=request,
            adapters=[_StaticAdapter(args.source_id, assets)],
            config=context.app_config.defaults,
        )
        overview = plan_overview(plan)
    print(f"PLAN_ID {plan.plan_id}")
    print(f"REQUEST_ID {plan.request_id}")
    print(f"TASKS {overview.get('task_count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
