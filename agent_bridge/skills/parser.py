"""YAML frontmatter + Markdown body parser for SKILL.md files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from agent_bridge.skills.models import RequiresSpec, SkillDoc, SkillManifest

logger = structlog.get_logger(__name__)

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<fm>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class SkillParseError(ValueError):
    """Raised when a SKILL.md cannot be parsed or lacks mandatory fields."""

    error_code = "2001"

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"SKILL.md 解析失败 ({path}): {reason}")
        self.path = Path(path)
        self.reason = reason


class SkillParser:
    """Parse SKILL.md into :class:`SkillDoc`, tolerating extension fields."""

    def parse(self, path: Path) -> SkillDoc:
        """Parse a single SKILL.md file.

        Args:
            path: Path to a SKILL.md file.

        Returns:
            Parsed skill document with raw frontmatter preserved.

        Raises:
            SkillParseError: Missing file, invalid YAML, or missing required fields.
        """
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillParseError(path, f"无法读取文件: {exc}") from exc
        fm, body = self._parse_frontmatter(text)
        self._validate(fm)
        metadata = fm.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        clawdbot = metadata.get("clawdbot")
        clawdbot = clawdbot if isinstance(clawdbot, dict) else {}
        requires_raw = clawdbot.get("requires")
        requires_raw = requires_raw if isinstance(requires_raw, dict) else {}
        try:
            requires = RequiresSpec(**requires_raw)
        except (TypeError, ValueError, ValidationError) as exc:
            raise SkillParseError(path, f"requires 字段非法: {exc}") from exc
        manifest = SkillManifest(
            name=str(fm["name"]),
            description=str(fm["description"]),
            description_zh=fm.get("description_zh"),
            version=str(fm.get("version", "0")),
            tags=self._parse_tags(path, fm.get("tags")),
            display_name=fm.get("display_name"),
            visibility=str(fm.get("visibility", "private")),
            requires=requires,
            raw_frontmatter=fm,  # 保留全部未知字段，绝不因未知字段报错
            source_path=path,
        )
        doc = SkillDoc(manifest=manifest, body=body)
        doc.resources, doc.scripts = self._collect_assets(path.parent)
        logger.debug("skill_parsed", skill=manifest.name, source=str(path))
        return doc

    def _parse_tags(self, path: Path, raw: Any) -> list[str]:
        """Normalise tags; a string becomes one tag instead of char-splitting."""
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, (list, tuple, set)):
            return [str(tag) for tag in raw]
        raise SkillParseError(path, f"tags 字段非法（需为字符串或列表）: {raw!r}")

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        """Split frontmatter from body and safe-load the YAML mapping."""
        match = FRONTMATTER_RE.match(text)
        if match is None:
            return {}, text.strip()
        try:
            loaded = yaml.safe_load(match.group("fm"))
        except yaml.YAMLError as exc:
            raise SkillParseError("<text>", f"YAML 语法错误: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise SkillParseError("<text>", "frontmatter 顶层必须是映射")
        return loaded, text[match.end() :].strip()

    def _validate(self, frontmatter: dict[str, Any]) -> None:
        """Require only ``name`` and ``description``; everything else is optional."""
        missing = [key for key in ("name", "description") if not frontmatter.get(key)]
        if missing:
            raise SkillParseError("<text>", f"缺少必填字段: {', '.join(missing)}")

    def _collect_assets(self, skill_dir: Path) -> tuple[list[Path], list[Path]]:
        """Index L3 resources and scripts without reading their contents."""
        if not skill_dir.is_dir():
            return [], []
        files = sorted(p for p in skill_dir.rglob("*") if p.is_file())
        scripts = [p for p in files if p.suffix.lower() in {".py", ".sh"}]
        resources = [p for p in files if p not in scripts and p.name != "SKILL.md"]
        return resources, scripts
