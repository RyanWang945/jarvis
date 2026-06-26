import json
from pathlib import Path
from types import SimpleNamespace

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


def test_skill_registry_get_uses_skill_id_not_display_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "release-checklist"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Release Checklist\n"
        "description: Release workflow guidance.\n"
        "---\n\n"
        "Follow release steps.\n",
        encoding="utf-8",
    )
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])

    assert registry.get("release-checklist").skill_id == "release-checklist"
    try:
        registry.get("Release Checklist")
    except ValueError as exc:
        assert "unknown skill" in str(exc)
    else:
        raise AssertionError("display name must not resolve as a skill id")


def test_weather_skill_is_listed_without_body(monkeypatch) -> None:
    skill_dir = Path("skills/weather-1.0.0")
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)

    messages = ContextManager().build_initial_messages(
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

    reminder_content = str(messages[1].content)
    assert "以下 skills 可通过 Skill 工具使用：" in reminder_content
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

    messages = ContextManager().build_initial_messages(
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

    result = run_load_skill(ToolExecutionRequest(tool_name="Skill", workdir=None, args={"skill": "external-review"}))
    payload = json.loads(result.stdout)

    assert payload["status"] == "loaded"
    assert payload["skill"]["skill_id"] == "external-review"
    assert payload["skills"][0]["name"] == "external-review"
    assert "Base directory for this skill:" in payload["content"]
    assert "Follow the external skill review checklist." in payload["content"]

    alias_result = run_load_skill(ToolExecutionRequest(tool_name="Skill", workdir=None, args={"skill": "Fancy Display Name"}))
    alias_payload = json.loads(alias_result.stdout)
    assert alias_payload["status"] == "unknown_skill"
