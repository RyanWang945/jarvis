import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage

from app.agent_react.context_manager import ContextManager
from app.skills.loader import SkillPackageLoader
from app.skills.registry import SkillRegistry
from app.tools.common import ToolExecutionRequest
from app.tools.skill_guidance import run_load_skill


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

    assert package.skill.skill_id == "social-search-guide"
    assert package.skill.display_name == "social-search-guide"
    assert package.skill.effective_description == "Social search guidance."
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
    assert matches[0].skill.skill_id == "release-checklist"
    assert matches[0].confidence in {"high", "medium"}
    assert matches[0].reason


def test_weather_skill_is_selected_for_chinese_weather_requests(monkeypatch) -> None:
    skill_dir = Path("skills/weather-1.0.0")
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)

    matches = registry.select_matches("查一下上海天气")
    messages, skill_names = ContextManager().build_initial_messages(
        [
            SimpleNamespace(
                id=1,
                role="user",
                content="查一下上海天气",
                raw_payload={},
                turn_id=1,
            )
        ],
        trigger_message_id=1,
        turn_records=[],
        current_turn_id=None,
    )

    assert matches[0].skill.skill_id == "weather-1.0.0"
    assert skill_names == []
    reminder_content = str(messages[1].content)
    assert "The following skills are available for use with the Skill tool:" in reminder_content
    assert "- weather-1.0.0:" in reminder_content
    assert "[Skill: weather-1.0.0]" not in reminder_content
    assert "If `tavily_search` is available" not in reminder_content


def test_skill_listing_shows_menu_without_body(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / "artifact-planner"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Artifact Planner\n"
        "description: Plan image and SVG artifacts.\n"
        "when_to_use: User asks to generate visual artifacts.\n"
        "---\n\n"
        "Preserve the user's requested format.\n",
        encoding="utf-8",
    )
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

    assert skill_names == []
    assert isinstance(messages[1], HumanMessage)
    reminder_content = str(messages[1].content)
    assert reminder_content.startswith("<system-reminder>")
    assert reminder_content.endswith("</system-reminder>")
    assert "- artifact-planner:" in reminder_content
    assert "[Skill: artifact-planner]" not in reminder_content
    assert "Preserve the user's requested format." not in reminder_content


def test_load_skill_loads_exact_skill_id_and_injects_body(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / "external-review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Fancy Display Name\n"
        "description: Review external skills.\n"
        "---\n\n"
        "Follow the external skill review checklist.\n",
        encoding="utf-8",
    )
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.tools.skill_guidance.get_skill_registry", lambda: registry)
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)

    result = run_load_skill(ToolExecutionRequest(tool_name="Skill", workdir=None, args={"skill": "external-review"}))
    payload = json.loads(result.stdout)
    rendered = ContextManager().build_skill_reminder_message([payload["skills"][0]["name"]])

    assert payload["status"] == "loaded"
    assert payload["skill"]["skill_id"] == "external-review"
    assert payload["skills"][0]["name"] == "external-review"
    assert "Base directory for this skill:" in payload["content"]
    assert rendered is not None
    assert not str(rendered.content).startswith("<system-reminder>")
    assert "[Skill: external-review]" in str(rendered.content)
    assert "Follow the external skill review checklist." in str(rendered.content)
