"""G2: generic MCP proxy chain (discovery -> proxy -> gateway) and live TC-LX-01~08."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
import yaml

from agent_bridge.config.settings import PROJECT_ROOT, Settings
from agent_bridge.mcp.discovery import ToolDiscovery
from agent_bridge.mcp.gateway import PermissionGateway
from agent_bridge.mcp.models import McpServerConfig, Permission, ToolSpec
from agent_bridge.mcp.proxy import McpToolProxy
from agent_bridge.mcp.registry import McpManager
from agent_bridge.mcp.transport import McpTransport

SPACE_ID = "c3f157b3d03d4c7fa46a8fb8e70b1ae7"
ROOT_ENTRY_ID = "571e43a904274b4c9aaa5f7c48e05d77"
RUN_LIVE = os.getenv("RUN_LIVE", "0") == "1" and bool(os.getenv("AGENT_BRIDGE_LEXIANG_TOKEN", ""))
live = pytest.mark.skipif(not RUN_LIVE, reason="需要 RUN_LIVE=1 与 AGENT_BRIDGE_LEXIANG_TOKEN")


def _catalog() -> dict[str, Any]:
    """Load the declarative lexiang catalog from disk."""
    path = PROJECT_ROOT / "config" / "lexiang_tools.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _text_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a dict as an MCP text content result."""
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}


def _mock_responder(store: dict[str, Any]):
    """Build a deterministic fake lexiang MCP backend."""

    def responder(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        store["calls"].append((tool_name, args))
        if tool_name == "search_kb_search":
            return _text_payload(
                {
                    "results": [
                        {
                            "title": "Agent Bridge 设计",
                            "space_id": SPACE_ID,
                            "content": args.get("query", ""),
                        }
                    ]
                }
            )
        if tool_name == "search_kb_embedding_search":
            return _text_payload({"results": [{"title": "语义命中", "score": 0.91}]})
        if tool_name == "space_list":
            return _text_payload({"spaces": [{"space_id": SPACE_ID, "name": "个人知识库"}]})
        if tool_name == "entry_list_children":
            return _text_payload({"entries": [{"entry_id": ROOT_ENTRY_ID, "children": []}]})
        if tool_name == "entry_create":
            entry_id = f"entry-{uuid.uuid4().hex[:8]}"
            entry = {
                "entry_id": entry_id,
                "title": args.get("title", ""),
                "parent_id": args.get("parent_id", ""),
            }
            store["entries"][entry_id] = entry
            return _text_payload(entry)
        if tool_name == "entry_describe":
            return _text_payload(store["entries"].get(args.get("entry_id", ""), {}))
        if tool_name == "block_fetch":
            return _text_payload({"blocks": [{"entry_id": args.get("entry_id", ""), "text": "正文"}]})
        return _text_payload({"tool": tool_name, "args": args})

    return responder


@pytest.fixture()
def mock_store() -> dict[str, Any]:
    """Mutable state of the fake MCP backend."""
    return {"entries": {}, "calls": []}


@pytest.fixture()
def lexiang_transport(mock_store: dict[str, Any]) -> McpTransport:
    """Mock transport with an empty tools/list, exercising catalog fallback."""
    transport = McpTransport.mock(responder=_mock_responder(mock_store), name="lexiang", namespace="lexiang")
    transport.connect()
    return transport


@pytest.fixture()
def manager(settings: Settings, lexiang_transport: McpTransport) -> McpManager:
    """Started manager with the mock lexiang transport."""
    instance = McpManager()
    instance.startup(settings, {"lexiang": lexiang_transport})
    return instance


def _catalog_specs() -> list[Any]:
    """Build ToolSpecs from the declarative catalog via the fallback path."""
    config = McpServerConfig(name="lexiang", namespace="lexiang", catalog_path="config/lexiang_tools.yaml")
    return ToolDiscovery()._fallback_catalog(config)


def test_catalog_declares_over_90_tools() -> None:
    """声明式目录覆盖 10+ 类、至少 90 个工具。"""
    specs = _catalog_specs()
    assert len(specs) >= 90
    names = {spec.qualified_name for spec in specs}
    expected = {
        "lexiang__search_kb_search",
        "lexiang__search_kb_embedding_search",
        "lexiang__knowledge__space_list",
        "lexiang__knowledge__entry_list_children",
        "lexiang__knowledge__entry_create",
        "lexiang__knowledge__entry_describe",
        "lexiang__knowledge__block_fetch",
    }
    assert expected <= names
    assert len(names) == len(specs)  # 目录内无同名冲突


def test_discovery_from_tools_list() -> None:
    """tools/list 路径生成 <ns>__<tool>，并正确分类读写权限。"""
    config = McpServerConfig(name="lexiang", namespace="lexiang", transport="mock")
    transport = McpTransport.mock(
        catalog=[
            {"name": "search_kb_search", "description": "检索", "inputSchema": {"type": "object"}},
            {"name": "entry_create", "description": "创建", "inputSchema": {"type": "object"}},
        ]
    )
    specs = ToolDiscovery().discover(transport, config)
    assert [spec.qualified_name for spec in specs] == [
        "lexiang__search_kb_search",
        "lexiang__entry_create",
    ]
    assert specs[0].permission == Permission.READ
    assert specs[1].permission == Permission.WRITE
    assert specs[0].input_schema == {"type": "object"}


def test_gateway_classification() -> None:
    """读写前缀分类与默认 WRITE 保守策略。"""
    assert PermissionGateway.classify_name("search_kb_search") == Permission.READ
    assert PermissionGateway.classify_name("list_repos") == Permission.READ
    assert PermissionGateway.classify_name("fetch_page") == Permission.READ
    assert PermissionGateway.classify_name("entry_create") == Permission.WRITE
    assert PermissionGateway.classify_name("unknown_tool") == Permission.WRITE


def test_gateway_trusts_explicit_write_override(manager: McpManager, lexiang_transport: McpTransport) -> None:
    """QA-P1：write_tools 覆盖写入权限后，即使名字像 READ 也必须拦截。"""
    proxy = manager.get_proxy()
    spec = proxy._find_spec("lexiang", "search_kb_search", None)
    assert isinstance(spec, ToolSpec)
    spec.permission = Permission.WRITE
    spec.permission_explicit = True
    before = lexiang_transport.call_count
    result = proxy.call("lexiang", "search_kb_search", {"query": "override"})
    assert result.error_code == "3003"
    assert result.dry_run is True
    assert lexiang_transport.call_count == before


def test_config_write_tools_override_blocks_by_default() -> None:
    """QA 复现 A：config write_tools 显式覆盖 READ 名称，默认 dry_run 必须拦截。"""
    config = McpServerConfig(
        name="lexiang",
        namespace="lexiang",
        transport="mock",
        write_tools=["search_kb_search"],
    )
    transport = McpTransport.mock(catalog=[{"name": "search_kb_search", "description": "检索"}])
    transport.connect()
    specs = ToolDiscovery().discover(transport, config)
    assert specs[0].permission == Permission.WRITE
    assert specs[0].permission_explicit is True
    index = {spec.qualified_name: spec for spec in specs}
    dry_proxy = McpToolProxy({"lexiang": transport}, index, settings=Settings())
    blocked = dry_proxy.call("lexiang", "search_kb_search", {"query": "x"})
    assert blocked.error_code == "3003" and blocked.dry_run is True
    assert transport.call_count == 0
    # 双开关放行：allow_writes=true 且 confirm_writes=true 仍可真实调用
    write_proxy = McpToolProxy({"lexiang": transport}, index, settings=Settings(allow_writes=True))
    allowed = write_proxy.call("lexiang", "search_kb_search", {"query": "x"}, confirm=True)
    assert allowed.error_code == "0" and allowed.dry_run is False
    assert transport.call_count == 1


def test_write_verb_in_read_prefixed_name_is_blocked(manager: McpManager, lexiang_transport: McpTransport) -> None:
    """QA-P1：list_delete_all_entries 含写动词，默认 dry_run 不得执行。"""
    assert PermissionGateway.classify_name("list_delete_all_entries") == Permission.WRITE
    assert PermissionGateway.classify_name("search_kb_search", ["search_kb_search"]) == Permission.WRITE
    before = lexiang_transport.call_count
    result = manager.get_proxy().call("lexiang", "list_delete_all_entries", {})
    assert result.error_code == "3003"
    assert result.dry_run is True
    assert lexiang_transport.call_count == before


def test_proxy_read_call(manager: McpManager) -> None:
    """通用 proxy 走通 READ 工具并返回统一结构。"""
    result = manager.get_proxy().call("lexiang", "search_kb_search", {"query": "Agent Bridge"})
    assert result.ok is True
    assert result.error_code == "0"
    assert result.dry_run is False
    assert "Agent Bridge" in result.data["content"]
    assert result.trace_id


def test_write_dry_run_by_default(manager: McpManager, lexiang_transport: McpTransport) -> None:
    """默认 dry_run：WRITE 工具返回 3003 且不触达 MCP server。"""
    before = lexiang_transport.call_count
    result = manager.get_proxy().call(
        "lexiang", "entry_create", {"title": "[POC-TEST] dry", "parent_id": ROOT_ENTRY_ID}
    )
    assert result.dry_run is True
    assert result.error_code == "3003"
    assert result.data["would_call"] == "lexiang__knowledge__entry_create"
    assert lexiang_transport.call_count == before


def test_confirmed_write_executes(lexiang_transport: McpTransport, mock_store: dict[str, Any]) -> None:
    """allow_writes + confirm_writes 双开关才真正写入。"""
    write_settings = Settings(allow_writes=True)
    manager = McpManager()
    manager.startup(write_settings, {"lexiang": lexiang_transport})
    proxy = manager.get_proxy()
    title = f"[POC-TEST]-{uuid.uuid4().hex[:8]}"
    created = proxy.call("lexiang", "entry_create", {"title": title, "parent_id": ROOT_ENTRY_ID}, confirm=True)
    assert created.ok is True and created.dry_run is False
    assert created.data["title"] == title
    assert lexiang_transport.call_count == 1
    described = proxy.call("lexiang", "entry_describe", {"entry_id": created.data["entry_id"]})
    assert described.data["title"] == title
    assert len(mock_store["entries"]) == 1


def test_transport_retries_transient_failure(manager: McpManager, lexiang_transport: McpTransport) -> None:
    """指数退避重连后调用成功。"""
    lexiang_transport.fail_next(1)
    result = manager.get_proxy().call("lexiang", "search_kb_search", {"query": "retry"})
    assert result.ok is True
    assert lexiang_transport.call_count == 1


def test_circuit_breaker_isolates_failure(manager: McpManager, lexiang_transport: McpTransport) -> None:
    """连续失败后熔断，恢复前不再调用 MCP server。"""
    lexiang_transport.simulate_failure = True
    proxy = manager.get_proxy()
    baseline = lexiang_transport.attempt_count  # 启动时 tools/list 的尝试次数
    for _ in range(3):
        failed = proxy.call("lexiang", "search_kb_search", {"query": "down"})
        assert failed.ok is False and failed.error_code == "3002"
    assert lexiang_transport.attempt_count - baseline == 9  # 3 次调用 × 每次 3 次尝试
    lexiang_transport.simulate_failure = False
    blocked = proxy.call("lexiang", "search_kb_search", {"query": "still-down"})
    assert blocked.error_code == "3002"
    assert lexiang_transport.attempt_count - baseline == 9  # 熔断期间无新调用


def test_unknown_server_returns_3001(manager: McpManager) -> None:
    """未连接的 server 返回 3001，不影响其它 server。"""
    result = manager.get_proxy().call("no-such-server", "search_kb_search", {})
    assert result.error_code == "3001"
    assert manager.get_proxy().call("lexiang", "search_kb_search", {}).ok is True


# ===== 真实乐享用例：RUN_LIVE=1 且注入凭证才执行 =====


@pytest.fixture(scope="module")
def live_proxies():
    """Build dry_run and write-enabled proxies against the real lexiang endpoint."""
    endpoint = os.getenv("AGENT_BRIDGE_LEXIANG_ENDPOINT", "")
    if not endpoint:
        pytest.skip("缺少 AGENT_BRIDGE_LEXIANG_ENDPOINT")
    config = McpServerConfig(
        name="lexiang",
        namespace="lexiang",
        transport="streamable_http",
        endpoint=endpoint,
        auth_env_key="AGENT_BRIDGE_LEXIANG_TOKEN",
    )
    transport = McpTransport.create(config)
    transport.connect()
    specs = ToolDiscovery().discover(transport, config)
    manager = McpManager()
    manager._transports["lexiang"] = transport
    manager._tools_by_server["lexiang"] = specs
    manager._resolve_conflicts()
    index = {spec.qualified_name: spec for spec in manager.all_tools()}
    dry_proxy = McpToolProxy({"lexiang": transport}, index, settings=Settings())
    write_proxy = McpToolProxy({"lexiang": transport}, index, settings=Settings(allow_writes=True))
    yield dry_proxy, write_proxy
    transport.close()


def _live_call(
    proxy: McpToolProxy,
    tool_name: str,
    args: dict[str, Any],
    *,
    confirm: bool = False,
):
    """Forward a business tool call through the gateway's ``call_tool`` meta-tool."""
    return proxy.call(
        "lexiang",
        "call_tool",
        {"tool_name": tool_name, "arguments": args},
        confirm=confirm,
    )


@pytest.mark.live
@live
def test_tc_lx_01_search(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-01：关键词检索命中个人 KB。"""
    result = _live_call(live_proxies[0], "search_kb_search", {"keyword": "Agent Bridge", "space_id": SPACE_ID})
    assert result.ok is True, result.message
    assert "docs" in result.data
    assert SPACE_ID in json.dumps(result.data, ensure_ascii=False)


@pytest.mark.live
@live
def test_tc_lx_02_embedding_search(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-02：向量检索返回带 score 的结果。"""
    result = _live_call(
        live_proxies[0],
        "search_kb_embedding_search",
        {"filters": {"keyword": "知识库设计"}, "limit": 5, "space_id": SPACE_ID},
    )
    assert result.ok is True, result.message
    assert "chunks" in result.data
    assert "score" in json.dumps(result.data, ensure_ascii=False)


@pytest.mark.live
@live
def test_tc_lx_03_space_list(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-03：个人 KB 在最近访问的空间列表中。"""
    result = _live_call(live_proxies[0], "space_list_recently_spaces", {})
    assert result.ok is True, result.message
    assert "spaces" in result.data
    assert SPACE_ID in json.dumps(result.data, ensure_ascii=False)


@pytest.mark.live
@live
def test_tc_lx_04_entry_list_children(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-04：根条目子列表返回。"""
    result = _live_call(
        live_proxies[0], "entry_list_children", {"parent_id": ROOT_ENTRY_ID, "space_id": SPACE_ID}
    )
    assert result.ok is True, result.message
    assert "entries" in result.data


@pytest.mark.live
@live
def test_tc_lx_05_to_07_entry_write_and_cleanup(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-05~07：dry_run 不写入 -> confirm 写入 -> 读回 -> teardown 删除。"""
    dry_proxy, write_proxy = live_proxies
    title = f"[POC-TEST]-{uuid.uuid4().hex}"
    create_args = {"entry_type": "page", "parent_entry_id": ROOT_ENTRY_ID, "name": title}
    dry = _live_call(dry_proxy, "entry_create_entry", create_args)
    assert dry.dry_run is True and dry.error_code == "3003"
    # dry_run 信封会回显 args 供审计；是否真正写入以下方 KB 列表为准。
    assert dry.data.get("would_call", "").endswith("entry_create_entry")
    before = _live_call(
        dry_proxy, "entry_list_children", {"parent_id": ROOT_ENTRY_ID, "space_id": SPACE_ID}
    )
    assert title not in json.dumps(before.data, ensure_ascii=False)
    created = _live_call(write_proxy, "entry_create_entry", create_args, confirm=True)
    assert created.ok is True, created.message
    entry_payload = created.data.get("entry") or created.data
    entry_id = str(entry_payload.get("id") or entry_payload.get("entry_id") or "")
    assert entry_id, created.data
    try:
        described = _live_call(dry_proxy, "entry_describe_entry", {"entry_id": entry_id})
        assert described.ok is True, described.message
        assert (described.data.get("entry") or {}).get("name") == title
        block = _live_call(dry_proxy, "block_fetch_page", {"entry_id": entry_id})
        assert block.ok is True, block.message
    finally:
        deleted = _live_call(write_proxy, "entry_delete_entry", {"entry_id": entry_id}, confirm=True)
        assert deleted.ok is True, f"teardown 失败，请手工清理 {entry_id}: {deleted.message}"
    after = _live_call(dry_proxy, "entry_list_children", {"parent_id": ROOT_ENTRY_ID, "space_id": SPACE_ID})
    assert title not in json.dumps(after.data, ensure_ascii=False)


@pytest.mark.live
@live
def test_tc_lx_08_default_dry_run_no_change(live_proxies: tuple[McpToolProxy, McpToolProxy]) -> None:
    """TC-LX-08：默认 dry_run 调用 WRITE 工具不产生实际变更。"""
    dry_proxy = live_proxies[0]
    result = _live_call(
        dry_proxy,
        "entry_rename_entry",
        {"entry_id": ROOT_ENTRY_ID, "name": "[POC-TEST]-should-not-exist"},
    )
    assert result.dry_run is True and result.error_code == "3003"
    after = _live_call(dry_proxy, "entry_list_children", {"parent_id": ROOT_ENTRY_ID, "space_id": SPACE_ID})
    assert "[POC-TEST]-should-not-exist" not in json.dumps(after.data, ensure_ascii=False)
