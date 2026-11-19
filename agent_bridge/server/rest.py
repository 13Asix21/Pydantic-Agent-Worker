"""REST channel: synchronous run, SSE stream, health and session inspection."""

from __future__ import annotations

import json
from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from agent_bridge.runtime.agent import AgentRuntime
from agent_bridge.server.schemas import RunRequest, RunResponse, SessionInfo

logger = structlog.get_logger(__name__)


def create_rest_router(runtime: AgentRuntime) -> APIRouter:
    """Build the REST router bound to one AgentRuntime."""
    router = APIRouter()

    @router.post("/agent/run", response_model=RunResponse)
    async def agent_run(req: RunRequest, request: Request) -> RunResponse:
        """Synchronous agent invocation with the unified response envelope."""
        trace_id = getattr(request.state, "trace_id", uuid4().hex)
        response = await runtime.run_async(req.prompt, req.session_id, req.confirm_writes)
        response.trace_id = trace_id
        return response

    @router.post("/agent/run/stream")
    async def agent_run_stream(req: RunRequest, request: Request) -> EventSourceResponse:
        """SSE stream: ``data: {...}\\n\\n`` per chunk, ending with ``data: [DONE]``."""
        trace_id = getattr(request.state, "trace_id", uuid4().hex)

        async def events():
            """Yield SSE frames from the runtime token stream."""
            try:
                async for chunk in runtime.stream(req.prompt, req.session_id, req.confirm_writes):
                    yield {
                        "data": json.dumps(
                            {
                                "code": 0,
                                "data": {"delta": chunk, "session_id": req.session_id},
                                "message": "ok",
                                "trace_id": trace_id,
                            },
                            ensure_ascii=False,
                        )
                    }
            except Exception as exc:  # pragma: no cover - defensive for stream failures
                logger.error("sse_stream_failed", code=5002, error=str(exc), trace_id=trace_id)
                yield {
                    "data": json.dumps(
                        {"code": 5002, "data": {}, "message": str(exc), "trace_id": trace_id},
                        ensure_ascii=False,
                    )
                }
            finally:
                yield {"data": "[DONE]"}

        return EventSourceResponse(events(), media_type="text/event-stream")

    @router.get("/sessions/{session_id}", response_model=SessionInfo)
    async def session_info(session_id: str) -> SessionInfo:
        """Return message count for one in-memory session."""
        history = runtime.sessions.get(session_id)
        return SessionInfo(
            code=0,
            data={"session_id": session_id, "message_count": len(history)},
            message="ok",
            trace_id=uuid4().hex,
        )

    return router
