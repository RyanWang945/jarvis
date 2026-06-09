from __future__ import annotations

from app.task_runtime.fast_intent import FastIntentDecision
from app.task_runtime.planning_router import PlanningRouterResult
from app.task_runtime.planner import ExecutionPlan, PlanNode
from scripts.run_task_planner_eval import (
    build_report,
    load_cases,
    run_cases,
    score_case,
    _prompt_matrix_runs,
)


class StaticPlanner:
    def __init__(self, plan: ExecutionPlan) -> None:
        self.plan_result = plan

    def plan(self, *, content, session_state=None, recent_artifacts=None, previous_node_results=None, runtime_hints=None, instructions=None):
        del content, session_state, recent_artifacts, previous_node_results, runtime_hints, instructions
        return self.plan_result

    def prompt_metadata(self):
        return {"prompt_id": "heavy_plan:test", "prompt_sha256": "abc123"}


class StaticRouter:
    def plan(self, *, content, session_state=None, recent_artifacts=None, previous_node_results=None, runtime_hints=None, instructions=None):
        del session_state, recent_artifacts, previous_node_results, runtime_hints, instructions
        if "报告发我" in content:
            return PlanningRouterResult(
                route="planned",
                plan=ExecutionPlan(
                    user_objective=content,
                    nodes=[
                        PlanNode(
                            id="deliver_report",
                            runtime="react",
                            objective="Deliver report",
                            input_refs=["artifact:A1"],
                        )
                    ],
                ),
                fast_intent=FastIntentDecision(route="needs_plan", confidence=0.95),
                elapsed_ms=100,
                planner_elapsed_ms=80,
            )
        return PlanningRouterResult(
            route="planned",
            plan=ExecutionPlan(
                user_objective=content,
                nodes=[
                    PlanNode(id="research", objective="research agent runtime", runtime="react"),
                    PlanNode(id="review", objective="review jarvis", runtime="coder", input_refs=["node:research"]),
                ],
            ),
            fast_intent=FastIntentDecision(route="needs_plan", confidence=0.95),
            elapsed_ms=700,
            planner_elapsed_ms=650,
        )

    def prompt_metadata(self):
        return {"fast_intent": {"prompt_id": "fast_intent:test"}, "planner": {"prompt_id": "heavy_plan:test"}}


def test_task_planner_eval_dataset_loads_simple_and_complex_cases() -> None:
    cases = load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl")

    assert len(cases) >= 6
    assert any(case.required_runtimes == ["llm"] for case in cases)
    assert any("coder" in case.required_runtimes and "react" in case.required_runtimes for case in cases)
    assert any(case.required_input_refs == ["artifact:A1"] for case in cases)


def test_task_planner_eval_score_checks_latency_and_plan_accuracy() -> None:
    case = next(case for case in load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl") if case.id == "complex_repo_report_reminder")
    plan = ExecutionPlan(
        user_objective=case.message,
        nodes=[
            PlanNode(id="review_report", objective="review jarvis and write markdown report", runtime="coder"),
            PlanNode(
                id="remind",
                objective="remind user at 23:00",
                runtime="react",
                input_refs=["node:review_report"],
            ),
        ],
    )

    result = score_case(case, plan, elapsed_ms=1000)

    assert result["passed"] is True
    assert result["runtimes"] == ["coder", "react"]
    assert result["tool_names"] == []


def test_task_planner_eval_score_fails_slow_or_inaccurate_plan() -> None:
    case = next(case for case in load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl") if case.id == "simple_artifact_delivery")
    plan = ExecutionPlan(user_objective=case.message, nodes=[PlanNode(id="main", objective="chat", runtime="llm")])

    result = score_case(case, plan, elapsed_ms=9000)

    assert result["passed"] is False
    failed_names = {check["name"] for check in result["checks"] if not check["passed"]}
    assert "latency" in failed_names
    assert "required_runtime:react" in failed_names
    assert "required_input_ref:artifact:A1" in failed_names


def test_task_planner_eval_report_includes_latency_summary() -> None:
    case = next(case for case in load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl") if case.id == "simple_llm_explain")
    plan = ExecutionPlan(
        user_objective="解释 Plan IR 和 ReActLoop",
        nodes=[PlanNode(id="main", objective="解释 Plan IR 和 ReActLoop 的区别", runtime="llm")],
    )
    result = score_case(case, plan, elapsed_ms=321)
    result["mode"] = "planner"

    report = build_report([result])

    assert "Cases: `1`" in report
    assert "Avg latency: `321 ms`" in report
    assert "PASS [planner] simple_llm_explain" in report


def test_task_planner_eval_router_mode_records_planned_routes() -> None:
    cases = [case for case in load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl") if case.id in {"simple_artifact_delivery", "complex_research_then_repo"}]

    results = run_cases(cases, mode="router", router=StaticRouter())

    by_id = {result["case_id"]: result for result in results}
    assert by_id["simple_artifact_delivery"]["route"] == "planned"
    assert by_id["simple_artifact_delivery"]["metrics"]["planner_elapsed_ms"] is not None
    assert by_id["complex_research_then_repo"]["route"] == "planned"
    assert by_id["complex_research_then_repo"]["metrics"]["planner_elapsed_ms"] is not None


def test_task_planner_eval_both_report_compares_latency() -> None:
    case = next(case for case in load_cases("tests/fixtures/task_planner_eval/planner_cases.jsonl") if case.id == "simple_llm_explain")
    plan = ExecutionPlan(
        user_objective="解释 Plan IR 和 ReActLoop",
        nodes=[PlanNode(id="main", objective="解释 Plan IR 和 ReActLoop 的区别", runtime="llm")],
    )
    planner_result = score_case(case, plan, elapsed_ms=1000)
    planner_result["mode"] = "planner"
    router_result = score_case(case, plan, elapsed_ms=100)
    router_result["mode"] = "router"
    router_result["route"] = "fast_reply"
    router_result["metrics"]["planner_elapsed_ms"] = None

    report = build_report([planner_result, router_result])

    assert "simple: planner `1000 ms`, router `100 ms`, delta `900 ms`" in report


def test_task_planner_eval_prompt_matrix_builds_router_version_product() -> None:
    runs = _prompt_matrix_runs(
        mode="router",
        planner_prompt_versions=["v1", "v2"],
        fast_intent_prompt_versions=["v1"],
    )

    assert runs == [
        {"mode": "router", "planner_prompt_version": "v1", "fast_intent_prompt_version": "v1"},
        {"mode": "router", "planner_prompt_version": "v2", "fast_intent_prompt_version": "v1"},
    ]


def test_task_planner_eval_prompt_matrix_does_not_duplicate_planner_runs_for_fast_versions() -> None:
    runs = _prompt_matrix_runs(
        mode="both",
        planner_prompt_versions=["v2"],
        fast_intent_prompt_versions=["v1", "v1"],
    )

    planner_runs = [run for run in runs if run["mode"] == "planner"]
    router_runs = [run for run in runs if run["mode"] == "router"]
    assert len(planner_runs) == 1
    assert len(router_runs) == 2
