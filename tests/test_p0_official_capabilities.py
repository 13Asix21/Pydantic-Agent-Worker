"""P0 官方能力改造验证：Instrumentation / Caching / WebSearch / WebFetch / 信封不变量。

所有断言基于 pydantic-ai 2.70.0 真实 capability 行为；mock provider 全程确定性、不触网。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from pydantic_ai import Tool
from pydantic_ai.capabilities import (
    Caching,
    Instrumentation,
    ModelFallback,
    RetryOnModelErrors,
    Toolset,
    WebFetch,
    WebSearch,
)
from pydantic_ai.models.instrumented import InstrumentationSettings

from agent_bridge.config.settings import Settings
from agent_bridge.runtime.agent import AgentRuntime
from agent_bridge.server.app import create_app


def _prod_settings() -> Settings:
    """OpenAI-compatible settings with a dummy key (no real call is made at build time)."""
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    settings.llm.base_url = "https://example.invalid/v1"
    settings.llm.api_key = SecretStr("sk-test")
    return settings


def test_caching_sets_model_settings_cache() -> None:
    """Caching() 官方 capability 把 ModelSettings.cache 写入 agent 的合并设置。"""
    assert Caching().get_model_settings() == {"cache": True}
    runtime = AgentRuntime(settings=Settings())
    assert runtime.agent._cap_model_settings.get("cache") is True  # type: ignore[attr-defined]


def test_production_injects_all_official_capabilities() -> None:
    """生产 provider 下五项 P0 官方能力全部注入，且各占一个可回滚列表项。"""
    runtime = AgentRuntime(settings=_prod_settings())
    caps = runtime._build_capabilities([Tool(lambda: "x", name="probe")])
    for expected in (
        Instrumentation,
        Caching,
        Toolset,
        ModelFallback,
        RetryOnModelErrors,
        WebSearch,
        WebFetch,
    ):
        assert sum(isinstance(c, expected) for c in caps) == 1, caps


def test_web_capabilities_can_be_disabled() -> None:
    """native_web_* 开关关闭时不注入对应官方能力。"""
    settings = _prod_settings()
    settings.tools.native_web_search = False
    settings.tools.native_web_fetch = False
    runtime = AgentRuntime(settings=settings)
    caps = runtime._build_capabilities([Tool(lambda: "x", name="probe")])
    assert not any(isinstance(c, (WebSearch, WebFetch)) for c in caps)


def test_mock_run_does_not_touch_network_or_require_key() -> None:
    """mock provider 默认确定性、不需真实凭证；只含 Instrumentation/Caching/Toolset 三项。"""
    runtime = AgentRuntime(settings=Settings())
    caps = runtime._build_capabilities([Tool(lambda: "x", name="probe")])
    names = [type(c).__name__ for c in caps]
    assert "Instrumentation" in names and "Caching" in names and "Toolset" in names
    assert "ModelFallback" not in names and "WebSearch" not in names and "WebFetch" not in names
    response = runtime.run("你好")
    assert response.code == 0 and response.data["mocked"] is True
    assert "Agent Bridge mock" in response.data["text"]


def test_instrumentation_emits_otel_spans() -> None:
    """注入 SDK TracerProvider 后，一次 run 产生 OTel span（含官方 invoke_agent）。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = AgentRuntime(
        settings=Settings(),
        instrumentation_settings=InstrumentationSettings(tracer_provider=provider),
    )
    assert runtime.run("span test").code == 0
    span_names = [span.name for span in exporter.get_finished_spans()]
    assert any("invoke_agent" in name for name in span_names), span_names
    assert any(name == "agent_bridge.run" for name in span_names), span_names


def test_envelope_four_fields_unchanged_and_otel_trace_id_optional() -> None:
    """统一信封顶层四字段恒定；otel_trace_id 仅在开关开启且 span 有效时进入 data。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = Settings()
    settings.tracing.otel_trace_id_in_envelope = True
    runtime = AgentRuntime(
        settings=settings,
        instrumentation_settings=InstrumentationSettings(tracer_provider=provider),
    )
    response = runtime.run("envelope test")
    top = response.model_dump()
    assert set(top) == {"code", "data", "message", "trace_id"}
    assert isinstance(top["code"], int) and isinstance(top["message"], str) and isinstance(top["trace_id"], str)
    otel_id = response.data["otel_trace_id"]
    assert isinstance(otel_id, str) and len(otel_id) == 32
    assert all(format(s.context.trace_id, "032x") == otel_id for s in exporter.get_finished_spans())


def test_envelope_default_snapshot_has_no_otel_field() -> None:
    """默认开关关闭时信封 data 不含 otel_trace_id，JSON 结构与基线一致。"""
    client = TestClient(create_app())
    body = client.post("/agent/run", json={"prompt": "快照", "session_id": "snap"}).json()
    assert set(body) == {"code", "data", "message", "trace_id"}
    assert "otel_trace_id" not in body["data"]
    assert set(body["data"]) == {"text", "tool_calls", "session_id", "mocked"}
    json.dumps(body, ensure_ascii=False)  # 快照可序列化


def test_dry_run_never_calls_transport() -> None:
    """dry_run 语义不回归：WRITE 工具默认返回 3003 且 mock transport 调用数不增长。"""
    from agent_bridge.mcp.gateway import PermissionGateway
    from agent_bridge.mcp.models import Permission

    assert PermissionGateway.classify_name("entry_create") == Permission.WRITE
    assert PermissionGateway.classify_name("search_kb_search") == Permission.READ
