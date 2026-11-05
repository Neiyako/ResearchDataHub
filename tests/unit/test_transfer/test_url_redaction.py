"""URL 脱敏：防止签名凭据写进日志/事件（安全事故，不是日志噪声）。

真实场景：NASA Earthdata 的 CloudFront 预签名地址、CDS 结果签名 URL、S3 预签名 URL
都会把临时凭据放在查询串里。这些 URL 一旦原样进入 ``transfer_logs``，凭据就持久化到
磁盘，并可能随报告/issue 外传。本文件锁住脱敏行为。
"""

from __future__ import annotations

from researchdatahub.transfer.base import redact_url

SIGNED_URL = (
    "https://d1nklfio7vscoe.cloudfront.net/s3-abc/lp-prod-protected/"
    "MOD11A1.A2000055.h12v10.061.2020043121102.hdf"
    "?backend_params=SECRETBACKEND"
    "&Expires=1793899894"
    "&Signature=VERYSECRETSIGNATURE"
    "&Key-Pair-Id=K1LL9XVWUBCJHB"
)


def test_signature_query_params_are_redacted() -> None:
    redacted = redact_url(SIGNED_URL)
    assert "VERYSECRETSIGNATURE" not in redacted
    assert "SECRETBACKEND" not in redacted
    assert "REDACTED" in redacted


def test_host_and_path_are_preserved() -> None:
    """脱敏只作用于凭据，定位信息（主机/路径）必须保留，否则日志失去诊断价值。"""
    redacted = redact_url(SIGNED_URL)
    assert redacted.startswith("https://d1nklfio7vscoe.cloudfront.net/s3-abc/")
    assert "MOD11A1.A2000055.h12v10.061.2020043121102.hdf" in redacted


def test_long_url_is_truncated_under_event_limit() -> None:
    """``TransferLogEvent.message`` 上限 2048；脱敏后必须远小于它。"""
    long_url = "https://example.org/" + "a" * 5000
    redacted = redact_url(long_url)
    assert len(redacted) <= 512 + len("...<truncated>")
    assert redacted.endswith("...<truncated>")


def test_plain_url_without_query_is_unchanged() -> None:
    plain = "https://data.gdex.ucar.edu/d633000/era5.nc"
    assert redact_url(plain) == plain


def test_non_signature_query_params_survive() -> None:
    """非凭据类查询参数应保留（例如分页/版本），便于排查。"""
    url = "https://example.org/data.nc?version=2&page=3"
    assert "version=2" in redact_url(url)


def test_malformed_url_never_raises() -> None:
    """脱敏函数在日志路径上调用，**绝不能因为畸形 URL 抛异常**（否则会把正常下载搞挂）。"""
    for raw in ("http://[bad", "not a url", "", "://missing-scheme"):
        result = redact_url(raw)
        assert isinstance(result, str)


def test_no_raw_credential_leaks_in_any_case() -> None:
    """穷举常见签名参数名，确认都被处理。"""
    for key in (
        "Signature",
        "X-Amz-Signature",
        "X-Amz-Credential",
        "X-Amz-Security-Token",
        "token",
        "access_token",
        "state",
        "code",
    ):
        url = f"https://example.org/f.nc?{key}=LEAKME"
        assert "LEAKME" not in redact_url(url), key
