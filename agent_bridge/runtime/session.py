"""In-memory session history for multi-turn PoC conversations."""

from __future__ import annotations

import time
from typing import Any


class SessionStore:
    """Bounded, TTL-aware store of PydanticAI message histories by session_id."""

    def __init__(self, max_turns: int = 20, ttl_s: int = 3600) -> None:
        """Create a store with a per-session message cap and idle TTL."""
        self.max_turns = max(1, max_turns)
        self.ttl_s = max(1, ttl_s)
        self._sessions: dict[str, tuple[float, list[Any]]] = {}

    def get(self, session_id: str) -> list[Any]:
        """Return a copy of the session history, empty when absent/expired."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return []
        updated_at, messages = entry
        if time.monotonic() - updated_at > self.ttl_s:
            self._sessions.pop(session_id, None)
            return []
        return list(messages)

    def put(self, session_id: str, messages: list[Any]) -> None:
        """Store (and truncate) a message history for a session."""
        limit = self.max_turns * 2
        self._sessions[session_id] = (time.monotonic(), list(messages[-limit:]))

    def append(self, session_id: str, message: Any) -> None:
        """Append one message to a session history."""
        history = self.get(session_id)
        history.append(message)
        self.put(session_id, history)

    def clear(self, session_id: str) -> None:
        """Drop one session; missing sessions are a no-op."""
        self._sessions.pop(session_id, None)

    def ids(self) -> list[str]:
        """Return all live session ids."""
        return sorted(self._sessions)

    def __len__(self) -> int:
        """Return the number of live sessions."""
        return len(self._sessions)
