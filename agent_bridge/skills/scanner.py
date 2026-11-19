"""Recursive discovery of SKILL.md files under configured roots."""

from __future__ import annotations

from pathlib import Path

import structlog

from agent_bridge.skills.models import SkillDoc
from agent_bridge.skills.parser import SkillParseError, SkillParser

logger = structlog.get_logger(__name__)


class SkillScanner:
    """Discover and parse skills; a single broken skill never aborts the scan."""

    def __init__(self, parser: SkillParser | None = None) -> None:
        """Create a scanner with an optional custom parser."""
        self.parser = parser or SkillParser()

    def scan(self, roots: list[Path]) -> list[SkillDoc]:
        """Scan roots recursively and return unique parsed skills.

        Args:
            roots: Directories (or single SKILL.md paths) to scan.

        Returns:
            Skill documents keyed uniquely by manifest name.
        """
        docs: list[SkillDoc] = []
        seen: set[str] = set()
        for root in roots:
            for path in self._find_skill_files(root):
                try:
                    doc = self.parser.parse(path)
                except SkillParseError as exc:
                    logger.warning("skill_skipped", path=str(path), reason=exc.reason, code=exc.error_code)
                    continue
                name = doc.manifest.name
                if name in seen:
                    logger.warning("skill_duplicate_skipped", skill=name, path=str(path))
                    continue
                seen.add(name)
                docs.append(doc)
        return docs

    def _find_skill_files(self, root: Path) -> list[Path]:
        """Return all SKILL.md files under ``root``, sorted deterministically."""
        path = Path(root).expanduser()
        if path.is_file():
            return [path] if path.name == "SKILL.md" else []
        if not path.is_dir():
            logger.warning("skill_root_missing", root=str(path))
            return []
        found = [
            candidate
            for candidate in path.rglob("SKILL.md")
            if not any(part.startswith(".") for part in candidate.relative_to(path).parts)
        ]
        return sorted(found)
