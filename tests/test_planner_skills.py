from pathlib import Path

from app.skills.loader import SkillPackageLoader
from app.task_runtime.planner_skills import _selection_from_payload


def test_planner_skill_selection_accepts_one_known_skill(tmp_path: Path) -> None:
    skill = _planner_skill(tmp_path, "code-planning")

    selection = _selection_from_payload(
        {"selected_planner_skill": {"skill_id": "code-planning", "reason": "repo task"}},
        [skill],
    )

    assert selection.skill_id == "code-planning"
    assert selection.reason == "repo task"
    assert selection.guidance == "Plan as code work."


def test_planner_skill_selection_ignores_unknown_or_null_skill(tmp_path: Path) -> None:
    skill = _planner_skill(tmp_path, "code-planning")

    unknown = _selection_from_payload({"selected_planner_skill": {"skill_id": "finance"}}, [skill])
    none = _selection_from_payload({"selected_planner_skill": None, "reason": "general"}, [skill])

    assert unknown.skill_id is None
    assert "unknown" in unknown.reason
    assert none.skill_id is None
    assert none.reason == "general"


def _planner_skill(tmp_path: Path, skill_id: str):
    skill_dir = tmp_path / skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"description: {skill_id}\n"
        "skill_type: planner\n"
        "planning_guidance: Plan as code work.\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    return SkillPackageLoader([]).load_package(skill_dir).skill
