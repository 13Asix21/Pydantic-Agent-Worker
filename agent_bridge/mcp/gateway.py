"""Permission gateway: READ executes, WRITE is intercepted by default dry_run."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import structlog

from agent_bridge.mcp.models import Permission, ToolCallResult, ToolSpec

logger = structlog.get_logger(__name__)

Executor = Callable[[ToolSpec, dict[str, Any]], ToolCallResult]


class PermissionGateway:
    """Classify tools and gate write operations behind dry_run + confirmation."""

    READ_PREFIXES = ("search_", "list_", "describe_", "fetch_", "get_", "download_")
    # 真实乐享工具名多为 "<对象>_<动词>"（entry_describe / block_fetch /
    # entry_list_children），因此同样识别动词出现在名称后半段的情况。
    READ_INFIXES = ("_search", "_list", "_describe", "_fetch", "_get", "_download")
    # 网关元工具 whoami 只读（返回身份与 personal_space），但名字不含任何 READ 前缀，
    # 必须显式纳入 READ，否则默认 WRITE 会被 dry_run 拦截。
    READ_EXACT = frozenset({"whoami", "who_am_i"})
    WRITE_PREFIXES = (
        "create_",
        "update_",
        "rename",
        "move",
        "delete_",
        "commit_",
        "import_",
        "apply_",
        "upload_",
    )
    # 任何位置出现写动词都判 WRITE，防止 list_delete_all_entries 这类
    # “READ 前缀 + 写动词”的工具名绕过默认拦截。
    WRITE_TOKENS = frozenset(
        {
            "create",
            "update",
            "rename",
            "move",
            "delete",
            "commit",
            "import",
            "apply",
            "upload",
            "set",
            "add",
            "remove",
            "clear",
            "drop",
            "purge",
            "write",
        }
    )

    def __init__(self, dry_run: bool = True, executor: Executor | None = None) -> None:
        """Create a gateway; dry_run defaults to ``True`` for PoC safety."""
        self.dry_run = dry_run
        self._executor = executor

    @classmethod
    def classify_name(cls, name: str, write_tools: list[str] | None = None) -> Permission:
        """Classify a tool name; unknown names default to WRITE (conservative)."""
        if write_tools and name in write_tools:
            return Permission.WRITE
        lowered = name.lower()
        if lowered in cls.READ_EXACT:
            return Permission.READ
        tokens = set(re.split(r"[^a-z0-9]+", lowered))
        if tokens & cls.WRITE_TOKENS:
            return Permission.WRITE
        if any(lowered.startswith(prefix) or lowered == prefix.rstrip("_") for prefix in cls.READ_PREFIXES):
            return Permission.READ
        if any(infix in lowered for infix in cls.READ_INFIXES) and not any(
            lowered.startswith(prefix) for prefix in cls.WRITE_PREFIXES
        ):
            return Permission.READ
        if any(lowered.startswith(prefix) or lowered == prefix.rstrip("_") for prefix in cls.WRITE_PREFIXES):
            return Permission.WRITE
        return Permission.WRITE

    def classify(self, spec: ToolSpec) -> Permission:
        """Trust discovery's explicit permission, fall back to name heuristics."""
        if spec.permission_explicit:
            return spec.permission
        return self.classify_name(spec.name)

    def check(self, spec: ToolSpec, args: dict[str, Any], confirm: bool = False) -> ToolCallResult:
        """Gate one call.

        Args:
            spec: Tool to call.
            args: Tool arguments.
            confirm: Caller's ``confirm_writes`` flag.

        Returns:
            Executed result for READ / confirmed WRITE, or a simulated
            ``dry_run=True`` result with code ``3003`` for intercepted writes.
        """
        permission = self.classify(spec)
        if permission == Permission.READ:
            return self._execute(spec, args)
        if self.dry_run or not confirm:
            payload = {"would_call": spec.qualified_name, "args": args}
            logger.info(
                "write_intercepted",
                tool_name=spec.qualified_name,
                server=spec.server,
                dry_run=True,
                code="3003",
            )
            return ToolCallResult(
                ok=True,
                dry_run=True,
                data=payload,
                error_code="3003",
                message="dry_run: 未实际写入",
            )
        return self._execute(spec, args)

    def _execute(self, spec: ToolSpec, args: dict[str, Any]) -> ToolCallResult:
        """Delegate the actual call to the injected executor."""
        if self._executor is None:
            return ToolCallResult(
                ok=False,
                error_code="3002",
                message="MCP 执行器不可用",
            )
        started = time.perf_counter()
        result = self._executor(spec, args)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result


__all__ = ["PermissionGateway"]
