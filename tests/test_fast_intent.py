from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.agent_react.session_state import ConversationSessionState
from app.task_runtime.fast_intent import (
    FastIntentDecision,
    FastIntentNode,
    _decision_from_payload,
    _decision_from_response,
    _fast_intent_messages,
)


def test_fast_intent_decision_defaults_needs_plan_to_no_runtime() -> None:
    decision = FastIntentDecision(route="needs_plan", confidence=0.9)

    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "tool_name")
    assert not hasattr(decision, "input_refs")
    assert not hasattr(decision, "finalization_hint")
    assert decision.reply == ""


def test_fast_intent_decision_ignores_legacy_planning_fields() -> None:
    decision = FastIntentDecision(
        route="needs_plan",
        confidence=0.9,
        runtime="legacy_runtime",
        tool_name="deliver_file",
        input_refs=["artifact:A1"],
        reply="should be ignored",
    )

    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "tool_name")
    assert not hasattr(decision, "input_refs")
    assert not hasattr(decision, "finalization_hint")
    assert decision.reply == ""


def test_fast_intent_decision_requires_reply_for_fast_reply() -> None:
    with pytest.raises(ValidationError, match="fast_reply requires reply"):
        FastIntentDecision(route="fast_reply", confidence=0.95)


def test_fast_intent_decision_accepts_fast_reply() -> None:
    decision = FastIntentDecision(route="fast_reply", confidence=0.95, reply="数学有时难，但能练会。")

    assert decision.reply == "数学有时难，但能练会。"
    assert not hasattr(decision, "finalization_hint")


def test_fast_intent_payload_maps_unknown_legacy_route_to_needs_plan() -> None:
    decision = _decision_from_payload(
        {
            "route": "legacy_direct",
            "runtime": "legacy_runtime",
            "tool_name": "legacy_tool",
            "confidence": 0.95,
            "reason": "single repo task",
        }
    )

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "tool_name")


def test_fast_intent_payload_maps_legacy_direct_tool_to_needs_plan() -> None:
    decision = _decision_from_payload(
        {
            "route": "direct_tool",
            "runtime": "tool",
            "tool_name": "codex_image_gen",
            "confidence": 0.95,
            "reason": "single image generation tool",
        }
    )

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "tool_name")
    assert decision.confidence == 0.95


def test_fast_intent_assistant_content_becomes_fast_reply() -> None:
    decision = _decision_from_response(
        NormalizedLLMResponse(
            content="数学有时难，但能练会。",
            tool_calls=(),
            reasoning_content=None,
            usage=None,
            model="test",
            finish_reason="stop",
            raw={},
        )
    )

    assert decision.route == "fast_reply"
    assert decision.reply == "数学有时难，但能练会。"
    assert decision.confidence == 1.0


def test_fast_intent_virtual_tool_maps_to_needs_plan() -> None:
    decision = _decision_from_response(
        NormalizedLLMResponse(
            content="",
            tool_calls=(
                NormalizedToolCall(
                    id="call_1",
                    name="needs_plan",
                    args={"confidence": 0.95, "reason": "single image task"},
                ),
            ),
            reasoning_content=None,
            usage=None,
            model="test",
            finish_reason="tool_calls",
            raw={},
        )
    )

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "input_refs")


def test_fast_intent_messages_include_temporal_context() -> None:
    messages = _fast_intent_messages(
        "查最近 7 天新闻",
        session_state=ConversationSessionState(),
        recent_artifacts=[],
        runtime_hints={
            "current_date": "2026-05-25",
            "current_time": "2026-05-25T10:30:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )

    assert "temporal_context" in messages[1].content
    assert "2026-05-25" in messages[1].content


def test_fast_intent_unknown_virtual_tool_falls_back_to_needs_plan() -> None:
    decision = _decision_from_response(
        NormalizedLLMResponse(
            content="",
            tool_calls=(
                NormalizedToolCall(
                    id="call_1",
                    name="single_codex",
                    args={"confidence": 0.95, "reason": "legacy tool"},
                ),
            ),
            reasoning_content=None,
            usage=None,
            model="test",
            finish_reason="tool_calls",
            raw={},
        )
    )

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "tool_name")
    assert "single_codex" in decision.reason


real_llm = pytest.mark.skipif(
    os.environ.get("JARVIS_RUN_TASK_PLANNER_EVAL") != "1",
    reason="real fast intent LLM tests are opt-in and require JARVIS_RUN_TASK_PLANNER_EVAL=1",
)


@real_llm
def test_fast_intent_real_llm_routes_artifact_delivery() -> None:
    get_settings.cache_clear()
    decision = FastIntentNode().decide(
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
    )

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "runtime")
    assert not hasattr(decision, "tool_name")


@real_llm
def test_fast_intent_real_llm_routes_multi_goal_to_plan() -> None:
    get_settings.cache_clear()
    decision = FastIntentNode().decide(content="先查资料，然后 review jarvis，最后提醒我")

    assert decision.route == "needs_plan"


@real_llm
def test_fast_intent_real_llm_does_not_invent_codex_image_tool() -> None:
    get_settings.cache_clear()
    decision = FastIntentNode().decide(content="用codex image gen skill生成一个抖音直播网红的图片，要包含足够多的细节")

    assert decision.route == "needs_plan"
    assert not hasattr(decision, "tool_name")
