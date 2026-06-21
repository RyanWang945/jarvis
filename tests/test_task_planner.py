from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.agent_react.session_state import ConversationSessionState
from app.config import get_settings
from app.task_runtime.planner import ExecutionPlan, PlanNode, TurnPlanner, _plan_from_payload, build_plan_input
from app.task_runtime.runtime_context import RuntimeContext


def test_plan_node_uses_input_refs_as_graph_edges() -> None:
    plan = ExecutionPlan(
        user_objective="research then review",
        nodes=[
            PlanNode(id="research", runtime="react", objective="Research agent runtime patterns"),
            PlanNode(
                id="review",
                runtime="coder",
                objective="Review jarvis using research result",
                input_refs=["node:research"],
            ),
        ],
    )

    assert plan.nodes[1].input_refs == ["node:research"]


def test_execution_plan_allows_previous_node_result_refs() -> None:
    plan = ExecutionPlan(
        user_objective="continue from previous result",
        nodes=[PlanNode(id="review", runtime="coder", objective="Review", input_refs=["node:previous_research"])],
    )

    assert plan.nodes[0].input_refs == ["node:previous_research"]


def test_execution_plan_rejects_self_ref() -> None:
    with pytest.raises(ValidationError, match="cannot reference itself"):
        ExecutionPlan(
            user_objective="bad graph",
            nodes=[PlanNode(id="review", runtime="coder", objective="Review", input_refs=["node:review"])],
        )


def test_execution_plan_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="acyclic"):
        ExecutionPlan(
            user_objective="bad graph",
            nodes=[
                PlanNode(id="a", runtime="llm", objective="A", input_refs=["node:b"]),
                PlanNode(id="b", runtime="llm", objective="B", input_refs=["node:a"]),
            ],
        )


def test_plan_node_rejects_legacy_tool_runtime() -> None:
    with pytest.raises(ValidationError):
        PlanNode(id="remind", runtime="tool", objective="Create reminder", tool_name="scheduled_task")  # type: ignore[arg-type]


def test_plan_node_rejects_deepresearch_runtime() -> None:
    with pytest.raises(ValidationError):
        PlanNode(id="research", runtime="deepresearch", objective="Run deep research")  # type: ignore[arg-type]


def test_plan_node_rejects_legacy_codex_runtime() -> None:
    with pytest.raises(ValidationError):
        PlanNode(id="fix", runtime="codex", objective="Fix repo")  # type: ignore[arg-type]


def test_build_plan_input_normalizes_artifacts_and_hints() -> None:
    plan_input = build_plan_input(
        current_user_input="把刚刚那个报告发我",
        artifacts=[{"id": "report-1", "filename": "rag_eval_report.md", "summary": "RAG report"}],
        previous_node_results=[],
        runtime_context=RuntimeContext.from_hints({"available_runtimes": ["llm", "react"]}),
    )

    assert plan_input.current_user_input == "把刚刚那个报告发我"
    assert plan_input.artifacts[0]["ref"] == "report-1"
    assert plan_input.artifacts[0]["name"] == "rag_eval_report.md"
    assert plan_input.runtime_context["current_date"]
    assert plan_input.runtime_context["current_time"]
    assert plan_input.runtime_context["timezone"] == "Asia/Shanghai"
    dumped = plan_input.model_dump(mode="json")
    assert "runtime_hints" not in dumped
    assert "typed_runtime_context" not in dumped


def test_plan_from_payload_derives_llm_finalization_for_non_llm_nodes() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "查最新资料",
            "finalization_hint": {"mode": "pass_through", "reason": "ignored", "user_facing": True},
            "nodes": [
                {
                    "id": "research",
                    "runtime": "react",
                    "objective": "查最新资料",
                    "output_hint": "用户可读的答案",
                }
            ],
        },
        fallback_objective="查最新资料",
    )

    assert plan.finalization_hint.mode == "llm"
    assert plan.finalization_hint.user_facing is True


def test_plan_from_payload_keeps_pass_through_for_single_llm_node() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "简单回答",
            "finalization_hint": {"mode": "pass_through", "user_facing": True},
            "nodes": [{"id": "answer", "runtime": "llm", "objective": "简单回答"}],
        },
        fallback_objective="简单回答",
    )

    assert plan.finalization_hint.mode == "pass_through"


def test_plan_from_payload_falls_back_for_legacy_tool_node() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "设置提醒",
            "finalization_hint": {"mode": "llm", "reason": "ignored", "user_facing": False},
            "nodes": [
                {
                    "id": "remind",
                    "runtime": "tool",
                    "objective": "设置提醒",
                    "tool_name": "scheduled_task",
                }
            ],
        },
        fallback_objective="设置提醒",
    )

    assert plan.nodes[0].id == "set_reminder"
    assert plan.nodes[0].runtime == "react"
    assert not hasattr(plan.nodes[0], "tool_name")
    assert plan.finalization_hint.mode == "llm"
    assert plan.finalization_hint.user_facing is False


def test_plan_from_payload_ignores_null_node_runtime_hints() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "review repo and remind me",
            "nodes": [
                {
                    "id": "review",
                    "runtime": "coder",
                    "objective": "Review jarvis",
                },
                {
                    "id": "remind",
                    "runtime": "react",
                    "objective": "Create reminder",
                    "tool_name": "scheduled_task",
                    "input_refs": ["node:review"],
                    "runtime_hints": None,
                },
            ],
        },
        fallback_objective="review repo and remind me",
    )

    assert [node.runtime for node in plan.nodes] == ["coder", "react"]
    assert not hasattr(plan.nodes[1], "runtime_hints")
    assert plan.nodes[1].input_refs == ["node:review"]


def test_plan_from_payload_does_not_parse_branch_hints_in_backend() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "在feat/test 里写个快排吧，用python写",
            "nodes": [
                {
                    "id": "write_quicksort",
                    "runtime": "coder",
                    "objective": "实现 quicksort.py",
                    "runtime_hints": {"access_mode": "write", "allow_commit": True, "allow_push": True},
                }
            ],
        },
        fallback_objective="在feat/test 里写个快排吧，用python写",
    )

    assert not hasattr(plan.nodes[0], "runtime_hints")


def test_plan_from_payload_ignores_node_runtime_hints() -> None:
    plan = _plan_from_payload(
        {
            "user_objective": "基于 main 创建 feat/my-skill 分支继续开发",
            "nodes": [
                {
                    "id": "implement",
                    "runtime": "coder",
                    "objective": "继续开发",
                    "runtime_hints": {
                        "source_branch": "main",
                        "target_branch": "feat/my-skill",
                        "worktree_mode": "node_branch_worktree",
                    },
                }
            ],
        },
        fallback_objective="基于 main 创建 feat/my-skill 分支继续开发",
    )

    assert not hasattr(plan.nodes[0], "runtime_hints")


def test_plan_from_payload_falls_back_to_artifact_delivery_for_empty_nodes() -> None:
    plan = _plan_from_payload(
        {"user_objective": "把刚刚那个报告发我", "nodes": []},
        fallback_objective="把刚刚那个报告发我",
        known_artifact_refs={"artifact:A1"},
    )

    assert len(plan.nodes) == 1
    assert plan.nodes[0].runtime == "react"
    assert not hasattr(plan.nodes[0], "tool_name")
    assert plan.nodes[0].input_refs == ["artifact:A1"]
    assert plan.finalization_hint.mode == "llm"


def test_plan_from_payload_falls_back_to_repo_then_reminder_dag_for_empty_nodes() -> None:
    plan = _plan_from_payload(
        {"user_objective": "先 review jarvis 的 agent runtime 重构风险，生成一份 markdown 报告，最后今晚 11 点提醒我看报告。", "nodes": []},
        fallback_objective="先 review jarvis 的 agent runtime 重构风险，生成一份 markdown 报告，最后今晚 11 点提醒我看报告。",
    )

    assert [node.runtime for node in plan.nodes] == ["coder", "react"]
    assert not hasattr(plan.nodes[0], "runtime_hints")
    assert plan.nodes[1].input_refs == ["node:repo_report"]
    assert "报告" in plan.nodes[0].objective
    assert "提醒" in plan.nodes[1].objective


def test_plan_from_payload_falls_back_to_coarse_code_business_dag_for_empty_nodes() -> None:
    objective = "在 jarvis 项目里实现会员积分能力：订单完成后累积积分，支付退款时撤销积分；把不同业务代码合并后做一次 code review。"

    plan = _plan_from_payload(
        {"user_objective": objective, "nodes": []},
        fallback_objective=objective,
    )

    assert len(plan.nodes) == 5
    assert {node.runtime for node in plan.nodes} == {"coder"}
    assert [node.id for node in plan.nodes[-2:]] == ["integrate_business_code", "code_review"]
    assert plan.nodes[-2].input_refs == ["node:implement_area_1", "node:implement_area_2", "node:implement_area_3"]
    assert plan.nodes[-1].input_refs == ["node:integrate_business_code"]
    assert not hasattr(plan.nodes[0], "runtime_hints")
    assert not hasattr(plan.nodes[-1], "runtime_hints")
    assert any("订单业务" in node.output_hint for node in plan.nodes)
    assert any("支付/退款业务" in node.output_hint for node in plan.nodes)
    assert "合并" in plan.nodes[-2].objective
    assert "Review" in plan.nodes[-1].objective


real_llm = pytest.mark.skipif(
    os.environ.get("JARVIS_RUN_TASK_PLANNER_EVAL") != "1",
    reason="real planner LLM tests are opt-in and require JARVIS_RUN_TASK_PLANNER_EVAL=1",
)


@real_llm
def test_turn_planner_real_llm_creates_artifact_delivery_node() -> None:
    get_settings.cache_clear()
    plan = TurnPlanner(prompt_version="v6").plan(
        content="把刚刚那个报告发我",
        recent_artifacts=[
            {
                "ref": "A1",
                "kind": "report",
                "name": "rag_eval_report.md",
                "description": "RAG 评测报告，包含 Recall@5、MRR、nDCG。",
                "availability": "available",
                "recency": "most_recent",
                "origin": "assistant_generated",
            }
        ],
        runtime_context=RuntimeContext.from_hints({"active_repo": None, "available_runtimes": ["llm", "react", "coder"]}),
    )

    assert len(plan.nodes) == 1
    assert plan.nodes[0].runtime == "react"
    assert not hasattr(plan.nodes[0], "tool_name")
    assert "artifact:A1" in plan.nodes[0].input_refs


@real_llm
def test_turn_planner_real_llm_reuses_previous_node_result_for_replan() -> None:
    get_settings.cache_clear()
    plan = TurnPlanner(prompt_version="v6").plan(
        content="根据刚才的调研结果，评估 jarvis 是否需要调整。",
        session_state=ConversationSessionState(session_mode="coding"),
        previous_node_results=[
            {
                "node_id": "research_agent_sdk",
                "runtime": "react",
                "status": "completed",
                "summary": "OpenAI agent SDK 最近更新了 tracing 和 handoff 相关能力。",
                "artifacts": [],
            }
        ],
        runtime_context=RuntimeContext.from_hints({"active_repo": "jarvis", "available_runtimes": ["llm", "react", "coder"]}),
    )

    assert [node.runtime for node in plan.nodes] == ["coder"]
    assert "node:research_agent_sdk" in plan.nodes[0].input_refs
