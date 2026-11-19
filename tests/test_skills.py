"""G1: Skills parse / progressive disclosure / requires degradation / sandbox."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_bridge.skills.adapter import SkillToolAdapter
from agent_bridge.skills.parser import SkillParseError, SkillParser
from agent_bridge.skills.registry import SkillError, SkillRegistry
from agent_bridge.skills.sandbox import ScriptSandbox
from agent_bridge.skills.scanner import SkillScanner
from agent_bridge.tools.registry import ToolNameConflictError, ToolRegistry


def test_parse_mini_skill_keeps_unknown_fields(mini_skill_path: Path, parser: SkillParser) -> None:
    """未知 frontmatter 字段必须完整保留，不能因扩展字段报错。"""
    doc = parser.parse(mini_skill_path)
    assert doc.manifest.name == "mini-skill"
    assert doc.manifest.description.startswith("最小测试技能")
    assert doc.manifest.version == "0.1.0"
    assert doc.manifest.tags == ["test"]
    assert doc.manifest.raw_frontmatter["future_field"] == "未知扩展字段必须被保留"
    assert doc.manifest.raw_frontmatter["metadata"]["clawdbot"]["requires"]["bins"] == []
    assert "Mini Skill" in doc.body


def test_parse_real_obsidian_nested_requires(real_skill_dirs: list[Path], parser: SkillParser) -> None:
    """obsidian 的 metadata.clawdbot 是 JSON 风格嵌套，requires.bins 必须可读。"""
    doc = parser.parse(real_skill_dirs[0] / "SKILL.md")
    manifest = doc.manifest
    assert manifest.name == "obsidian"
    assert "Obsidian vaults" in manifest.description
    assert manifest.requires.bins == ["obsidian-cli"]
    assert manifest.raw_frontmatter["metadata"]["clawdbot"]["install"][0]["kind"] == "brew"
    assert manifest.raw_frontmatter["homepage"] == "https://help.obsidian.md"
    assert "icon" in manifest.raw_frontmatter


def test_parse_real_football_bayes_standard_frontmatter(real_skill_dirs: list[Path], parser: SkillParser) -> None:
    """football-bayes 为标准 frontmatter + tags list。"""
    doc = parser.parse(real_skill_dirs[1] / "SKILL.md")
    assert doc.manifest.name == "football-bayes"
    assert doc.manifest.version == "1.0.2"
    assert "football" in doc.manifest.tags
    assert "bayesian-analysis" in doc.manifest.tags
    assert "足球" in (doc.manifest.description_zh or "")


def test_parser_rejects_missing_required_fields(tmp_path: Path, parser: SkillParser) -> None:
    """仅 name/description 必填，缺失时用 2001 报错。"""
    bad = tmp_path / "SKILL.md"
    bad.write_text("---\nname: no-description\n---\nbody", encoding="utf-8")
    with pytest.raises(SkillParseError) as exc:
        parser.parse(bad)
    assert exc.value.error_code == "2001"
    assert "description" in exc.value.reason


def test_parser_rejects_invalid_requires_type(tmp_path: Path, parser: SkillParser) -> None:
    """QA-P2：requires.bins 为字符串时抛 2001，不能漏出 ValidationError。"""
    bad = tmp_path / "SKILL.md"
    bad.write_text(
        "---\nname: bad-requires\ndescription: broken\n"
        'metadata: {"clawdbot": {"requires": {"bins": "obsidian-cli"}}}\n'
        "---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(SkillParseError) as exc:
        parser.parse(bad)
    assert exc.value.error_code == "2001"


def test_scanner_skills_invalid_requires(tmp_path: Path) -> None:
    """QA-P2：扫描器跳过解析失败的技能，不让整批扫描崩溃。"""
    bad = tmp_path / "SKILL.md"
    bad.write_text(
        "---\nname: bad-requires\ndescription: broken\n"
        'metadata: {"clawdbot": {"requires": {"bins": "obsidian-cli"}}}\n'
        "---\nbody",
        encoding="utf-8",
    )
    assert SkillScanner().scan([tmp_path]) == []


def test_parser_normalises_string_tags(tmp_path: Path, parser: SkillParser) -> None:
    """QA-P2：tags 字符串归一化为单元素列表，不拆成逐字符。"""
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: tag-skill\ndescription: ok\ntags: football,bayes\n---\nbody",
        encoding="utf-8",
    )
    assert parser.parse(skill).manifest.tags == ["football,bayes"]


def test_scanner_finds_three_real_skills(real_skill_dirs: list[Path]) -> None:
    """递归扫描三个真实 skill 目录。"""
    docs = SkillScanner().scan(real_skill_dirs)
    names = {doc.manifest.name for doc in docs}
    assert {"obsidian", "football-bayes", "github-trending-cn"} <= names


def test_manifest_prompt_contains_real_skills(real_skill_dirs: list[Path]) -> None:
    """L1 manifest 只含名称+描述，且覆盖 obsidian / football-bayes。"""
    registry = SkillRegistry(SkillScanner().scan(real_skill_dirs))
    prompt = registry.manifest_prompt(top_k=10)
    assert "obsidian" in prompt and "football-bayes" in prompt
    assert "Obsidian 知识库管理与自动化" in prompt  # 本地化描述进入 L1
    assert "github-trending-cn" in prompt
    assert "# Skill: football-bayes" not in prompt  # 正文不能进入 L1


def test_check_requires_obsidian_degrades(real_skill_dirs: list[Path]) -> None:
    """缺 obsidian-cli 时返回缺失列表，不抛异常。"""
    registry = SkillRegistry(SkillScanner().scan(real_skill_dirs))
    missing = registry.check_requires("obsidian")
    assert isinstance(missing, list)
    if shutil.which("obsidian-cli") is None:
        assert "obsidian-cli" in missing
    assert registry.check_requires("no-such-skill") == []


def test_l2_load_and_l3_script_path(real_skill_dirs: list[Path], mini_skill_path: Path) -> None:
    """L2 正文按需加载；L3 脚本路径解析且防穿越。"""
    docs = SkillScanner().scan(real_skill_dirs)
    registry = SkillRegistry(docs + [SkillParser().parse(mini_skill_path)])
    assert "# Obsidian" in registry.load("obsidian")
    script = registry.script_path("github-trending-cn", "scripts/github_trending.py")
    assert script.is_file() and script.name == "github_trending.py"
    assert script.is_relative_to(real_skill_dirs[2].resolve())
    with pytest.raises(SkillError) as exc:
        registry.resource("mini-skill", "../../../../etc/passwd")
    assert exc.value.error_code == "4001"


def test_sandbox_echo_success(skill_registry: SkillRegistry, sandbox: ScriptSandbox) -> None:
    """fixture echo.py 在锁定 cwd 下执行成功。"""
    doc = skill_registry.get("mini-skill")
    result = sandbox.run(doc, "scripts/echo.py", ["hello", "world"], timeout=5)
    assert result.ok is True
    assert result.error_code == "0"
    assert "ECHO:hello world" in result.data["stdout"]
    assert result.latency_ms >= 0


def test_sandbox_path_traversal_rejected(skill_registry: SkillRegistry, sandbox: ScriptSandbox) -> None:
    """路径穿越返回 4001。"""
    doc = skill_registry.get("mini-skill")
    result = sandbox.run(doc, "../../../../etc/passwd", timeout=5)
    assert result.ok is False
    assert result.error_code == "4001"


def test_sandbox_timeout_rejected(skill_registry: SkillRegistry, sandbox: ScriptSandbox) -> None:
    """超时返回 4002。"""
    doc = skill_registry.get("mini-skill")
    result = sandbox.run(doc, "scripts/slow.py", timeout=1)
    assert result.ok is False
    assert result.error_code == "4002"
    assert result.latency_ms < 5000


def test_sandbox_disallowed_suffix(skill_registry: SkillRegistry, sandbox: ScriptSandbox) -> None:
    """非白名单后缀返回 4003。"""
    doc = skill_registry.get("mini-skill")
    result = sandbox.run(doc, "SKILL.md", timeout=5)
    assert result.error_code == "4003"


def test_adapter_registers_tools(skill_registry: SkillRegistry, sandbox: ScriptSandbox) -> None:
    """三个 skill 工具均注册成功，且执行路径走 sandbox。"""
    registry = ToolRegistry()
    SkillToolAdapter(skill_registry, sandbox).register(registry)
    assert {"skill__load_skill", "skill__read_resource", "skill__run_script"} <= set(registry.names())
    body = registry.get("skill__load_skill")("mini-skill")
    assert "Mini Skill" in body
    result = registry.get("skill__run_script")("mini-skill", "scripts/echo.py", ["a"], 5)
    assert result["ok"] is True
    assert "ECHO:a" in result["data"]["stdout"]
    with pytest.raises(ToolNameConflictError):
        registry.register("skill__load_skill", lambda: None)
