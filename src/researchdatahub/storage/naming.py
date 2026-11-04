r"""存储路径与文件名的安全规则。

为什么需要本模块
----------------
存储层是唯一把**远端提供的字符串**变成**本地路径**的地方。一个小失误就会把
``../../etc/passwd`` 或绝对路径写成真实文件。因此规则集中在这里，并且每条规则都必须
有对应测试（``tests/unit/test_storage/test_path_safety.py``），而不是散落在调用点。

标识符型路径片段（``task_id`` / ``dataset_id``）
------------------------------------------------
* 必须是 ``^[a-z0-9][a-z0-9._-]{0,127}$``（比数据契约的 ``Identifier`` 略宽长度，
  但同样小写、不以 ``.`` 开头），因此 ``.`` / ``..`` / ``/`` / ``\\`` 天然不可能出现；
* 不满足即抛 :class:`~researchdatahub.core.errors.StorageError`，**不做静默替换**——
  任务 ID 被悄悄改名会让日志与数据库对不上号。

科学文件名（``file_name``）
---------------------------
显式清洗规则（按顺序执行）：

1. Unicode NFC 规范化（避免 macOS NFD/NFC 造成同名不同串）；
2. 去掉首尾空白；
3. 非空、不是 ``.`` / ``..``、不以 ``.`` 开头（避免隐藏文件与 ``.commit.json`` 混淆）；
4. 长度 1–200 字符（留出目录与后缀空间，低于常见文件系统 255 字节限制）；
5. 拒绝绝对路径、路径分隔符 ``/`` ``\\``、控制字符（``ord < 0x20`` 或 ``0x7f``）、
   ``:``（macOS 资源叉/Windows 盘符）、任何**非字母数字且不在** ``" ._-+,()"``
   白名单内的字符（例如引号、分号、``$``、Unicode 方向覆盖符 U+202E）；
6. 拒绝以 ``.commit.json`` 结尾的名字（与提交日志侧车文件重名）；
7. 拒绝以内部保留前缀 ``.rdh-partial-`` / ``.rdh-journal-`` 开头。

不满足规则时抛 ``StorageError``，错误信息说明**违反了哪条规则**，并把原值放进
``detail``，便于调用方修正来源数据而不是猜测。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Final

from researchdatahub.core.errors import StorageError

__all__ = [
    "MAX_FILE_NAME_LENGTH",
    "ensure_within",
    "sanitize_file_name",
    "sha256_file",
    "validate_component",
]

MAX_FILE_NAME_LENGTH: Final[int] = 200
"""文件名最大字符数（NFC 之后）。"""

MAX_COMPONENT_LENGTH: Final[int] = 128
"""路径片段（task_id / dataset_id）最大字符数。"""

_ID_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FILE_NAME_ALLOWED_EXTRA = frozenset(" ._-+,()")
_RESERVED_NAME_SUFFIXES: Final[tuple[str, ...]] = (".commit.json",)
_RESERVED_NAME_PREFIXES: Final[tuple[str, ...]] = (".rdh-partial-", ".rdh-journal-")
_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024

#: 1 MiB 分块——哈希与复制都用流式，8GB 内存机器上不得整文件入内存。
HASH_CHUNK_SIZE: Final[int] = _HASH_CHUNK_SIZE


def _reject(value: str, *, field: str, rule: str) -> StorageError:
    """构造带上下文的 ``StorageError``（不直接 raise，便于调用点统一 from 链）。"""
    return StorageError(
        f"unsafe {field} {value!r}: {rule}",
        detail={"field": field, "value": value, "rule": rule},
    )


def validate_component(value: str, *, field: str) -> str:
    """校验标识符型路径片段（``task_id`` / ``dataset_id`` / 归档片段）。

    Parameters
    ----------
    value:
        待校验字符串。
    field:
        字段名，只用于错误信息与 ``detail``。

    Returns
    -------
    str
        原值（已通过校验，未做任何改写）。

    Raises
    ------
    StorageError
        为空、超长、含路径分隔符/控制字符或不符合小写标识符规则。
    """
    if not isinstance(value, str):  # pragma: no cover - 类型系统已拦截，防御性检查
        raise StorageError(f"{field} must be a string", detail={"field": field})
    if value == "":
        raise _reject(value, field=field, rule="must not be empty")
    if len(value) > MAX_COMPONENT_LENGTH:
        raise _reject(value, field=field, rule=f"longer than {MAX_COMPONENT_LENGTH} characters")
    if not _ID_COMPONENT.fullmatch(value):
        raise _reject(
            value,
            field=field,
            rule=(
                "must match ^[a-z0-9][a-z0-9._-]{0,127}$ "
                "(lowercase; no path separators, spaces or control characters)"
            ),
        )
    return value


def sanitize_file_name(raw: str) -> str:
    """按模块 docstring 的七条规则规范化科学文件名。

    Parameters
    ----------
    raw:
        来自数据源或用户的名字。

    Returns
    -------
    str
        NFC 规范化并去掉首尾空白后的名字。合法名字原样返回（幂等）。

    Raises
    ------
    StorageError
        违反任一规则。错误里包含 ``detail.rule`` 指明具体原因。
    """
    if not isinstance(raw, str):  # pragma: no cover - 类型系统已拦截，防御性检查
        raise StorageError("file_name must be a string", detail={"field": "file_name"})
    name = unicodedata.normalize("NFC", raw).strip()
    if name == "":
        raise _reject(raw, field="file_name", rule="must not be empty")
    if name in {".", ".."}:
        raise _reject(raw, field="file_name", rule="must not be '.' or '..'")
    if name.startswith("."):
        raise _reject(raw, field="file_name", rule="must not start with '.' (hidden/reserved)")
    if len(name) > MAX_FILE_NAME_LENGTH:
        raise _reject(raw, field="file_name", rule=f"longer than {MAX_FILE_NAME_LENGTH} characters")
    if Path(name).is_absolute() or name.startswith(("/", "\\")):
        raise _reject(raw, field="file_name", rule="must not be an absolute path")
    for char in name:
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            raise _reject(raw, field="file_name", rule="control characters are not allowed")
        if char in {"/", "\\", ":"}:
            raise _reject(raw, field="file_name", rule="path separators are not allowed")
        if not (char.isalnum() or char in _FILE_NAME_ALLOWED_EXTRA):
            raise _reject(
                raw,
                field="file_name",
                rule=(
                    "characters must be alphanumeric or one of ' ._-+,()' "
                    "(quotes, '$', ';', ':' and Unicode format characters are rejected)"
                ),
            )
    lowered = name.lower()
    if any(lowered.endswith(suffix) for suffix in _RESERVED_NAME_SUFFIXES):
        raise _reject(raw, field="file_name", rule="'.commit.json' suffix is reserved for journals")
    if any(lowered.startswith(prefix) for prefix in _RESERVED_NAME_PREFIXES):
        raise _reject(raw, field="file_name", rule="'.rdh-partial-' prefix is reserved")
    return name


def ensure_within(path: Path, base: Path, *, field: str) -> Path:
    """确认 ``path``（已解析）位于 ``base`` 之内，返回它。

    用于防御符号链接逃逸：调用方应先 ``resolve(strict=True)``，再调用本函数。

    Raises
    ------
    StorageError
        路径不在 ``base`` 之下。
    """
    if not path.is_relative_to(base):
        raise StorageError(
            f"{field} escapes managed directory",
            detail={"field": field, "path": str(path), "base": str(base)},
        )
    return path


def sha256_file(path: Path, *, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    """流式计算文件 SHA-256（十六进制小写），内存占用与文件大小无关。

    Raises
    ------
    StorageError
        文件无法读取（权限、I/O 错误）。
    """
    if chunk_size <= 0:
        raise StorageError("chunk_size must be positive", detail={"chunk_size": chunk_size})
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StorageError(
            f"cannot hash file {path}",
            detail={"path": str(path), "reason": str(exc)},
        ) from exc
    return digest.hexdigest()
