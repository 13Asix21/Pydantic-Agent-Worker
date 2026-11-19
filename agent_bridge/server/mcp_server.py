"""MCP Server channel: expose the same AgentRuntime as an ``agent_run`` tool."""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from agent_bridge.runtime.agent import AgentRuntime
from agent_bridge.server.schemas import RunResponse

logger = structlog.get_logger(__name__)


def create_mcp_server(runtime: AgentRuntime, name: str = "agent-bridge") -> FastMCP:
    """Build a FastMCP server exposing ``agent_run`` over the shared runtime."""
    mcp_server = FastMCP(
        name=name,
        stateless_http=True,
        streamable_http_path="/",
        json_response=True,
    )

    @mcp_server.tool(
        name="agent_run",
        description="调用 Agent Bridge 运行时执行一轮对话，返回统一 {code,data,message,trace_id} 结构。",
    )
    async def agent_run(prompt: str, session_id: str = "default") -> dict[str, object]:
        """MCP tool implementation delegating to the shared AgentRuntime."""
        response: RunResponse = await runtime.run_async(prompt, session_id)
        logger.info(
            "mcp_server_agent_run",
            tool_name="agent_run",
            session_id=session_id,
            code=response.code,
            trace_id=response.trace_id,
        )
        return response.model_dump()

    return mcp_server


def mount_mcp(app: FastAPI, mcp_server: FastMCP, path: str = "/mcp") -> None:
    """Mount the FastMCP Streamable HTTP ASGI app under ``path``."""
    app.mount(path, mcp_server.streamable_http_app())
