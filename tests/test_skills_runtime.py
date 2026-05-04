from pathlib import Path

from app.agent_react.context_manager import ContextManager
from app.skills.loader import SkillPackageLoader
from app.skills.registry import SkillRegistry


def test_skill_body_strips_frontmatter_and_manifest_supports_guide_fields(tmp_path: Path) -> None:
    skill_dir = tmp_path / "social-search-guide"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: social-search-guide\n"
        "description: Social search guidance.\n"
        "when_to_use: User asks about tweets.\n"
        "tools:\n"
        "  - x_search\n"
        "tags:\n"
        "  - social\n"
        "---\n\n"
        "# Social Search Guide\n\n"
        "Use x_search for X/Twitter posts.\n",
        encoding="utf-8",
    )

    package = SkillPackageLoader([]).load_package(skill_dir)

    assert package.manifest.when_to_use == "User asks about tweets."
    assert package.manifest.tools == ["x_search"]
    assert package.manifest.tags == ["social"]
    body = package.skill.load_body()
    assert body.startswith("# Social Search Guide")
    assert "when_to_use:" not in body


def test_context_manager_bounds_selected_skill_body(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / "long-guide"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: long-guide\n"
        "description: Long guide.\n"
        "---\n\n"
        + ("Long procedural guidance. " * 1000),
        encoding="utf-8",
    )
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)

    rendered = ContextManager()._render_selected_skills(["long-guide"])

    assert rendered is not None
    assert "[Skill: long-guide]" in rendered
    assert "name: long-guide" not in rendered
    assert "[Skill content truncated by Jarvis token budget.]" in rendered
