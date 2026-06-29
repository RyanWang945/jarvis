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
    assert [skill.skill_id for skill in selection.skills] == ["code-planning"]


def test_planner_skill_selection_accepts_multiple_known_skills(tmp_path: Path) -> None:
    finance = _planner_skill(tmp_path, "finance-analysis-planning", guidance="Plan finance evidence.")
    stock = _planner_skill(tmp_path, "stock-analysis-planning", guidance="Plan stock metrics.")

    selection = _selection_from_payload(
        {
            "selected_planner_skills": [
                {"skill_id": "finance-analysis-planning", "reason": "financial research"},
                {"skill_id": "stock-analysis-planning", "reason": "A-share metrics"},
            ],
            "reason": "combine finance and stock planning",
        },
        [finance, stock],
    )

    assert [skill.skill_id for skill in selection.skills] == [
        "finance-analysis-planning",
        "stock-analysis-planning",
    ]
    assert [skill.guidance for skill in selection.skills] == ["Plan finance evidence.", "Plan stock metrics."]


def test_planner_skill_selection_ignores_unknown_or_null_skill(tmp_path: Path) -> None:
    skill = _planner_skill(tmp_path, "code-planning")

    unknown = _selection_from_payload({"selected_planner_skill": {"skill_id": "finance"}}, [skill])
    none = _selection_from_payload({"selected_planner_skill": None, "reason": "general"}, [skill])

    assert unknown.skill_id is None
    assert "unknown" in unknown.reason
    assert none.skill_id is None
    assert none.reason == "general"


def _planner_skill(tmp_path: Path, skill_id: str, *, guidance: str = "Plan as code work."):
    skill_dir = tmp_path / skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"description: {skill_id}\n"
        "skill_type: planner\n"
        f"planning_guidance: {guidance}\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    return SkillPackageLoader([]).load_package(skill_dir).skill
