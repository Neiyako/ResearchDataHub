"""配置解析测试：env 展开、严格校验、错误消息、真实仓库配置文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from researchdatahub.config import (
    load_config,
    load_defaults,
    load_sources,
    validate_config_files,
)
from researchdatahub.core.errors import ConfigError

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"
SOURCES = REPO / "configs" / "sources.example.yaml"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL_DEFAULTS = """
defaults:
  storage:
    root: "~/.rdh"
"""

MINIMAL_SOURCES = """
sources:
  demo:
    provider: Demo Provider
    display_name: Demo Source
    adapter: demo
    transports:
      - route: https
        adapter: https
    datasets: [demo-dataset]
    credentials:
      key: env:DEMO_TOKEN
"""


def test_repository_defaults_load() -> None:
    config = load_defaults(DEFAULTS)
    assert config is not None
    assert config.defaults.storage.root == "~/.rdh"


def test_repository_configs_have_no_absolute_user_path() -> None:
    """禁止写死用户的本地绝对路径（任务书 §6）：只检查生效的键值，不检查注释里的说明文字。"""
    import yaml

    for path in (DEFAULTS, SOURCES):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))

        def walk(node: object, trail: tuple[str, ...] = ()) -> list[tuple[str, object]]:
            if isinstance(node, dict):
                return [item for k, v in node.items() for item in walk(v, (*trail, str(k)))]
            if isinstance(node, list):
                return [item for i, v in enumerate(node) for item in walk(v, (*trail, str(i)))]
            return [(".".join(trail), node)]

        for key, value in walk(document):
            if isinstance(value, str):
                assert not value.startswith("/Users/"), f"{path.name}: {key} 写死了用户路径"
                assert not value.startswith("/home/"), f"{path.name}: {key} 写死了用户路径"


def test_repository_sources_require_cds_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """示例配置的 CDS 密钥必须走环境变量：未设置时明确报错点名该变量。"""
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    result = validate_config_files(DEFAULTS, SOURCES)
    sources_result = result.files[1]
    assert sources_result.ok is False
    assert "CDSAPI_KEY" in sources_result.env_vars
    assert any("CDSAPI_KEY" in error for error in sources_result.errors)


def test_repository_sources_load_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDSAPI_KEY", "test-token-not-real")
    result = validate_config_files(DEFAULTS, SOURCES)
    assert result.ok is True, [f.errors for f in result.files]
    sources = load_sources(SOURCES)
    assert {"ncar", "cds"} <= set(sources.sources)


def test_env_reference_missing_raises_config_error_naming_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    path = write(tmp_path, "sources.yaml", MINIMAL_SOURCES)
    with pytest.raises(ConfigError) as excinfo:
        load_sources(path)
    message = str(excinfo.value)
    assert "DEMO_TOKEN" in message
    assert excinfo.value.code == "RDH_CONFIG"


def test_env_reference_expands_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_TOKEN", "s3cr3t-value")
    path = write(tmp_path, "sources.yaml", MINIMAL_SOURCES)
    sources = load_sources(path)
    credential = sources.sources["demo"].credentials["key"]
    assert credential == "s3cr3t-value"
    assert "env:" not in credential


def test_validate_reports_missing_env_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate_config_files 的契约：从不抛异常，把失败折算成结构化错误。"""
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    sources_path = write(tmp_path, "sources.yaml", MINIMAL_SOURCES)
    defaults_path = write(tmp_path, "defaults.yaml", MINIMAL_DEFAULTS)
    result = validate_config_files(defaults_path, sources_path)
    assert result.ok is False
    assert [f.kind.value for f in result.files] == ["defaults", "sources"]
    assert result.files[0].ok is True
    assert result.files[1].ok is False
    assert "DEMO_TOKEN" in " ".join(result.files[1].errors)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "defaults.yaml",
        """
defaults:
  storage:
    root: "~/.rdh"
  totally_unknown_section: true
""",
    )
    with pytest.raises(ConfigError, match="totally_unknown_section"):
        load_defaults(path)


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    """缺必填字段必须失败，且错误信息点名缺失字段。"""
    path = write(
        tmp_path,
        "sources.yaml",
        """
sources:
  incomplete:
    provider: Demo Provider
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_sources(path)
    message = str(excinfo.value)
    assert "display_name" in message or "adapter" in message


def test_invalid_yaml_syntax_reports_location(tmp_path: Path) -> None:
    path = write(tmp_path, "defaults.yaml", "defaults:\n  storage:\n   root: [unclosed\n")
    with pytest.raises(ConfigError) as excinfo:
        load_defaults(path)
    assert "defaults.yaml" in str(excinfo.value)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_defaults(tmp_path / "does-not-exist.yaml")


def test_load_config_is_defaults_alias(tmp_path: Path) -> None:
    """``load_config`` 只是 ``load_defaults`` 的别名（文档与 docstring 如此声明）。"""
    defaults_path = write(tmp_path, "defaults.yaml", MINIMAL_DEFAULTS)
    assert load_config(defaults_path) == load_defaults(defaults_path)


def test_load_config_refuses_sources_file_with_clear_message(tmp_path: Path) -> None:
    """把 sources 文件当 defaults 加载时必须明确报错类型不匹配，而不是静默半成功。"""
    sources_path = write(tmp_path, "sources.yaml", MINIMAL_SOURCES)
    with pytest.raises(ConfigError) as excinfo:
        load_config(sources_path)
    message = str(excinfo.value)
    assert "kind_mismatch" in message or "expected a 'defaults' file" in message


def test_validate_reports_bad_yaml_as_error_not_exception(tmp_path: Path) -> None:
    bad = write(tmp_path, "bad.yaml", "defaults: [\n")
    good = write(tmp_path, "defaults-good.yaml", MINIMAL_DEFAULTS)
    result = validate_config_files(bad, good)
    assert result.ok is False
    assert result.files[0].ok is False


def test_no_real_secret_committed() -> None:
    """仓库内的示例配置不得包含真实密钥形态的字符串。"""
    for path in [SOURCES, DEFAULTS]:
        text = path.read_text(encoding="utf-8")
        for marker in ("sk-", "cdsapi-key=", "Authorization: Bearer"):
            assert marker not in text, f"{path.name} 疑似包含真实密钥片段: {marker}"
