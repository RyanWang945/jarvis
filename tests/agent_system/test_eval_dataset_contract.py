from __future__ import annotations

import json
from pathlib import Path


EVAL_DIR = Path(__file__).parent / "fixtures" / "eval"
REQUIRED_FILES = {
    "turn_classifier_real.jsonl",
    "intent_planning_real.jsonl",
    "react_tool_selection_real.jsonl",
    "agent_e2e_real.jsonl",
    "safety_real.jsonl",
    "multi_turn_real.jsonl",
}


def _load_jsonl(path: Path) -> list[dict]:
    cases = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                cases.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"Invalid JSON in {path}:{line_number}") from exc
    return cases


def test_agent_system_eval_dataset_files_are_independent_and_complete() -> None:
    names = {path.name for path in EVAL_DIR.glob("*.jsonl")}

    assert REQUIRED_FILES <= names
    assert "smoke.jsonl" not in names


def test_agent_system_eval_dataset_schema_is_stable() -> None:
    seen_ids: set[str] = set()
    all_cases: list[dict] = []
    for path in sorted(EVAL_DIR.glob("*.jsonl")):
        cases = _load_jsonl(path)
        assert cases, f"{path} must contain at least one eval case"
        all_cases.extend(cases)

    for case in all_cases:
        assert isinstance(case.get("id"), str) and case["id"]
        assert case["id"] not in seen_ids
        seen_ids.add(case["id"])
        assert isinstance(case.get("layer"), str) and case["layer"]
        assert isinstance(case.get("category"), str) and case["category"]
        assert isinstance(case.get("messages"), list) and case["messages"]
        assert isinstance(case["messages"][0].get("content"), str) and case["messages"][0]["content"]
        assert isinstance(case.get("requires", []), list)
        assert isinstance(case.get("success_criteria", []), list)

        expected = case.get("expected_classification")
        if expected is not None:
            assert isinstance(expected, dict)
            assert expected.get("scene") in {"chat", "project", "research", "reminder", "control"}
            assert expected.get("access") in {"none", "read", "write", "commit", "push"}
            assert isinstance(expected.get("deliver"), bool)
            assert "turn_type" not in expected
            assert "required_capabilities" not in expected
            assert "forbidden_capabilities" not in expected
            assert "task_plan" not in case
            assert "expected_task_plan" not in case
            if "target_resources" in expected:
                assert isinstance(expected["target_resources"], list)
            if "objective_contains" in expected:
                assert isinstance(expected["objective_contains"], list)

        assert isinstance(case.get("expected_tools", []), list)
        assert isinstance(case.get("forbidden_tools", []), list)
        assert isinstance(case.get("max_tool_calls", {}), dict)
