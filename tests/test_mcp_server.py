"""G4-MCP: external MCP client sees agent_run and receives the unified envelope."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from agent_bridge.runtime.agent import AgentRuntime
from agent_bridge.server.app import create_app
from agent_bridge.server.mcp_server import create_mcp_server


@pytest.fixture(scope="module")
def runtime() -> AgentRuntime:
    """Runtime assembled by the application factory."""
    return create_app().state.runtime


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Application with the MCP sub-app mounted at /mcp."""
    return create_app()


@pytest.mark.asyncio
async def test_mcp_tools_list_contains_agent_run(runtime: AgentRuntime) -> None:
    """外部 client tools/list 能看到 agent_run。"""
    server = create_mcp_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        assert isinstance(client, ClientSession)
        tools = await client.list_tools()
        names = [tool.name for tool in tools.tools]
        assert "agent_run" in names


@pytest.mark.asyncio
async def test_mcp_call_tool_returns_unified_envelope(runtime: AgentRuntime) -> None:
    """call_tool('agent_run') 返回与 REST 一致的 {code,data,message,trace_id}。"""
    server = create_mcp_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("agent_run", {"prompt": "MCP 通道你好", "session_id": "mcp-session"})
        payload = json.loads(result.content[0].text)
        assert payload["code"] == 0
        assert payload["trace_id"]
        assert "MCP 通道你好" in payload["data"]["text"]


@pytest.mark.asyncio
async def test_mcp_channel_reuses_same_tool_registry(runtime: AgentRuntime) -> None:
    """MCP 通道与 REST 共享运行时：tool_call 同样能派发到已注册工具。"""
    server = create_mcp_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "agent_run",
            {
                "prompt": '调用 native__http_request 参数:{"method":"GET","url":"https://example.invalid"}',
                "session_id": "mcp-tool",
            },
        )
        payload = json.loads(result.content[0].text)
        assert payload["code"] == 0
        assert "native__http_request" in payload["data"]["tool_calls"]
        assert "mock GET" in payload["data"]["text"]


def test_mcp_sub_app_mounted(app: FastAPI) -> None:
    """Streamable HTTP ASGI app 挂载在 /mcp 子路径。"""
    paths = [getattr(route, "path", "") for route in app.routes]
    assert any(path.startswith("/mcp") for path in paths)
