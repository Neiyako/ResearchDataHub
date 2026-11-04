"""默认值漂移锁定：YAML 默认值必须与代码内 Field(default=...) 保持一致。

背景（arch 提出）：`configs/defaults.yaml` 与 `config/models.py` 各存一份默认值，
目前一致但无自动断言；改一处会静默分叉。本测试把两者钉在一起。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from researchdatahub.config import load_defaults
from researchdatahub.config.models import AppConfig

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"


def test_yaml_matches_code_defaults_for_concurrency() -> None:
    loaded = load_defaults(DEFAULTS)
    code_default = AppConfig().defaults.concurrency
    yaml_value = loaded.defaults.concurrency
    assert yaml_value.workers == code_default.workers, "workers 默认值分叉"
    assert yaml_value.per_source_limit == code_default.per_source_limit, "per_source_limit 分叉"


def test_yaml_matches_code_defaults_for_retry_and_integrity() -> None:
    loaded = load_defaults(DEFAULTS).defaults
    code_default = AppConfig().defaults
    assert loaded.retry.max_attempts == code_default.retry.max_attempts
    assert loaded.retry.backoff_seconds == code_default.retry.backoff_seconds
    assert loaded.integrity.default_checks == code_default.integrity.default_checks
    assert loaded.integrity.stale_timeout_seconds == code_default.integrity.stale_timeout_seconds


def test_stale_timeout_matches_state_machine_constant() -> None:
    """配置里的过期阈值必须与状态机常量一致，否则恢复逻辑与用户预期不符。"""
    from researchdatahub.core.state import DEFAULT_STALE_TIMEOUT_SECONDS

    loaded = load_defaults(DEFAULTS)
    assert loaded.defaults.integrity.stale_timeout_seconds == DEFAULT_STALE_TIMEOUT_SECONDS


def test_storage_root_is_portable() -> None:
    """storage.root 不得是机器相关的绝对路径。"""
    root = load_defaults(DEFAULTS).defaults.storage.root
    assert not root.startswith("/Users/")
    assert not root.startswith("/home/")


@pytest.mark.parametrize("section", ["storage", "concurrency", "retry", "integrity", "logging"])
def test_every_documented_section_exists(section: str) -> None:
    loaded = load_defaults(DEFAULTS)
    assert hasattr(loaded.defaults, section), f"defaults.yaml 应覆盖 {section} 段"
