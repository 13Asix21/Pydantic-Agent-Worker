"""FastAPI application factory: assemble all layers and mount both channels."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_bridge.config.settings import Settings
from agent_bridge.mcp.adapter import McpToolAdapter
from agent_bridge.mcp.registry import McpManager
from agent_bridge.runtime.agent import AgentRuntime
from agent_bridge.runtime.session import SessionStore
from agent_bridge.server.mcp_server import create_mcp_server, mount_mcp
from agent_bridge.server.rest import create_rest_router
from agent_bridge.skills.adapter import SkillToolAdapter
from agent_bridge.skills.models import SkillDoc
from agent_bridge.skills.registry import SkillRegistry
from agent_bridge.skills.sandbox import ScriptSandbox
from agent_bridge.skills.scanner import SkillScanner
from agent_bridge.tools.registry import ToolRegistry
from agent_bridge.tools.web import NativeTools

logger = structlog.get_logger(__name__)
_SKILL_CACHE: dict[tuple[str, ...], list[SkillDoc]] = {}


def _build_runtime(settings: Settings) -> AgentRuntime:
    """Wire Skills, native tools, MCP servers and the shared runtime."""
    roots = tuple(settings.skills.roots)
    docs = _SKILL_CACHE.get(roots)
    if docs is None:
        docs = SkillScanner().scan([Path(root) for root in roots])
        _SKILL_CACHE[roots] = docs
    skills = SkillRegistry(docs)
    tools = ToolRegistry()
    SkillToolAdapter(skills, ScriptSandbox(settings.sandbox)).register(tools)
    NativeTools(settings.tools).register(tools)
    manager = McpManager()
    manager.startup(settings)
    McpToolAdapter().register(tools, manager)
    runtime = AgentRuntime(settings, tools, skills, SessionStore(), manager)
    logger.info(
        "agent_bridge_layers_ready",
        skills=len(skills.names()),
        tools=len(tools.names()),
        mcp_tools=len(manager.all_tools()),
        dry_run=settings.dry_run,
    )
    return runtime


def create_app(settings: Settings | None = None, runtime: AgentRuntime | None = None) -> FastAPI:
    """Build the ASGI app with REST, MCP Server and the shared runtime state."""
    settings = settings or Settings()
    runtime = runtime or _build_runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Log lifecycle boundaries; all layers are assembled eagerly."""
        logger.info("agent_bridge_startup", dry_run=settings.dry_run)
        try:
            yield
        finally:
            logger.info("agent_bridge_shutdown")

    app = FastAPI(title="Agent Bridge", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.tools = runtime.tools
    app.state.skills = runtime.skills
    app.state.mcp_manager = runtime.mcp_manager
    app.state.sessions = runtime.sessions

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Map parameter errors to the unified 5xxx envelope."""
        return JSONResponse(
            status_code=400,
            content={
                "code": 5001,
                "data": {"errors": str(exc.errors())},
                "message": "5001: 参数校验失败",
                "trace_id": uuid4().hex,
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        """Liveness probe with the unified response envelope."""
        return {
            "code": 0,
            "data": {
                "status": "ok",
                "dry_run": settings.dry_run,
                "tools": len(runtime.tools.names()),
                "llm_provider": settings.llm.provider,
            },
            "message": "ok",
            "trace_id": "health",
        }

    app.include_router(create_rest_router(runtime))
    mount_mcp(app, create_mcp_server(runtime))
    return app
