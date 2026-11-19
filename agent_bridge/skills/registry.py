"""Skill index with L1 manifest prompt, L2 body and L3 asset access."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import structlog

from agent_bridge.skills.models import SkillDoc

logger = structlog.get_logger(__name__)


class SkillError(ValueError):
    """Error raised by skill registry operations."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class SkillRegistry:
    """In-memory skill index and progressive-disclosure entry points."""

    def __init__(self, docs: list[SkillDoc] | None = None) -> None:
        """Create an empty registry or index a list of documents."""
        self._index: dict[str, SkillDoc] = {}
        for doc in docs or []:
            self.register(doc)

    def register(self, doc: SkillDoc) -> None:
        """Index one parsed skill document."""
        self._index[doc.manifest.name] = doc

    def get(self, name: str) -> SkillDoc:
        """Return a skill document or raise ``2002``."""
        doc = self._index.get(name)
        if doc is None:
            raise SkillError("2002", f"未找到 skill: {name}")
        return doc

    def names(self) -> list[str]:
        """Return all indexed skill names in stable order."""
        return sorted(self._index)

    def manifest_prompt(self, top_k: int = 20) -> str:
        """Build the L1 system-prompt fragment (name + description only).

        Args:
            top_k: Maximum number of skills to include.

        Returns:
            Human-readable manifest listing for the LLM system prompt.
        """
        lines = ["可用技能（用 skill__load_skill 按需加载正文）："]
        for name in self.names()[:top_k]:
            manifest = self._index[name].manifest
            description = manifest.description_zh or manifest.description
            lines.append(f"- {name}: {description}")
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """L2: return the full SKILL.md body of a skill."""
        return self.get(name).body

    def resource(self, name: str, path: str) -> str:
        """L3: read a text resource inside a skill directory."""
        target = self.resolve_inside_skill(name, path)
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError("2002", f"资源读取失败: {path} ({exc})") from exc

    def script_path(self, name: str, path: str) -> Path:
        """L3: resolve a script path inside a skill directory."""
        return self.resolve_inside_skill(name, path)

    def resolve_inside_skill(self, name: str, relative: str | Path) -> Path:
        """Resolve ``relative`` inside the skill root, rejecting traversal.

        Raises:
            SkillError: ``4001`` for traversal, ``2002`` for missing target.
        """
        doc = self.get(name)
        root = doc.manifest.source_path.parent.resolve()
        candidate_path = Path(relative)
        if candidate_path.is_absolute():
            raise SkillError("4001", f"禁止绝对路径: {relative}")
        target = (root / candidate_path).resolve()
        if target != root and root not in target.parents:
            raise SkillError("4001", f"路径穿越被拒绝: {relative}")
        if not target.exists():
            raise SkillError("2002", f"资源不存在: {relative}")
        return target

    def check_requires(self, name: str) -> list[str]:
        """Return missing binary/env requirements, never raising.

        ``obsidian-cli`` is absent on the PoC host; callers receive a list such
        as ``["obsidian-cli"]`` and can degrade gracefully.
        """
        try:
            doc = self.get(name)
        except SkillError:
            return []
        missing: list[str] = []
        requires = doc.manifest.requires
        for binary in requires.bins:
            if shutil.which(binary) is None:
                missing.append(binary)
        for env_name in requires.env:
            if not os.environ.get(env_name, "").strip():
                missing.append(f"ENV:{env_name}")
        if missing:
            logger.info("skill_requires_missing", skill=name, missing=missing)
        return missing
