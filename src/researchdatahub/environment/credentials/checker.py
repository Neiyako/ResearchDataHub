"""凭据检查：回答「凭据**在不在**」，永不回答「凭据是什么」。

三种引用形式
------------
``env:NAME``
    环境变量存在且非空白即 ``present``。
``file:PATH``
    文件存在且非空即 ``present``；对 ``~/.netrc`` / ``~/.cdsapirc`` 这类「多行键值」文件，
    进一步要求出现了与凭据键相关的标记（例如 ``key`` / ``machine`` 行），避免空模板文件
    被误判为已配置。
``keyring:SERVICE/USERNAME``
    ``keyring`` 库可导入 **且** ``keyring.get_password(service, username)`` 返回非空。

安全边界（硬约束）
------------------
* :meth:`CredentialChecker.check` 的返回值只有 ``present`` 布尔、变量名、文件路径、
  长度等元数据；**任何 secret 内容都不会出现在返回值里**。
* 检查 ``keyring`` 时会读到密码字符串，但它只在函数内部用于 ``bool(...)`` 判断，
  立即丢弃，不写入任何返回值、日志或异常消息。
* 读取文件时只统计「行数 / 是否含标记」，从不回显行内容。
* 所有异常消息经过 :func:`~researchdatahub.environment.models.redact_secrets` 打码，
  防止第三方库把敏感值拼进异常文本后泄漏到报告里。

与 ``configs/sources*.yaml`` 的对齐
-----------------------------------
:func:`load_credential_references` 读取 ``sources.<id>.credentials``，把
``CDSAPI_KEY: env:CDSAPI_KEY`` 这样的映射还原成 :class:`CredentialReference`。
它**只用 YAML 解析，不走 config.loader 的 env 展开**——展开会把真实值带进内存，
与「只检查存在性」的目标相悖。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import yaml

from researchdatahub.environment.models import (
    CREDENTIAL_REF_PREFIXES,
    CheckKind,
    CredentialKind,
    CredentialReference,
    CredentialRequirement,
    RequirementResult,
    RequirementStatus,
    redact_secrets,
)

__all__ = [
    "CredentialChecker",
    "load_credential_references",
    "parse_credential_reference",
]

_LOGGER = logging.getLogger(__name__)

_FILE_MARKERS: Final[Mapping[str, tuple[str, ...]]] = {
    "~/.cdsapirc": ("key",),
    "~/.netrc": ("machine",),
}
"""多行凭据文件的内容标记（小写、子串匹配）。不在表里的文件只判「存在且非空」。"""


def _default_keyring_getter() -> Callable[[str, str], str | None]:
    """用 ``importlib`` 动态取 ``keyring.get_password``（返回的字符串随即被丢弃）。

    为什么不用 ``import keyring``
    ----------------------------
    ``keyring`` 是**可选**依赖，且项目依赖清单里不引入它：
    * 静态 ``import`` 会让 mypy 在未安装时报 ``import-not-found``，
      而项目 mypy 是 strict 且明确不允许行内 ``type: ignore``；
    * 动态 import 让「未安装」变成运行期可报告的状态（``library_available=False``），
      而不是让整个包 import 失败。
    """
    module = importlib.import_module("keyring")
    getter = getattr(module, "get_password", None)
    if not callable(getter):
        raise RuntimeError("keyring module has no get_password()")
    return cast("Callable[[str, str], str | None]", getter)


_MAX_SNIFF_BYTES: Final[int] = 4096
"""只读文件开头这么多字节做标记检查；凭据文件通常 <1KB，避免读大文件。"""


def _expand(path_text: str) -> Path:
    """展开 ``~`` 与 ``${VAR}``；**不**创建任何文件。"""
    expanded = os.path.expandvars(path_text)
    return Path(expanded).expanduser()


_NAME_LIMIT: Final[int] = 160


def _display_name(key: str, reference: CredentialReference) -> str:
    """结果里的展示名：``key@去哪找``。超长时截断引用描述，保留 ``key@`` 前缀不变。"""
    prefix = f"{key}@"
    room = _NAME_LIMIT - len(prefix)
    described = reference.describe()
    if len(described) > room:
        described = described[: max(room - 1, 1)] + "…"
    return prefix + described


def _markers_for(path_text: str, resolved: Path) -> tuple[str, ...]:
    """找出需要检查内容标记的「多行凭据文件」规则。

    匹配顺序：字面路径（``~/.cdsapirc`` / ``~/.netrc``）优先，其次按**文件名**兼容用户
    自选位置的同类文件（``/custom/dir/.netrc``）。只按文件名判断，不读内容；
    不比较 ``Path.home()``，因此测试用 monkeypatch 改 home 也不会影响判定。
    """
    literal = _FILE_MARKERS.get(path_text)
    if literal is not None:
        return literal
    by_name = {".cdsapirc": _FILE_MARKERS["~/.cdsapirc"], ".netrc": _FILE_MARKERS["~/.netrc"]}
    return by_name.get(resolved.name, ())


def parse_credential_reference(text: str) -> CredentialReference:
    """把 ``"env:CDSAPI_KEY"`` / ``"file:~/.netrc"`` / ``"keyring:researchdatahub/user"``
    解析成 :class:`CredentialReference`。

    Parameters
    ----------
    text:
        配置里的引用字符串。

    Returns
    -------
    CredentialReference

    Raises
    ------
    ValueError
        缺少合法前缀，或 ``keyring:`` 缺少 ``service/username`` 形式。

    Examples
    --------
    >>> parse_credential_reference("env:CDSAPI_KEY").name
    'CDSAPI_KEY'
    >>> parse_credential_reference("keyring:researchdatahub/alice").service
    'researchdatahub'
    """
    stripped = text.strip()
    for prefix in CREDENTIAL_REF_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        body = stripped[len(prefix) :].strip()
        if not body:
            raise ValueError(f"empty credential reference body in {stripped!r}")
        if prefix == "env:":
            return CredentialReference(kind=CredentialKind.ENV, name=body)
        if prefix == "file:":
            return CredentialReference(kind=CredentialKind.FILE, name=body)
        service, _, username = body.partition("/")
        if not service or not username:
            raise ValueError(f"keyring reference {stripped!r} must be 'keyring:SERVICE/USERNAME'")
        return CredentialReference(kind=CredentialKind.KEYRING, name=username, service=service)
    raise ValueError(
        f"credential reference {stripped!r} has no recognized prefix "
        f"({', '.join(CREDENTIAL_REF_PREFIXES)}); literal secrets must never be written "
        "into config files"
    )


def load_credential_references(path: str | Path) -> dict[str, dict[str, CredentialReference]]:
    """从 ``configs/sources*.yaml`` 读取 ``sources.<id>.credentials`` 的**引用**（不含值）。

    Parameters
    ----------
    path:
        YAML 文件路径。

    Returns
    -------
    dict
        ``{source_id: {credential_key: CredentialReference}}``。
        非 ``env:`` / ``file:`` / ``keyring:`` 形式的值（例如公开的 ``url`` 字面量）
        会被**跳过**（它是公开端点，不是凭据），而不是报错。

    Raises
    ------
    FileNotFoundError
        文件不存在（调用方通常把「没有 sources.yaml」当作「没有额外要求」）。
    yaml.YAMLError
        YAML 语法错误。

    Notes
    -----
    该函数**不**会展开 ``env:``；它只读结构，因此即使环境变量缺失也能安全调用。
    """
    text = Path(path).read_text(encoding="utf-8")
    document: object = yaml.safe_load(text)
    if not isinstance(document, dict):
        return {}
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return {}
    result: dict[str, dict[str, CredentialReference]] = {}
    for source_id, source in sources.items():
        if not isinstance(source, dict):
            continue
        credentials = source.get("credentials")
        if not isinstance(credentials, dict):
            continue
        parsed: dict[str, CredentialReference] = {}
        for key, value in credentials.items():
            if not isinstance(value, str):
                continue
            try:
                parsed[str(key)] = parse_credential_reference(value)
            except ValueError:
                # 公开字面量（如 url: https://…）不是凭据引用，跳过而不是让整份配置不可读。
                continue
        if parsed:
            result[str(source_id)] = parsed
    return result


class CredentialChecker:
    """检查凭据引用是否存在，**永不返回值本身**。

    Parameters
    ----------
    environ:
        环境变量来源；默认 ``os.environ``。测试应传显式映射。
    keyring_getter:
        ``(service, username) -> str | None``；默认使用 ``keyring.get_password``。
        测试可注入假函数，从而**完全不接触真实钥匙串**。
    keyring_available:
        ``keyring`` 库是否可导入；``None`` 表示运行时用 ``find_spec`` 探测。
    redact_values:
        额外需要打码的值（例如测试里的 sentinel），用于兜底清洗 guidance/错误消息。

    Examples
    --------
    >>> checker = CredentialChecker(environ={"CDSAPI_KEY": "secret"})
    >>> ref = parse_credential_reference("env:CDSAPI_KEY")
    >>> result = checker.check_reference("key", ref, required=True, purpose="认证")
    >>> result.status.value, "secret" in str(result.model_dump())
    ('present', False)
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        keyring_getter: Callable[[str, str], str | None] | None = None,
        keyring_available: bool | None = None,
        redact_values: Sequence[str] = (),
    ) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._keyring_getter = keyring_getter
        self._keyring_available = keyring_available
        self._redact: tuple[str, ...] = tuple(item for item in redact_values if item)

    # -- 单一引用 ---------------------------------------------------------- #
    def check_reference(
        self,
        key: str,
        reference: CredentialReference,
        *,
        required: bool,
        purpose: str = "",
        help_url: str | None = None,
    ) -> RequirementResult:
        """检查一条引用；返回结果里只有「在不在」与元数据。

        Parameters
        ----------
        key:
            逻辑键名（``key`` / ``token`` / ``url`` …），出现在 ``name`` 字段。
        reference:
            去哪找（env / file / keyring）。
        required:
            是否为必需凭据；非必需且缺失时状态是 ``optional``。
        purpose:
            这条凭据干什么用，写进 guidance。
        help_url:
            去哪拿，写进 ``help_url``。
        """
        present, observed, problem = self._probe_reference(reference)
        if present:
            status = RequirementStatus.PRESENT
            guidance = ""
        elif required:
            status = RequirementStatus.MISSING
            guidance = self._missing_guidance(key, reference, purpose, problem)
        else:
            status = RequirementStatus.OPTIONAL
            guidance = (
                f"可选凭据 {key}（{reference.describe()}）未配置"
                + (f"：{problem}" if problem else "")
                + f"；需要时再配置。用途：{purpose}"
            )
        return RequirementResult(
            name=_display_name(key, reference),
            kind=CheckKind.CREDENTIAL,
            status=status,
            required=required,
            guidance=self._redact_text(guidance),
            help_url=help_url,
            observed={**observed, "kind": reference.kind.value},
        )

    def check_credentials(
        self, requirements: Sequence[CredentialRequirement]
    ) -> list[RequirementResult]:
        """批量检查（保持输入顺序）。"""
        return [
            self.check_reference(
                item.key,
                item.reference,
                required=item.required,
                purpose=item.purpose,
                help_url=item.how_to_obtain or None,
            )
            for item in requirements
        ]

    def any_present(self, requirements: Sequence[CredentialRequirement]) -> bool:
        """是否**至少有一项**存在（用于「多来源满足其一」的判定，如 CDS key 在 env 或文件）。

        注意：这不改变逐条报告的语义，只是给 validator 提供聚合判断。
        """
        return any(self._probe_reference(item.reference)[0] for item in requirements)

    # -- 探针实现 ---------------------------------------------------------- #
    def _probe_reference(
        self, reference: CredentialReference
    ) -> tuple[bool, dict[str, str | int | float | bool | None], str]:
        """返回 ``(present, observed, problem)``；``problem`` 是中文原因（不含 secret）。"""
        if reference.kind is CredentialKind.ENV:
            return self._probe_env(reference.name)
        if reference.kind is CredentialKind.FILE:
            return self._probe_file(reference.name)
        return self._probe_keyring(reference)

    def _probe_env(self, name: str) -> tuple[bool, dict[str, str | int | float | bool | None], str]:
        try:
            value = self._environ.get(name)
        except Exception as exc:
            return False, {"variable": name}, self._redact_text(f"读取环境变量失败：{exc}")
        # 用 len(bool) 判断，value 本身不进入返回结构。
        present = value is not None and bool(value.strip())
        observed: dict[str, str | int | float | bool | None] = {
            "variable": name,
            "present": present,
        }
        problem = "" if present else f"环境变量 {name} 未设置或为空"
        return present, observed, problem

    def _probe_file(
        self, path_text: str
    ) -> tuple[bool, dict[str, str | int | float | bool | None], str]:
        path = _expand(path_text)
        observed: dict[str, str | int | float | bool | None] = {
            "path": str(path),
            "present": False,
        }
        try:
            if not path.is_file():
                return False, observed, f"文件不存在：{path}"
            size = path.stat().st_size
        except OSError as exc:
            return False, observed, self._redact_text(f"无法访问 {path}：{exc}")
        observed["size_bytes"] = size
        if size == 0:
            return False, observed, f"文件为空：{path}"
        markers = _markers_for(path_text, path)
        if markers:
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:_MAX_SNIFF_BYTES]
            except OSError as exc:
                return False, observed, self._redact_text(f"无法读取 {path}：{exc}")
            lowered = head.lower()
            if not any(marker in lowered for marker in markers):
                # 只报告「缺少哪个标记」，不回显文件内容。
                return (
                    False,
                    {**observed, "has_marker": False},
                    f"文件 {path} 存在但不含所需字段（{' / '.join(markers)}），可能只是空模板",
                )
            observed["has_marker"] = True
        observed["present"] = True
        return True, observed, ""

    def _probe_keyring(
        self, reference: CredentialReference
    ) -> tuple[bool, dict[str, str | int | float | bool | None], str]:
        service = reference.service or ""
        observed: dict[str, str | int | float | bool | None] = {
            "service": service,
            "username": reference.name,
            "present": False,
        }
        available = self._keyring_is_available()
        if not available:
            observed["library_available"] = False
            return (
                False,
                observed,
                "keyring 库未安装（pip install keyring）或后端不可用",
            )
        observed["library_available"] = True
        getter = self._keyring_getter or _default_keyring_getter()
        try:
            secret = getter(service, reference.name)
        except Exception as exc:
            _LOGGER.debug("keyring lookup failed for service=%s: %s", service, type(exc).__name__)
            return False, observed, self._redact_text(f"钥匙串读取失败：{exc}")
        present = secret is not None and bool(secret.strip())
        # secret 只在这里用于布尔判断，随即丢弃；绝不进入 observed / guidance。
        del secret
        observed["present"] = present
        problem = "" if present else f"钥匙串中找不到 service={service}, user={reference.name}"
        return present, observed, problem

    def _keyring_is_available(self) -> bool:
        if self._keyring_available is not None:
            return self._keyring_available
        if self._keyring_getter is not None:
            return True
        return importlib.util.find_spec("keyring") is not None

    def _missing_guidance(
        self,
        key: str,
        reference: CredentialReference,
        purpose: str,
        problem: str,
    ) -> str:
        """构造「缺什么 + 去哪设 + 怎么验证」的可操作指引（打码后返回）。"""
        if reference.kind is CredentialKind.ENV:
            fix_where = f"export {reference.name}='…'（写进 ~/.zshrc，chmod 600）"
            verify = f'python -c "import os; print({reference.name!r} in os.environ)"'
        elif reference.kind is CredentialKind.FILE:
            fix_where = (
                f"按官方文档创建 {reference.name}（chmod 600），把令牌写进去，"
                "不要写进仓库或聊天窗口"
            )
            verify = f"test -f {reference.name} && echo present"
        else:
            fix_where = (
                f"keyring.set_password({(reference.service or '')!r}, {reference.name!r}, secret)"
            )
            verify = (
                f'python -c "import keyring; print(bool(keyring.get_password('
                f'{(reference.service or "")!r}, {reference.name!r})))"'
            )
        cause = f"（{problem}）" if problem else ""
        return self._redact_text(
            f"缺少凭据 {key}{cause}。用途：{purpose or '认证'}。"
            f"修复：{fix_where}；验证：{verify}。**不要**把令牌粘贴到聊天窗口或 issue。"
        )

    def _redact_text(self, text: str) -> str:
        if not text:
            return text
        if not self._redact:
            return text
        return redact_secrets(text, self._redact)
