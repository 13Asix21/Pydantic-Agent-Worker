"""MCP transport factory: stdio / Streamable HTTP / SSE / in-process mock.

Each remote call opens a short-lived MCP session (simple and reliable for the
PoC). Connection failures are retried with exponential backoff; the SDK is
imported lazily so the mock path needs no live endpoint.
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import structlog

from agent_bridge.mcp.models import McpServerConfig

logger = structlog.get_logger(__name__)

MockResponder = Callable[[str, dict[str, Any]], dict[str, Any]]


class McpTransport:
    """One MCP connection with lazy sessions and retry support."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        catalog: list[dict[str, Any]] | None = None,
        responder: MockResponder | None = None,
    ) -> None:
        """Create a transport; ``mock`` mode needs no network."""
        self.config = config
        self._catalog: list[dict[str, Any]] = copy.deepcopy(catalog or [])
        self._responder = responder
        self._calls: list[tuple[str, dict[str, Any]]] = []
        self._attempts = 0
        self._connected = False
        self._fail_first_n = 0
        self.simulate_failure = False

    @classmethod
    def create(
        cls,
        config: McpServerConfig,
        *,
        catalog: list[dict[str, Any]] | None = None,
        responder: MockResponder | None = None,
    ) -> McpTransport:
        """Factory for all supported transports."""
        valid = {"stdio", "streamable_http", "http", "sse", "mock"}
        if config.transport not in valid:
            raise ValueError(f"不支持的 MCP transport: {config.transport}")
        return cls(config, catalog=catalog, responder=responder)

    @classmethod
    def mock(
        cls,
        catalog: list[dict[str, Any]] | None = None,
        responder: MockResponder | None = None,
        name: str = "mock",
        namespace: str = "mock",
    ) -> McpTransport:
        """Build an in-process transport used by default tests and PoC config."""
        config = McpServerConfig(name=name, namespace=namespace, transport="mock")
        return cls(config, catalog=catalog, responder=responder)

    @property
    def call_count(self) -> int:
        """Number of mock ``call_tool`` invocations (drives dry_run assertions)."""
        return len(self._calls)

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Recorded mock tool calls."""
        return list(self._calls)

    @property
    def attempt_count(self) -> int:
        """Number of operation attempts including failed retries."""
        return self._attempts

    def fail_next(self, count: int = 1) -> None:
        """Make the next ``count`` operations fail (retry test helper)."""
        self._fail_first_n = count

    def connect(self) -> None:
        """Validate configuration; remote sessions are opened lazily per call."""
        if self.config.transport == "mock":
            self._connected = True
            return
        if self.config.transport in {"streamable_http", "http", "sse"} and not self.config.endpoint:
            raise ValueError(f"{self.config.name}: 缺少 MCP endpoint")
        if self.config.transport == "stdio" and not self.config.command:
            raise ValueError(f"{self.config.name}: 缺少 stdio command")
        self._connected = True

    def list_tools(self) -> list[dict[str, Any]]:
        """Return tools/list results, retrying transient failures."""
        return self._with_retry(self._list_tools_once)

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call one MCP tool, returning a JSON-serialisable mapping."""
        return self._with_retry(lambda: self._call_tool_once(name, args))

    def close(self) -> None:
        """Mark the transport closed (no persistent handles in PoC)."""
        self._connected = False

    def _with_retry(self, operation: Callable[[], Any]) -> Any:
        """Retry an operation with exponential backoff."""
        attempts = max(0, self.config.max_retries) + 1
        for attempt in range(attempts):
            self._attempts += 1
            try:
                if self._fail_first_n > 0:
                    self._fail_first_n -= 1
                    raise RuntimeError("mock transport transient failure")
                if self.simulate_failure:
                    raise RuntimeError(f"{self.config.name} MCP 不可用")
                return operation()
            except Exception:
                if attempt == attempts - 1:
                    raise
                delay = min(0.1 * (2**attempt), 2.0)
                logger.warning(
                    "mcp_retry",
                    server=self.config.name,
                    attempt=attempt + 1,
                    delay_s=delay,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _list_tools_once(self) -> list[dict[str, Any]]:
        """One tools/list operation (mock catalog or live MCP session)."""
        if self.config.transport == "mock":
            return copy.deepcopy(self._catalog)
        return self._remote_call("list_tools", None, {})

    def _call_tool_once(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """One tools/call operation (mock responder or live MCP session)."""
        self._calls.append((name, copy.deepcopy(args)))
        if self.config.transport == "mock":
            if self._responder is not None:
                return self._responder(name, args)
            return {
                "content": [{"type": "text", "text": json.dumps({"tool": name, "args": args})}],
                "isError": False,
            }
        return self._remote_call("call_tool", name, args)

    def _remote_call(
        self, operation: str, name: str | None, args: dict[str, Any]
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Open an MCP client session and perform one operation."""
        import asyncio

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        async def run() -> list[dict[str, Any]] | dict[str, Any]:
            async with AsyncExitStack() as stack:
                headers: dict[str, str] = {}
                token = self.config.token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                if self.config.transport == "stdio":
                    env = os.environ.copy()
                    params = StdioServerParameters(
                        command=self.config.command or "",
                        args=self.config.args,
                        env=env,
                    )
                    streams = await stack.enter_async_context(stdio_client(params))
                elif self.config.transport == "sse":
                    streams = await stack.enter_async_context(sse_client(self.config.endpoint, headers=headers))
                else:
                    streams = await stack.enter_async_context(
                        streamablehttp_client(self.config.endpoint, headers=headers)
                    )
                read, write = streams[0], streams[1]
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                if operation == "list_tools":
                    result = await session.list_tools()
                    return [
                        {
                            "name": tool.name,
                            "description": getattr(tool, "description", "") or "",
                            "inputSchema": getattr(tool, "inputSchema", None)
                            or getattr(tool, "input_schema", {})
                            or {"type": "object", "properties": {}},
                        }
                        for tool in result.tools
                    ]
                result = await session.call_tool(name or "", arguments=args)
                content = [
                    item.model_dump(mode="json")
                    for item in getattr(result, "content", [])
                    if hasattr(item, "model_dump")
                ]
                return {
                    "content": content,
                    "isError": bool(getattr(result, "isError", False)),
                    "structuredContent": getattr(result, "structuredContent", None),
                }

        return asyncio.run(run())
