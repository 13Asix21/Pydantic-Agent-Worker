"""Single generic MCP tool proxy with per-server circuit breaker."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from agent_bridge.config.settings import Settings
from agent_bridge.mcp.gateway import PermissionGateway
from agent_bridge.mcp.models import Permission, ToolCallResult, ToolSpec
from agent_bridge.mcp.transport import McpTransport

logger = structlog.get_logger(__name__)

# 网关模式下 tools/list 只暴露元工具，业务工具通过 call_tool 二次转发。
# 这类工具的真实权限取决于 arguments.tool_name，而非元工具自身名称。
GATEWAY_DISPATCH_TOOLS = frozenset({"call_tool"})


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
        spec = self._resolve_spec(server, tool_name, args, category)

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
            upstream_code = data.get("code")
            failed = bool(data.get("isError", False)) or (
                isinstance(upstream_code, int) and not isinstance(upstream_code, bool) and upstream_code != 0
            )
            return ToolCallResult(
                ok=not failed,
                data=data,
                error_code="3002" if failed else "0",
                message=str(data.get("message") or "ok") if failed else "ok",
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

    def _resolve_spec(
        self, server: str, tool_name: str, args: dict[str, Any], category: str | None
    ) -> ToolSpec:
        """Resolve the effective spec, unwrapping gateway ``call_tool`` calls.

        In gateway mode ``call_tool`` is a generic dispatcher: its own name says
        nothing about side effects. Permission is therefore decided by the
        inner ``tool_name`` carried in the arguments, which keeps dry_run
        protection intact for write operations forwarded through the gateway.
        """
        if tool_name not in GATEWAY_DISPATCH_TOOLS:
            return self._find_spec(server, tool_name, category)
        candidate = args.get("tool_name")
        inner_name = candidate.strip() if isinstance(candidate, str) else ""
        if not inner_name:
            return self._find_spec(server, tool_name, category)
        known = any(spec.server == server and spec.name == inner_name for spec in self._specs.values())
        if known:
            return self._find_spec(server, inner_name, category)
        # 真实网关工具名（如 entry_create_entry）不在声明式 catalog 中，
        # 回退到 discovery 使用的同一套名称启发式，未知工具仍保守判 WRITE。
        namespace = server
        for spec in self._specs.values():
            if spec.server == server:
                namespace = spec.qualified_name.split("__")[0]
                break
        return ToolSpec(
            name=inner_name,
            qualified_name=f"{namespace}__{inner_name}",
            server=server,
            permission=PermissionGateway.classify_name(inner_name),
            permission_explicit=True,
            category=category,
        )

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
        """Flatten an MCP result and promote business fields to the top level.

        Two input shapes are supported:

        * Mock/already-unwrapped: ``content[0].text`` is a JSON object whose
          keys are the business payload itself.
        * Real lexiang gateway: ``content[0].text`` carries an envelope
          ``{"code": 0, "message": "success", "data": {...}, "request_id": ...}``
          and ``structuredContent`` carries the same business mapping.

        ``structuredContent`` wins on duplicate keys; the original text is
        still available under ``data["content"]`` for backwards compatibility.
        """
        content = raw.get("content", [])
        texts: list[str] = []
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("text") is not None:
                texts.append(str(item["text"]))
            elif isinstance(item, str):
                texts.append(item)
        joined = "\n".join(texts)
        parsed: dict[str, Any] = {}
        if joined:
            try:
                candidate = json.loads(joined)
                if isinstance(candidate, dict):
                    parsed = candidate
            except (ValueError, TypeError):
                parsed = {}
        data: dict[str, Any] = {"content": joined, "isError": bool(raw.get("isError", False))}
        is_envelope = "code" in parsed and "request_id" in parsed
        if parsed and not is_envelope:
            # Mock path: the text JSON already *is* the business payload.
            data.update(parsed)
        elif is_envelope:
            inner = parsed.get("data")
            if isinstance(inner, dict):
                data.update(inner)
            for key in ("code", "message", "request_id"):
                if key in parsed:
                    data[key] = parsed[key]
        structured = raw.get("structuredContent")
        if isinstance(structured, dict):
            data.update(structured)
        return data
