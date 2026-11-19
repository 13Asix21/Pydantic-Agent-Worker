"""Tolerant data models for SKILL.md frontmatter.

Real skills in the wild carry nested extension fields (``metadata.clawdbot``,
``install``, ``homepage``, ``icon`` ...). All unknown fields are preserved in
``SkillManifest.raw_frontmatter`` and never cause a parse failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TOLERANT = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class RequiresSpec(BaseModel):
    """Dependency declaration; unknown sub-fields are retained."""

    model_config = TOLERANT

    bins: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    install: list[dict[str, Any]] = Field(default_factory=list)


class SkillManifest(BaseModel):
    """L1 manifest. Only ``name`` and ``description`` are mandatory."""

    model_config = TOLERANT

    name: str
    description: str
    description_zh: str | None = None
    version: str = "0"
    tags: list[str] = Field(default_factory=list)
    display_name: str | None = None
    visibility: str = "private"
    requires: RequiresSpec = Field(default_factory=RequiresSpec)
    raw_frontmatter: dict[str, Any] = Field(default_factory=dict)
    source_path: Path = Field(default_factory=Path)


class SkillDoc(BaseModel):
    """A parsed skill: manifest (L1), body (L2) and resource/script paths (L3)."""

    model_config = TOLERANT

    manifest: SkillManifest
    body: str = ""
    resources: list[Path] = Field(default_factory=list)
    scripts: list[Path] = Field(default_factory=list)
