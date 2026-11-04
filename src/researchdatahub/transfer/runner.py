"""有限并发：把多个传输任务交给固定大小线程池，且**在提交端就限流**。

为什么不用“先全部 submit 再等”
------------------------------
``ThreadPoolExecutor`` 内部队列无上限：一次性 ``submit`` 一万个任务会立刻创建
一万个 Future + 闭包，在 8GB 设备上是实打实的内存尖峰。这里的做法是：

* 用 ``max_workers`` 个信号量做**在途许可**；
* 拿到许可才 ``submit``，Future 完成后回调释放许可；
* 因此进程内“已提交未完成”的传输数永远 ≤ ``max_workers``，排队发生在调用方的
  循环里（显式、可测），而不是藏在 executor 的无界队列里。

调度器用法
----------
>>> runner = BoundedTransferRunner(http_adapter, max_workers=2)
>>> results = runner.run([TransferJob(asset, dest, task) for ...])
>>> [r.status for r in results]  # 与 jobs 顺序一一对应
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from researchdatahub.core.models import AcquisitionTask, SourceAsset, TransferResult
from researchdatahub.transfer.base import TransportAdapterBase

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "MAX_MAX_WORKERS",
    "BoundedTransferRunner",
    "TransferJob",
]

DEFAULT_MAX_WORKERS = 2
"""默认并发。8GB 设备上 2 路流式下载已能打满多数家用带宽而不会挤爆内存。"""

MAX_MAX_WORKERS = 4
"""硬上限：再多只会增加磁盘寻道与 SQLite 写冲突，不会更快。"""


@dataclass(frozen=True, slots=True)
class TransferJob:
    """一个待执行传输任务（显式携带 cancel_event，便于调度器按任务取消）。"""

    asset: SourceAsset
    destination: Path
    task: AcquisitionTask
    cancel_event: threading.Event | None = None


class BoundedTransferRunner:
    """固定并发、提交端限流的传输执行器。

    Parameters
    ----------
    adapter:
        传输适配器；同一实例可被多个 worker 线程并发使用（ports.py 的契约）。
    max_workers:
        并发传输数，取值 ``1..4``（默认 2）。
    """

    def __init__(
        self,
        adapter: TransportAdapterBase,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        if not 1 <= max_workers <= MAX_MAX_WORKERS:
            raise ValueError(f"max_workers must be within 1..{MAX_MAX_WORKERS}, got {max_workers}")
        self._adapter = adapter
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="rdh-transfer"
        )
        self._slots = threading.BoundedSemaphore(max_workers)
        self._closed = False

    @property
    def max_workers(self) -> int:
        """并发上限。"""
        return self._max_workers

    @property
    def adapter(self) -> TransportAdapterBase:
        """本次运行使用的传输适配器。"""
        return self._adapter

    def run(self, jobs: Sequence[TransferJob]) -> list[TransferResult]:
        """并发执行全部任务，按输入顺序返回结果。

        失败不会中断整批：每个任务的结果是 ``TransferResult``（失败为 ``FAILED``，
        带 ``retryable`` / ``error_code``）；程序缺陷（非传输异常）会从 ``run`` 抛出。
        单个任务失败**不影响**同批其它任务继续执行。

        Raises
        ------
        RuntimeError
            runner 已关闭。
        """
        if self._closed:
            raise RuntimeError("BoundedTransferRunner is closed")
        futures: list[Future[TransferResult]] = []
        for job in jobs:
            self._slots.acquire()
            try:
                future = self._executor.submit(self._run_one, job)
            except RuntimeError:
                self._slots.release()
                raise
            future.add_done_callback(self._release_slot)
            futures.append(future)
        return [future.result() for future in futures]

    def _run_one(self, job: TransferJob) -> TransferResult:
        return self._adapter.transfer_outcome(
            job.asset,
            job.destination,
            task=job.task,
            cancel_event=job.cancel_event,
        )

    def _release_slot(self, _future: Future[TransferResult]) -> None:
        self._slots.release()

    def close(self) -> None:
        """关闭线程池（等待在途任务结束）；幂等。"""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> BoundedTransferRunner:
        """上下文管理器入口。"""
        if self._closed:
            raise RuntimeError("BoundedTransferRunner is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """上下文管理器出口：关闭线程池。"""
        self.close()
