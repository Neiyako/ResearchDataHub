"""``python -m researchdatahub.network.workers.agent`` 的入口。

命令行参数与行为见 :func:`researchdatahub.network.workers.agent.main`；本文件只做转接，
保持"模块可 ``-m`` 运行"这一部署约定（无需安装 console script）。
"""

from __future__ import annotations

from researchdatahub.network.workers.agent import main

if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
