"""Phase-0 Gate G1–G4 happy paths, fully offline (mock LLM + mock transports)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from agent_bridge.server.app import create_app
from agent_bridge.server.mcp_server import create_mcp_server


@pytest.fixture(scope="module")
def app():
    """One assembled application shared by all gate checks."""
    return create_app()


@pytest.fixture(scope="module")
def client(app) -> TestClient:
    """TestClient bound to the assembled application."""
    with TestClient(app) as instance:
        yield instance


def _run(client: TestClient, prompt: str, session_id: str) -> dict:
    """POST /agent/run and return the unified envelope."""
    response = client.post("/agent/run", json={"prompt": prompt, "session_id": session_id})
    assert response.status_code == 200
    return response.json()


def test_gate_g1_skills_manifest_and_load_skill(app, client: TestClient) -> None:
    """G1：L1 manifest 注入 system prompt，L2 load_skill 可由 LLM 派发调用。"""
    manifest = app.state.runtime.skills.manifest_prompt(top_k=len(app.state.runtime.skills.names()))
    assert "obsidian" in manifest and "football-bayes" in manifest
    body = _run(
        client,
        '调用 skill__load_skill 参数:{"name":"obsidian"}',
        "e2e-g1",
    )
    assert body["code"] == 0
    assert "skill__load_skill" in body["data"]["tool_calls"]
    assert "Obsidian" in body["data"]["text"]


def test_gate_g2_generic_mcp_proxy_dry_run(app, client: TestClient) -> None:
    """G2：通用 call_mcp_tool 经 proxy/gateway 调用乐享，写入默认 dry_run 不落库。"""
    transport = app.state.mcp_manager._transports["lexiang"]
    before = transport.call_count
    body = _run(
        client,
        '调用 native__call_mcp_tool 参数:{"server":"lexiang","tool_name":"entry_create",'
        '"arguments":{"title":"[POC-TEST]-e2e","parent_id":"571e43a904274b4c9aaa5f7c48e05d77"}}',
        "e2e-g2",
    )
    assert body["code"] == 0
    assert "native__call_mcp_tool" in body["data"]["tool_calls"]
    assert "未实际写入" in body["data"]["text"]
    assert transport.call_count == before


def test_gate_g3_two_servers_coexist(app) -> None:
    """G3：乐享与 GitHub 工具共存于同一注册表且无命名冲突。"""
    specs = app.state.mcp_manager.all_tools()
    assert any(spec.qualified_name.startswith("lexiang__") for spec in specs)
    assert any(spec.qualified_name.startswith("github__") for spec in specs)
    names = app.state.runtime.tools.names()
    assert len(names) == len(set(names))
    assert app.state.mcp_manager.health("lexiang") is True
    assert app.state.mcp_manager.health("github") is True


def test_gate_g4_rest_sync_and_sse(app, client: TestClient) -> None:
    """G4-REST：同步返回 code=0，SSE 逐块输出并以 [DONE] 收尾。"""
    sync_body = _run(client, "端到端同步调用", "e2e-g4-sync")
    assert sync_body["code"] == 0
    assert sync_body["trace_id"]
    response = client.post(
        "/agent/run/stream",
        json={"prompt": "端到端流式输出验证" * 4, "session_id": "e2e-g4-sse"},
    )
    frames = [line for line in response.text.splitlines() if line.startswith("data:")]
    assert len(frames) >= 4
    assert frames[-1] == "data: [DONE]"
    deltas = [json.loads(frame[len("data: ") :])["data"]["delta"] for frame in frames[:-1]]
    assert "".join(deltas).startswith("Agent Bridge mock")


@pytest.mark.asyncio
async def test_gate_g4_mcp_server_channel(app) -> None:
    """G4-MCP：外部 agent 经 MCP tools/list + call_tool 调同一运行时。"""
    server = create_mcp_server(app.state.runtime)
    async with create_connected_server_and_client_session(server) as mcp_client:
        assert isinstance(mcp_client, ClientSession)
        listed = await mcp_client.list_tools()
        assert "agent_run" in [tool.name for tool in listed.tools]
        called = await mcp_client.call_tool("agent_run", {"prompt": "端到端 MCP 调用", "session_id": "e2e-g4-mcp"})
        payload = json.loads(called.content[0].text)
        assert payload["code"] == 0
        assert payload["trace_id"]
        assert "端到端 MCP 调用" in payload["data"]["text"]
