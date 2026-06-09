from scripts.run_agent_eval import DEFAULT_DATASET, build_report, load_cases, score_case


def test_agent_eval_smoke_dataset_loads() -> None:
    cases = load_cases(DEFAULT_DATASET)

    assert [case.id for case in cases] == [
        "basic_qa_no_tool",
        "current_info_uses_search",
        "wiki_query_project_memory",
        "code_task_delegates_coder",
        "unsafe_push_rejected_or_not_called",
    ]
    assert cases[1].expected_tools == ["tavily_search"]
    assert cases[3].requires == ["coder"]
    assert cases[3].expected_tools == []
    assert cases[3].expected_runtimes == ["coder"]


def test_agent_eval_score_checks_expected_and_forbidden_tools() -> None:
    case = load_cases(DEFAULT_DATASET)[1]

    passed = score_case(
        case,
        {
            "case_id": case.id,
            "category": case.category,
            "description": case.description,
            "status": "completed",
            "reply": "Based on search results.",
            "tool_names": ["tavily_search"],
            "tool_calls": [{"tool_name": "tavily_search"}],
            "messages": [],
            "turns": [],
            "turn_ids": [1],
            "conversation_id": 1,
            "run_responses": [],
            "metrics": {"elapsed_ms": 10, "turn_count": 1, "tool_call_count": 1},
            "success_criteria": case.success_criteria,
        },
    )
    failed = score_case(
        case,
        {
            "case_id": case.id,
            "category": case.category,
            "description": case.description,
            "status": "completed",
            "reply": "I inspected files instead.",
            "tool_names": ["shell_inspect"],
            "tool_calls": [{"tool_name": "shell_inspect"}],
            "messages": [],
            "turns": [],
            "turn_ids": [1],
            "conversation_id": 1,
            "run_responses": [],
            "metrics": {"elapsed_ms": 10, "turn_count": 1, "tool_call_count": 1},
            "success_criteria": case.success_criteria,
        },
    )

    assert passed["passed"] is True
    assert failed["passed"] is False
    assert any(check["name"] == "expected_tool:tavily_search" for check in failed["checks"])
    assert any(check["name"] == "forbidden_tool:shell_inspect" for check in failed["checks"])


def test_agent_eval_score_checks_expected_runtimes() -> None:
    case = load_cases(DEFAULT_DATASET)[3]

    passed = score_case(
        case,
        {
            "case_id": case.id,
            "category": case.category,
            "description": case.description,
            "status": "completed",
            "reply": "Coder finished.",
            "tool_names": [],
            "tool_calls": [],
            "runtime_names": ["coder"],
            "messages": [],
            "turns": [],
            "turn_ids": [1],
            "conversation_id": 1,
            "run_responses": [],
            "metrics": {"elapsed_ms": 10, "turn_count": 1, "tool_call_count": 0},
            "success_criteria": case.success_criteria,
        },
    )
    failed = score_case(
        case,
        {
            "case_id": case.id,
            "category": case.category,
            "description": case.description,
            "status": "completed",
            "reply": "No coder used.",
            "tool_names": [],
            "tool_calls": [],
            "runtime_names": ["react"],
            "messages": [],
            "turns": [],
            "turn_ids": [1],
            "conversation_id": 1,
            "run_responses": [],
            "metrics": {"elapsed_ms": 10, "turn_count": 1, "tool_call_count": 0},
            "success_criteria": case.success_criteria,
        },
    )

    assert passed["passed"] is True
    assert failed["passed"] is False
    assert any(check["name"] == "expected_runtime:coder" for check in failed["checks"])


def test_agent_eval_report_lists_skipped_cases() -> None:
    cases = load_cases(DEFAULT_DATASET)

    report = build_report([], skipped=[cases[1]])

    assert "Skipped: 1" in report
    assert "`current_info_uses_search` requires: tavily" in report
