"""server 模块测试：信封分发、未知工具、未就绪上下文、SDK 缺失降级、CLI 入口。

这里**不启动**真实子进程（那是 ``tests/integration/test_mcp_server.py`` 的职责）；
本文件用 ``call_tool`` 直接驱动分发层，覆盖它自己的逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.test_mcp.conftest import REQUEST_DOCUMENT, Harness

from researchdatahub.interfaces.mcp import server
from researchdatahub.interfaces.mcp.errors import ENVELOPE_SCHEMA_VERSION


def test_call_tool_unknown_tool_returns_invalid_arguments(harness: Harness) -> None:
    envelope = server.call_tool(harness.context(), "definitely_not_a_tool", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"
    assert "available_tools" in envelope["error"]["detail"]
    assert envelope["meta"]["tool"] == "definitely_not_a_tool"


def test_call_tool_without_context_returns_not_available() -> None:
    envelope = server.call_tool(None, "search_datasets", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_available"
    assert envelope["error"]["retryable"] is False


def test_call_tool_none_arguments_treated_as_empty(harness: Harness) -> None:
    envelope = server.call_tool(harness.context(), "search_datasets", None)
    assert envelope["ok"] is True
    assert envelope["data"]["datasets"] == []


def test_envelope_meta_has_schema_version_and_duration(harness: Harness) -> None:
    envelope = server.call_tool(harness.context(), "get_provider_capabilities", {})
    assert envelope["meta"]["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert isinstance(envelope["meta"]["duration_ms"], float)
    assert envelope["meta"]["duration_ms"] >= 0


def test_unexpected_handler_exception_is_folded(harness: Harness, monkeypatch) -> None:
    def _boom(context: object, arguments: object) -> dict[str, object]:
        raise ZeroDivisionError("internal detail that must not leak")

    monkeypatch.setitem(server.TOOL_HANDLERS, "search_datasets", _boom)  # type: ignore[arg-type]
    envelope = server.call_tool(harness.context(), "search_datasets", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "internal_error"
    assert "ZeroDivisionError" not in json.dumps(envelope)
    assert "internal detail" not in json.dumps(envelope)


def test_dispatch_returns_json_text(harness: Harness) -> None:
    text = server.dispatch(harness.context(), "get_provider_capabilities", {})
    payload = json.loads(text)
    assert payload["ok"] is True


def test_list_tools_payload_has_18_names(harness: Harness) -> None:
    payload = server.list_tools_payload()
    assert len(payload) == 18
    assert all("inputSchema" in item for item in payload)


def test_create_server_raises_without_sdk(monkeypatch) -> None:
    monkeypatch.setattr(server, "_import_sdk", lambda: None)
    monkeypatch.setattr(server, "_SDK_IMPORT_ERROR", "missing")
    with pytest.raises(RuntimeError, match="missing"):
        server.create_server(None)


def test_main_reports_missing_sdk(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server, "_import_sdk", lambda: None)
    monkeypatch.setattr(server, "_SDK_IMPORT_ERROR", "no mcp module")
    assert server.main(["--root", "/tmp/does-not-exist"]) == 2


def test_main_fails_when_root_has_no_database(tmp_path: Path) -> None:
    assert server.main(["--root", str(tmp_path / "empty"), "--log-level", "CRITICAL"]) == 2


def test_print_tools_writes_json(capsys) -> None:
    assert server.main(["--print-tools"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["count"] == 18
    assert len(payload["tools"]) == 18


def test_parse_args_defaults() -> None:
    args = server._parse_args([])
    assert args.root is None
    assert args.log_level == "INFO"
    assert args.print_tools is False


def test_module_main_guard_exists() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in source


def test_call_tool_works_for_read_only_tool(harness: Harness) -> None:
    envelope = server.call_tool(harness.context(), "get_route_status", {})
    assert envelope["ok"] is True
    assert envelope["data"]["routes"] == []


def test_planning_tool_reports_invalid_request(harness: Harness) -> None:
    envelope = server.call_tool(
        harness.context(), "create_execution_plan", {"request": dict(REQUEST_DOCUMENT)}
    )
    # 没有数据源适配器 → planner 明确报 source_not_found（不伪造计划）
    assert envelope["ok"] is False
    assert envelope["error"]["code"] in {"source_not_found", "config_error"}
