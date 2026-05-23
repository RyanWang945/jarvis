from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.agent_react.session_state import ConversationSessionState
from app.config import get_settings
from app.task_runtime.planner import ExecutionPlan, PlanNode, TurnPlanner, build_plan_input


def test_plan_node_uses_input_refs_as_graph_edges() -> None:
    plan = ExecutionPlan(
        user_objective="research then review",
        nodes=[
            PlanNode(id="research", runtime="react", objective="Research agent runtime patterns"),
            PlanNode(
                id="review",
                runtime="codex",
                objective="Review jarvis using research result",
                input_refs=["node:research"],
            ),
        ],
    )

    assert plan.nodes[1].input_refs == ["node:research"]


def test_execution_plan_allows_previous_node_result_refs() -> None:
    plan = ExecutionPlan(
        user_objective="continue from previous result",
        nodes=[PlanNode(id="review", runtime="codex", objective="Review", input_refs=["node:previous_research"])],
    )

    assert plan.nodes[0].input_refs == ["node:previous_research"]


def test_execution_plan_rejects_self_ref() -> None:
    with pytest.raises(ValidationError, match="cannot reference itself"):
        ExecutionPlan(
            user_objective="bad graph",
            nodes=[PlanNode(id="review", runtime="codex", objective="Review", input_refs=["node:review"])],
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


def test_plan_node_requires_tool_name_only_for_tool_runtime() -> None:
    with pytest.raises(ValidationError, match="tool nodes require tool_name"):
        PlanNode(id="remind", runtime="tool", objective="Create reminder")
    with pytest.raises(ValidationError, match="tool_name is only valid"):
        PlanNode(id="answer", runtime="llm", objective="Answer", tool_name="scheduled_task")


def test_build_plan_input_normalizes_artifacts_and_hints() -> None:
    plan_input = build_plan_input(
        current_user_input="把刚刚那个报告发我",
        artifacts=[{"id": "report-1", "filename": "rag_eval_report.md", "summary": "RAG report"}],
        previous_node_results=[],
        runtime_hints={"available_runtimes": ["llm", "tool"]},
        session_state=ConversationSessionState(active_repo_id="jarvis"),
    )

    assert plan_input.current_user_input == "把刚刚那个报告发我"
    assert plan_input.artifacts[0]["ref"] == "report-1"
    assert plan_input.artifacts[0]["name"] == "rag_eval_report.md"
    assert plan_input.runtime_hints["active_repo"] == "jarvis"


real_llm = pytest.mark.skipif(
    os.environ.get("JARVIS_RUN_TASK_PLANNER_EVAL") != "1",
    reason="real planner LLM tests are opt-in and require JARVIS_RUN_TASK_PLANNER_EVAL=1",
)


@real_llm
def test_turn_planner_real_llm_creates_artifact_delivery_node() -> None:
    get_settings.cache_clear()
    plan = TurnPlanner(prompt_version="v2").plan(
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
        runtime_hints={"active_repo": None, "available_runtimes": ["llm", "react", "codex", "tool", "deepresearch"]},
    )

    assert len(plan.nodes) == 1
    assert plan.nodes[0].runtime == "tool"
    assert plan.nodes[0].tool_name == "deliver_file"
    assert "artifact:A1" in plan.nodes[0].input_refs


@real_llm
def test_turn_planner_real_llm_reuses_previous_node_result_for_replan() -> None:
    get_settings.cache_clear()
    plan = TurnPlanner(prompt_version="v2").plan(
        content="根据刚才的调研结果，评估 jarvis 是否需要调整。",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="jarvis"),
        previous_node_results=[
            {
                "node_id": "research_agent_sdk",
                "runtime": "react",
                "status": "completed",
                "summary": "OpenAI agent SDK 最近更新了 tracing 和 handoff 相关能力。",
                "artifacts": [],
            }
        ],
        runtime_hints={"active_repo": "jarvis", "available_runtimes": ["llm", "react", "codex", "tool", "deepresearch"]},
    )

    assert [node.runtime for node in plan.nodes] == ["codex"]
    assert "node:research_agent_sdk" in plan.nodes[0].input_refs
