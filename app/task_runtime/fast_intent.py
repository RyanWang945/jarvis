from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent_react.context_manager import ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.llm.client import LLMMessage, parse_json_content
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

FastIntentRoute = Literal["fast_reply", "needs_plan"]

class FastIntentDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    route: FastIntentRoute
    confidence: float = Field(ge=0.0, le=1.0)
    usage_records: list[dict[str, Any]] = Field(default_factory=list)
    reply: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _normalize_contract(self) -> FastIntentDecision:
        if self.route == "fast_reply":
            text = self.reply.strip()
            if not text:
                raise ValueError("fast_reply requires reply")
            object.__setattr__(self, "reply", text)
        else:
            object.__setattr__(self, "reply", "")
        return self


class FastIntentNode:
    """Low-latency routing node for simple turns."""

    def __init__(self, *, prompt_registry: PromptRegistry | None = None, prompt_version: str | None = None) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._prompt_version = prompt_version

    def prompt_metadata(self) -> dict[str, Any]:
        return self._prompt_registry.load("fast_intent", self._prompt_version).metadata()

    def decide(
        self,
        *,
        content: str,
        session_state: ConversationSessionState | None = None,
        conversation_metadata: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
        conversation_context: ConversationContext | None = None,
        runtime_hints: dict[str, Any] | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> FastIntentDecision:
        text = (content or "").strip()
        if not text:
            raise ValueError("content must not be empty")

        session = session_state or ConversationSessionState()
        prompt = self._prompt_registry.load("fast_intent", self._prompt_version)
        resolved = ModelRouter().resolve(LLMNode.INTENT_CLASSIFIER, _fast_intent_model_metadata(prompt.model_profile))
        if not resolved.profile.api_key:
            logger.info("fast intent llm skipped reason=missing_api_key profile=%s", resolved.profile.id)
            return FastIntentDecision(route="needs_plan", confidence=0.5, reason="missing LLM API key")

        response = resolved.client.chat_normalized(
            _fast_intent_messages(
                text,
                session_state=session,
                recent_artifacts=recent_artifacts or [],
                conversation_context=conversation_context,
                runtime_hints=runtime_hints,
                runtime_context=runtime_context,
                prompt_registry=self._prompt_registry,
                prompt_version=self._prompt_version,
            ),
            tools=_fast_intent_virtual_tools(),
            tool_choice="auto",
        )
        return _decision_from_response(response)


def _fast_intent_messages(
    text: str,
    *,
    session_state: ConversationSessionState,
    recent_artifacts: list[dict[str, Any]],
    conversation_context: ConversationContext | None = None,
    runtime_hints: dict[str, Any] | None = None,
    runtime_context: RuntimeContext | None = None,
    prompt_registry: PromptRegistry | None = None,
    prompt_version: str | None = None,
) -> list[LLMMessage]:
    registry = prompt_registry or PromptRegistry()
    prompt = registry.load("fast_intent", prompt_version)
    resolved_runtime_context = runtime_context or RuntimeContext.from_hints(runtime_hints)
    return prompt.render(
        {
            "input_json": json.dumps(
                {
                    "session_mode": session_state.session_mode,
                    "active_repo_id": session_state.active_repo_id,
                    "session_goal": session_state.session_goal,
                    "working_summary": session_state.working_summary,
                    "temporal_context": _temporal_context(resolved_runtime_context),
                    "conversation_context": (
                        conversation_context.fast_payload()
                        if conversation_context is not None
                        else {
                            "has_history": False,
                            "context_reference_detected": False,
                            "summary": None,
                            "recent_messages": [],
                        }
                    ),
                    "recent_artifacts": recent_artifacts,
                    "message": text,
                },
                ensure_ascii=False,
            )
        }
    )


def _temporal_context(runtime_context: RuntimeContext) -> dict[str, str]:
    return runtime_context.temporal.as_payload()


def _fast_intent_model_metadata(model_profile: str | None) -> dict[str, Any]:
    if not model_profile:
        return {}
    return {"model_overrides": {LLMNode.INTENT_CLASSIFIER.value: model_profile}}


def _decision_from_payload(payload: dict[str, Any]) -> FastIntentDecision:
    nested = payload.get("decision") if isinstance(payload.get("decision"), dict) else payload.get("fast_intent")
    candidate = dict(nested if isinstance(nested, dict) else payload)
    candidate["route"] = _normalize_route(candidate.get("route"))
    candidate["confidence"] = _coerce_confidence(candidate.get("confidence"))
    if isinstance(candidate.get("reply"), str):
        candidate["reply"] = candidate["reply"].strip()
    try:
        return FastIntentDecision.model_validate(candidate)
    except ValidationError as exc:
        logger.info("fast intent payload rejected; needs_plan error=%s", exc)
        return FastIntentDecision(route="needs_plan", confidence=0.5, reason="invalid fast intent payload")


def _decision_from_response(response: NormalizedLLMResponse) -> FastIntentDecision:
    if response.tool_calls:
        return _with_response_usage(_decision_from_tool_call(response.tool_calls[0]), response, stage="fast_intent")

    reply = (response.content or "").strip()
    if reply:
        payload = parse_json_content({"content": reply})
        if payload:
            return _with_response_usage(_decision_from_payload(payload), response, stage="fast_intent")
        return _with_response_usage(
            FastIntentDecision(
                route="fast_reply",
                confidence=1.0,
                reply=reply,
                reason="fast intent returned assistant content",
            ),
            response,
            stage="fast_intent",
        )

    logger.info("fast intent returned no content or tool call; needs_plan")
    return _with_response_usage(
        FastIntentDecision(route="needs_plan", confidence=0.0, reason="empty fast intent response"),
        response,
        stage="fast_intent",
    )


def _with_response_usage(
    decision: FastIntentDecision,
    response: NormalizedLLMResponse,
    *,
    stage: str,
) -> FastIntentDecision:
    record = usage_record_from_response(response, stage=stage)
    if record is None:
        return decision
    decision.usage_records.append(record)
    return decision


def _decision_from_tool_call(tool_call: NormalizedToolCall) -> FastIntentDecision:
    name = tool_call.name.strip()
    args = dict(tool_call.args)
    confidence = _coerce_confidence(args.get("confidence", 0.9))
    reason = str(args.get("reason") or f"fast intent virtual tool: {name}").strip()
    if name == "needs_plan":
        return FastIntentDecision(
            route="needs_plan",
            confidence=confidence,
            reason=reason,
        )

    logger.info("fast intent unknown virtual tool rejected tool=%s", name)
    return FastIntentDecision(
        route="needs_plan",
        confidence=min(confidence, 0.5),
        reason=f"unknown fast intent virtual tool: {name}",
    )


def _normalize_route(value: Any) -> FastIntentRoute:
    text = str(value or "").strip()
    if text in {"fast_reply", "needs_plan"}:
        return text  # type: ignore[return-value]
    if text in {"reply", "fast_reply", "fast"}:
        return "fast_reply"
    return "needs_plan"


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"high", "certain", "confident"}:
            return 0.9
        if normalized in {"medium", "moderate"}:
            return 0.75
        if normalized in {"low", "uncertain"}:
            return 0.4
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _fast_intent_virtual_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "needs_plan",
                "description": (
                    "Route any executable, tool-backed, artifact-dependent, current-information, repository, "
                    "file, scheduled, delivery, ambiguous, or multi-step work to the Planner."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["confidence", "reason"],
                    "properties": {
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Confidence that the turn should be planned rather than answered directly.",
                        },
                        "reason": {"type": "string", "description": "Brief routing rationale."},
                    },
                },
            },
        },
    ]
