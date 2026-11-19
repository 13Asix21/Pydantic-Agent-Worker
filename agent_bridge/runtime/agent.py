"""PydanticAI runtime: one agent over Skills, MCP and native tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

import anyio
from opentelemetry import trace as otel_trace
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import (
    AgentCapability,
    Caching,
    Instrumentation,
    ModelFallback,
    RetryOnModelErrors,
    Toolset,
    WebFetch,
    WebSearch,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets.function import FunctionToolset

from agent_bridge.config.settings import Settings
from agent_bridge.mcp.registry import McpManager
from agent_bridge.runtime.session import SessionStore
from agent_bridge.runtime.tracing import TraceContext, current_otel_trace_id
from agent_bridge.server.schemas import RunResponse
from agent_bridge.skills.registry import SkillRegistry
from agent_bridge.tools.registry import ToolRegistry

TOOL_CALL_RE = re.compile(r"调用\s+([A-Za-z0-9_]+)\s*(?:参数\s*[:：]\s*(\{.*\}))?")
_CURRENT_CONFIRM: ContextVar[bool] = ContextVar("agent_bridge_confirm_writes", default=False)
_CURRENT_CALLS: ContextVar[list[str] | None] = ContextVar("agent_bridge_tool_calls", default=None)


def _last_user_prompt(messages: list[ModelMessage]) -> str:
    """Extract the most recent user prompt text from a PydanticAI message list."""
    for message in reversed(messages):
        for part in reversed(getattr(message, "parts", [])):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


def _has_tool_return(messages: list[ModelMessage]) -> bool:
    """Whether a tool result has already been fed back to the model."""
    return any(isinstance(part, ToolReturnPart) for message in messages for part in getattr(message, "parts", []))


def _default_tool_args(tool_name: str, prompt: str) -> dict[str, Any]:
    """Sensible default arguments for the deterministic mock model."""
    if "web_search" in tool_name:
        return {"query": prompt}
    if "load_skill" in tool_name:
        match = re.search(r"([\w-]+)", prompt)
        return {"name": match.group(1) if match else "mini-skill"}
    if "http_request" in tool_name:
        return {"method": "GET", "url": "https://example.invalid"}
    return {}


def _mock_response(messages: list[ModelMessage], _info: Any) -> ModelResponse:
    """Deterministic model: obey ``调用 <tool> 参数:{json}`` to trigger real dispatch."""
    prompt = _last_user_prompt(messages)
    match = TOOL_CALL_RE.search(prompt)
    if match and not _has_tool_return(messages):
        name = match.group(1)
        raw_args = match.group(2)
        try:
            args = json.loads(raw_args) if raw_args else _default_tool_args(name, prompt)
        except ValueError:
            args = _default_tool_args(name, prompt)
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])
    if _has_tool_return(messages):
        returns = [
            part for message in messages for part in getattr(message, "parts", []) if isinstance(part, ToolReturnPart)
        ]
        return ModelResponse(parts=[TextPart(f"已完成：{returns[-1].content}")])
    return ModelResponse(parts=[TextPart(f"Agent Bridge mock 响应：{prompt}")])


async def _mock_stream(messages: list[ModelMessage], _info: Any) -> AsyncIterator[str]:
    """Deterministic token stream for SSE (chunked, no network)."""
    prompt = _last_user_prompt(messages)
    text = f"Agent Bridge mock 流式响应：{prompt}"
    for start in range(0, len(text), 8):
        yield text[start : start + 8]
        await anyio.sleep(0)


def _run_coroutine(coro: Any) -> Any:
    """Run a coroutine from a sync caller (threadpool or plain sync context)."""
    try:
        return anyio.from_thread.run(coro)
    except RuntimeError:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError("异步上下文中请直接 await run_async()，不要调用同步 run()") from None


class AgentRuntime:
    """Build and run a PydanticAI agent over the unified ToolRegistry."""

    def __init__(
        self,
        settings: Settings | None = None,
        tools: ToolRegistry | None = None,
        skills: SkillRegistry | None = None,
        sessions: SessionStore | None = None,
        mcp_manager: McpManager | None = None,
        instrumentation_settings: InstrumentationSettings | None = None,
    ) -> None:
        """Assemble the runtime; the agent is built immediately from current registries.

        ``instrumentation_settings`` is an optional injection point for wiring an OTel
        ``TracerProvider`` (e.g. an in-memory exporter in tests); production leaves it ``None``.
        """
        self.settings = settings or Settings()
        self.tools = tools or ToolRegistry()
        self.skills = skills or SkillRegistry()
        self.sessions = sessions or SessionStore()
        self.mcp_manager = mcp_manager
        self.instrumentation_settings = instrumentation_settings
        self.trace = TraceContext(redact_keys=self.settings.tracing.redact_keys)
        self.agent: Agent[None] = self._build_agent()

    def _build_model(self) -> FunctionModel | OpenAIChatModel:
        """Select the deterministic mock or an OpenAI-compatible chat model."""
        provider = self.settings.llm.provider.lower()
        if provider == "mock":
            return FunctionModel(function=_mock_response, stream_function=_mock_stream)
        if provider == "openai_compatible":
            api_key = self.settings.llm.api_key.get_secret_value()
            if not self.settings.llm.base_url or not api_key:
                raise ValueError("1001: openai_compatible 需要 base_url 与 ENV 注入的 api_key")
            return OpenAIChatModel(
                self.settings.llm.model,
                provider=OpenAIProvider(base_url=self.settings.llm.base_url, api_key=api_key),
            )
        raise ValueError(f"1001: 未知 LLM provider: {provider}")

    def _build_capabilities(self, tools: list[Tool] | None = None) -> list[AgentCapability]:
        """Assemble the P0 official capability stack.

        Each appended entry is one independently removable concern (rollback granularity = one
        list item). Composition order is decided by each capability's ``get_ordering()`` —
        ``Instrumentation`` is outermost, ``ModelFallback``/``RetryOnModelErrors`` innermost —
        so the order below is logical, not semantic.

        When ``tools`` is ``None`` only the fallback-chain capabilities are returned (the
        fallback-only test view); ``_build_agent`` passes the registered tools to get the full
        stack. Production-only concerns (fallback chain, native web tools) are gated behind
        ``provider=openai_compatible`` so the deterministic mock path needs neither a real key
        nor network access.
        """
        caps: list[AgentCapability] = []
        llm = self.settings.llm
        production = llm.provider.lower() == "openai_compatible"

        # 关注点 1：官方主备链 + 瞬时错误重试（innermost）。仅生产 provider 注入。
        if production and llm.fallback_models:
            caps.append(ModelFallback(*llm.fallback_models, sticky=True))
            caps.append(RetryOnModelErrors(max_attempts=llm.retry_max_attempts))

        if tools is None:
            return caps

        tracing = self.settings.tracing
        # 关注点 2：OTel/Logfire span 交给官方 Instrumentation（outermost）。
        if tracing.instrumentation:
            inst_settings = self.instrumentation_settings or InstrumentationSettings()
            caps.append(Instrumentation(settings=inst_settings))

        # 关注点 4：统一 prompt caching（写入 ModelSettings.cache）。
        caps.append(Caching())

        # 关注点 3：官方 Toolset 注册。工具名在 ToolRegistry 注册阶段已完成规范化
        # （qualify_tool_name 冲突升级 + assert_unique），此处仅做官方注册，不再二次加前缀，
        # 以免破坏统一信封与 REST/MCP 工具名契约。
        caps.append(Toolset(FunctionToolset(tools)))

        # 关注点 5：官方原生 web 能力，默认关闭。
        # WebSearch(native=True)/WebFetch(native=True) 仅 OpenAIResponsesModel 支持；
        # OpenAIChatModel（deepseek 等网关渠道）会在建图时抛 5002 not supported。
        # 由 native_web_search / native_web_fetch 显式开启，供确认兼容的高级用户使用。
        # 联网默认仍走自研 native__web_search / native__http_request 工具。
        if production:
            if self.settings.tools.native_web_search:
                caps.append(WebSearch(native=True))
            if self.settings.tools.native_web_fetch:
                caps.append(WebFetch(native=True))
        return caps

    def _build_agent(self) -> Agent[None]:
        """Create the agent with L1 manifest, MCP catalog summary and all tools."""
        # G1 三个真实样本优先进入 L1，其余技能按字母序补足，避免技能量大时被截断。
        manifest_lines = self.skills.manifest_prompt(top_k=max(1, len(self.skills.names()))).splitlines()
        header = manifest_lines[0] if manifest_lines else "可用技能："
        entries = manifest_lines[1:]
        g1_names = ("obsidian", "football-bayes", "github-trending-cn")
        priority = [entry for name in g1_names for entry in entries if entry.startswith(f"- {name}:")]
        remaining = [entry for entry in entries if entry not in priority][:30]
        system_prompt = [
            "你是 Agent Bridge PoC 助手。所有外部能力必须通过注册工具调用，禁止臆造工具结果。",
            "\n".join([header, *priority, *remaining]),
        ]
        if self.mcp_manager is not None:
            names = [spec.qualified_name for spec in self.mcp_manager.all_tools()][:60]
            system_prompt.append("可用 MCP 工具（节选）：" + ", ".join(names))
        tools = [
            Tool(self._make_tool(name), takes_ctx=False, name=name, description=f"统一工具: {name}")
            for name in self.tools.names()
        ]
        return Agent(
            model=self._build_model(),
            system_prompt=system_prompt,
            capabilities=self._build_capabilities(tools),
        )

    def _make_tool(self, name: str):
        """Wrap one registry handler as a dynamically typed PydanticAI tool."""
        handler = self.tools.get(name)

        def tool(**kwargs: Any) -> str:
            """Invoke a unified-registry tool and serialise its result."""
            calls = _CURRENT_CALLS.get([])
            if calls is not None:
                calls.append(name)
            arguments = dict(kwargs)
            if "confirm_writes" not in arguments and _CURRENT_CONFIRM.get():
                arguments["confirm_writes"] = True
            try:
                value = handler(**arguments)
            except TypeError:
                value = handler(arguments) if arguments else handler()
            if asyncio.iscoroutine(value):
                value = _run_coroutine(value)
            self.trace.log(
                "agent_tool_call",
                tool_name=name,
                dry_run=bool(arguments.get("confirm_writes") is not True),
                code=0,
            )
            return json.dumps(value, ensure_ascii=False, default=str)

        tool.__name__ = f"ab_{name.replace('__', '_')}"
        tool.__doc__ = f"调用 Agent Bridge 统一工具 {name}。"
        return tool

    def run(self, prompt: str, session_id: str = "default", confirm_writes: bool = False) -> RunResponse:
        """Synchronous convenience wrapper around :meth:`run_async`."""
        return _run_coroutine(self.run_async(prompt, session_id, confirm_writes))

    async def run_async(self, prompt: str, session_id: str = "default", confirm_writes: bool = False) -> RunResponse:
        """Run one agent turn, replaying and persisting session history."""
        started = time.perf_counter()
        trace = self.trace.child(session_id=session_id, tool_name="agent_run")
        calls: list[str] = []
        confirm_token = _CURRENT_CONFIRM.set(bool(confirm_writes))
        calls_token = _CURRENT_CALLS.set(calls)
        if self.instrumentation_settings:
            tracer = self.instrumentation_settings.tracer
        else:
            tracer = otel_trace.get_tracer("agent_bridge")
        otel_id = ""
        try:
            history = self.sessions.get(session_id)
            # 父 span 让官方 Instrumentation 的 run/model/tool span 继承同一 trace context；
            # 无 tracer provider 时是 no-op，不影响确定性 mock 行为。
            with tracer.start_as_current_span("agent_bridge.run"):
                result = await self.agent.run(prompt, message_history=history)
                if self.settings.tracing.otel_trace_id_in_envelope:
                    otel_id = current_otel_trace_id()
            self.sessions.put(session_id, result.all_messages())
            data = {
                "text": str(result.output),
                "tool_calls": list(calls),
                "session_id": session_id,
                "mocked": self.settings.llm.provider == "mock",
            }
            if otel_id:
                data["otel_trace_id"] = otel_id
            trace.log("agent_run", code=0, latency_ms=int((time.perf_counter() - started) * 1000))
            return RunResponse(code=0, data=data, message="ok", trace_id=trace.trace_id)
        except Exception as exc:
            trace.log(
                "agent_run_failed",
                level="error",
                code=5002,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
            )
            return RunResponse(
                code=5002,
                data={"tool_calls": calls, "session_id": session_id},
                message=f"内部错误: {exc}",
                trace_id=trace.trace_id,
            )
        finally:
            _CURRENT_CALLS.reset(calls_token)
            _CURRENT_CONFIRM.reset(confirm_token)

    async def stream(
        self, prompt: str, session_id: str = "default", confirm_writes: bool = False
    ) -> AsyncIterator[str]:
        """Yield incremental text chunks for SSE, then persist session history."""
        confirm_token = _CURRENT_CONFIRM.set(bool(confirm_writes))
        calls_token = _CURRENT_CALLS.set([])
        try:
            if not self.settings.llm.stream:
                response = await self.run_async(prompt, session_id, confirm_writes)
                yield str(response.data.get("text", ""))
                return
            history = self.sessions.get(session_id)
            async with self.agent.run_stream(prompt, message_history=history) as result:
                async for chunk in result.stream_text(delta=True, debounce_by=None):
                    if chunk:
                        yield chunk
                self.sessions.put(session_id, result.all_messages())
            self.trace.log("agent_stream", session_id=session_id, code=0)
        except Exception as exc:
            self.trace.log("agent_stream_failed", level="error", code=5002, error=str(exc))
            yield json.dumps(
                {"code": 5002, "data": {}, "message": f"内部错误: {exc}", "trace_id": self.trace.trace_id},
                ensure_ascii=False,
            )
        finally:
            _CURRENT_CALLS.reset(calls_token)
            _CURRENT_CONFIRM.reset(confirm_token)
