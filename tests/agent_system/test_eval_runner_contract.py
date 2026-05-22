from __future__ import annotations

from scripts.run_agent_system_eval import DEFAULT_DATASET_DIR, load_cases, summarize_cases


def test_agent_system_eval_runner_loads_independent_dataset() -> None:
    cases = load_cases(DEFAULT_DATASET_DIR)

    assert len(cases) == 12
    assert {case.layer for case in cases} == {
        "agent_e2e",
        "intent_planning",
        "multi_turn",
        "react_tool_selection",
        "safety",
        "turn_classifier",
    }
    assert cases[0].id != "basic_qa_no_tool"


def test_agent_system_eval_runner_summary_tracks_requires_and_layers() -> None:
    cases = load_cases(DEFAULT_DATASET_DIR)
    runnable = [case for case in cases if case.requires == ["llm"]]
    skipped = [case for case in cases if case not in runnable]

    summary = summarize_cases(cases, runnable, skipped)

    assert summary["case_count"] == 12
    assert summary["runnable_count"] == len(runnable)
    assert summary["skipped_count"] == len(skipped)
    assert summary["layers"]["turn_classifier"] == 2
    assert summary["requires"]["llm"] == 12
    assert summary["requires"]["coder"] == 3
