"""凭据检查子包：只回答「凭据在不在」，永不返回值本身。

公开入口
--------
* :class:`~researchdatahub.environment.credentials.checker.CredentialChecker`
* :func:`~researchdatahub.environment.credentials.checker.parse_credential_reference`
* :func:`~researchdatahub.environment.credentials.checker.load_credential_references`
"""

from researchdatahub.environment.credentials.checker import (
    CredentialChecker,
    load_credential_references,
    parse_credential_reference,
)

__all__ = [
    "CredentialChecker",
    "load_credential_references",
    "parse_credential_reference",
]
