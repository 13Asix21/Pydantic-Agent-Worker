"""G3: two MCP servers coexist with unique namespaces and failure isolation."""

from __future__ import annotations

import json

import pytest

from agent_bridge.config.settings import PROJECT_ROOT, Settings
from agent_bridge.mcp.adapter import GENERIC_TOOL, McpToolAdapter
from agent_bridge.mcp.registry import McpManager
from agent_bridge.mcp.transport import McpTransport
from agent_bridge.tools.registry import ToolNameConflictError, ToolRegistry

SPACE_ID = "c3f157b3d03d4c7fa46a8fb8e70b1ae7"


def _catalog() -> list[dict]:
    """Minimal mock tools/list payload for lexiang (fallback catalog handles the rest)."""
    return []


def _github_catalog() -> list[dict]:
    """Minimal GitHub tool list for G3 conflict checks."""
    return [
        {"name": "list_repos", "description": "列出仓库", "inputSchema": {"type": "object"}},
        {
            "name": "get_file_contents",
            "description": "读取文件",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
        },
    ]


def _responder(name: str, args: dict):
    """Simple responder shared by both mock servers."""
    return {
        "content": [{"type": "text", "text": json.dumps({"tool": name, "args": args})}],
        "isError": False,
    }


def _transports() -> dict[str, McpTransport]:
    """Two independent mock transports, one per server."""
    return {
        "lexiang": McpTransport.mock(catalog=_catalog(), responder=_responder, name="lexiang", namespace="lexiang"),
        "github": McpTransport.mock(catalog=_github_catalog(), responder=_responder, name="github", namespace="github"),
    }


@pytest.fixture()
def manager(settings: Settings) -> McpManager:
    """Manager with both servers started."""
    instance = McpManager()
    instance.startup(settings, _transports())
    return instance


def test_two_servers_coexist_without_conflicts(manager: McpManager) -> None:
    """lexiang__* 与 github__* 在同一 registry 中共存且唯一。"""
    names = [spec.qualified_name for spec in manager.all_tools()]
    assert len(names) == len(set(names))
    assert any(name.startswith("lexiang__") for name in names)
    assert any(name.startswith("github__") for name in names)
    assert "github__list_repos" in names
    assert "lexiang__search_kb_search" in names
    assert manager.health("lexiang") is True
    assert manager.health("github") is True
    assert manager.unhealthy_servers() == []


def test_all_tools_register_in_unified_registry(manager: McpManager, tool_registry: ToolRegistry) -> None:
    """所有工具可无冲突注册到统一 ToolRegistry。"""
    for spec in manager.all_tools():
        tool_registry.register(spec.qualified_name, lambda **kwargs: kwargs)
    assert len(tool_registry.names()) == len(manager.all_tools())
    assert tool_registry.assert_unique("lexiang__search_kb_search") is False


def test_adapter_registers_generic_and_hot_tools(manager: McpManager, tool_registry: ToolRegistry) -> None:
    """通用 call_mcp_tool + 热点 typed 工具都走同一个 proxy。"""
    McpToolAdapter().register(tool_registry, manager)
    names = set(tool_registry.names())
    assert GENERIC_TOOL in names
    assert {
        "lexiang__search_kb_search",
        "lexiang__knowledge__entry_create",
        "lexiang__knowledge__space_list",
        "github__list_repos",
    } <= names
    result = tool_registry.get(GENERIC_TOOL)("github", "list_repos", {})
    assert result["ok"] is True
    assert "list_repos" in result["data"]["content"]


def test_name_conflict_escalates_to_three_levels(tool_registry: ToolRegistry) -> None:
    """撞名升级为 <ns>__<category>__<tool>。"""
    tool_registry.register("lexiang__entry_create", lambda: "a")
    upgraded = tool_registry.register("lexiang__entry_create", lambda: "b", category="knowledge", on_conflict="upgrade")
    assert upgraded == "lexiang__knowledge__entry_create"
    assert tool_registry.get(upgraded)() == "b"
    with pytest.raises(ToolNameConflictError):
        tool_registry.register("lexiang__entry_create", lambda: "c")


def test_single_server_failure_is_isolated(settings: Settings) -> None:
    """一个 server 连接失败不影响另一个 server 的工具与调用。"""

    class BrokenTransport(McpTransport):
        def connect(self) -> None:
            raise RuntimeError("github endpoint unreachable")

    transports = _transports()
    transports["github"] = BrokenTransport(transports["github"].config)
    manager = McpManager()
    manager.startup(settings, transports)
    assert manager.health("lexiang") is True
    assert manager.health("github") is False
    assert "github" in manager.unhealthy_servers()
    assert all(not spec.qualified_name.startswith("github__") for spec in manager.all_tools())
    assert any(spec.qualified_name.startswith("lexiang__") for spec in manager.all_tools())
    result = manager.get_proxy().call("lexiang", "search_kb_search", {"query": "ok"})
    assert result.ok is True
    unavailable = manager.get_proxy().call("github", "list_repos", {})
    assert unavailable.error_code == "3001"


def test_config_declares_two_namespaces() -> None:
    """配置文件明确声明两个 server 及命名空间前缀。"""
    config_text = (PROJECT_ROOT / "config" / "mcp_servers.yaml").read_text(encoding="utf-8")
    assert "namespace: lexiang" in config_text
    assert "namespace: github" in config_text
