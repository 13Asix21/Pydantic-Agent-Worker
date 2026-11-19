"""Native web tools with deterministic mock defaults and opt-in real HTTP."""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
import httpx
import structlog

from agent_bridge.config.settings import Settings, ToolsSettings
from agent_bridge.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)


def _run_async(coro: Any) -> Any:
    """Run a coroutine from sync tool wrappers (thread or plain sync context)."""
    try:
        return anyio.from_thread.run(coro)
    except RuntimeError:
        return asyncio.run(coro)


class NativeTools:
    """HTTP/web native tools, registered with the reserved ``native__`` prefix."""

    def __init__(self, config: ToolsSettings | None = None) -> None:
        """Create native tools from settings (mock by default)."""
        self.config = config or Settings().tools

    def register(self, registry: ToolRegistry) -> None:
        """Register ``native__web_search`` and ``native__http_request``."""
        registry.register("native__web_search", self.web_search)
        registry.register("native__http_request", self.http_request)

    def web_search(self, query: str) -> dict[str, Any]:
        """Search the web; return deterministic mock results unless real HTTP is enabled."""
        if not self.config.enable_real_http:
            logger.info("native_web_search_mock", code=0, dry_run=True)
            return {
                "results": [
                    {
                        "title": f"Mock result for {query}",
                        "url": f"https://example.invalid/search?q={query}",
                        "snippet": f"确定性 mock 结果：{query}",
                    }
                ],
                "mocked": True,
            }
        return _run_async(self._web_search_real(query))

    def http_request(
        self,
        method: str = "GET",
        url: str = "https://example.invalid",
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Perform an HTTP request; mock by default, real network is opt-in."""
        if not self.config.enable_real_http:
            logger.info("native_http_request_mock", code=0, dry_run=True)
            return {
                "status_code": 200,
                "body": f"mock {method.upper()} {url}",
                "headers": {},
                "mocked": True,
            }
        return _run_async(self._http_request_real(method, url, headers, body))

    async def _http_request_real(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        body: str | None,
    ) -> dict[str, Any]:
        """Real HTTP call with timeout and linear retry."""
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.max_retries) + 1):
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
                    response = await client.request(method.upper(), url, headers=headers, content=body)
                return {
                    "status_code": response.status_code,
                    "body": response.text,
                    "headers": dict(response.headers),
                    "mocked": False,
                }
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    await anyio.sleep(0.1 * (attempt + 1))
        return {"status_code": 0, "body": str(last_error), "headers": {}, "mocked": False}

    async def _web_search_real(self, query: str) -> dict[str, Any]:
        """Real search fallback: query an OpenAI-compatible-free public endpoint if configured."""
        response = await self._http_request_real(
            "GET",
            f"https://www.google.com/search?q={query}",
            {"User-Agent": "AgentBridge-PoC/0.1"},
            None,
        )
        return {
            "results": [
                {
                    "title": f"Search: {query}",
                    "url": f"https://www.google.com/search?q={query}",
                    "snippet": response["body"][:500],
                }
            ],
            "mocked": False,
            "status_code": response["status_code"],
        }
