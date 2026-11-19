"""Structured trace context shared by runtime, REST and MCP channels."""

from __future__ import annotations

from uuid import uuid4

import structlog
from opentelemetry.trace import get_current_span

from agent_bridge.config.settings import Settings

logger = structlog.get_logger(__name__)


def current_otel_trace_id() -> str:
    """Return the active OpenTelemetry trace id as 32-hex, or ``""`` when no span is active.

    Used to optionally correlate the business envelope with the OTel spans emitted by the official
    ``Instrumentation`` capability (audit §7). Never mutates the four top-level envelope fields.
    """
    span_context = get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return format(span_context.trace_id, "032x")


class TraceContext:
    """Carrier for trace_id/run_id/session_id and unified span fields."""

    def __init__(
        self,
        trace_id: str | None = None,
        run_id: str = "",
        session_id: str = "",
        redact_keys: list[str] | None = None,
    ) -> None:
        """Create a trace context with a generated trace_id when absent."""
        self.trace_id = trace_id or uuid4().hex
        self.run_id = run_id
        self.session_id = session_id
        self.redact_keys = redact_keys or Settings().tracing.redact_keys
        self._extra: dict[str, object] = {}

    @classmethod
    def from_ids(cls, trace_id: str, run_id: str = "", session_id: str = "") -> TraceContext:
        """Build a context from inbound identifiers."""
        return cls(trace_id=trace_id, run_id=run_id, session_id=session_id)

    def child(self, **fields: object) -> TraceContext:
        """Return a derived context carrying extra span fields."""
        derived = TraceContext(self.trace_id, self.run_id, self.session_id, self.redact_keys)
        derived._extra = {**self._extra, **fields}
        return derived

    def as_dict(self) -> dict[str, object]:
        """Return all non-sensitive context fields as a mapping."""
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            **self._extra,
        }

    def bind(self, **fields: object) -> structlog.BoundLogger:
        """Bind the context to a structlog logger."""
        return logger.bind(**self._redact({**self.as_dict(), **fields}))

    def log(self, event: str, level: str = "info", **fields: object) -> None:
        """Emit one structured event with redaction of sensitive keys."""
        payload = self._redact({**self.as_dict(), **fields})
        getattr(logger, level)(event, **payload)

    def _redact(self, payload: dict[str, object]) -> dict[str, object]:
        """Replace values of sensitive keys with ``***``."""
        lowered = {key.lower() for key in self.redact_keys}
        return {key: "***" if str(key).lower() in lowered else value for key, value in payload.items()}
