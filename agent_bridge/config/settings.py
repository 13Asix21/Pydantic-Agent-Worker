"""Strongly typed settings with YAML defaults and ENV overrides.

Static configuration lives in ``config/*.yaml``; all credentials and switches
come from ``AGENT_BRIDGE_*`` environment variables. A YAML source deliberately
drops credential-looking keys so secrets can never be committed accidentally.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR: Path = PROJECT_ROOT / "config"
CREDENTIAL_HINTS: tuple[str, ...] = ("token", "api_key", "apikey", "secret", "password", "credential")


class ServerSettings(BaseModel):
    """HTTP server bind settings."""

    host: str = "127.0.0.1"
    port: int = 8000


class LLMSettings(BaseModel):
    """LLM model settings. API key is ENV-only; provider defaults to deterministic mock."""

    provider: str = "mock"  # mock | openai_compatible
    base_url: str = ""
    model: str = "gpt-4o-mini"
    stream: bool = True
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    # 官方 ModelFallback 主备链：agent 自带 model 为首选，以下为按序候选（sticky 保 prompt cache）。
    fallback_models: list[str] = Field(default_factory=lambda: ["qwen3.8-27b", "glm-5.3-flash"])
    retry_max_attempts: int = 3  # 官方 RetryOnModelErrors 同模型最大尝试次数（含首次）


class ToolsSettings(BaseModel):
    """Native HTTP/web tool policy; real network access is opt-in."""

    enable_real_http: bool = False
    timeout_s: int = 10
    max_retries: int = 2
    # 官方 WebSearch/WebFetch(native=True) 能力开关（仅生产 provider 注入；mock 全程不触网）。
    native_web_search: bool = True
    native_web_fetch: bool = True


class SkillsSettings(BaseModel):
    """Skill discovery roots."""

    roots: list[str] = Field(default_factory=lambda: ["C:/Users/Administrator/.workbuddy/skills"])


class SandboxSettings(BaseModel):
    """Script sandbox policy."""

    allowed_interpreters: list[str] = Field(default_factory=lambda: ["python", "python3"])
    allowed_suffixes: list[str] = Field(default_factory=lambda: [".py"])
    default_timeout_s: int = 30
    max_timeout_s: int = 120
    audit_log: str = "logs/sandbox-audit.jsonl"


class TracingSettings(BaseModel):
    """Structured logging / trace configuration."""

    level: str = "INFO"
    fields: list[str] = Field(
        default_factory=lambda: [
            "trace_id",
            "run_id",
            "session_id",
            "tool_name",
            "server",
            "skill",
            "latency_ms",
            "dry_run",
            "code",
        ]
    )
    redact_keys: list[str] = Field(
        default_factory=lambda: ["token", "api_key", "authorization", "prompt", "password", "secret"]
    )
    # 官方 Instrumentation(OTel/Logfire span) 开关。
    instrumentation: bool = True
    # 是否在信封 data 内可选携带 otel_trace_id（顶层四字段始终不变）；默认关闭以维持快照。
    otel_trace_id_in_envelope: bool = False


class McpSettings(BaseModel):
    """MCP transport global policy."""

    default_timeout_s: int = 30
    max_retries: int = 2
    failure_threshold: int = 3
    cooldown_s: int = 30
    catalog_path: str = "config/lexiang_tools.yaml"


class TestSettings(BaseModel):
    """Known-good personal knowledge base identifiers used by live tests."""

    lexiang_space_id: str = "c3f157b3d03d4c7fa46a8fb8e70b1ae7"
    lexiang_root_entry_id: str = "571e43a904274b4c9aaa5f7c48e05d77"


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Read ``config/settings.yaml`` as the lowest-priority settings source."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Return one field value from YAML, if present."""
        value = self._data.get(field_name)
        return value, field_name, value is not None

    def __call__(self) -> dict[str, Any]:
        """Return all YAML-provided values."""
        return dict(self._data)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping, returning an empty dict on missing/invalid files."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _strip_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """Remove credential-looking keys from YAML data (defence in depth)."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if any(hint in str(key).lower() for hint in CREDENTIAL_HINTS):
            continue
        if isinstance(value, dict):
            value = _strip_credentials(value)
        cleaned[key] = value
    return cleaned


def config_dir() -> Path:
    """Resolve the config directory, honouring ``AGENT_BRIDGE_CONFIG_DIR``."""
    override = os.getenv("AGENT_BRIDGE_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


class Settings(BaseSettings):
    """Application settings: YAML defaults overlaid by ``AGENT_BRIDGE_*`` ENV."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_BRIDGE_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        validate_assignment=True,
    )

    config_path: ClassVar[Path] = DEFAULT_CONFIG_DIR / "settings.yaml"

    version: int = 1
    server: ServerSettings = Field(default_factory=ServerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    allow_writes: bool = False
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    test: TestSettings = Field(default_factory=TestSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order sources so ENV wins over YAML defaults."""
        yaml_data = _strip_credentials(_load_yaml(config_dir() / "settings.yaml"))
        return (init_settings, env_settings, YamlSettingsSource(settings_cls, yaml_data), dotenv_settings)

    @property
    def dry_run(self) -> bool:
        """PoC default: writes are simulated unless explicitly allowed."""
        return not self.allow_writes
