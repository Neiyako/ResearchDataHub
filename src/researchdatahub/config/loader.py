"""配置加载：YAML 读取 → ``env:VAR`` 递归展开 → Pydantic 严格校验 → 强类型对象。

核心不变式
----------
1. **不静默**：文件不存在、YAML 语法错、未知字段、缺必填字段、``env:`` 变量缺失，
   全部抛 :class:`~researchdatahub.core.errors.ConfigError`；绝不回退到默认值。
2. **不泄漏**：展开后的 ``env:`` 值只存在于强类型对象中；错误消息与
   :class:`~researchdatahub.config.models.FileValidation` 只记录**变量名**，不记录值。
   这样 ``rdh config validate`` 的输出可以直接贴进 issue。
3. **不残留**：加载结果中不允许留下任何 ``env:`` 前缀——这由
   :func:`_assert_no_env_refs` 在模型校验之后再做一次全树扫描兜底。
4. **依赖方向**：``config`` 依赖 ``core``，反向禁止。

``env:`` 语法
-------------
* ``"env:CDSAPI_KEY"`` —— **整串即引用**，展开为环境变量值；缺失时报错并点名变量。
* ``"${RDH_HOME}/state"`` —— **内嵌插值**，可出现在任意位置；同样递归处理。
* 两者可嵌套在 dict / list 中任意深度；键名**不**参与展开（避免变量名意外展开）。
* 空字符串值 ``"env:"`` 视为语法错误（多半是写漏了变量名）。

注意：展开后的值如果是 YAML 特殊字符（如 ``"*"``），不会重新解析成 YAML 结构——
展开发生在解析之后，类型保持为 ``str``。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import NoReturn, cast

import yaml  # PyYAML 官方不提供 stub；策略见 pyproject [tool.mypy] disable_error_code
from pydantic import ValidationError

from researchdatahub.config.models import (
    AppConfig,
    ConfigFileKind,
    ConfigKind,
    FileValidation,
    SourcesConfig,
    ValidationResult,
)
from researchdatahub.core.errors import ConfigError

__all__ = [
    "EnvResolver",
    "detect_config_kind",
    "load_config",
    "load_defaults",
    "load_raw_config",
    "load_sources",
    "resolve_env_refs",
    "validate_config_files",
]

_ENV_PREFIX = "env:"
_ENV_INLINE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _origin(path: Path | None) -> str:
    """用于错误消息与结果结构的路径字符串：保持用户传入的相对形式，避免暴露绝对路径。

    ``None`` 表示调用方在“尚未绑定文件”的上下文中使用展开器（例如测试直接注入映射）。
    """
    return "<no file>" if path is None else str(path)


def _fail(message: str, *, path: Path | None = None, **detail: object) -> NoReturn:
    """构造并抛出 ConfigError；``detail`` 会进入结构化字段（调用方/日志可用）。

    返回类型是 :data:`typing.NoReturn`，调用方可以把“报错后不可能继续”交给类型检查器，
    从而不必写多余的 ``else``/``return``。
    """
    raise ConfigError(message, detail=detail or None)


def _read_yaml_document(path: Path) -> object:
    """读取并解析单个 YAML 文档；任何 I/O 或语法问题都转成 ConfigError。

    语法错误的错误消息**必须**带文件路径与行号——否则用户无法定位。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _fail(f"config file not found: {_origin(path)}", path=path, reason="not_found")
    except IsADirectoryError:
        _fail(f"config path is a directory, not a file: {_origin(path)}", path=path)
    except UnicodeDecodeError as exc:
        _fail(f"config file is not valid UTF-8: {_origin(path)}: {exc}", path=path)
    except OSError as exc:  # 权限、IO 错误
        _fail(f"cannot read config file {_origin(path)}: {exc}", path=path, errno=exc.errno)

    try:
        # PyYAML 未随包发布类型标注；这里显式标注为 object，再按需收窄。
        document: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = ""
        if mark is not None:
            line = mark.line + 1  # PyYAML 行号从 0 开始
            column = mark.column + 1
            where = f" at line {line}, column {column}"
        problem = getattr(exc, "problem", None) or str(exc)
        _fail(
            f"invalid YAML in {_origin(path)}{where}: {problem}",
            path=path,
            line=None if mark is None else mark.line + 1,
            column=None if mark is None else mark.column + 1,
            reason="yaml_syntax",
        )
    return document


def _iter_strings(node: object) -> Iterator[str]:
    """深度优先遍历，产出所有字符串叶子（键不含在内）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in cast("Mapping[object, object]", node).values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            yield from _iter_strings(item)


class EnvResolver:
    """``env:`` 引用的展开器，封装「变量从哪来」以便测试注入不受真实环境影响的映射。

    Parameters
    ----------
    environ:
        变量来源。默认 ``os.environ``；测试应传显式 dict，避免依赖机器环境。
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        #: 本次展开中实际被使用的变量名（排序去重），供 ValidationResult 报告。
        self.used: set[str] = set()

    # -- 单值展开 -------------------------------------------------------- #
    def expand_string(self, value: str, *, path: Path | None, location: str) -> str:
        """展开一个字符串：整串 ``env:VAR`` 引用 + 内嵌 ``${VAR}`` 插值。"""
        if value.startswith(_ENV_PREFIX):
            name = value[len(_ENV_PREFIX) :].strip()
            if not name:
                _fail(
                    f"empty environment variable reference at {location} in {_origin(path)}: "
                    f"'{_ENV_PREFIX}' must be followed by a variable name",
                    path=path,
                    location=location,
                )
            if not name.isidentifier():
                _fail(
                    f"invalid environment variable name {name!r} at {location} in {_origin(path)}",
                    path=path,
                    location=location,
                )
            return self._require(name, path=path, location=location)

        if _ENV_INLINE.search(value):
            return _ENV_INLINE.sub(
                lambda match: self._require(match.group(1), path=path, location=location),
                value,
            )
        return value

    def _require(self, name: str, *, path: Path | None, location: str) -> str:
        if name not in self._environ:
            # 消息必须含变量名：CLI 与测试都依赖这一点定位缺失的凭证。
            _fail(
                f"environment variable '{name}' is not set but is referenced at "
                f"{location} in {_origin(path)}",
                path=path,
                variable=name,
                location=location,
                reason="missing_env",
            )
        raw = self._environ[name]
        if raw == "":
            _fail(
                f"environment variable '{name}' is empty but is referenced at "
                f"{location} in {_origin(path)}",
                path=path,
                variable=name,
                location=location,
                reason="empty_env",
            )
        self.used.add(name)
        return raw

    # -- 递归展开 -------------------------------------------------------- #
    def expand(self, node: object, *, path: Path | None, location: str = "$") -> object:
        """递归展开 dict / list / str；其它标量原样返回。``location`` 是 JSONPath 风格的位置。"""
        if isinstance(node, str):
            return self.expand_string(node, path=path, location=location)
        if isinstance(node, Mapping):
            result: dict[str, object] = {}
            for raw_key, raw_value in cast("Mapping[object, object]", node).items():
                if not isinstance(raw_key, str):
                    _fail(
                        f"config keys must be strings, got {type(raw_key).__name__} at {location} "
                        f"in {_origin(path)}",
                        path=path,
                        location=location,
                    )
                # 键不展开：防止变量名被误当成键内容改写结构。
                result[raw_key] = self.expand(
                    raw_value, path=path, location=f"{location}.{raw_key}"
                )
            return result
        if isinstance(node, list):
            return [
                self.expand(item, path=path, location=f"{location}[{index}]")
                for index, item in enumerate(cast("list[object]", node))
            ]
        return node


def _default_resolver(environ: Mapping[str, str] | None) -> EnvResolver:
    return EnvResolver(environ)


def resolve_env_refs(
    node: object, *, environ: Mapping[str, str] | None = None, path: Path | None = None
) -> object:
    """便捷函数：对已解析的 YAML 树递归展开 ``env:`` 引用（不写文件、不做模型校验）。

    Raises
    ------
    ConfigError
        引用的环境变量缺失或为空。
    """
    return _default_resolver(environ).expand(node, path=path)


def _assert_no_env_refs(node: object, *, path: Path | None) -> None:
    """兜底：模型校验后结果中若仍有 ``env:`` 前缀，说明某处绕过了展开，必须报错。"""
    for text in _iter_strings(node):
        if text.startswith(_ENV_PREFIX):
            _fail(
                f"unexpanded environment reference {text!r} survived loading of {_origin(path)}",
                path=path,
                reason="unexpanded_env",
            )


# --------------------------------------------------------------------------- #
# 文档结构
# --------------------------------------------------------------------------- #
def _check_root_mapping(document: object, *, path: Path) -> dict[str, object]:
    """顶层必须是映射（YAML 空文件得到 None，同样视为错误）。"""
    if document is None:
        _fail(
            f"config file is empty: {_origin(path)}",
            path=path,
            reason="empty_file",
        )
    if not isinstance(document, Mapping):
        _fail(
            f"top level of {_origin(path)} must be a mapping with a "
            f"'{ConfigKind.DEFAULTS.top_level_key}:' key, got {type(document).__name__}",
            path=path,
            reason="bad_root",
        )
    return {str(key): value for key, value in cast("Mapping[object, object]", document).items()}


def _require_section(
    document: Mapping[str, object], kind: ConfigKind, *, path: Path
) -> dict[str, object]:
    """取出顶层段（``defaults:`` / ``sources:``），并拒绝多余顶层键。"""
    key = kind.top_level_key
    unexpected = sorted(set(document) - {key})
    if unexpected:
        _fail(
            f"unexpected top-level key(s) in {_origin(path)}: {', '.join(unexpected)}; "
            f"a {kind.value} file must contain exactly one top-level '{key}:' section",
            path=path,
            unexpected=unexpected,
            kind=kind.value,
        )
    if key not in document:
        _fail(
            f"missing top-level '{key}:' section in {_origin(path)}",
            path=path,
            kind=kind.value,
            reason="missing_section",
        )
    section = document[key]
    if not isinstance(section, Mapping):
        _fail(
            f"top-level '{key}:' in {_origin(path)} must be a mapping, "
            f"got {type(section).__name__}",
            path=path,
            kind=kind.value,
            reason="bad_section",
        )
    return {str(k): v for k, v in cast("Mapping[object, object]", section).items()}


def detect_config_kind(document: Mapping[str, object], *, path: Path) -> ConfigKind:
    """根据顶层键推断文件类型；两种键都不存在时报错。

    只有恰好包含 ``defaults`` 或 ``sources`` 之一时才成功——同时包含视为错误，
    因为「一个文件只描述一类配置」是本项目的约定（错误定位更简单）。
    """
    has_defaults = ConfigKind.DEFAULTS.top_level_key in document
    has_sources = ConfigKind.SOURCES.top_level_key in document
    if has_defaults and has_sources:
        _fail(
            f"{_origin(path)} contains both 'defaults:' and 'sources:' sections; "
            "split them into two files",
            path=path,
            reason="ambiguous_kind",
        )
    if has_defaults:
        return ConfigKind.DEFAULTS
    if has_sources:
        return ConfigKind.SOURCES
    if not document:
        _fail(f"config file is empty: {_origin(path)}", path=path, reason="empty_file")
    _fail(
        f"{_origin(path)} has neither a 'defaults:' nor a 'sources:' section "
        f"(found keys: {', '.join(sorted(document)) or '<none>'})",
        path=path,
        reason="missing_section",
    )


# --------------------------------------------------------------------------- #
# 模型校验错误格式化
# --------------------------------------------------------------------------- #
def _format_validation_error(exc: ValidationError, *, path: Path, kind: ConfigKind) -> None:
    """把 pydantic 的字段级错误压成可读消息，并包装为 ConfigError（detail 保留字段路径）。"""
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or kind.top_level_key
        problems.append(f"{location}: {error['msg']}")
    joined = "; ".join(problems)
    _fail(
        f"invalid {kind.value} config {_origin(path)}: {joined}",
        path=path,
        kind=kind.value,
        errors=problems,
        reason="validation",
    )


def _validate_app(
    model_cls: type[AppConfig], payload: dict[str, object], *, path: Path
) -> AppConfig:
    """校验 defaults 配置；pydantic 校验错误统一包装为 ConfigError。"""
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        _format_validation_error(exc, path=path, kind=ConfigKind.DEFAULTS)
        raise  # 不可达：_format_validation_error 只抛 ConfigError；保留给类型检查器


def _validate_sources(
    model_cls: type[SourcesConfig], payload: dict[str, object], *, path: Path
) -> SourcesConfig:
    """校验 sources 配置；pydantic 校验错误统一包装为 ConfigError。"""
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        _format_validation_error(exc, path=path, kind=ConfigKind.SOURCES)
        raise  # 不可达：同上


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #
def load_raw_config(path: str | Path, *, kind: ConfigKind) -> dict[str, object]:
    """读取文件并返回**已展开、已裁剪**的配置段（不含顶层键、无 env 引用、未经模型校验）。

    供需要「先看原始配置再决定 schema」的调用方使用；绝大多数代码应使用
    :func:`load_defaults` / :func:`load_sources`。

    Parameters
    ----------
    path:
        YAML 文件路径。
    kind:
        期望的文件类型；若文件顶层是另一种类型，抛出 ConfigError（防止拿错文件）。

    Raises
    ------
    ConfigError
        文件缺失、YAML 非法、顶层结构不符、``env:`` 变量缺失。
    """
    file_path = Path(path)
    document = _check_root_mapping(_read_yaml_document(file_path), path=file_path)
    actual = detect_config_kind(document, path=file_path)
    if actual is not kind:
        _fail(
            f"{_origin(file_path)} looks like a '{actual.value}' file but a "
            f"'{kind.value}' file was expected "
            f"(expected a top-level '{kind.top_level_key}:' section)",
            path=file_path,
            expected=kind.value,
            actual=actual.value,
            reason="kind_mismatch",
        )
    section = _require_section(document, kind, path=file_path)
    expanded = _default_resolver(None).expand(section, path=file_path, location=kind.top_level_key)
    if not isinstance(expanded, dict):  # pragma: no cover - _require_section 已保证是映射
        _fail(f"expanded section in {_origin(file_path)} is not a mapping", path=file_path)
    return {str(key): value for key, value in cast("dict[object, object]", expanded).items()}


def _load_app_config(path: str | Path, *, kind: ConfigKind) -> AppConfig:
    file_path = Path(path)
    section = load_raw_config(file_path, kind=kind)
    # 传回展开后的完整文档形状（{"defaults": {...}}），让 AppConfig 校验顶层结构。
    config = _validate_app(AppConfig, {ConfigKind.DEFAULTS.top_level_key: section}, path=file_path)
    _assert_no_env_refs(config.defaults.storage.root, path=file_path)
    return config


def load_config(path: str | Path) -> AppConfig:
    """加载 ``defaults.yaml`` 风格的完整应用配置（返回 :class:`AppConfig`）。

    这是 **load_defaults 的别名**，保留给「按惯例默认就是默认值配置」的调用点；
    需要明确语义时请用 :func:`load_defaults`。

    Raises
    ------
    ConfigError
        任何读取、展开、校验失败。
    """
    return load_defaults(path)


def load_defaults(path: str | Path) -> AppConfig:
    """加载 ``configs/defaults.yaml``，返回强类型 :class:`AppConfig`。

    Raises
    ------
    ConfigError
        任何读取、展开、校验失败；消息中含文件路径与字段级原因。
    """
    return _load_app_config(path, kind=ConfigKind.DEFAULTS)


def load_sources(path: str | Path) -> SourcesConfig:
    """加载 ``configs/sources.yaml``，返回强类型 :class:`SourcesConfig`。

    凭证（``credentials``）在这里完成 ``env:`` 展开：返回对象中的值是真实凭证，
    **禁止**把该对象整体打印或序列化进日志/报告；需要展示时用
    :meth:`SourcesConfig.required_env_vars`（只给键名）。

    Raises
    ------
    ConfigError
        任何读取、展开、校验失败；缺失的环境变量会在消息中点名。
    """
    file_path = Path(path)
    section = load_raw_config(file_path, kind=ConfigKind.SOURCES)
    return _validate_sources(
        SourcesConfig, {ConfigKind.SOURCES.top_level_key: section}, path=file_path
    )


# --------------------------------------------------------------------------- #
# 批量校验（CLI 与测试共用的稳定接口）
# --------------------------------------------------------------------------- #
def _validation_error_messages(exc: ConfigError) -> list[str]:
    """把 ConfigError 转成人类可读的一行/多行消息，供 ValidationResult.errors 使用。"""
    problem_list = exc.detail.get("errors")
    if isinstance(problem_list, list) and problem_list:
        return [str(item) for item in cast("list[object]", problem_list)]
    return [exc.message]


def _env_vars_from_document(path: Path) -> list[str]:
    """文件内引用到的环境变量名（不含值）。解析失败时返回空列表——错误已由别处报告。"""
    try:
        document = _read_yaml_document(path)
    except ConfigError:
        return []
    names: set[str] = set()
    for text in _iter_strings(document):
        if text.startswith(_ENV_PREFIX):
            names.add(text[len(_ENV_PREFIX) :].strip())
        names.update(_ENV_INLINE.findall(text))
    names.discard("")
    return sorted(names)


def _validate_one(path: Path, expected: ConfigKind) -> FileValidation:
    """校验单个文件，返回 :class:`FileValidation`；绝不抛 ConfigError。"""
    env_vars = _env_vars_from_document(path)
    try:
        if expected is ConfigKind.DEFAULTS:
            load_defaults(path)
        else:
            load_sources(path)
    except ConfigError as exc:
        return FileValidation(
            path=_origin(path),
            kind=ConfigFileKind(expected.value),
            ok=False,
            errors=_validation_error_messages(exc),
            env_vars=env_vars,
        )
    return FileValidation(
        path=_origin(path),
        kind=ConfigFileKind(expected.value),
        ok=True,
        errors=[],
        env_vars=env_vars,
    )


def validate_config_files(defaults_path: str | Path, sources_path: str | Path) -> ValidationResult:
    """校验两个配置文件，返回结构化结果；**从不抛异常**（I/O 异常也折算成错误项）。

    Parameters
    ----------
    defaults_path:
        ``defaults.yaml`` 路径。
    sources_path:
        ``sources.yaml`` 路径。

    Returns
    -------
    ValidationResult
        ``ok`` 为两者的合取；``files`` 顺序固定为 ``[defaults, sources]``，
        每项含 ``path`` / ``kind`` / ``ok`` / ``errors`` / ``env_vars``。

    Notes
    -----
    这是 CLI ``rdh config validate`` 与测试共用的**稳定接口**，签名与返回结构不得随意变更。
    """
    results = [
        _validate_one(Path(defaults_path), ConfigKind.DEFAULTS),
        _validate_one(Path(sources_path), ConfigKind.SOURCES),
    ]
    return ValidationResult(ok=all(item.ok for item in results), files=results)
