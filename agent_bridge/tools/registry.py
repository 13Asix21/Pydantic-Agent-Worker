"""Single source of truth for tool names across Skills, MCP and native tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

ToolHandler = Callable[..., Any]

logger = structlog.get_logger(__name__)


class ToolNameConflictError(ValueError):
    """Raised when a tool name is already registered and cannot be upgraded."""

    error_code = "3001"

    def __init__(self, name: str) -> None:
        super().__init__(f"工具名冲突: {name}")
        self.name = name


class ToolNotFoundError(KeyError):
    """Raised when a requested tool is absent from the registry."""

    error_code = "3001"

    def __init__(self, name: str) -> None:
        super().__init__(f"工具不存在: {name}")
        self.name = name


def qualify_tool_name(name: str, category: str) -> str:
    """Upgrade ``<ns>__<tool>`` to ``<ns>__<category>__<tool>`` on conflict.

    Args:
        name: Original qualified tool name.
        category: Conflict-disambiguating category (e.g. ``knowledge``).

    Returns:
        The upgraded tool name. Already-upgraded names are returned unchanged.
    """
    parts = name.split("__")
    if len(parts) >= 3 and parts[1] == category:
        return name
    if len(parts) == 1:
        return f"{category}__{name}"
    return f"{parts[0]}__{category}__{'__'.join(parts[1:])}"


class ToolRegistry:
    """In-memory map of qualified tool names to callable handlers."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._handlers: dict[str, ToolHandler] = {}

    def assert_unique(self, name: str) -> bool:
        """Return ``True`` when ``name`` is not yet registered."""
        return name not in self._handlers

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        category: str | None = None,
        on_conflict: str = "raise",
    ) -> str:
        """Register a handler, optionally upgrading a conflicting name.

        Args:
            name: Qualified tool name.
            handler: Callable implementing the tool.
            category: Category used for ``<ns>__<category>__<tool>`` upgrade.
            on_conflict: ``"raise"`` (default) or ``"upgrade"``.

        Returns:
            The name under which the handler was actually registered.
        """
        if not name:
            raise ValueError("工具名不能为空")
        if self.assert_unique(name):
            self._handlers[name] = handler
            return name
        if on_conflict != "upgrade":
            raise ToolNameConflictError(name)
        if not category:
            raise ToolNameConflictError(name)
        upgraded = qualify_tool_name(name, category)
        suffix = 2
        while not self.assert_unique(upgraded):
            upgraded = f"{qualify_tool_name(name, category)}_{suffix}"
            suffix += 1
        logger.warning("tool_name_upgraded", original=name, upgraded=upgraded)
        self._handlers[upgraded] = handler
        return upgraded

    def get(self, name: str) -> ToolHandler:
        """Return the handler for ``name`` or raise :class:`ToolNotFoundError`."""
        try:
            return self._handlers[name]
        except KeyError:
            raise ToolNotFoundError(name) from None

    def names(self) -> list[str]:
        """Return all registered tool names in stable order."""
        return sorted(self._handlers)
