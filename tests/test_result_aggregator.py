from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.llm.provider_adapters import NormalizedLLMResponse
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeError, NodeResult
from app.task_runtime.planner import ExecutionPlan, FinalizationHint, PlanNode
from app.task_runtime.result_aggregator import ResultAggregator
from app.task_runtime.runtime_context import RuntimeContext


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


def test_result_aggregator_fallback_prefers_workspace_result_markdown(tmp_path: Path) -> None:
    result_path = tmp_path / "RESULT.md"
    result_path.write_text("# Final Workspace Result\n\nDetailed result from workspace.", encoding="utf-8")
    aggregator = ResultAggregator(model_resolver=lambda metadata: _missing_key_model())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="completed",
                summary="short summary",
                data={"workspace": {"result_markdown_path": str(result_path)}},
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert "# Final Workspace Result" in result.reply
    assert "short summary" not in result.reply


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

    result = aggregator.aggregate(
        plan=_plan(),
        report=report,
        current_user_input="帮我 review",
        runtime_context=RuntimeContext.from_hints({"active_repo": "jarvis"}),
    )

    assert result.status == "completed"
    assert result.reply == "调研和 review 都完成了。"
    assert result.artifact_refs == ["artifact:R1"]
    assert result.data["confidence"] == "high"
    assert chat.calls[0]["response_format"] == {"type": "json_object"}
    prompt_payload = json.loads(chat.calls[0]["messages"][1].content.split("\n\n", 1)[1])
    assert prompt_payload["runtime_context"]["active_repo"] == "jarvis"
    assert "runtime_hints" not in prompt_payload


def test_result_aggregator_includes_evidence_claim_artifact_in_prompt(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence_claims.md"
    evidence_path.write_text(
        "# 固高科技证据记录\n\n"
        "| Claim | 来源URL | 日期/期间 | 置信度 | 备注 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 固高科技股票代码为301510.SZ | https://example.com/301510 | 2026-06-27 | high | 证券身份 |\n",
        encoding="utf-8",
    )
    chat = ScriptedSummaryChat('{"status":"completed","reply":"已基于证据汇总。","artifact_refs":[],"data":{}}')
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="collect_financial_evidence",
                runtime="react",
                status="completed",
                summary="Collected evidence claims.",
                artifacts=[
                    NodeArtifact(
                        ref="evidence_claims",
                        kind="file",
                        path=str(evidence_path),
                        filename="evidence_claims.md",
                        mime_type="text/markdown",
                    )
                ],
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    prompt_payload = json.loads(chat.calls[0]["messages"][1].content.split("\n\n", 1)[1])
    evidence_artifacts = prompt_payload["evidence_artifacts"]
    assert evidence_artifacts[0]["node_id"] == "collect_financial_evidence"
    assert evidence_artifacts[0]["filename"] == "evidence_claims.md"
    assert "固高科技股票代码为301510.SZ" in evidence_artifacts[0]["content"]["markdown"]
    assert "https://example.com/301510" in evidence_artifacts[0]["content"]["markdown"]


def test_result_aggregator_does_not_downgrade_completed_report_to_failed() -> None:
    chat = ScriptedSummaryChat('{"status":"failed","reply":"没有生成完整报告。"}')
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(node_id="research", runtime="react", status="completed", summary="Found enough source material.")
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert result.reply == "没有生成完整报告。"


def test_result_aggregator_strips_unbacked_attachment_claims() -> None:
    chat = ScriptedSummaryChat(
        '{"status":"completed","reply":"核心结论如下。\\n\\n详细对比报告已生成，可查看附件。","artifact_refs":[],"data":{}}'
    )
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(node_id="research", runtime="react", status="completed", summary="Found enough source material.")
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert "核心结论如下" in result.reply
    assert "附件" not in result.reply


def test_result_aggregator_normalizes_nullable_collection_fields() -> None:
    chat = ScriptedSummaryChat(
        '{"status":"completed","reply":"已完成。","artifact_refs":null,"approval_requests":null,"data":null}'
    )
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="research",
                runtime="react",
                status="completed",
                summary="Found enough source material.",
                artifacts=[NodeArtifact(ref="A1")],
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert result.artifact_refs == ["artifact:A1"]
    assert result.approval_requests == []
    assert result.data == {}


def test_result_aggregator_uses_claude_agent_sdk_backend_with_no_tools() -> None:
    captured_options: list[dict] = []

    async def _fake_query(**kwargs):
        captured_options.append(kwargs["options"])
        payload = {
            "status": "completed",
            "reply": "| 维度 | Claude Tag | YouMind |\n| --- | --- | --- |\n| 产品类型 | Slack AI teammate | AI 创作工作室 |",
            "artifact_refs": [],
            "approval_requests": [],
            "data": {"confidence": "medium"},
        }
        yield type(
            "AssistantMessage",
            (),
            {
                "content": [],
                "session_id": "agg-session",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )()
        yield type(
            "ResultMessage",
            (),
            {
                "status": "completed",
                "structured_output": payload,
                "session_id": "agg-session",
                "usage": {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330},
            },
        )()

    mock_sdk = MagicMock()
    mock_sdk.ClaudeAgentOptions = lambda **kwargs: kwargs
    mock_sdk.query = _fake_query

    with patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}):
        aggregator = ResultAggregator(
            model_resolver=lambda metadata: FakeResolvedModel(ScriptedSummaryChat("{}")),
            backend="claude_agent_sdk",
        )
        report = ExecutionReport(
            status="completed",
            node_results=[
                NodeResult(node_id="research", runtime="react", status="completed", summary="Found source material.")
            ],
        )

        result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "completed"
    assert "| 维度 | Claude Tag | YouMind |" in result.reply
    assert result.data["aggregator_backend"] == "claude_agent_sdk"
    assert result.data["agent_session_id"] == "agg-session"
    assert len(result.usage_records) == 1
    assert result.usage_records[0]["source"] == "claude_agent_sdk"
    assert result.usage_records[0]["stage"] == "result_aggregator_claude_sdk"
    assert result.usage_records[0]["total_tokens"] == 330
    options = captured_options[0]
    assert options["max_turns"] == 1
    assert options["tools"] == []
    assert options["mcp_servers"] == {}
    assert options["strict_mcp_config"] is True
    assert options["permission_mode"] == "dontAsk"


def test_result_aggregator_pass_through_skips_llm() -> None:
    chat = ScriptedSummaryChat('{"status":"completed","reply":"should not be used"}')
    aggregator = ResultAggregator(model_resolver=lambda metadata: FakeResolvedModel(chat))
    plan = ExecutionPlan(
        user_objective="你觉得数学难吗",
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True),
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
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True),
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
                summary="Coder runtime requires an active repository in runtime context.",
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
                approval_requests=[approval],
                error=NodeError(code="coder_approval_required", message="approval required"),
            )
        ],
    )

    result = aggregator.aggregate(plan=_plan(), report=report)

    assert result.status == "needs_user_input"
    assert result.reply == "该操作需要确认后继续。"
    assert result.approval_requests == [approval]
    assert "approval_requests" not in result.data


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
