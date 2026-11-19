"""Register Skills capabilities into the unified ToolRegistry."""

from __future__ import annotations

from typing import Any

from agent_bridge.skills.registry import SkillRegistry
from agent_bridge.skills.sandbox import ScriptSandbox
from agent_bridge.tools.registry import ToolRegistry


class SkillToolAdapter:
    """Thin bridge between :class:`SkillRegistry`/sandbox and ToolRegistry."""

    def __init__(self, skills: SkillRegistry, sandbox: ScriptSandbox | None = None) -> None:
        """Create an adapter over a skill registry and optional sandbox."""
        self.skills = skills
        self.sandbox = sandbox or ScriptSandbox()

    def register(self, registry: ToolRegistry) -> None:
        """Register L2/L3 skill tools with reserved ``skill__`` namespace."""
        registry.register("skill__load_skill", self.load_skill)
        registry.register("skill__read_resource", self.read_resource)
        registry.register("skill__run_script", self.run_script)
        # compat shim: 设计时序图使用的旧名称，不改原 SKILL.md
        registry.register("skill__run_skill_script", self.run_script)

    def load_skill(self, name: str) -> str:
        """L2 tool: load a skill body into the agent context."""
        return self.skills.load(name)

    def read_resource(self, name: str, path: str) -> str:
        """L3 tool: read a text resource bundled with a skill."""
        return self.skills.resource(name, path)

    def run_script(
        self,
        name: str,
        script: str,
        args: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """L3 tool: execute a skill script in the sandbox."""
        doc = self.skills.get(name)
        result = self.sandbox.run(doc, script, args, timeout)
        return result.model_dump()
