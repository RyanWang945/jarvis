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
            PlanNode(id="review", runtime="coder", objective="Review", input_refs=["node:research"]),
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
                runtime="coder",
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
            NodeResult(node_id="review", runtime="coder", status="completed", summary="Reviewed.", artifacts=[NodeArtifact(ref="R1")])
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


def test_result_aggregator_pass_through_renders_structured_llm_data() -> None:
    chat = ScriptedSummaryChat('{"status":"completed","reply":"should not be used"}')
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    plan = ExecutionPlan(
        user_objective="这个和金价有关系吗",
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True, reason="single user-facing node"),
        nodes=[PlanNode(id="explain_gold_relation", runtime="llm", objective="解释美伊局势与金价的关系")],
    )
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="explain_gold_relation",
                runtime="llm",
                status="completed",
                summary="美伊局势与金价存在密切关联，主要通过以下几条传导路径：",
                data={
                    "primary_factors": [
                        {
                            "factor": "避险需求",
                            "explanation": "美伊冲突升级会加剧不确定性，投资者转向黄金等避险资产。",
                        },
                        {
                            "factor": "石油价格传导",
                            "explanation": "霍尔木兹海峡风险可能推升油价和通胀预期，从而提振金价。",
                        },
                    ],
                    "typical_pattern": "紧张（金价上涨） -> 停火（金价回落） -> 反复拉锯（金价震荡）",
                },
            )
        ],
    )

    result = aggregator.aggregate(plan=plan, report=report)

    assert result.status == "completed"
    assert "避险需求" in result.reply
    assert "石油价格传导" in result.reply
    assert "典型走势" in result.reply
    assert len(result.reply) > len(report.node_results[0].summary)
    assert chat.calls == []


def test_result_aggregator_pass_through_prefers_explicit_reply() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    plan = ExecutionPlan(
        user_objective="解释关系",
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True),
        nodes=[PlanNode(id="main", runtime="llm", objective="解释关系")],
    )
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="main",
                runtime="llm",
                status="completed",
                summary="短摘要",
                data={"reply": "这是完整回复。", "primary_factors": [{"factor": "A", "explanation": "B"}]},
            )
        ],
    )

    result = aggregator.aggregate(plan=plan, report=report)

    assert result.reply == "这是完整回复。"


def test_result_aggregator_blocked_missing_repo_needs_user_input() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    report = ExecutionReport(
        status="blocked",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="blocked",
                summary="Codex runtime requires runtime_hints.active_repo.",
                error=NodeError(code="missing_active_repo", message="active_repo missing"),
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "needs_user_input"
    assert result.reply == "需要先指定要操作的仓库。"


def test_result_aggregator_preserves_blocked_approval_requests() -> None:
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    approval = {
        "approval_id": "runtime_git_1",
        "action_kind": "merge_to_protected",
        "command": "git merge --no-ff node_branch",
        "reason": "Merge to main.",
        "payload": {"source": "runtime_git"},
    }
    report = ExecutionReport(
        status="blocked",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="blocked",
                summary="approval required",
                data={"approval_requests": [approval]},
                error=NodeError(code="coder_approval_required", message="approval required"),
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "needs_user_input"
    assert result.reply == "该操作需要确认后继续。"
    assert result.data["approval_requests"] == [approval]


def test_result_aggregator_failed_result_returns_failed_reply_without_replan_contract() -> None:
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
    assert not hasattr(result, "replan_instructions")
    assert not hasattr(result, "missing_info_question")


def _missing_key_model():
    class MissingKeyProfile:
        api_key = ""
        id = "missing"
        supports_json_object = True

    class MissingKeyModel:
        profile = MissingKeyProfile()

    return MissingKeyModel()
