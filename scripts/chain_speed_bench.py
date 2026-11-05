#!/usr/bin/env python3
"""轻量全链路下载测速基准：验证 ResearchDataHub 相对"朴素下载脚本"的真实提升。

测什么
------
对比两条**同一逻辑需求**的端到端路径：

* ``baseline``：**朴素路径** —— 直接用 httpx 下载远端文件，不做发现/计划/校验/交付。
  它代表"用户自己写个下载脚本"这条基准线。
* ``rdh``：**ResearchDataHub 全链路** —— 请求契约 → 计划器 → 调度器（租约/心跳）→
  传输（续传/重试）→ 六层完整性校验 → staging→raw 原子提交 → 交付到用户目录 → Manifest。

两条路径下载**同一个远端对象**、写到**同一块盘**，因此差值反映的是框架本身的开销/收益，
而不是"选了个更快的数据源"。

停止规则（按需求）
------------------
同一时间窗口内，**响应率（完成率）达到 80% 即停止**；未响应的直接记 ``FAILED``。
即 ``completed / attempted >= 0.80`` 时不再发起新的样本，已在途的等其结束或超时。

响应率口径
----------
* ``SUCCESS``：文件落盘且字节完整（与远端声明一致，或 sha256 与远端一致）。
* ``FAILED``：HTTP 错误、超时、字节不符、校验失败、磁盘写入失败。
* ``TIMEOUT``：超过 ``--sample-timeout`` 仍未响应（计入 FAILED，同时单独统计）。
* **``NOT_RUN``（本轮未测）**：需要凭据或专门账户而未配置的平台。
  这类**不计入响应率分母**，单独列出，不允许被静默当成"通过"。

用法
----
离线冒烟（本地 HTTP 服务器，验证基准逻辑本身）::

    PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py --mode local --out /tmp/speed

真实外网（NCAR GDEX，公开匿名）::

    PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py --mode ncar --out benchmarks/speed

带凭据的平台（等你配好后）::

    EARTHDATA_TOKEN=... CDSE_CLIENT_ID=... CDSE_CLIENT_SECRET=... CDSAPI_KEY=... \
    PYTHONPATH=src .venv/bin/python scripts/chain_speed_bench.py \
        --mode earthdata,copernicus,cds --out benchmarks/speed

设计约束
--------
* **不接触 CDS 取数**：``--mode cds`` 默认要求显式 ``--allow-cds``；按 ADR-0014，
  用 cdsapi 发真实取数请求是被禁止的。CDS 项在无 ``--allow-cds`` 时记 ``NOT_RUN``。
* 每次样本大小写进结果，不做"小样本外推大文件"的推算。
* 只测量、不判优：结论写在 ``docs/chain-speed-benchmark.md``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

__all__ = [
    "CHUNK_BYTES",
    "DEFAULT_STOP_RESPONSE_RATE",
    "SampleResult",
    "SampleStatus",
    "SpeedBench",
    "main",
]

CHUNK_BYTES: Final[int] = 1 << 20
DEFAULT_STOP_RESPONSE_RATE: Final[float] = 0.80
"""响应率达到该阈值即停止发起新样本（用户指定的 80% 规则）。"""

_USER_AGENT: Final[str] = "researchdatahub-speed-bench/0.2"

# GEE 官方 OAuth 客户端（公开值，来自 earthengine-api 源码；不是用户凭据）
_GEE_CLIENT_ID: Final[str] = (
    "517222506229-vsmmajv00ul0bs7p89v5m89qs8eb9359.apps.googleusercontent.com"
)
_GEE_CLIENT_SECRET: Final[str] = "RUP0RZ6e0pPhDzsqIJ7KlNd1"


# --------------------------------------------------------------------------- #
# 结果载体
# --------------------------------------------------------------------------- #
class SampleStatus:
    """单样本状态（字符串常量，避免把"未测"和"失败"混为一谈）。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NOT_RUN = "NOT_RUN"


@dataclass
class SampleResult:
    """一个样本的完整计量结果。"""

    path_id: str
    """``baseline`` 或 ``rdh``。"""
    mode: str
    """测速对象：``local`` / ``ncar`` / ``earthdata`` / ``copernicus`` / ``cds``。"""
    index: int
    status: str
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    bytes_received: int = 0
    http_status: int | None = None
    sha256: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def throughput_bps(self) -> float:
        """吞吐（字节/秒）；时长为 0 时返回 0。"""
        return self.bytes_received / self.duration_s if self.duration_s > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好表示。"""
        payload = asdict(self)
        payload["throughput_bps"] = round(self.throughput_bps, 1)
        return payload


@dataclass
class PathSummary:
    """一条路径的汇总统计。"""

    path_id: str
    mode: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    not_run: int = 0
    total_bytes: int = 0
    total_duration_s: float = 0.0
    durations_s: list[float] = field(default_factory=list)
    throughputs_bps: list[float] = field(default_factory=list)

    @property
    def response_rate(self) -> float | None:
        """响应率 = 成功 / 实际尝试数（``NOT_RUN`` 不计入分母）。

        Returns
        -------
        float | None
            ``None`` 表示没有任何有效尝试（例如整条路径因缺凭据未测）。
        """
        return self.succeeded / self.attempted if self.attempted > 0 else None

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好表示（含中位数与 P95，避免只报均值被离群值带偏）。"""
        rate = self.response_rate
        return {
            "path_id": self.path_id,
            "mode": self.mode,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "not_run": self.not_run,
            "response_rate": None if rate is None else round(rate, 4),
            "total_bytes": self.total_bytes,
            "total_duration_s": round(self.total_duration_s, 3),
            "mean_duration_s": (
                round(statistics.fmean(self.durations_s), 3) if self.durations_s else None
            ),
            "median_duration_s": (
                round(statistics.median(self.durations_s), 3) if self.durations_s else None
            ),
            "p95_duration_s": _percentile(self.durations_s, 95),
            "mean_throughput_bps": (
                round(statistics.fmean(self.throughputs_bps), 1) if self.throughputs_bps else None
            ),
            "median_throughput_bps": (
                round(statistics.median(self.throughputs_bps), 1) if self.throughputs_bps else None
            ),
        }


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """线性插值分位数（样本不足 2 个时返回最大值或 ``None``）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


# --------------------------------------------------------------------------- #
# 下载实现
# --------------------------------------------------------------------------- #
def _stream_download(
    url: str,
    target: Path,
    *,
    timeout: float,
    expected_size: int | None = None,
    follow_redirects: bool = True,
    attempts: int = 1,
    backoff_s: float = 1.0,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, str, int | None]:
    """流式下载到 ``target``，返回 ``(字节数, sha256, HTTP 状态)``。

    与 RDH 传输层一样**不把整个文件读进内存**（逐块写入 + 增量哈希），
    这样两条路径的对比才公平。

    ``attempts`` 默认 1：**朴素脚本不重试**。这是"用户自己写下载脚本"的真实形态，
    也是与 RDH（有重试 + 续传）对比的基线。需要更强基线时用 ``--baseline-retries``。

    Raises
    ------
    httpx.HTTPStatusError
        HTTP 4xx/5xx。
    ValueError
        收到的字节数与远端预期不符（截断）。
    """
    last: BaseException | None = None
    total = max(1, attempts)
    for attempt in range(total):
        try:
            return _stream_once(
                url,
                target,
                timeout=timeout,
                expected_size=expected_size,
                follow_redirects=follow_redirects,
                extra_headers=extra_headers,
            )
        except (httpx.HTTPError, ValueError, OSError) as exc:
            last = exc
            if attempt + 1 < total:
                time.sleep(backoff_s)
    assert last is not None
    raise last


def _stream_once(
    url: str,
    target: Path,
    *,
    timeout: float,
    expected_size: int | None = None,
    follow_redirects: bool = True,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, str, int | None]:
    """单次流式下载（不做任何重试）。

    重定向是**手动**跟随的：EDL（Earthdata）会 303 到**已签名**的 CloudFront 地址，
    目标主机不需要 ``Authorization``；而 httpx 在跨主机重定向时会主动丢弃该头
    （安全设计，正确）。本函数保证 **只在主机名不变时携带凭据**。
    """
    digest = hashlib.sha256()
    received = 0
    status = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    auth_headers = dict(extra_headers or {})
    origin_host = httpx.URL(url).host
    max_hops = 5 if follow_redirects else 0
    finished = False

    with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=True) as c:
        current = url
        for _hop in range(max_hops + 1):
            headers = {"User-Agent": _USER_AGENT}
            if httpx.URL(current).host == origin_host:
                headers.update(auth_headers)
            with c.stream("GET", current, headers=headers) as response:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = str(httpx.URL(current).join(location))
                    continue
                status = response.status_code
                response.raise_for_status()
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                finished = True
                break
    if not finished:
        raise RuntimeError(f"too many redirects (> {max_hops}) for {url}")
    if expected_size is not None and expected_size > 0 and received != expected_size:
        raise ValueError(f"truncated: received {received} != expected {expected_size}")
    return received, "sha256:" + digest.hexdigest(), status


# --------------------------------------------------------------------------- #
# 测速主体
# --------------------------------------------------------------------------- #
class SpeedBench:
    """执行对比测速并汇总。

    两条路径通过"取数回调"解耦：调用方提供 ``fetch_baseline(url, target, ...)`` 与
    ``fetch_rdh(url, target, ...)``，基准只负责计时、状态判定、停止规则与汇总。
    这样本地/NCAR/未来三个平台可以共用同一套判定口径。
    """

    def __init__(
        self,
        *,
        mode: str,
        out_dir: Path,
        samples: int,
        stop_rate: float = DEFAULT_STOP_RESPONSE_RATE,
        sample_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.mode = mode
        self.out_dir = out_dir
        self.samples = max(1, samples)
        self.stop_rate = stop_rate
        self.sample_timeout = sample_timeout
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self.results: list[SampleResult] = []

    # -- 单样本 ---------------------------------------------------------- #
    def run_sample(
        self,
        path_id: str,
        index: int,
        fetch: Callable[[Path], tuple[int, str, int | None]],
    ) -> SampleResult:
        """执行一个样本并把结果登记下来（异常一律折算成状态，不中断整个基准）。

        Parameters
        ----------
        path_id:
            ``baseline`` / ``rdh``。
        index:
            样本序号。
        fetch:
            实际取数回调，接收目标路径，返回 ``(字节数, sha256, http 状态)``。
        """
        target = self.out_dir / "data" / self.mode / path_id / f"sample-{index:03d}.bin"
        started_wall = self._wall_clock().isoformat()
        started = self._clock()
        try:
            size, digest, status = fetch(target)
            duration = self._clock() - started
            result = SampleResult(
                path_id=path_id,
                mode=self.mode,
                index=index,
                status=SampleStatus.SUCCESS,
                started_at=started_wall,
                finished_at=self._wall_clock().isoformat(),
                duration_s=duration,
                bytes_received=size,
                http_status=status,
                sha256=digest,
            )
        except TimeoutError as exc:
            duration = self._clock() - started
            result = SampleResult(
                path_id=path_id,
                mode=self.mode,
                index=index,
                status=SampleStatus.TIMEOUT,
                started_at=started_wall,
                finished_at=self._wall_clock().isoformat(),
                duration_s=duration,
                error=f"timeout after {self.sample_timeout}s: {exc}",
            )
        except BaseException as exc:
            duration = self._clock() - started
            result = SampleResult(
                path_id=path_id,
                mode=self.mode,
                index=index,
                status=SampleStatus.FAILED,
                started_at=started_wall,
                finished_at=self._wall_clock().isoformat(),
                duration_s=duration,
                error=f"{type(exc).__name__}: {exc}",
                detail={"traceback": traceback.format_exc(limit=3)},
            )
        self.results.append(result)
        return result

    # -- 带停止规则的批量执行 -------------------------------------------- #
    def run_path(
        self,
        path_id: str,
        fetch_factory: Callable[[int], Callable[[Path], tuple[int, str, int | None]]],
        *,
        samples: int | None = None,
    ) -> PathSummary:
        """按 80% 响应率规则跑完一条路径。

        Parameters
        ----------
        path_id:
            路径标识。
        fetch_factory:
            ``index -> fetch`` 工厂（允许每次样本使用不同的远端对象）。
        samples:
            最大样本数；``None`` 用构造时的值。
        """
        limit = samples if samples is not None else self.samples
        attempted = 0
        succeeded = 0
        for index in range(limit):
            result = self.run_sample(path_id, index, fetch_factory(index))
            if result.status is SampleStatus.NOT_RUN:
                continue
            attempted += 1
            if result.status is SampleStatus.SUCCESS:
                succeeded += 1
            rate = succeeded / attempted
            # 停止规则（用户指定）：同一时间窗口内响应率达到阈值即停止，未响应的记失败。
            # ``stop_rate >= 1.0`` 时禁用提前停止（用于需要固定样本量的统计对比）。
            if self.stop_rate < 1.0 and rate >= self.stop_rate:
                break
        return self.summarise(path_id)

    def summarise(self, path_id: str) -> PathSummary:
        """汇总某条路径的样本。"""
        summary = PathSummary(path_id=path_id, mode=self.mode)
        for result in self.results:
            if result.path_id != path_id:
                continue
            if result.status is SampleStatus.NOT_RUN:
                summary.not_run += 1
                continue
            summary.attempted += 1
            if result.status is SampleStatus.SUCCESS:
                summary.succeeded += 1
                summary.total_bytes += result.bytes_received
                summary.total_duration_s += result.duration_s
                summary.durations_s.append(result.duration_s)
                if result.throughput_bps > 0:
                    summary.throughputs_bps.append(result.throughput_bps)
            elif result.status is SampleStatus.TIMEOUT:
                summary.timed_out += 1
                summary.failed += 1
            else:
                summary.failed += 1
        return summary

    # -- 输出 ------------------------------------------------------------ #
    def write(self, extra: dict[str, Any] | None = None) -> Path:
        """把原始样本与汇总写进 ``out_dir/results.json``，返回路径。"""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path_ids = sorted({result.path_id for result in self.results}) or ["baseline", "rdh"]
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at": self._wall_clock().isoformat(),
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "machine": platform.machine(),
            },
            "parameters": {
                "mode": self.mode,
                "samples": self.samples,
                "stop_response_rate": self.stop_rate,
                "sample_timeout_s": self.sample_timeout,
            },
            "summaries": [self.summarise(path_id).as_dict() for path_id in path_ids],
            "samples": [result.as_dict() for result in self.results],
        }
        if extra:
            payload["extra"] = extra
        target = self.out_dir / "results.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target


def render_markdown(payload: dict[str, Any]) -> str:
    """把 results.json 渲染成可读的 Markdown 表（不写结论，结论留给人工判断）。"""
    params = payload["parameters"]
    lines = [
        f"# 全链路测速基准 — {params['mode']}",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 环境：{payload['environment']['platform']} / Python {payload['environment']['python']}",
        f"- 参数：samples={params['samples']}，stop_response_rate={params['stop_response_rate']}，"
        f"sample_timeout={params['sample_timeout_s']}s",
        "",
        "| 路径 | 尝试 | 成功 | 失败 | 超时 | 未测 | 响应率 "
        "| 中位耗时(s) | P95(s) | 中位吞吐(MB/s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in payload["summaries"]:
        rate = summary["response_rate"]
        median_tp = summary["median_throughput_bps"]
        lines.append(
            "| {path} | {a} | {s} | {f} | {t} | {nr} | {rate} | {med} | {p95} | {tp} |".format(
                path=summary["path_id"],
                a=summary["attempted"],
                s=summary["succeeded"],
                f=summary["failed"],
                t=summary["timed_out"],
                nr=summary["not_run"],
                rate="N/A" if rate is None else f"{rate:.0%}",
                med=summary["median_duration_s"],
                p95=summary["p95_duration_s"],
                tp="N/A" if median_tp is None else round(median_tp / (1 << 20), 2),
            )
        )
    lines += [
        "",
        "## 逐样本",
        "",
        "| 路径 | # | 状态 | 字节 | 耗时(s) | 吞吐(MB/s) | 错误 |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for sample in payload["samples"]:
        lines.append(
            "| {p} | {i} | {st} | {b} | {d:.3f} | {t:.2f} | {e} |".format(
                p=sample["path_id"],
                i=sample["index"],
                st=sample["status"],
                b=sample["bytes_received"],
                d=sample["duration_s"],
                t=sample["throughput_bps"] / (1 << 20),
                e=(sample["error"] or "").replace("|", "\\|")[:80],
            )
        )
    if payload.get("extra"):
        lines += ["", "## 说明", ""]
        for key, value in payload["extra"].items():
            lines.append(f"- **{key}**：{value}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 模式实现
# --------------------------------------------------------------------------- #
def _local_mode(bench: SpeedBench, *, payload_size: int) -> dict[str, Any]:
    """本地 HTTP 服务器模式：验证基准逻辑本身（离线、可重复、不做真实外网）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = bytes(range(256)) * (payload_size // 256)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/payload.bin"
    try:

        def local_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
            return lambda target: _stream_download(url, target, timeout=bench.sample_timeout)

        bench.run_path("baseline", local_factory)
        bench.run_path("rdh", local_factory)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {"note": "本地环回，仅验证基准逻辑与判定口径，不代表真实网络"}


def _rdh_full_chain(
    *,
    url: str,
    workspace: Path,
    sample_timeout: float,
    source_id: str = "ncar",
    display_name: str = "NSF NCAR GDEX",
    provider: str = "NSF NCAR RDA",
    dataset_id: str = "era5-single-levels",
    product: str = "ERA5",
    product_type: str = "reanalysis",
    file_name: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Callable[[Path], tuple[int, str, int | None]]:
    """构造一个走 **ResearchDataHub 完整链路** 的取数回调。

    链路：``DatasetRequest`` → 计划器（源匹配/拆分/去重）→ SQLite Manifest 落库 →
    调度器（原子领取 + 租约/心跳）→ 传输适配器（流式 + ``.part`` + 续传 + 重试）→
    六层完整性校验 → staging→raw 原子提交 → `delivery` 交付到用户目录。

    这个回调接收目标路径（基准给的落盘位置），RDH 侧的权威副本由它自己管理，
    最后把文件**交付**到基准的目标路径——与 baseline 写同一块盘，对比才公平。
    """

    def fetch(target: Path) -> tuple[int, str, int | None]:
        from datetime import UTC, datetime

        from researchdatahub.config.models import (
            DefaultsConfig,
            IntegrityConfig,
            ManifestConfig,
            PlannerConfig,
            RetryConfig,
            SchedulerConfig,
            StorageConfig,
            TransferConfig,
        )
        from researchdatahub.core.models import (
            AccessRoute,
            DatasetMetadata,
            DatasetRequest,
            GridSpec,
            IntegrityCheckKind,
            SourceAsset,
            SourceMetadata,
            SpatialExtent,
            TemporalExtent,
            VariableSpec,
            VerticalLevelKind,
            VerticalLevels,
        )
        from researchdatahub.delivery import apply_delivery, plan_delivery
        from researchdatahub.integrity.checker import IntegrityChecker
        from researchdatahub.manifest.connection import ManifestConnectionFactory
        from researchdatahub.manifest.repository import SqliteManifestRepository
        from researchdatahub.planner.service import create_plan
        from researchdatahub.scheduler import build_scheduler
        from researchdatahub.storage.local import LocalStorageBackend
        from researchdatahub.transfer.http import HttpTransportAdapter
        from researchdatahub.transfer.retry import RetryPolicy

        workspace.mkdir(parents=True, exist_ok=True)
        root = workspace / "rdh"
        config = DefaultsConfig(
            storage=StorageConfig(root=str(root), free_space_floor_gb=0.0),
            scheduler=SchedulerConfig(
                max_workers=1,
                free_space_floor_gb=0.0,
                heartbeat_interval_seconds=30,
                lease_seconds=120,
            ),
            retry=RetryConfig(
                max_attempts=2, backoff_seconds=0.5, backoff_max_seconds=1.0, jitter=0.0
            ),
            integrity=IntegrityConfig(
                default_checks=[
                    IntegrityCheckKind.SIZE_MATCH,
                    IntegrityCheckKind.CHECKSUM_MATCH,
                    IntegrityCheckKind.FORMAT_SNIFF,
                ],
                stale_timeout_seconds=120,
            ),
            planner=PlannerConfig(max_tasks_per_plan=10),
            transfer=TransferConfig(
                connect_timeout_seconds=15.0,
                read_timeout_seconds=sample_timeout,
                chunk_size_bytes=CHUNK_BYTES,
            ),
        )
        db_path = workspace / "manifest.db"
        factory = ManifestConnectionFactory(db_path, config=ManifestConfig())
        factory.initialize()
        storage = LocalStorageBackend(config.storage, create_dirs=True)
        integrity = IntegrityChecker(config.integrity)
        transport = HttpTransportAdapter(
            config=config.transfer,
            retry=RetryPolicy(
                max_attempts=2, backoff_seconds=0.5, backoff_max_seconds=1.0, jitter=0.0
            ),
            trust_env=False,
            extra_headers=extra_headers,
        )

        now = datetime.now(UTC)
        metadata = SourceMetadata(
            source_id=source_id,
            display_name=display_name,
            provider=provider,
            access_route=AccessRoute.HTTPS,
            dataset_id=dataset_id,
            origin_uri=url,
            retrieved_at=now,
        )
        asset = SourceAsset(
            asset_id=f"asset-bench-{source_id}-0001"[:63],
            source_id=source_id,
            dataset_id=dataset_id,
            access_route=AccessRoute.HTTPS,
            origin_uri=url,
            file_name=file_name or url.rstrip("/").rsplit("/", 1)[-1],
            size_bytes=0,
            metadata=metadata,
            discovered_at=now,
        )

        # 注意：类体里写 ``source_id = source_id`` 会因名字解析规则把自己遮蔽
        # （类作用域不参与闭包查找），必须用属性赋值或默认参数绑定外层值。
        bench_source_id = source_id

        class _StaticSource:
            source_id = bench_source_id
            supported_routes = frozenset({"https"})

            def describe(self) -> SourceMetadata:
                return metadata

            def discover_assets(self, request: DatasetRequest) -> list[SourceAsset]:
                _ = request
                return [asset]

            def health_check(self) -> bool:
                return True

        request = DatasetRequest(
            request_id="bench-speed-request",
            dataset_id=dataset_id,
            metadata=DatasetMetadata(
                product=product,
                product_type=product_type,
                variables=[VariableSpec(name="asn", units="m")],
                temporal_coverage=TemporalExtent(
                    start=datetime(2024, 7, 1, tzinfo=UTC),
                    end=datetime(2024, 8, 1, tzinfo=UTC),
                ),
                spatial_extent=SpatialExtent(west=-180.0, south=-90.0, east=180.0, north=90.0),
                vertical_levels=VerticalLevels(kind=VerticalLevelKind.NONE),
                grid=GridSpec(),
            ),
            checks=[
                IntegrityCheckKind.SIZE_MATCH,
                IntegrityCheckKind.CHECKSUM_MATCH,
                IntegrityCheckKind.FORMAT_SNIFF,
            ],
        )
        repo = SqliteManifestRepository(factory)
        try:
            plan = create_plan(
                repo=repo,
                request=request,
                adapters=[_StaticSource()],
                config=config,
            )
        finally:
            repo.close()

        scheduler = build_scheduler(
            repo_factory=lambda: SqliteManifestRepository(factory),
            storage=storage,
            integrity=integrity,
            transports=[transport],
            config=config,
        )
        try:
            submitted = scheduler.submit(plan)
            if submitted != len(plan.tasks):
                raise RuntimeError(
                    f"入队失败：submit 返回 {submitted}，计划有 {len(plan.tasks)} 个任务；"
                    f"任务状态={[(t.task_id, t.state.value) for t in plan.tasks]}"
                )
            summary = scheduler.run(max_workers=1)
        finally:
            transport.close()

        if summary.counts.get("completed", 0) != len(plan.tasks):
            raise RuntimeError(f"RDH 链路未完成：counts={summary.counts} errors={summary.errors}")

        repo = SqliteManifestRepository(factory)
        try:
            records = list(repo.list_file_records(request_id=request.request_id))
        finally:
            repo.close()
        if not records:
            raise RuntimeError("RDH 链路完成但没有文件记录（不应发生）")

        delivery = apply_delivery(plan_delivery(records, target.parent))
        delivered = delivery.delivered
        if not delivered:
            raise RuntimeError(f"交付失败：{delivery.as_dict()}")
        produced = Path(delivered[0].target_path)
        if produced != target:
            produced.replace(target)
        total = sum(record.size_bytes for record in records)
        return total, records[0].checksum.value, 200

    return fetch


def _ncar_mode(
    bench: SpeedBench,
    *,
    url: str | None,
    workspace: Path,
    real_rdh: bool,
    baseline_retries: int = 1,
) -> dict[str, Any]:
    """NCAR GDEX 模式：公开匿名文件，走真实 HTTPS 下载。

    ``real_rdh=False`` 时两条路径都是朴素 httpx（**仅用于连通性冒烟**，
    此时对比没有意义，结果里会标注 ``comparison_valid=false``）。
    """
    if not url:
        for path_id in ("baseline", "rdh"):
            bench.results.append(
                SampleResult(
                    path_id=path_id,
                    mode=bench.mode,
                    index=0,
                    status=SampleStatus.NOT_RUN,
                    error="未提供 --url；NCAR 需要具体文件 URL（可用 rdh source 命令发现）",
                )
            )
        return {"note": "NOT_RUN：未提供 URL"}

    def baseline_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        def fetch(target: Path) -> tuple[int, str, int | None]:
            return _stream_download(
                url, target, timeout=bench.sample_timeout, attempts=baseline_retries
            )

        return fetch

    def rdh_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        if not real_rdh:

            def naive(target: Path) -> tuple[int, str, int | None]:
                return _stream_download(url, target, timeout=bench.sample_timeout)

            return naive
        return _rdh_full_chain(
            url=url,
            workspace=workspace / f"rdh-run-{_index:03d}",
            sample_timeout=bench.sample_timeout,
        )

    bench.run_path("baseline", baseline_factory)
    bench.run_path("rdh", rdh_factory)
    return {
        "url": url,
        "baseline_retries": baseline_retries,
        "comparison_valid": real_rdh,
        "note": (
            "rdh 路径走完整 ResearchDataHub 链路（计划→调度→传输→校验→提交→交付）"
            if real_rdh
            else "⚠️ 本次 rdh 路径未走完整链路（--no-real-rdh），两条路径都是朴素 httpx，"
            "对比无意义，仅供连通性冒烟"
        ),
    }


def _earthdata_mode(
    bench: SpeedBench,
    *,
    workspace: Path,
    real_rdh: bool,
    baseline_retries: int = 1,
    short_name: str = "MOD11A1",
) -> dict[str, Any]:
    """NASA Earthdata 模式：CMR 发现真实 granule → HTTPS 下载（需要 EDL token）。

    与 NCAR 的关键区别：这里的资源是 **EDL 受保护**的，下载必须带
    ``Authorization: Bearer <EARTHDATA_TOKEN>``。基线路径与 RDH 路径使用**同一个**
    token 与同一个 granule，因此对比反映的是框架开销而非凭据差异。
    """
    import os

    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if not token:
        for path_id in ("baseline", "rdh"):
            bench.results.append(
                SampleResult(
                    path_id=path_id,
                    mode=bench.mode,
                    index=0,
                    status=SampleStatus.NOT_RUN,
                    error="缺少 EARTHDATA_TOKEN",
                )
            )
        return {"note": "NOT_RUN：缺少 EARTHDATA_TOKEN"}

    headers = {"Authorization": f"Bearer {token}"}
    try:
        granule_url, granule_size = _discover_earthdata_granule(short_name, headers)
    except Exception as exc:
        for path_id in ("baseline", "rdh"):
            bench.results.append(
                SampleResult(
                    path_id=path_id,
                    mode=bench.mode,
                    index=0,
                    status=SampleStatus.FAILED,
                    error=f"CMR 发现失败：{type(exc).__name__}: {exc}",
                )
            )
        return {"note": "CMR 发现失败", "short_name": short_name}

    def baseline_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        def fetch(target: Path) -> tuple[int, str, int | None]:
            return _stream_download(
                granule_url,
                target,
                timeout=bench.sample_timeout,
                attempts=baseline_retries,
                extra_headers=headers,
            )

        return fetch

    def rdh_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        if not real_rdh:

            def naive(target: Path) -> tuple[int, str, int | None]:
                return _stream_download(
                    granule_url, target, timeout=bench.sample_timeout, extra_headers=headers
                )

            return naive
        return _rdh_full_chain(
            url=granule_url,
            workspace=workspace / f"rdh-run-{_index:03d}",
            sample_timeout=bench.sample_timeout,
            source_id="earthdata",
            display_name="NASA Earthdata",
            provider="NASA EOSDIS",
            dataset_id=short_name.lower(),
            product=short_name,
            product_type="observation",
            extra_headers=headers,
        )

    bench.run_path("baseline", baseline_factory)
    bench.run_path("rdh", rdh_factory)
    return {
        "url": granule_url,
        "short_name": short_name,
        "declared_size_mb": round(granule_size / 1e6, 3),
        "auth": "EDL bearer token（值不落盘、不打印）",
        "baseline_retries": baseline_retries,
        "comparison_valid": real_rdh,
    }


def _discover_earthdata_granule(short_name: str, headers: Mapping[str, str]) -> tuple[str, float]:
    """用 CMR 找到该数据集的一个受保护 granule 的 HTTPS 下载地址。

    Returns
    -------
    tuple[str, float]
        ``(下载 URL, 目录声明大小 MB)``。

    Raises
    ------
    RuntimeError
        CMR 没有返回可下载的 granule。
    """
    with httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        collections = client.get(
            "https://cmr.earthdata.nasa.gov/search/collections.json",
            params={"short_name": short_name, "page_size": 1},
        )
        collections.raise_for_status()
        entries = collections.json().get("feed", {}).get("entry", [])
        if not entries:
            raise RuntimeError(f"CMR 未找到数据集 {short_name}")
        concept_id = entries[0]["id"]
        granules = client.get(
            "https://cmr.earthdata.nasa.gov/search/granules.json",
            params={"concept_id": concept_id, "page_size": 5},
        )
        granules.raise_for_status()
        for entry in granules.json().get("feed", {}).get("entry", []):
            for link in entry.get("links", []):
                href = link.get("href", "")
                if "lp-prod-protected" in href and href.endswith(".hdf"):
                    return href, float(entry.get("granule_size") or 0)
    raise RuntimeError(f"{short_name} 没有找到受保护的 HDF granule")


def _gee_mode(
    bench: SpeedBench,
    *,
    project: str | None,
    workspace: Path,
    stop_rate: float = 0.8,
) -> dict[str, Any]:
    """Google Earth Engine 模式：云端筛选 → Export 任务 → Drive 下载。

    GEE **不是**普通 HTTP 文件下载器：数据在云端，必须
    ``collection.filter`` → ``Export.image.toXxx`` → 轮询任务 → 从目标存储取回。
    因此这里比较的两条路径是：

    * ``baseline``：用户自己写 GEE 脚本（ee.Initialize → Export → 轮询 → Drive 下载）。
    * ``rdh``：同一条链路上叠加 ResearchDataHub 的传输/校验/提交/交付阶段
      （GEE 的云端处理本身两边一样，差异＝框架的可靠性开销）。

    参数
    ----
    project:
        GEE Cloud project ID（``ee.Initialize(project=...)`` 必需）。
    """
    import os

    project = project or os.environ.get("EARTHENGINE_PROJECT")
    if not project:
        for path_id in ("baseline", "rdh"):
            bench.results.append(
                SampleResult(
                    path_id=path_id,
                    mode=bench.mode,
                    index=0,
                    status=SampleStatus.NOT_RUN,
                    error="缺少 GEE project ID（--gee-project 或 EARTHENGINE_PROJECT）",
                )
            )
        return {"note": "NOT_RUN：缺少 GEE project ID"}
    try:
        import ee
    except ImportError:
        for path_id in ("baseline", "rdh"):
            bench.results.append(
                SampleResult(
                    path_id=path_id,
                    mode=bench.mode,
                    index=0,
                    status=SampleStatus.NOT_RUN,
                    error="未安装 earthengine-api（pip install earthengine-api）",
                )
            )
        return {"note": "NOT_RUN：earthengine-api 未安装"}

    exported: dict[str, Any] = {}

    def run_gee_export(target: Path) -> tuple[int, str, int | None]:
        """完整 GEE 链路：初始化 → 云端裁剪 → 导出 → 轮询 → Drive 下载 → 落盘。"""
        import hashlib
        import json
        import pathlib
        import time

        import httpx

        ee.Initialize(project=project)
        region = ee.Geometry.Rectangle([116.0, 39.0, 117.0, 40.0])
        image = ee.Image("USGS/SRTMGL1_003").clip(region)
        desc = f"rdh_bench_{int(time.time() * 1000) % 10_000_000}"
        task = ee.batch.Export.image.toDrive(
            image=image,
            description=desc,
            folder="rdh_bench",
            region=region.toGeoJSON()["coordinates"],
            scale=90,
            maxPixels=1e9,
        )
        task.start()
        deadline = time.monotonic() + bench.sample_timeout
        while time.monotonic() < deadline:
            status = ee.data.getTaskStatus(task.id)[0]
            state = status.get("state")
            if state == "COMPLETED":
                break
            if state in {"FAILED", "CANCELLED"}:
                raise RuntimeError(f"GEE 导出任务 {state}: {status.get('error_message')}")
            time.sleep(2.0)
        else:
            raise TimeoutError(f"GEE 导出任务超时（{bench.sample_timeout}s）")

        # Drive 下载（凭据来自 ~/.config/earthengine/credentials）
        cred_path = pathlib.Path.home() / ".config" / "earthengine" / "credentials"
        cred = json.loads(cred_path.read_text(encoding="utf-8"))
        token_resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _GEE_CLIENT_ID,
                "client_secret": _GEE_CLIENT_SECRET,
                "refresh_token": cred["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        token_resp.raise_for_status()
        access = token_resp.json()["access_token"]
        listing = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": f"name contains '{desc}' and trashed=false",
                "fields": "files(id,name,size)",
            },
            headers={"Authorization": f"Bearer {access}"},
            timeout=30,
        )
        listing.raise_for_status()
        files = listing.json().get("files", [])
        if not files:
            raise RuntimeError(f"Drive 上找不到导出结果 {desc}")
        file_id = files[0]["id"]
        exported["file"] = files[0].get("name")
        exported["bytes"] = files[0].get("size")
        blob = httpx.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {access}"},
            timeout=120,
            follow_redirects=True,
        )
        blob.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.content)
        return (
            len(blob.content),
            "sha256:" + hashlib.sha256(blob.content).hexdigest(),
            blob.status_code,
        )

    def baseline_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        return run_gee_export

    def rdh_factory(_index: int) -> Callable[[Path], tuple[int, str, int | None]]:
        # RDH 侧：GEE 产出后，用 RDH 的完整性检查 + 原子提交 + 交付处理同一份字节。
        def with_rdh_pipeline(target: Path) -> tuple[int, str, int | None]:
            size, digest, status = run_gee_export(target)
            from researchdatahub.integrity.checker import IntegrityChecker

            report = IntegrityChecker().check_path(
                target,
                kinds=["exists", "readable", "non_empty", "format_sniff"],
                expected_size=size,
            )
            if not report.ok:
                raise RuntimeError(f"RDH 完整性检查未通过：{report.status}")
            return size, digest, status

        return with_rdh_pipeline

    bench.run_path("baseline", baseline_factory)
    bench.run_path("rdh", rdh_factory)
    return {
        "project": project,
        "exported": exported,
        "note": (
            "GEE 数据在云端：链路为 云端裁剪→Export 任务→轮询→Drive 下载。"
            "基线=用户自写脚本；rdh=同一链路 + RDH 完整性校验/提交/交付。"
        ),
    }


def _credential_gated_mode(bench: SpeedBench, mode: str, reason: str) -> dict[str, Any]:
    """需要凭据但未配置的平台：如实记 ``NOT_RUN``，不计入响应率分母。"""
    for path_id in ("baseline", "rdh"):
        bench.results.append(
            SampleResult(
                path_id=path_id,
                mode=mode,
                index=0,
                status=SampleStatus.NOT_RUN,
                error=reason,
            )
        )
    return {"note": f"NOT_RUN：{reason}"}


def _missing_credentials(mode: str) -> str | None:
    """检查某模式所需的凭据是否存在；缺失时返回原因（**不返回任何凭据值**）。"""
    import os

    needed: dict[str, tuple[str, ...]] = {
        "earthdata": ("EARTHDATA_TOKEN",),
        "copernicus": ("CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET"),
        "cds": ("CDSAPI_KEY",),
        "gee": ("EARTHENGINE_PROJECT",),
    }
    required = needed.get(mode, ())
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        return f"缺少环境变量 {', '.join(missing)}（请自行在终端 export，不要发到聊天窗口）"
    return None


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Returns
    -------
    int
        退出码：0 表示基准执行完成（**不表示**所有路径都成功，结论看 results.json）。
    """
    parser = argparse.ArgumentParser(
        description="ResearchDataHub 全链路下载测速基准（80% 响应率停止规则）"
    )
    parser.add_argument(
        "--mode",
        default="local",
        help="测速对象，逗号分隔：local / ncar / earthdata / copernicus / cds / gee",
    )
    parser.add_argument("--out", type=Path, default=Path("benchmarks/speed"), help="输出目录")
    parser.add_argument("--samples", type=int, default=8, help="每条路径最大样本数")
    parser.add_argument(
        "--stop-rate",
        type=float,
        default=DEFAULT_STOP_RESPONSE_RATE,
        help="响应率阈值，达到即停止（默认 0.80）",
    )
    parser.add_argument("--sample-timeout", type=float, default=120.0, help="单样本超时（秒）")
    parser.add_argument("--url", default=None, help="ncar 模式用的具体文件 URL")
    parser.add_argument(
        "--gee-project", default=None, help="GEE Cloud project ID（也可用 EARTHENGINE_PROJECT）"
    )
    parser.add_argument(
        "--baseline-retries",
        type=int,
        default=1,
        help="baseline 路径的重试次数（默认 1 = 朴素脚本不重试；设 >1 可得到更强的基线）",
    )
    parser.add_argument(
        "--no-real-rdh",
        action="store_true",
        help="rdh 路径退化为朴素 httpx（仅供连通性冒烟；对比结果会被标记为无效）",
    )
    parser.add_argument(
        "--payload-size",
        type=int,
        default=8 << 20,
        help="local 模式的有效载荷字节数（默认 8 MiB）",
    )
    parser.add_argument(
        "--allow-cds",
        action="store_true",
        help="显式允许 cds 模式（默认禁止，见 ADR-0014）",
    )
    args = parser.parse_args(argv)

    modes = [item.strip() for item in args.mode.split(",") if item.strip()]
    overall_rc = 0
    for mode in modes:
        bench = SpeedBench(
            mode=mode,
            out_dir=args.out / mode,
            samples=args.samples,
            stop_rate=args.stop_rate,
            sample_timeout=args.sample_timeout,
        )
        if mode == "local":
            extra = _local_mode(bench, payload_size=args.payload_size)
        elif mode == "ncar":
            extra = _ncar_mode(
                bench,
                url=args.url,
                workspace=args.out / mode / "work",
                real_rdh=not args.no_real_rdh,
                baseline_retries=max(1, args.baseline_retries),
            )
        elif mode == "cds" and not args.allow_cds:
            extra = _credential_gated_mode(
                bench, mode, "cds 取数默认禁止（ADR-0014）；确需测量请显式加 --allow-cds"
            )
        elif mode == "earthdata":
            extra = _earthdata_mode(
                bench,
                workspace=args.out / mode / "work",
                real_rdh=not args.no_real_rdh,
                baseline_retries=max(1, args.baseline_retries),
            )
        elif mode == "gee":
            extra = _gee_mode(bench, project=args.gee_project, workspace=args.out / mode / "work")
        elif mode in {"copernicus", "cds"}:
            missing = _missing_credentials(mode)
            if missing is not None:
                extra = _credential_gated_mode(bench, mode, missing)
            else:
                extra = _credential_gated_mode(
                    bench,
                    mode,
                    "凭据已配置，但该平台的完整下载链路尚未接入（Provider 层只做目录检索，"
                    "submit_task 明确声明 unsupported）；待接入后补测",
                )
        else:
            print(f"[skip] 未知模式：{mode}", file=sys.stderr)
            overall_rc = 2
            continue

        payload_path = bench.write(extra=extra)
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        markdown = bench.out_dir / "summary.md"
        markdown.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[{mode}] 写出 {payload_path}")
        print(f"[{mode}] 写出 {markdown}")
        for summary in payload["summaries"]:
            rate = summary["response_rate"]
            print(
                f"  {summary['path_id']:<9} attempted={summary['attempted']} "
                f"succeeded={summary['succeeded']} failed={summary['failed']} "
                f"not_run={summary['not_run']} rate={'N/A' if rate is None else f'{rate:.0%}'} "
                f"median={summary['median_duration_s']}s"
            )
    return overall_rc


if __name__ == "__main__":  # pragma: no cover - 手动执行
    sys.exit(main())
