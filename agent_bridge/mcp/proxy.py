"""Single generic MCP tool proxy with per-server circuit breaker."""

from __future__ import annotations

import time
from typing import Any

import structlog

from agent_bridge.config.settings import Settings
from agent_bridge.mcp.gateway import PermissionGateway
from agent_bridge.mcp.models import Permission, ToolCallResult, ToolSpec
from agent_bridge.mcp.transport import McpTransport

logger = structlog.get_logger(__name__)


class CircuitBreaker:
    """Simple failure counter with a cooldown window, scoped to one server."""

    def __init__(self, failure_threshold: int = 3, cooldown_s: int = 30) -> None:
        """Create a closed breaker."""
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = max(1, cooldown_s)
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        """Whether the breaker currently rejects calls."""
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_s:
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_failure(self) -> None:
        """Record a failed call and open the breaker at the threshold."""
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        """Reset the breaker after a successful call."""
        self._failures = 0
        self._opened_at = None


class McpToolProxy:
    """One ``call(server, tool_name, args)`` entry for every MCP tool."""

    def __init__(
        self,
        transports: dict[str, McpTransport],
        specs: dict[str, ToolSpec] | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        """Create a proxy over server transports and an optional tool index."""
        self._transports = transports
        self._specs = specs or {}
        self.settings = settings or Settings()
        self._breakers: dict[str, CircuitBreaker] = {}

    def call(
        self,
        server: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        confirm: bool = False,
        category: str | None = None,
    ) -> ToolCallResult:
        """Call any discovered MCP tool through the permission gateway.

        Error codes: ``3001`` unknown tool, ``3002`` timeout/unavailable/circuit,
        ``3003`` write intercepted by dry_run.
        """
        started = time.perf_counter()
        args = args or {}
        transport = self._transports.get(server)
        if transport is None:
            return ToolCallResult(
                ok=False,
                error_code="3001",
                message=f"MCP server 未连接: {server}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        breaker = self._breakers.setdefault(
            server,
            CircuitBreaker(self.settings.mcp.failure_threshold, self.settings.mcp.cooldown_s),
        )
        if breaker.is_open:
            return ToolCallResult(
                ok=False,
                error_code="3002",
                message=f"{server} 熔断器打开，知识库暂时不可用",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        spec = self._find_spec(server, tool_name, category)

        def executor(_spec: ToolSpec, arguments: dict[str, Any]) -> ToolCallResult:
            try:
                raw = transport.call_tool(tool_name, arguments)
            except Exception as exc:
                breaker.record_failure()
                logger.warning("mcp_call_failed", server=server, tool_name=tool_name, error=str(exc))
                return ToolCallResult(
                    ok=False,
                    error_code="3002",
                    message=f"{server} 暂时不可用: {exc}",
                    data={"would_call": _spec.qualified_name},
                )
            breaker.record_success()
            data = self._normalise(raw)
            return ToolCallResult(
                ok=not bool(data.get("isError", False)),
                data=data,
                error_code="0" if not data.get("isError", False) else "3002",
                message="ok",
            )

        gateway = PermissionGateway(dry_run=self.settings.dry_run, executor=executor)
        result = gateway.check(spec, args, confirm)
        result.trace_id = result.trace_id or ""
        result.latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        logger.info(
            "mcp_tool_call",
            tool_name=spec.qualified_name,
            server=server,
            latency_ms=result.latency_ms,
            dry_run=result.dry_run,
            code=result.error_code,
        )
        return result

    def _find_spec(self, server: str, tool_name: str, category: str | None) -> ToolSpec:
        """Find a spec by qualified name, raw name, or synthesise a fallback."""
        qualified = f"{server}__{tool_name}"
        spec = self._specs.get(qualified)
        if spec is None and category:
            spec = self._specs.get(f"{server}__{category}__{tool_name}")
        if spec is None:
            for candidate in self._specs.values():
                if candidate.server == server and candidate.name == tool_name:
                    spec = candidate
                    break
        if spec is None:
            namespace = server
            for candidate in self._specs.values():
                if candidate.server == server:
                    namespace = candidate.qualified_name.split("__")[0]
                    break
            # 未注册工具保守视为 WRITE，避免 READ 前缀误判绕过 dry_run。
            spec = ToolSpec(
                name=tool_name,
                qualified_name=f"{namespace}__{tool_name}",
                server=server,
                permission=Permission.WRITE,
                permission_explicit=True,
                category=category,
            )
        return spec

    @staticmethod
    def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
        """Flatten MCP content blocks into a JSON-friendly mapping."""
        content = raw.get("content", [])
        texts: list[str] = []
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("text") is not None:
                texts.append(str(item["text"]))
            elif isinstance(item, str):
                texts.append(item)
        joined = "\n".join(texts)
        data: dict[str, Any] = {"content": joined, "isError": bool(raw.get("isError", False))}
        if joined:
            try:
                import json

                parsed = json.loads(joined)
                if isinstance(parsed, dict):
                    data.update(parsed)
            except (ValueError, TypeError):
                pass
        if raw.get("structuredContent") is not None:
            data["structuredContent"] = raw["structuredContent"]
        return data
