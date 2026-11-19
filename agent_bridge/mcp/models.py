"""Shared data models for MCP servers, discovered tools and unified results."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Permission(StrEnum):
    """Coarse classification of a tool's side effects."""

    READ = "READ"
    WRITE = "WRITE"


class McpServerConfig(BaseModel):
    """One MCP server definition (loaded from ``config/mcp_servers.yaml``)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str
    transport: str = "mock"
    endpoint: str = ""
    auth_env_key: str | None = None
    timeout_s: int = 30
    max_retries: int = 2
    failure_threshold: int = 3
    cooldown_s: int = 30
    catalog_path: str | None = None
    catalog_inline: list[dict[str, Any]] = Field(default_factory=list)
    write_tools: list[str] = Field(default_factory=list)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    enabled: bool = True

    def token(self) -> str:
        """Read the credential from ENV only; never store it in config files."""
        if not self.auth_env_key:
            return ""
        return os.environ.get(self.auth_env_key, "").strip()


class ToolSpec(BaseModel):
    """One discovered MCP tool."""

    model_config = ConfigDict(extra="ignore")

    name: str
    qualified_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    server: str
    permission: Permission = Permission.WRITE
    # True 表示该权限由 discovery 显式算出（含 write_tools 覆盖），网关必须采信。
    permission_explicit: bool = False
    category: str | None = None


class ToolCallResult(BaseModel):
    """Unified tool result used by sandbox, proxy and gateway."""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str = "0"
    message: str = ""
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    latency_ms: int = 0
    dry_run: bool = False

    def envelope(self) -> dict[str, Any]:
        """Return the REST/MCP wire envelope ``{code, data, message, trace_id}``."""
        code: int | str = int(self.error_code) if self.error_code.isdigit() else self.error_code
        return {
            "code": code,
            "data": self.data,
            "message": self.message,
            "trace_id": self.trace_id,
        }


def resolve_project_path(path: str | Path) -> Path:
    """Resolve a config-relative path against the project root."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return Path(__file__).resolve().parents[2] / candidate


__all__ = [
    "McpServerConfig",
    "Permission",
    "SecretStr",
    "ToolCallResult",
    "ToolSpec",
    "resolve_project_path",
]
