from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage

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


def test_skill_registry_select_matches_returns_reason_and_confidence() -> None:
    skill_dir = Path("sandbox") / f"test-skill-registry-{uuid4().hex}" / "release-checklist"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: release-checklist\n"
        "description: Release workflow guidance.\n"
        "when_to_use: User asks for release workflow.\n"
        "capabilities:\n"
        "  - release\n"
        "---\n\n"
        "Follow release steps.\n",
        encoding="utf-8",
    )
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])

    matches = registry.select_matches("please run the release workflow")

    assert len(matches) == 1
    assert matches[0].skill.name == "release-checklist"
    assert matches[0].confidence in {"high", "medium"}
    assert matches[0].reason


def test_image_artifact_planner_skill_is_selected_for_svg_requests(monkeypatch) -> None:
    skill_dir = Path("data/skills/image-artifact-planner-1.0.0")
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)

    messages, skill_names = ContextManager().build_initial_messages(
        [
            SimpleNamespace(
                id=1,
                role="user",
                content="用codex给我个jarvis项目当前架构的svg图",
                raw_payload={},
                turn_id=1,
            )
        ],
        trigger_message_id=1,
        turn_records=[],
        current_turn_id=None,
    )

    assert skill_names == ["image-artifact-planner"]
    assert isinstance(messages[1], HumanMessage)
    reminder_content = str(messages[1].content)
    assert reminder_content.startswith("<system-reminder>")
    assert reminder_content.endswith("</system-reminder>")
    assert "[Skill: image-artifact-planner]" in reminder_content
    assert "Do not ask Codex to render, screenshot, convert, upload, or deliver the artifact." in reminder_content
    assert "Jarvis Runtime owns artifact discovery" in reminder_content
    assert "[Skill: image-artifact-planner]" not in str(messages[0].content)
