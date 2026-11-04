#!/usr/bin/env python3
"""M6 轻量下载效率基准：纯 CDS API / 纯 NCAR GDEX / 混合（双源对冲）。

三种条件（同一逻辑需求：ERA5 2t，2024-01-01..03 逐时，30–45°N / 100–125°E）
----------------------------------------------------------------------------

1. ``cds``   纯 CDS API：``cdsapi`` 提交 → 排队 → 生成 → 下载子集 NetCDF。
2. ``ncar``  纯 NCAR GDEX：目录 API 定位 ERA5 月度 NetCDF（全域逐时，
   无子集能力）→ HTTPS 直接下载整文件。
3. ``mixed`` 混合制：同一需求**同时**投两源（对冲/racing），记录先就绪时间、
   两者各自耗时与冗余字节；"先到先用、另一路丢弃"是真实调度器可实现的策略。

只测量、不判优：本脚本产出原始记录（JSON）与 Markdown 表，分析结论写在
``docs/M6-efficiency-benchmark.md``，避免把结论写死在代码里。

用法（真实联网，会产生流量；CDS 请求可能排队数分钟）::

    PYTHONPATH=src .venv/bin/python scripts/m6_efficiency_bench.py \
        --mode all --out benchmarks/m6 --workdir /tmp/rdh-m6-bench

    # 冒烟（单日、单时次，验证链路）
    ... --mode cds --cds-days 1 --cds-hours 1 --out /tmp/m6-smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

CDS_COLLECTION = "reanalysis-era5-single-levels"
GDEX_ENDPOINT = "https://gdex.ucar.edu"
GDEX_DSID = "d633000"
# GDEX 把 ds633.0 的 netCDF4 文件按年拆成子分组，gindex = 年份 × 100 + 29（实测 202429 → 2024）。
SURFACE_NETCDF_GINDEX_SUFFIX = 29
VAR_CODE_2T = "128_167_2t"
CHUNK_BYTES = 1 << 20
STATUS_TERMINAL = ("successful", "failed", "rejected", "dismissed", "deleted")
STATUS_RUNNING = ("running", "accepted")


# ---------------------------------------------------------------- 数据载体


@dataclass
class DownloadStats:
    """一次流式下载的计量结果。"""

    http_status: int | None = None
    declared_bytes: int | None = None
    downloaded_bytes: int = 0
    ttfb_seconds: float | None = None
    download_seconds: float = 0.0
    mbps: float | None = None
    sha256: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """是否拿到 200/206 且字节数 > 0。"""
        return self.error is None and self.downloaded_bytes > 0


@dataclass
class RunRecord:
    """一个条件的一次完整运行。"""

    mode: str
    workload: dict[str, Any]
    ok: bool = False
    wall_seconds: float = 0.0
    bytes_total: int = 0
    phases: dict[str, float] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict。"""
        return asdict(self)


# ---------------------------------------------------------------- 通用工具


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def download_stream(
    url: str,
    target: Path,
    *,
    byte_cap: int | None = None,
    timeout_seconds: float = 1800.0,
) -> DownloadStats:
    """流式下载到 ``target``，记录 TTFB、吞吐、字节数与 SHA-256。

    ``byte_cap`` 非空时用 HTTP Range 只取前 N 字节（用于"轻量"测吞吐，
    产物是**不完整文件**，必须在报告里如实标注）。
    """
    stats = DownloadStats()
    headers = {"Range": f"bytes=0-{byte_cap - 1}"} if byte_cap else None
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
    started = time.perf_counter()
    first_byte_at: float | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with (
            httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            stats.http_status = response.status_code
            length = response.headers.get("Content-Length")
            stats.declared_bytes = int(length) if length and length.isdigit() else None
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    if first_byte_at is None:
                        first_byte_at = time.perf_counter()
                    handle.write(chunk)
                    stats.downloaded_bytes += len(chunk)
            if response.status_code not in (200, 206):
                stats.error = f"HTTP {response.status_code}"
    except (httpx.HTTPError, OSError) as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
    finished = time.perf_counter()
    stats.download_seconds = max(finished - started, 1e-9)
    if first_byte_at is not None:
        stats.ttfb_seconds = first_byte_at - started
    if stats.downloaded_bytes:
        stats.mbps = stats.downloaded_bytes / 1e6 / stats.download_seconds
        stats.sha256 = _sha256_of(target)
    return stats


def netcdf_facts(path: Path) -> dict[str, Any]:
    """用 xarray 读产物元数据（变量、形状、时间与经纬度范围），不整载数据。"""
    try:
        import xarray as xr

        with xr.open_dataset(path) as dataset:
            facts: dict[str, Any] = {
                "variables": sorted(str(name) for name in dataset.data_vars),
                "dims": dict(dataset.sizes),
            }
            for name in ("valid_time", "time"):
                if name in dataset.coords and dataset.sizes.get(name):
                    values = dataset[name].values
                    facts["time"] = [str(values[0]), str(values[-1]), int(dataset.sizes[name])]
                    break
            for name in ("latitude", "lat", "longitude", "lon"):
                if name in dataset.coords and dataset.sizes.get(name):
                    values = dataset[name].values
                    facts[name] = [
                        float(values.min()),
                        float(values.max()),
                        int(dataset.sizes[name]),
                    ]
            return facts
    except Exception as exc:  # 校验失败只记录，不掩盖下载结果
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 纯 CDS


def build_cds_request(
    *,
    year: int,
    month: int,
    days: Sequence[int],
    hours: Sequence[str],
    area: Sequence[float],
    variable: str = "2m_temperature",
) -> dict[str, Any]:
    """构造 ERA5 single-levels 请求（CDS 官方参数名，不是本项目内部模型）。"""
    return {
        "product_type": "reanalysis",
        "variable": [variable],
        "year": str(year),
        "month": f"{month:02d}",
        "day": [f"{day:02d}" for day in days],
        "time": list(hours),
        "area": [float(value) for value in area],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


def _cds_failure_reason(remote: Any) -> str | None:
    """从 CDS 任务对象里取出服务端拒绝/失败原因（有就返回，不猜）。"""
    try:
        payload = remote.json
    except Exception:  # 仅用于诊断，取不到就不写
        return None
    if not isinstance(payload, Mapping):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    results = metadata.get("results")
    if not isinstance(results, Mapping):
        return None
    reason = results.get("traceback") or results.get("detail") or results.get("title")
    return str(reason) if reason else None


def run_cds(
    *,
    request: Mapping[str, Any],
    workdir: Path,
    timeout_seconds: float,
    collection: str = CDS_COLLECTION,
) -> tuple[RunRecord, dict[str, Any]]:
    """纯 CDS API 路径：submit → 轮询 → get_results → 下载。"""
    import cdsapi

    record = RunRecord(mode="cds", workload={"collection": collection, **dict(request)})
    t0 = time.perf_counter()
    client = cdsapi.Client(quiet=True)
    api = client.client  # ecmwf.datastores.client.Client（cdsapi 内部同一实例）
    record.phases["client_init"] = time.perf_counter() - t0

    try:
        submitted_at = time.perf_counter()
        remotes = api.submit(collection, dict(request))
        remote = remotes[0] if isinstance(remotes, list) else remotes
        record.phases["submit"] = time.perf_counter() - submitted_at

        transitions: list[dict[str, Any]] = []
        last_status: str | None = None
        queue_start: float | None = None
        running_at: float | None = None
        while True:
            status = str(remote.status)
            if status != last_status:
                transitions.append({"status": status, "at": time.perf_counter() - t0})
                if status == "accepted" and queue_start is None:
                    queue_start = time.perf_counter()
                if status == "running" and running_at is None:
                    running_at = time.perf_counter()
                last_status = status
            if status in STATUS_TERMINAL:
                break
            if time.perf_counter() - t0 > timeout_seconds:
                record.error = f"CDS 轮询超时（>{timeout_seconds:.0f}s），最后状态 {status}"
                break
            time.sleep(2.0)
        record.phases["poll_total"] = time.perf_counter() - t0
        if last_status != "successful":
            reason = _cds_failure_reason(remote)
            record.error = record.error or f"CDS 请求未成功：{last_status}"
            if reason:
                record.error = f"{record.error}（{reason}）"
            # 失败/超时也要记录真实耗时：否则 wall=0 会让"混合制先就绪"误判失败一路为赢家。
            record.wall_seconds = time.perf_counter() - t0
            return record, {
                "transitions": transitions,
                "request_id": getattr(remote, "request_id", None),
            }

        if queue_start is not None and running_at is not None:
            record.phases["queue_wait"] = running_at - queue_start
        if running_at is not None:
            record.phases["generate"] = time.perf_counter() - running_at

        results_at = time.perf_counter()
        results = remote.get_results()
        record.phases["get_results"] = time.perf_counter() - results_at
        location = str(results.location)
        record.artifacts.append(
            {
                "kind": "cds_result",
                "declared_bytes": int(results.content_length),
                "content_type": str(results.content_type),
                "asset": dict(results.asset or {}),
            }
        )
        target = workdir / f"cds-era5-2t-{int(time.time())}.nc"
        download_started = time.perf_counter()
        stats = download_stream(location, target, timeout_seconds=timeout_seconds)
        record.phases["download"] = time.perf_counter() - download_started
        record.artifacts[-1].update(
            {
                "path": str(target),
                "http_status": stats.http_status,
                "downloaded_bytes": stats.downloaded_bytes,
                "ttfb_seconds": stats.ttfb_seconds,
                "download_mbps": stats.mbps,
                "sha256": stats.sha256,
            }
        )
        record.bytes_total = stats.downloaded_bytes
        record.validation = netcdf_facts(target)
        record.ok = stats.ok and "error" not in record.validation
        if stats.error:
            record.error = stats.error
        record.wall_seconds = time.perf_counter() - t0
        return record, {
            "transitions": transitions,
            "request_id": getattr(remote, "request_id", None),
        }
    except Exception as exc:  # 基准脚本要记录失败而不是崩掉矩阵
        record.error = f"{type(exc).__name__}: {exc}"
        record.wall_seconds = time.perf_counter() - t0
        return record, {}


# ---------------------------------------------------------------- 纯 NCAR


def surface_filelist_url(year: int) -> str:
    """GDEX netCDF4 文件清单端点（按年分组）。"""
    return (
        f"{GDEX_ENDPOINT}/api/datasets/{GDEX_DSID}/filelist/{year}{SURFACE_NETCDF_GINDEX_SUFFIX}/"
    )


def select_ncar_file(
    payload: Mapping[str, Any],
    *,
    var_code: str,
    year: int,
    month: int,
) -> dict[str, Any] | None:
    """从 GDEX filelist JSON 里挑出 "变量 + 年月" 对应的月度文件（纯函数，可离线测）。"""
    groups = payload.get("data", {}).get("groups") or []
    if not isinstance(groups, list):
        return None
    prefix = f"{year}{month:02d}"
    for group in groups:
        rows = group.get("rows") if isinstance(group, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            cells = {str(cell.get("name")): cell for cell in row if isinstance(cell, Mapping)}
            name_cell = cells.get("File Name") or {}
            name = str(name_cell.get("value") or "")
            if var_code not in name or f".{prefix}" not in name.split(var_code, 1)[-1]:
                continue
            size_raw = (cells.get("Size") or {}).get("value")
            return {
                "file_name": name,
                "size_bytes": int(size_raw)
                if isinstance(size_raw, (int, float, str)) and str(size_raw).isdigit()
                else None,
                "url": name_cell.get("url"),
                "thredds_url": name_cell.get("thredds_url"),
                "data_format": (cells.get("Data Format") or {}).get("value"),
            }
    return None


def run_ncar(
    *,
    var_code: str,
    year: int,
    month: int,
    workdir: Path,
    byte_cap: int | None,
    timeout_seconds: float,
) -> tuple[RunRecord, dict[str, Any]]:
    """纯 NCAR 路径：目录 API 定位文件 → HEAD 核对大小 → HTTPS 流式下载。"""
    record = RunRecord(
        mode="ncar",
        workload={"var_code": var_code, "year": year, "month": month, "byte_cap": byte_cap},
    )
    t0 = time.perf_counter()
    discovery: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True, trust_env=True) as client:
            response = client.get(surface_filelist_url(year))
            response.raise_for_status()
            payload = response.json()
        record.phases["catalog"] = time.perf_counter() - t0
        entry = select_ncar_file(payload, var_code=var_code, year=year, month=month)
        if entry is None:
            record.error = f"GDEX 文件清单中找不到 {var_code} {year}-{month:02d}"
            return record, discovery
        discovery.update(entry)
        url = str(entry.get("url"))

        head_started = time.perf_counter()
        with httpx.Client(timeout=60.0, follow_redirects=True, trust_env=True) as client:
            head = client.head(url)
        record.phases["head"] = time.perf_counter() - head_started
        discovery["head_status"] = head.status_code
        discovery["head_content_length"] = head.headers.get("Content-Length")
        discovery["accept_ranges"] = head.headers.get("Accept-Ranges")

        target = workdir / f"ncar-{entry['file_name']}"
        download_started = time.perf_counter()
        stats = download_stream(url, target, byte_cap=byte_cap, timeout_seconds=timeout_seconds)
        record.phases["download"] = time.perf_counter() - download_started
        record.artifacts.append(
            {
                "kind": "ncar_https",
                "path": str(target),
                "url": url,
                "partial": byte_cap is not None,
                "declared_bytes": stats.declared_bytes,
                "downloaded_bytes": stats.downloaded_bytes,
                "http_status": stats.http_status,
                "ttfb_seconds": stats.ttfb_seconds,
                "download_mbps": stats.mbps,
                "sha256": stats.sha256,
            }
        )
        record.bytes_total = stats.downloaded_bytes
        if byte_cap is None:
            record.validation = netcdf_facts(target)
        else:
            record.validation = {"skipped": "byte_cap 指定的部分下载，不能当完整文件做科学校验"}
        record.ok = stats.ok
        if stats.error:
            record.error = stats.error
        record.wall_seconds = time.perf_counter() - t0
        if (
            entry.get("size_bytes")
            and stats.declared_bytes
            and entry["size_bytes"] != stats.declared_bytes
        ):
            record.notes.append(
                f"目录声明 {entry['size_bytes']} B ≠ HEAD Content-Length "
                f"{stats.declared_bytes} B（以 HEAD 为准）"
            )
        return record, discovery
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        record.wall_seconds = time.perf_counter() - t0
        return record, discovery


# ---------------------------------------------------------------- 混合制


def choose_hedge_winner(
    ready_times: Mapping[str, float], ok_flags: Mapping[str, bool]
) -> tuple[str | None, float | None]:
    """返回 (先就绪**且成功**的路径, 其耗时)；全部失败返回 (None, None)。

    只按"成功"排序：被拒绝/超时的一路 wall 可能接近 0，不能因此成为赢家。
    """
    usable = {name: seconds for name, seconds in ready_times.items() if ok_flags.get(name)}
    if not usable:
        return None, None
    winner = min(usable, key=usable.__getitem__)
    return winner, usable[winner]


def run_mixed(
    *,
    cds_request: Mapping[str, Any],
    ncar_args: Mapping[str, Any],
    workdir: Path,
    timeout_seconds: float,
    hedge_grace_seconds: float = 0.0,
) -> tuple[RunRecord, dict[str, Any]]:
    """混合制：CDS 与 NCAR 同时启动，记录各自就绪时间与"先就绪"时间。

    ``hedge_grace_seconds > 0`` 时模拟"先到先用"：第一路就绪后再等 grace 秒，
    仍未就绪的另一路被放弃（记录为 abandoned），不再阻塞整体交付。
    """
    record = RunRecord(mode="mixed", workload={"cds": dict(cds_request), "ncar": dict(ncar_args)})
    results: dict[str, tuple[RunRecord, dict[str, Any]]] = {}
    start_barrier = threading.Barrier(2)
    started = time.perf_counter()

    def _cds() -> None:
        start_barrier.wait()
        results["cds"] = run_cds(
            request=cds_request, workdir=workdir, timeout_seconds=timeout_seconds
        )

    def _ncar() -> None:
        start_barrier.wait()
        results["ncar"] = run_ncar(
            workdir=workdir,
            timeout_seconds=timeout_seconds,
            **dict(ncar_args),
        )

    threads = [
        threading.Thread(target=_cds, name="cds", daemon=True),
        threading.Thread(target=_ncar, name="ncar", daemon=True),
    ]
    for thread in threads:
        thread.start()
    if hedge_grace_seconds > 0:
        # 先到先用：等第一路就绪，再给另一路 grace 秒；超时放弃（daemon 线程，进程可先返回）。
        deadline = started + timeout_seconds
        while results == {} and time.perf_counter() < deadline:
            time.sleep(2.0)
        grace_deadline = time.perf_counter() + hedge_grace_seconds
        while len(results) < 2 and time.perf_counter() < grace_deadline:
            time.sleep(2.0)
    else:
        for thread in threads:
            thread.join()
    abandoned = [name for name in ("cds", "ncar") if name not in results]
    wall = time.perf_counter() - started

    ready_times = {name: result[0].wall_seconds for name, result in results.items()}
    ok_flags = {name: result[0].ok for name, result in results.items()}
    winner, effective = choose_hedge_winner(ready_times, ok_flags)
    wasted = sum(result[0].bytes_total for name, result in results.items() if name != winner)
    record.phases["cds"] = results.get("cds", (RunRecord(mode="cds", workload={}), {}))[
        0
    ].wall_seconds
    record.phases["ncar"] = results.get("ncar", (RunRecord(mode="ncar", workload={}), {}))[
        0
    ].wall_seconds
    record.artifacts = [
        {"side": name, **artifact}
        for name, (result, _discovery) in results.items()
        for artifact in result.artifacts
    ]
    record.validation = {
        "winner": winner,
        "time_to_first_artifact_seconds": effective,
        "both_sides_seconds": ready_times,
        "wasted_bytes": wasted,
        "abandoned_sides": abandoned,
        "cds_ok": results.get("cds", (RunRecord(mode="cds", workload={}), {}))[0].ok,
        "ncar_ok": results.get("ncar", (RunRecord(mode="ncar", workload={}), {}))[0].ok,
    }
    record.ok = any(result[0].ok for result in results.values())
    record.bytes_total = sum(result[0].bytes_total for result in results.values())
    record.wall_seconds = wall
    for name, (result, _discovery) in results.items():
        if result.error:
            record.notes.append(f"{name}: {result.error}")
    return record, {
        "sides": {name: value[1] for name, value in results.items()},
        "wasted_bytes": wasted,
    }


# ---------------------------------------------------------------- 汇总输出


def build_summary(records: Sequence[RunRecord]) -> list[dict[str, Any]]:
    """每行一个条件：端到端耗时、字节数、有效吞吐、主要阶段。"""
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {
            "mode": record.mode,
            "ok": record.ok,
            "wall_seconds": round(record.wall_seconds, 2),
            "bytes": record.bytes_total,
            "effective_mbps": round(record.bytes_total / 1e6 / record.wall_seconds, 2)
            if record.bytes_total and record.wall_seconds
            else None,
        }
        if record.mode == "mixed":
            first = record.validation.get("time_to_first_artifact_seconds")
            row["time_to_first_artifact_seconds"] = round(first, 2) if first else None
            row["winner"] = record.validation.get("winner")
            row["wasted_bytes"] = record.validation.get("wasted_bytes")
        row["phase_seconds"] = {name: round(value, 2) for name, value in record.phases.items()}
        if record.error:
            row["error"] = record.error
        rows.append(row)
    return rows


def render_markdown(records: Sequence[RunRecord], meta: Mapping[str, Any]) -> str:
    """生成可直接贴进报告的结果表。"""
    lines = [
        f"- 运行时间：{meta.get('generated_at')}",
        f"- 环境：{meta.get('environment')}",
        "",
        "| 条件 | 成功 | 端到端耗时 (s) | 字节数 | 有效吞吐 (MB/s) | 主要阶段 (s) |",
        "|---|---|---|---|---|---|",
    ]
    for row in build_summary(records):
        phases = ", ".join(f"{name}={value}" for name, value in row["phase_seconds"].items())
        lines.append(
            f"| {row['mode']} | {'PASS' if row['ok'] else 'FAIL'} | {row['wall_seconds']} | "
            f"{row['bytes']} | {row['effective_mbps']} | {phases} |"
        )
    mixed = next((row for row in build_summary(records) if row["mode"] == "mixed"), None)
    if mixed:
        lines += [
            "",
            f"混合制先就绪：{mixed.get('time_to_first_artifact_seconds')} s"
            f"（winner={mixed.get('winner')}，冗余字节={mixed.get('wasted_bytes')}）",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：跑矩阵、写 JSON 与 Markdown 摘要。"""
    parser = argparse.ArgumentParser(description="M6 下载效率基准（CDS / NCAR / 混合）")
    parser.add_argument("--mode", choices=("cds", "ncar", "mixed", "all"), default="all")
    parser.add_argument(
        "--collection",
        default=CDS_COLLECTION,
        help="CDS 集合名（默认 reanalysis-era5-single-levels；可换 era5-land 做连通对照）",
    )
    parser.add_argument("--out", type=Path, default=Path("benchmarks/m6"))
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/rdh-m6-bench"))
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--month", type=int, default=1)
    parser.add_argument("--cds-days", type=int, default=3, help="从 1 号起的天数")
    parser.add_argument(
        "--cds-start-day", type=int, default=1, help="起始日（混合制用不同日期避免 CDS 缓存命中）"
    )
    parser.add_argument(
        "--mixed-day-offset",
        type=int,
        default=10,
        help="混合制 CDS 侧相对 --cds-start-day 的日偏移（0 = 与 --mode cds 同目标同日期）",
    )
    parser.add_argument(
        "--hedge-grace-seconds",
        type=float,
        default=0.0,
        help=">0 时混合制先到先用：第一路就绪后再等 N 秒，放弃未就绪的另一路",
    )
    parser.add_argument("--cds-hours", type=int, default=24, help="逐时次数量（24 = 全天）")
    parser.add_argument("--area", default="45,100,30,125", help="N,W,S,E")
    parser.add_argument("--ncar-var", default=VAR_CODE_2T)
    parser.add_argument(
        "--ncar-cap-mb", type=int, default=0, help=">0 时只下载前 N MB（轻量吞吐测）"
    )
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args(argv)

    modes = ["cds", "ncar", "mixed"] if args.mode == "all" else [args.mode]

    area = [float(value) for value in args.area.split(",")]
    hours = [f"{index:02d}:00" for index in range(args.cds_hours)]
    days = list(range(args.cds_start_day, args.cds_start_day + args.cds_days))
    cds_request = build_cds_request(
        year=args.year, month=args.month, days=days, hours=hours, area=area
    )
    # 混合制用错开的日期：CDS 对同一请求有缓存，直接复用无法测出真实排队时间。
    mixed_days = list(
        range(
            args.cds_start_day + args.mixed_day_offset,
            args.cds_start_day + args.mixed_day_offset + args.cds_days,
        )
    )
    mixed_request = build_cds_request(
        year=args.year, month=args.month, days=mixed_days, hours=hours, area=area
    )
    ncar_args = {
        "var_code": args.ncar_var,
        "year": args.year,
        "month": args.month,
        "byte_cap": args.ncar_cap_mb * 1024 * 1024 if args.ncar_cap_mb else None,
    }

    args.workdir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    records: list[RunRecord] = []
    extra: dict[str, Any] = {}
    for mode in modes:
        print(f"[m6-bench] 开始 {mode} …", file=sys.stderr)
        if mode == "cds":
            record, extra["cds"] = run_cds(
                request=cds_request,
                workdir=args.workdir,
                timeout_seconds=args.timeout,
                collection=args.collection,
            )
        elif mode == "ncar":
            record, extra["ncar"] = run_ncar(
                workdir=args.workdir, timeout_seconds=args.timeout, **ncar_args
            )
        else:
            record, extra["mixed"] = run_mixed(
                cds_request=mixed_request,
                ncar_args=ncar_args,
                workdir=args.workdir,
                timeout_seconds=args.timeout,
                hedge_grace_seconds=args.hedge_grace_seconds,
            )
        records.append(record)
        print(
            f"[m6-bench] {mode}: ok={record.ok} wall={record.wall_seconds:.1f}s "
            f"bytes={record.bytes_total} error={record.error}",
            file=sys.stderr,
        )

    generated_at = datetime.now(UTC).isoformat()
    meta = {
        "schema": "rdh-m6-bench/1",
        "generated_at": generated_at,
        "environment": (
            f"{platform.platform()} / Python {platform.python_version()} / {platform.machine()}"
        ),
        "workload": {
            "cds_request": cds_request,
            "mixed_cds_request": mixed_request,
            "ncar": ncar_args,
        },
    }
    (args.out / "results.json").write_text(
        json.dumps(
            {**meta, "runs": [record.to_dict() for record in records], "extra": extra},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = render_markdown(records, meta)
    (args.out / "summary.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    return 0 if all(record.ok for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
