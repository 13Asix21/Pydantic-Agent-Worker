"""Wire schemas shared by the REST and MCP channels."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunRequest(BaseModel):
    """Inbound agent invocation parameters."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1, max_length=8000)
    session_id: str = "default"
    stream: bool = False
    confirm_writes: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, value: str) -> str:
        """Reject whitespace-only prompts with a 5xxx-class validation error."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("5001: prompt 不能为空")
        return stripped


class RunResponse(BaseModel):
    """Unified ``{code, data, message, trace_id}`` response envelope."""

    model_config = ConfigDict(extra="ignore")

    code: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "ok"
    trace_id: str = ""


class ErrorBody(BaseModel):
    """Unified error envelope (same fields as :class:`RunResponse`)."""

    model_config = ConfigDict(extra="ignore")

    code: int
    data: dict[str, Any] = Field(default_factory=dict)
    message: str
    trace_id: str = ""


class SessionInfo(BaseModel):
    """Session lookup response."""

    model_config = ConfigDict(extra="ignore")

    code: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "ok"
    trace_id: str = ""
