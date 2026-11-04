"""重试策略与 HTTP 状态分类的单元测试（无网络）。"""

from __future__ import annotations

import pytest

from researchdatahub.config.models import RetryConfig
from researchdatahub.core.errors import PermanentTransportError, RetryableTransportError
from researchdatahub.transfer import (
    HttpStatusClass,
    RetryPolicy,
    classify_http_status,
    condition_for_http_status,
)
from researchdatahub.transfer.retry import DEFAULT_RETRY_CONDITIONS


def test_delay_is_exponential() -> None:
    policy = RetryPolicy(max_attempts=5, backoff_seconds=1.0, backoff_max_seconds=60.0, jitter=0.0)
    assert [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        policy.delay_for(0)


def test_delay_is_capped() -> None:
    policy = RetryPolicy(max_attempts=10, backoff_seconds=4.0, backoff_max_seconds=10.0, jitter=0.0)
    assert policy.delay_for(2) == 8.0
    assert policy.delay_for(3) == 10.0
    assert policy.delay_for(9) == 10.0


def test_jitter_stays_within_bounds_and_cap() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_seconds=10.0, backoff_max_seconds=11.0, jitter=0.5)
    low = policy.delay_for_attempt(1, random_source=lambda: 0.0)
    high = policy.delay_for_attempt(1, random_source=lambda: 1.0)
    assert low == pytest.approx(5.0)
    assert high == pytest.approx(11.0)  # 15 被上限截断
    for value in (0.0, 0.25, 0.5, 0.75, 0.999):
        delay = policy.delay_for_attempt(1, random_source=lambda v=value: v)
        assert 5.0 <= delay <= 11.0


def test_retry_after_extends_delay_but_stays_capped() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_seconds=1.0, backoff_max_seconds=4.0, jitter=0.0)
    assert policy.delay_for_attempt(1, retry_after_seconds=2.5) == pytest.approx(2.5)
    assert policy.delay_for_attempt(1, retry_after_seconds=99.0) == pytest.approx(4.0)


def test_policy_from_config_matches_and_config_validation_holds() -> None:
    config = RetryConfig(
        max_attempts=6,
        backoff_seconds=2.0,
        backoff_max_seconds=30.0,
        jitter=0.1,
        retry_on=["timeout", "http_5xx"],
    )
    policy = RetryPolicy.from_config(config)
    assert policy.max_attempts == 6
    assert policy.backoff_seconds == 2.0
    assert policy.backoff_max_seconds == 30.0
    assert policy.jitter == 0.1
    assert policy.retry_on == frozenset({"timeout", "http_5xx"})
    assert (
        frozenset({"timeout", "connection_reset", "http_5xx", "http_429"})
        == DEFAULT_RETRY_CONDITIONS
    )


def test_invalid_policy_construction_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="backoff_seconds"):
        RetryPolicy(backoff_seconds=0.0)
    with pytest.raises(ValueError, match="backoff_max_seconds"):
        RetryPolicy(backoff_seconds=10.0, backoff_max_seconds=5.0)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=1.5)


def test_http_status_condition_mapping() -> None:
    assert condition_for_http_status(500) == "http_5xx"
    assert condition_for_http_status(503) == "http_5xx"
    assert condition_for_http_status(429) == "http_429"
    assert condition_for_http_status(408) == "timeout"
    assert condition_for_http_status(403) is None


def test_classify_http_status_with_full_whitelist() -> None:
    retry_on = DEFAULT_RETRY_CONDITIONS
    assert classify_http_status(500, retry_on) is HttpStatusClass.RETRYABLE
    assert classify_http_status(599, retry_on) is HttpStatusClass.RETRYABLE
    assert classify_http_status(429, retry_on) is HttpStatusClass.RETRYABLE
    assert classify_http_status(403, retry_on) is HttpStatusClass.PERMANENT
    assert classify_http_status(404, retry_on) is HttpStatusClass.PERMANENT
    with pytest.raises(ValueError, match="not an error status"):
        classify_http_status(204, retry_on)


def test_retry_whitelist_can_disable_a_condition() -> None:
    retry_on = frozenset({"timeout", "http_429"})
    assert classify_http_status(500, retry_on) is HttpStatusClass.PERMANENT
    assert classify_http_status(429, retry_on) is HttpStatusClass.RETRYABLE
    policy = RetryPolicy(max_attempts=3, retry_on=retry_on)
    assert policy.allows_condition("http_5xx", retryable=True) is False
    assert policy.allows_condition("timeout", retryable=True) is True
    assert policy.allows_condition(None, retryable=True) is True
    assert policy.allows_condition("timeout", retryable=False) is False


def test_should_retry_uses_exception_flags_and_condition() -> None:
    policy = RetryPolicy(max_attempts=3, retry_on=["timeout"])
    timeout_error = RetryableTransportError(
        "timeout", detail={"condition": "timeout", "error_code": "timeout_read"}
    )
    server_error = RetryableTransportError(
        "500", detail={"condition": "http_5xx", "error_code": "http_server_error"}
    )
    permanent = PermanentTransportError("404", detail={"error_code": "http_not_found"})
    assert policy.should_retry(timeout_error) is True
    assert policy.should_retry(server_error) is False
    assert policy.should_retry(permanent) is False
    assert policy.should_retry(ConnectionError("reset")) is False  # timeout 白名单不含它
    assert RetryPolicy(max_attempts=3).should_retry(ConnectionError("reset")) is True
    assert policy.should_retry(ValueError("bug")) is False
