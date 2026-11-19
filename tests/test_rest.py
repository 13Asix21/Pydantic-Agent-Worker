"""G4-REST: synchronous run, SSE stream, validation errors and sessions."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_bridge.server.app import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """One app instance for the REST suite."""
    with TestClient(create_app()) as instance:
        yield instance


def test_health(client: TestClient) -> None:
    """health 保持 T0 的统一响应且不回归。"""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["llm_provider"] == "mock"


def test_sync_run_returns_unified_envelope(client: TestClient) -> None:
    """POST /agent/run 返回 code=0 且带 trace_id 与 session 历史。"""
    response = client.post("/agent/run", json={"prompt": "你好，Agent Bridge", "session_id": "rest-sync"})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["trace_id"]
    assert "Agent Bridge mock" in body["data"]["text"]
    assert body["data"]["session_id"] == "rest-sync"
    assert client.get("/sessions/rest-sync").json()["data"]["message_count"] >= 2


def test_sync_run_dispatches_registered_tool(client: TestClient) -> None:
    """mock LLM 的 tool_call 经 PydanticAI 自动派发到 native__web_search。"""
    response = client.post(
        "/agent/run",
        json={
            "prompt": '调用 native__web_search 参数:{"query":"sse-test"}',
            "session_id": "rest-tool",
        },
    )
    body = response.json()
    assert body["code"] == 0
    assert "native__web_search" in body["data"]["tool_calls"]
    assert "Mock result for sse-test" in body["data"]["text"]


def test_sse_stream_has_incremental_frames_and_done(client: TestClient) -> None:
    """SSE 逐块推送 data:{...}，并以 data:[DONE] 收尾。"""
    response = client.post(
        "/agent/run/stream",
        json={"prompt": "这是一段用于验证流式分块输出的较长提示词" * 3, "session_id": "rest-sse"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    frames = [line for line in response.text.splitlines() if line.startswith("data:")]
    assert len(frames) >= 4
    assert frames[-1] == "data: [DONE]"
    payloads = [json.loads(frame[len("data: ") :]) for frame in frames[:-1]]
    assert all(item["code"] == 0 for item in payloads)
    assert all(item["data"]["delta"] for item in payloads)
    assert "".join(item["data"]["delta"] for item in payloads).startswith("Agent Bridge mock")


def test_validation_error_uses_5xxx(client: TestClient) -> None:
    """空 prompt 走 5001 统一错误响应。"""
    response = client.post("/agent/run", json={"prompt": "   ", "session_id": "bad"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 5001
    assert body["trace_id"]
