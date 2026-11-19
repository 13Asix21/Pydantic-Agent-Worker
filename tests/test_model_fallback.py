"""官方 ModelFallback + RetryOnModelErrors 主备链实证测试。

替换 CREDENTIALS.md 中的手工主备约定（deepseek → qwen → glm）。
所有断言均基于 pydantic-ai 2.70.0 真实 capability 行为，非 mock 替身。
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.capabilities import ModelFallback, RetryOnModelErrors
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from agent_bridge.config.settings import Settings
from agent_bridge.runtime.agent import AgentRuntime


def test_retry_then_fallback() -> None:
    """瞬时错误先在同一模型重试到上限，再切到备模型。"""
    calls: list[str] = []

    def primary(messages: object, info: object) -> ModelResponse:
        calls.append("primary")
        raise ModelRateLimitError("primary", "rate limited")

    def backup(messages: object, info: object) -> ModelResponse:
        calls.append("backup")
        return ModelResponse(parts=[TextPart("backup-ok")])

    agent = Agent(
        FunctionModel(primary, model_name="primary"),
        capabilities=[
            RetryOnModelErrors(max_attempts=3, initial_delay=0.001, max_delay=0.01),
            ModelFallback(FunctionModel(backup, model_name="backup")),
        ],
    )
    result = agent.run_sync("hi")
    assert calls == ["primary", "primary", "primary", "backup"]
    assert str(result.output) == "backup-ok"


def test_mock_provider_keeps_zero_capabilities() -> None:
    """mock provider 默认行为不变：零 capability、确定性响应。"""
    settings = Settings()
    assert settings.llm.provider == "mock"
    runtime = AgentRuntime(settings=settings)
    assert runtime._build_capabilities() == []
    response = runtime.run("hello")
    assert response.code == 0
    assert response.data["mocked"] is True


def test_openai_compatible_injects_official_capabilities() -> None:
    """配置了 fallback_models 时注入官方主备链与重试 capability。"""
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    settings.llm.base_url = "https://example.invalid/v1"
    settings.llm.api_key = SecretStr("sk-test")
    settings.llm.fallback_models = ["qwen3.8-27b", "glm-5.3-flash"]

    runtime = AgentRuntime(settings=settings)
    caps = runtime._build_capabilities()
    assert len(caps) == 2
    assert sum(isinstance(c, ModelFallback) for c in caps) == 1
    assert sum(isinstance(c, RetryOnModelErrors) for c in caps) == 1
