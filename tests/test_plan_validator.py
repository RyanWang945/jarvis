from app.task_runtime.plan_validator import validate_plan
from app.task_runtime.planner import ExecutionPlan, PlanNode


def test_plan_validator_flags_runtime_repo_and_artifact_issues() -> None:
    plan = ExecutionPlan(
        user_objective="deliver and inspect",
        nodes=[
            PlanNode(
                id="deliver",
                runtime="react",
                repo_id="jarvis",
                objective="Deliver artifact",
                input_refs=["artifact:missing"],
            ),
            PlanNode(
                id="inspect",
                runtime="coder",
                repo_id="unknown",
                objective="Inspect repo",
            ),
        ],
    )

    issues = validate_plan(
        plan,
        allowed_runtimes={"react", "coder"},
        known_artifact_refs={"artifact:A1"},
        registered_repo_ids={"jarvis"},
    )

    assert {issue.code for issue in issues} == {
        "react_repo_id_not_allowed",
        "unknown_artifact_ref",
        "unknown_repo_id",
    }
