"""ResearchDataHub — 面向气候科学与地球科学的科研数据采集基础设施。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from researchdatahub.core.models import MODELS_VERSION

__all__ = ["MODELS_VERSION", "__version__"]

try:
    __version__ = version("researchdatahub")
except PackageNotFoundError:  # 未安装（例如直接 PYTHONPATH=src 运行）
    __version__ = "1.1.0"
