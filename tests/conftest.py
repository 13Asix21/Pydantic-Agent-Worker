"""Shared pytest fixtures for Agent Bridge PoC tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_bridge.config.settings import Settings
from agent_bridge.skills.parser import SkillParser
from agent_bridge.skills.registry import SkillRegistry
from agent_bridge.skills.sandbox import ScriptSandbox
from agent_bridge.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"
REAL_SKILLS_ROOT = Path("C:/Users/Administrator/.workbuddy/skills")


@pytest.fixture()
def project_root() -> Path:
    """Project root path."""
    return PROJECT_ROOT


@pytest.fixture()
def settings() -> Settings:
    """Default application settings."""
    return Settings()


@pytest.fixture()
def mini_skill_path() -> Path:
    """Path of the minimal fixture SKILL.md."""
    return FIXTURE_ROOT / "mini_skill" / "SKILL.md"


@pytest.fixture()
def parser() -> SkillParser:
    """A fresh skill parser."""
    return SkillParser()


@pytest.fixture()
def skill_registry(mini_skill_path: Path, parser: SkillParser) -> SkillRegistry:
    """Registry holding the minimal fixture skill."""
    return SkillRegistry([parser.parse(mini_skill_path)])


@pytest.fixture()
def sandbox(settings: Settings) -> ScriptSandbox:
    """Sandbox using project settings."""
    return ScriptSandbox(settings.sandbox)


@pytest.fixture()
def tool_registry() -> ToolRegistry:
    """Empty unified tool registry."""
    return ToolRegistry()


@pytest.fixture()
def real_skill_dirs() -> list[Path]:
    """The three real skills used for G1 validation."""
    root = Path(os.getenv("AGENT_BRIDGE_SKILLS_ROOTS", str(REAL_SKILLS_ROOT))).expanduser()
    names = ("obsidian", "football-bayes", "github-trending-cn")
    return [root / name for name in names]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers programmatically as well as in pyproject."""
    config.addinivalue_line("markers", "live: 需要真实凭证与 RUN_LIVE=1")
