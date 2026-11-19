"""Register the generic MCP call tool plus typed hot tools into ToolRegistry."""

from __future__ import annotations

from typing import Any

from agent_bridge.mcp.models import ToolCallResult, ToolSpec
from agent_bridge.mcp.proxy import McpToolProxy
from agent_bridge.mcp.registry import McpManager
from agent_bridge.tools.registry import ToolRegistry

GENERIC_TOOL = "native__call_mcp_tool"
HOT_TOOL_NAMES = {
    "search_kb_search",
    "search_kb_embedding_search",
    "space_list",
    "entry_list_children",
    "entry_create",
    "entry_describe",
    "block_fetch",
    "list_repos",
}


class McpToolAdapter:
    """Bridge discovered MCP tools into the unified registry."""

    def register(self, registry: ToolRegistry, manager: McpManager) -> None:
        """Register one generic entry for all tools and typed hot paths."""
        proxy = manager.get_proxy()

        def call_mcp_tool(
            server: str,
            tool_name: str,
            arguments: dict[str, Any] | None = None,
            confirm_writes: bool = False,
        ) -> dict[str, Any]:
            """Generic path covering every discovered tool."""
            return proxy.call(server, tool_name, arguments, confirm=confirm_writes).model_dump()

        registry.register(GENERIC_TOOL, call_mcp_tool)
        for spec in manager.all_tools():
            if spec.name not in HOT_TOOL_NAMES:
                continue
            registry.register(spec.qualified_name, self._typed_handler(proxy, spec))

    @staticmethod
    def _typed_handler(proxy: McpToolProxy, spec: ToolSpec):
        """Build a typed handler whose closure captures the right server/tool."""

        def handler(
            arguments: dict[str, Any] | None = None,
            confirm_writes: bool = False,
        ) -> dict[str, Any]:
            result: ToolCallResult = proxy.call(spec.server, spec.name, arguments, confirm=confirm_writes)
            return result.model_dump()

        handler.__name__ = f"mcp_{spec.qualified_name.replace('__', '_')}"
        return handler
