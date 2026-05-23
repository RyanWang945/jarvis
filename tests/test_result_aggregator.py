from __future__ import annotations

from app.llm.provider_adapters import NormalizedLLMResponse
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeError, NodeResult
from app.task_runtime.planner import ExecutionPlan, FinalizationHint, PlanNode
from app.task_runtime.result_aggregator import ResultAggregator


class FakeProfile:
    api_key = "test-key"
    supports_json_object = True


class FakeResolvedModel:
    def __init__(self, client) -> None:
        self.client = client
        self.profile = FakeProfile()


class ScriptedSummaryChat:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    def chat_normalized(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        return NormalizedLLMResponse(
            content=self.response,
            tool_calls=(),
            reasoning_content=None,
            usage=None,
            model="fake",
            finish_reason=None,
            raw={},
        )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        user_objective="research then review",
        nodes=[
            PlanNode(id="research", runtime="react", objective="Research"),
            PlanNode(id="review", runtime="codex", objective="Review", input_refs=["node:research"]),
        ],
    )


def test_result_aggregator_fallback_completed_summarizes_node_results() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(node_id="research", runtime="react", status="completed", summary="Found SDK facts."),
            NodeResult(
                node_id="review",
                runtime="codex",
                status="completed",
                summary="Reviewed implementation.",
                artifacts=[NodeArtifact(ref="R1", kind="report")],
            ),
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert "Found SDK facts" in result.reply
    assert "Reviewed implementation" in result.reply
    assert result.artifact_refs == ["artifact:R1"]


def test_result_aggregator_uses_llm_json_result() -> None:
    chat = ScriptedSummaryChat(
        '{"status":"completed","reply":"调研和 review 都完成了。","artifact_refs":["artifact:R1"],"data":{"confidence":"high"}}'
    )
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(node_id="review", runtime="codex", status="completed", summary="Reviewed.", artifacts=[NodeArtifact(ref="R1")])
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report, current_user_input="帮我 review")

    assert result.status == "completed"
    assert result.reply == "调研和 review 都完成了。"
    assert result.artifact_refs == ["artifact:R1"]
    assert result.data["confidence"] == "high"
    assert chat.calls[0]["response_format"] == {"type": "json_object"}


def test_result_aggregator_pass_through_skips_llm() -> None:
    chat = ScriptedSummaryChat('{"status":"completed","reply":"should not be used"}')
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    plan = ExecutionPlan(
        user_objective="你觉得数学难吗",
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True, reason="simple chat"),
        nodes=[PlanNode(id="main", runtime="llm", objective="你觉得数学难吗")],
    )
    report = ExecutionReport(
        status="completed",
        node_results=[NodeResult(node_id="main", runtime="llm", status="completed", summary="数学有时难，但可以练会。")],
    )

    result = aggregator.aggregate(plan=plan, report=report)

    assert result.status == "completed"
    assert result.reply == "数学有时难，但可以练会。"
    assert result.data["finalization"] == "pass_through"
    assert chat.calls == []


def test_result_aggregator_blocked_missing_repo_needs_user_input() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    report = ExecutionReport(
        status="blocked",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="codex",
                status="blocked",
                summary="Codex runtime requires runtime_hints.active_repo.",
                error=NodeError(code="missing_active_repo", message="active_repo missing"),
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "needs_user_input"
    assert result.missing_info_question == "需要先指定要操作的仓库。"


def test_result_aggregator_failed_result_includes_replan_instruction() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    report = ExecutionReport(
        status="failed",
        node_results=[
            NodeResult(node_id="research", runtime="react", status="failed", summary="Search backend unavailable.")
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "failed"
    assert result.reply == "Search backend unavailable."
    assert result.replan_instructions == ["Replan around failed node research: Search backend unavailable."]


def _missing_key_model():
    class MissingKeyProfile:
        api_key = ""
        id = "missing"
        supports_json_object = True

    class MissingKeyModel:
        profile = MissingKeyProfile()

    return MissingKeyModel()
