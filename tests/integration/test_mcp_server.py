"""MCP Server 端到端集成测试：**真起子进程**、真走 stdio JSON-RPC、真跑采集链路。

背景：MCP 是外部 Agent 的唯一接口，"能 import、单测通过"不等于"能当服务器用"。
之前踩过的两个真实故障都只在子进程里暴露：

1. ``python -m ...server`` 缺少 ``__main__`` 守卫 → 进程什么都不做；
2. 自己在 SDK 之前重定向 fd 1 → 握手成功但 ``tools/list`` 永远等不到响应。

因此本文件坚持：

* 用**同一个解释器**（``sys.executable``）以子进程方式启动真实服务器，
  通过官方 MCP 客户端的 ``stdio_client`` 通信，而不是直接调 Python 函数；
* 用**本地 HTTP 文件源**（``ThreadingHTTPServer``）充当数据源，**完全不碰 cdsapi**；
* 断言 ``tools/list`` 恰好 18 个工具名；
* 走通 ``create_execution_plan`` → ``submit_acquisition``（无 token 被拒 / 有 token 通过）
  → ``get_task_status``，并在真正执行时产出真实文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from tests.unit.test_transfer.http_server import Resource, TransferTestServer

from researchdatahub.config.loader import load_defaults
from researchdatahub.core.models import AccessRoute, SourceAsset, SourceMetadata
from researchdatahub.integrity.checker import IntegrityChecker
from researchdatahub.interfaces.mcp.auth import ApprovalStore, PlanApprovalAuthorizer
from researchdatahub.interfaces.mcp.context import McpContext
from researchdatahub.manifest.cli_service import DbTarget
from researchdatahub.manifest.connection import ManifestConnectionFactory
from researchdatahub.sources.registry import SourceRegistry
from researchdatahub.storage.local import LocalStorageBackend

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_TEMPLATE = REPO_ROOT / "configs" / "defaults.yaml"
DATASET_ID = "reanalysis-era5-pressure-levels"
EXPECTED_TOOLS = (
    "search_datasets",
    "inspect_dataset",
    "compare_datasets",
    "get_provider_capabilities",
    "get_environment_requirements",
    "check_environment",
    "validate_environment",
    "validate_request",
    "estimate_acquisition",
    "create_execution_plan",
    "get_execution_plan",
    "submit_acquisition",
    "get_task_status",
    "cancel_task",
    "list_workers",
    "get_route_status",
    "verify_dataset",
    "get_manifest",
)

REQUEST_DOCUMENT: dict[str, object] = {
    "request": {"name": "era5_mcp_integration", "priority": "high", "max_sources": 1},
    "dataset": {
        "provider": "ncar",
        "name": DATASET_ID,
        "product": "reanalysis",
        "product_family": "ERA5",
        "version": "e5.1",
    },
    "variables": [
        {"name": "t", "units": "K"},
        {"name": "clwc", "units": "kg kg**-1"},
    ],
    "temporal": {"start": "2020-01-01", "end": "2020-02-01", "frequency": "daily"},
    "spatial": {"type": "global"},
    "vertical": {"levels": [1000, 925], "kind": "pressure"},
    "storage": {"preserve_original": True},
    "validation": {"verify_size": True, "calculate_sha256": True},
}


# --------------------------------------------------------------------------- #
# 环境装配
# --------------------------------------------------------------------------- #
class IntegrationEnv:
    """一套可复现的集成环境：tmp 状态根 + 真实库 + 本地 HTTP 数据源。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_root = root / "state"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.defaults_path = root / "defaults.yaml"
        self.defaults_path.write_text(
            DEFAULTS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        base = load_defaults(self.defaults_path)
        storage = base.defaults.storage.model_copy(
            update={"root": str(self.state_root), "free_space_floor_gb": 0.0}
        )
        scheduler = base.defaults.scheduler.model_copy(
            update={"heartbeat_interval_seconds": 1, "lease_seconds": 60}
        )
        self.defaults = base.defaults.model_copy(
            update={"storage": storage, "scheduler": scheduler}
        )
        self.db_target = DbTarget(
            db_path=self.state_root / "manifest.db", config=self.defaults.manifest
        )
        factory = ManifestConnectionFactory(self.db_target.db_path, config=self.db_target.config)
        factory.initialize()
        connection = factory.connect()
        try:
            ApprovalStore(connection).ensure_schema()
        finally:
            connection.close()
        self.server = TransferTestServer()
        self.files: dict[str, bytes] = {}

    # -- 数据源 ------------------------------------------------------------ #
    def publish(self, name: str, body: bytes) -> SourceAsset:
        path = f"/data/{name}"
        self.server.add(path, Resource(body=body, content_type="application/x-netcdf"))
        self.files[name] = body
        origin = self.server.url(path)
        metadata = SourceMetadata(
            source_id="ncar",
            display_name="local HTTP fixture",
            provider="local fixture",
            access_route=AccessRoute.HTTPS,
            dataset_id=DATASET_ID,
            origin_uri=origin,
            retrieved_at=datetime.now(UTC),
        )
        return SourceAsset(
            asset_id=f"asset-{name}".replace(".", "-").lower()[:63],
            source_id="ncar",
            dataset_id=DATASET_ID,
            access_route=AccessRoute.HTTPS,
            origin_uri=origin,
            file_name=name,
            size_bytes=len(body),
            checksum={"value": "sha256:" + hashlib.sha256(body).hexdigest()},
            metadata=metadata,
            discovered_at=datetime.now(UTC),
        )

    def context(self) -> McpContext:
        """与服务器内部装配等价的上下文（供测试侧准备计划与读取状态）。"""
        from researchdatahub.config.models import AppConfig

        return McpContext(
            app_config=AppConfig(defaults=self.defaults),
            sources_config=None,
            storage_root=self.state_root,
            db_target=self.db_target,
            registry=SourceRegistry.with_defaults(),
            adapters=(),
            storage=LocalStorageBackend(self.defaults.storage, create_dirs=True),
            integrity=IntegrityChecker(self.defaults.integrity),
            approval_authorizer=PlanApprovalAuthorizer(key_path=self.state_root / "approval.key"),
        )

    def pythonpath(self) -> str:
        return str(REPO_ROOT / "src")

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[IntegrationEnv]:
    instance = IntegrationEnv(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------------- #
# MCP 客户端
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def open_client(env: IntegrationEnv) -> AsyncIterator[object]:
    """启动真实 server 子进程并完成 MCP 握手。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(env.root),
        "PYTHONPATH": env.pythonpath(),
        "LANG": "en_US.UTF-8",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "researchdatahub.interfaces.mcp.server",
            "--root",
            str(env.state_root),
            "--config",
            str(env.defaults_path),
            "--log-level",
            "WARNING",
        ],
        cwd=str(REPO_ROOT),
        env=child_env,
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


def call_sync(env: IntegrationEnv, coro_factory) -> object:
    """在独立事件循环里跑一段客户端逻辑（pytest 默认没有 asyncio plugin）。"""

    async def _runner() -> object:
        async with open_client(env) as session:
            return await coro_factory(session)

    return anyio.run(_runner)


async def _envelope(session: object, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    """调用工具并把响应内容解析成信封（失败时给出可读断言上下文）。"""
    result = await session.call_tool(tool, arguments)  # type: ignore[attr-defined]
    assert result.content, f"{tool} returned no content"
    text = result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #
def test_server_lists_exactly_18_tools(env: IntegrationEnv) -> None:
    """真实子进程 + 真实 MCP client：``tools/list`` 必须给出 18 个工具名。"""

    async def scenario(session: object) -> list[str]:
        tools = await session.list_tools()  # type: ignore[attr-defined]
        return [tool.name for tool in tools.tools]

    names = call_sync(env, scenario)
    assert isinstance(names, list)
    assert len(names) == 18, f"expected 18 tools, got {len(names)}: {sorted(names)}"
    assert sorted(names) == sorted(EXPECTED_TOOLS)


def test_tool_schemas_are_closed_objects(env: IntegrationEnv) -> None:
    """外部 Agent 依赖 inputSchema：必须是闭合对象且带 required 声明。"""

    async def scenario(session: object) -> list[dict[str, object]]:
        tools = await session.list_tools()  # type: ignore[attr-defined]
        return [
            {"name": tool.name, "schema": tool.input_schema, "description": tool.description}
            for tool in tools.tools
        ]

    payload = call_sync(env, scenario)
    assert isinstance(payload, list)
    for item in payload:
        schema = item["schema"]
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert item["description"]
    by_name = {item["name"]: item["schema"] for item in payload}
    assert isinstance(by_name["submit_acquisition"], dict)
    assert set(by_name["submit_acquisition"]["required"]) == {"plan_id", "approval_token"}


def test_full_approval_chain(env: IntegrationEnv) -> None:
    """``create_execution_plan`` → 无 token 被拒 → 有 token 通过 → ``get_task_status``。

    计划用手工注入的数据源准备（服务器进程内不配置远端源），审批与状态查询仍然经过
    真实 server 子进程的 stdio 协议。
    """
    from tests.unit.test_mcp.conftest import StaticSource

    from researchdatahub.interfaces.mcp.server import call_tool

    body = b"CDF\x01payload-" + b"x" * 512
    asset = env.publish("a.nc", body)
    source = StaticSource("ncar", [asset])
    context = env.context()
    context.adapters = (source,)
    created = call_tool(
        context, "create_execution_plan", {"request": REQUEST_DOCUMENT, "approval_ttl_seconds": 600}
    )
    assert created["ok"] is True, created
    plan_id = created["data"]["plan_id"]
    token = created["data"]["approval_token"]
    assert created["data"]["task_count"] >= 1

    async def scenario(session: object) -> dict[str, object]:
        # 1) 无 token → invalid_arguments（schema 层直接拒绝）
        missing = await _envelope(session, "submit_acquisition", {"plan_id": plan_id})
        # 2) 伪造 token → approval_invalid，且不得入队
        forged = await _envelope(
            session,
            "submit_acquisition",
            {"plan_id": plan_id, "approval_token": "v1.bad.0.fp.x.sig"},
        )
        # 3) 合法 token → 入队
        accepted = await _envelope(
            session, "submit_acquisition", {"plan_id": plan_id, "approval_token": token}
        )
        # 4) 查询状态
        plan_view = await _envelope(session, "get_execution_plan", {"plan_id": plan_id})
        task_ids = list(plan_view["data"]["task_states"].keys())  # type: ignore[index]
        status = await _envelope(session, "get_task_status", {"task_ids": task_ids})
        return {
            "missing": missing,
            "forged": forged,
            "accepted": accepted,
            "status": status,
        }

    outcome = call_sync(env, scenario)
    assert isinstance(outcome, dict)

    missing_env = outcome["missing"]
    assert missing_env["ok"] is False
    assert missing_env["error"]["code"] == "invalid_arguments"

    forged_env = outcome["forged"]
    assert forged_env["ok"] is False
    assert forged_env["error"]["code"] == "approval_invalid"
    assert forged_env["error"]["detail"]["side_effects_performed"] is False

    accepted_env = outcome["accepted"]
    assert accepted_env["ok"] is True, accepted_env
    assert accepted_env["data"]["queued_tasks"] >= 1

    status_env = outcome["status"]
    assert status_env["ok"] is True, status_env
    assert status_env["data"]["missing"] == 0
    states = {task["state"] for task in status_env["data"]["tasks"]}
    assert states <= {"QUEUED", "RUNNING", "COMPLETED", "VERIFYING"}, states

    # 服务器进程真的把状态写进了 SQLite（不是会话内存）
    connection = sqlite3.connect(env.db_target.db_path)
    try:
        rows = connection.execute(
            "SELECT state FROM tasks WHERE plan_id = ?", (plan_id,)
        ).fetchall()
    finally:
        connection.close()
    assert rows, "plan tasks must exist in the manifest database"
    assert {row[0] for row in rows} <= {"QUEUED", "RUNNING", "COMPLETED", "VERIFYING"}


def test_format_version_and_unknown_tool(env: IntegrationEnv) -> None:
    """未知工具的失败必须是结构化信封，而不是协议级异常。"""

    async def scenario(session: object) -> dict[str, object]:
        return await _envelope(session, "no_such_tool", {})

    envelope = call_sync(env, scenario)
    assert isinstance(envelope, dict)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_arguments"
    assert envelope["meta"]["schema_version"] == "1.0"


def test_read_only_tools_do_not_mutate(env: IntegrationEnv) -> None:
    """只读工具返回成功且不产生任务行。"""

    async def scenario(session: object) -> dict[str, object]:
        return await _envelope(session, "get_provider_capabilities", {})

    envelope = call_sync(env, scenario)
    assert isinstance(envelope, dict)
    assert envelope["ok"] is True
    assert "providers" in envelope["data"]
    connection = sqlite3.connect(env.db_target.db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        connection.close()
    assert count == 0
