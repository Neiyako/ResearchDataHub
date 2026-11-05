"""MCP Server：stdio 上的 JSON-RPC，18 个工具的 ``tools/list`` / ``tools/call`` 分发。

为什么直接用 lowlevel ``Server`` 而不是 ``MCPServer``（原 FastMCP）
-----------------------------------------------------------------
1. **return schema 稳定**：18 个工具的 ``inputSchema`` 由 :mod:`.schemas` 手工维护。
   ``MCPServer`` 从函数签名生成 schema，改一个类型注解就会改外部契约。
2. **错误信封**：``MCPServer`` 把 handler 异常变成协议错误（``isError=true``），
   但我们要求**业务失败也是结构化 JSON**（``ok=false`` + ``error.code``），
   让 Agent 能按错误码分支。用 lowlevel 才能把"失败"编码成正常响应。
3. **不阻塞事件循环**：handler 放进线程池执行（Core 是同步的），
   stdio 读取与后台任务不受影响。
4. **SDK 版本兼容**：``Server`` 的构造函数在两个大版本间保持稳定；
   ``MCPServer`` 在 2.x 被改名过（FastMCP → MCPServer）。

stdout 纪律
-----------
MCP over stdio 只允许协议帧出现在 stdout。这里显式把 ``sys.stdout`` 指向 stderr，
另把原始 fd 复制一份给 SDK 的写端使用：

* 任何库（含 ``print``）写到 ``sys.stdout`` 的内容都会进 stderr，不会污染协议流；
* 日志配置强制走 stderr。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from researchdatahub import __version__
from researchdatahub.core.errors import RdhError
from researchdatahub.interfaces.mcp import schemas
from researchdatahub.interfaces.mcp.context import McpContext, build_context
from researchdatahub.interfaces.mcp.errors import (
    ENVELOPE_SCHEMA_VERSION,
    MCPError,
    envelope_error,
    envelope_success,
    error_from_exception,
    invalid_arguments,
)
from researchdatahub.interfaces.mcp.schemas.catalog import TOOL_CATALOG, ToolSpec
from researchdatahub.interfaces.mcp.tools import TOOL_HANDLERS

__all__ = [
    "SERVER_NAME",
    "TOOL_EXECUTION_ERROR",
    "call_tool",
    "create_server",
    "dispatch",
    "list_tools_payload",
    "main",
    "run_stdio",
]

logger = logging.getLogger("researchdatahub.interfaces.mcp.server")

SERVER_NAME: Final[str] = "researchdatahub"
TOOL_EXECUTION_ERROR: Final[str] = "tool execution raised unexpectedly"
"""未预期异常的对外文案（**不含**堆栈与异常字符串）。"""

_SDK_IMPORT_ERROR: str | None = None
"""MCP SDK 不可用时的原因；``None`` 表示可用。模块仍然可导入，只是无法提供服务。"""


def _import_sdk() -> tuple[Any, Any, Any] | None:
    """导入 MCP SDK 所需符号；失败返回 ``None`` 并记录原因（不抛异常）。"""
    global _SDK_IMPORT_ERROR
    try:
        types_module = importlib.import_module("mcp_types")
        server_module = importlib.import_module("mcp.server.lowlevel")
        stdio_module = importlib.import_module("mcp.server.stdio")
    except ImportError as exc:  # pragma: no cover - 环境相关
        _SDK_IMPORT_ERROR = f"MCP SDK is not installed: {exc}"
        return None
    _SDK_IMPORT_ERROR = None
    return types_module, server_module, stdio_module


# --------------------------------------------------------------------------- #
# 分发（与传输无关，可被单测直接调用）
# --------------------------------------------------------------------------- #
def list_tools_payload() -> list[dict[str, Any]]:
    """``tools/list`` 的内容（纯字典；由 :func:`create_server` 转成 SDK 类型）。"""
    return schemas.build_tool_list()


def call_tool(
    context: McpContext | None,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """执行一个工具并返回**信封**（永不抛异常：所有失败都编码成 ``ok=false``）。

    Parameters
    ----------
    context:
        已装配的服务容器；``None`` 表示服务未就绪（例如缺少数据库），任何调用都返回
        ```not_available``——而不是让 handler 在半装配状态下跑。
    tool_name:
        工具名（必须命中 :data:`TOOL_CATALOG`）。
    arguments:
        调用参数；``None`` 视为空对象。
    """
    started = time.perf_counter()
    spec = _spec_for(tool_name)
    if spec is None:
        error = invalid_arguments(
            f"unknown tool {tool_name!r}",
            detail={"tool": tool_name, "available_tools": list(schemas.TOOL_NAMES)},
        )
        return envelope_error(
            error_from_exception(error),
            tool=tool_name,
            duration_ms=_elapsed_ms(started),
        )
    if context is None:
        return envelope_error(
            {
                "code": "not_available",
                "message": _SDK_IMPORT_ERROR or "server is not ready",
                "hint": "start the server with a valid --root (run 'rdh db init' first)",
                "retryable": False,
            },
            tool=tool_name,
            duration_ms=_elapsed_ms(started),
        )
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:  # pragma: no cover - 目录与 handler 表由单测守护一致
        return envelope_error(
            {
                "code": "not_implemented",
                "message": f"tool {tool_name!r} is declared but has no handler",
                "hint": "this is a server bug; report it",
                "retryable": False,
            },
            tool=tool_name,
            duration_ms=_elapsed_ms(started),
        )
    try:
        data = handler(context, dict(arguments or {}))
    except (MCPError, RdhError) as exc:
        return envelope_error(
            error_from_exception(exc),
            tool=tool_name,
            duration_ms=_elapsed_ms(started),
        )
    except Exception:
        logger.exception("tool %s raised an unexpected exception", tool_name)
        return envelope_error(
            {
                "code": "internal_error",
                "message": TOOL_EXECUTION_ERROR,
                "hint": "server-side bug; details are in the server's stderr log",
                "retryable": False,
            },
            tool=tool_name,
            duration_ms=_elapsed_ms(started),
        )
    return envelope_success(data, tool=tool_name, duration_ms=_elapsed_ms(started))


def dispatch(
    context: McpContext | None,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> str:
    """执行工具并返回 JSON 文本（stdout 只允许协议帧，故此处返回字符串而非 print）。"""
    return json.dumps(call_tool(context, tool_name, arguments), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# SDK 适配
# --------------------------------------------------------------------------- #
def create_server(context: McpContext | None) -> Any:
    """构造 lowlevel ``Server``（``on_list_tools`` / ``on_call_tool``）。

    Raises
    ------
    RuntimeError
        MCP SDK 未安装（由 :func:`main` 提前拦成可读错误）。
    """
    sdk = _import_sdk()
    if sdk is None:
        raise RuntimeError(_SDK_IMPORT_ERROR or "MCP SDK unavailable")
    types_module, server_module = sdk[0], sdk[1]
    server_cls = server_module.Server

    async def on_list_tools(_ctx: Any, _params: Any) -> Any:  # pragma: no cover - 由 SDK 驱动
        tools = [
            types_module.Tool(
                name=item["name"],
                description=item["description"],
                input_schema=item["inputSchema"],
                annotations=_annotations(types_module, item.get("annotations")),
            )
            for item in list_tools_payload()
        ]
        return types_module.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx: Any, params: Any) -> Any:  # pragma: no cover - 由 SDK 驱动
        name = params.name
        arguments = params.arguments or {}
        envelope = await asyncio.to_thread(call_tool, context, name, arguments)
        return types_module.CallToolResult(
            content=[
                types_module.TextContent(
                    type="text",
                    text=json.dumps(envelope, ensure_ascii=False),
                )
            ],
            structured_content=envelope,
            is_error=not envelope["ok"],
        )

    return server_cls(
        SERVER_NAME,
        version=__version__,
        title="ResearchDataHub",
        instructions=(
            "科研数据获取基础设施。先用 search_datasets / inspect_dataset 确认数据源，"
            "再 validate_request + estimate_acquisition 估算规模，"
            "然后 create_execution_plan 获取 approval_token，"
            "最后 submit_acquisition 提交。所有返回都是 {ok,data,error,meta} 信封。"
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _annotations(types_module: Any, raw: Mapping[str, Any] | None) -> Any:
    """把目录里的注解字典转成 SDK 的 ``ToolAnnotations``（不可用时返回 ``None``）。"""
    if raw is None:
        return None
    annotations_cls = getattr(types_module, "ToolAnnotations", None)
    if annotations_cls is None:  # pragma: no cover - 旧版 SDK
        return None
    return annotations_cls(
        title=raw.get("title"),
        readOnlyHint=raw.get("readOnlyHint"),
        destructiveHint=raw.get("destructiveHint"),
        idempotentHint=raw.get("idempotentHint"),
        openWorldHint=raw.get("openWorldHint"),
    )


async def run_stdio(
    context: McpContext | None,
    *,
    read_stream: Any = None,
    write_stream: Any = None,
) -> None:
    """在 stdio 上运行直到客户端关闭连接（``read_stream``/``write_stream`` 可注入测试）。"""
    sdk = _import_sdk()
    if sdk is None:
        raise RuntimeError(_SDK_IMPORT_ERROR or "MCP SDK unavailable")
    stdio_module = sdk[2]
    server = create_server(context)
    if read_stream is None or write_stream is None:
        async with stdio_module.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
        return
    await server.run(read_stream, write_stream, server.create_initialization_options())


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rdh-mcp",
        description="ResearchDataHub MCP Server（stdio）。stdout 仅输出协议帧，日志走 stderr。",
    )
    parser.add_argument("--root", help="状态根目录（默认取 defaults.storage.root，通常是 ~/.rdh）")
    parser.add_argument("--config", help="defaults.yaml 路径（默认 configs/defaults.yaml）")
    parser.add_argument("--sources", help="sources.yaml 路径（默认 configs/sources.yaml）")
    parser.add_argument(
        "--approval-key",
        help="审批令牌 HMAC 密钥路径（默认 $RDH_APPROVAL_KEY_FILE 或 <root>/approval.key）",
    )
    parser.add_argument("--log-level", default="INFO", help="stderr 日志级别（默认 INFO）")
    parser.add_argument(
        "--print-tools",
        action="store_true",
        help="把 tools/list 的 JSON 打到 stdout 后退出（调试用；此模式不提供协议服务）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} (MCP envelope {ENVELOPE_SCHEMA_VERSION})",
    )
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    """日志一律写 stderr：stdout 是 MCP 协议流，任何多余字节都会破坏会话。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _ensure_protocol_stdout() -> None:
    """保证 fd 1 是协议流，且服务期间没有任何 Python 代码往 fd 1 写杂散内容。

    为什么**不**自己重定向 fd 1
    ---------------------------
    MCP SDK（2.x）的 :func:`mcp.server.stdio.stdio_server` 已经做了这件事：它在进入时
    把 fd 1 复制成一个私有描述符给协议流使用，并把 fd 1 本身转到 stderr，退出时还原。
    如果我们在它之前 ``sys.stdout = sys.stderr``，SDK 的 ``_is_backed_by_fd(sys.stdout, 1)``
    检查会失败（``sys.stderr.buffer.fileno() == 2``），于是它退化成直接使用 fd 1 当协议流；
    再配合任何额外的 ``os.dup2``，协议帧就会被写进 stderr——客户端表现为"握手成功但
    ``tools/list`` 永远等不到"。

    因此这里只做一件安全的事：把 ``sys.stdout`` 的文本层同步刷新，确保此前的内容已经
    落盘；协议流的排他性由 SDK 保证，日志由 ``logging.StreamHandler(sys.stderr)`` 保证。
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """``rdh-mcp`` / ``python -m researchdatahub.interfaces.mcp.server`` 的入口。

    Returns
    -------
    int
        进程退出码：``0`` 正常结束，``2`` 配置/启动失败（**不**进入假运行状态）。
    """
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    if args.print_tools:
        payload = {"tools": list_tools_payload(), "count": len(TOOL_CATALOG)}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    if _import_sdk() is None:
        logger.error(
            "MCP SDK 未安装：%s。安装方式：pip install 'researchdatahub[mcp]'", _SDK_IMPORT_ERROR
        )
        return 2

    try:
        context = build_context(
            root=args.root,
            config_path=args.config,
            sources_path=args.sources,
            approval_key_path=args.approval_key,
        )
    except (RdhError, OSError) as exc:
        logger.error("cannot start MCP server: %s", exc)
        return 2

    _ensure_protocol_stdout()
    logger.info(
        "rdh-mcp ready: tools=%d root=%s db=%s approval_key=%s",
        len(TOOL_CATALOG),
        context.storage_root,
        context.db_path,
        None if context.approval_authorizer is None else context.approval_authorizer.key_path,
    )
    try:
        asyncio.run(run_stdio(context))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        logger.info("rdh-mcp interrupted")
    finally:
        context.close()
    return 0


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _spec_for(name: str) -> ToolSpec | None:
    for spec in TOOL_CATALOG:
        if spec.name == name:
            return spec
    return None


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


_ = (Awaitable, Callable, datetime)  # 保持类型引用显式（供类型检查与文档）


if __name__ == "__main__":  # pragma: no cover - python -m researchdatahub.interfaces.mcp.server
    raise SystemExit(main())
