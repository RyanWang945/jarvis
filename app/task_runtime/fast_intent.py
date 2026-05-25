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
from app.task_runtime.planner import FinalizationHint, NodeRuntime

logger = logging.getLogger(__name__)

FastIntentRoute = Literal["fast_reply", "needs_plan"]
FAST_INTENT_MODEL_PROFILE = "deepseek-v4-flash"

_ROUTE_RUNTIMES: dict[str, NodeRuntime | None] = {
    "fast_reply": None,
    "needs_plan": None,
}


class FastIntentDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    route: FastIntentRoute
    confidence: float = Field(ge=0.0, le=1.0)
    runtime: NodeRuntime | None = None
    tool_name: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    finalization_hint: FinalizationHint = Field(default_factory=FinalizationHint)
    reply: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _normalize_contract(self) -> FastIntentDecision:
        expected_runtime = _ROUTE_RUNTIMES[self.route]
        if self.runtime is None:
            object.__setattr__(self, "runtime", expected_runtime)
        elif expected_runtime is not None and self.runtime != expected_runtime:
            object.__setattr__(self, "runtime", expected_runtime)
        if self.route == "fast_reply":
            text = self.reply.strip()
            if not text:
                raise ValueError("fast_reply requires reply")
            object.__setattr__(self, "reply", text)
        else:
            object.__setattr__(self, "reply", "")
        object.__setattr__(self, "runtime", None)
        object.__setattr__(self, "tool_name", None)
        object.__setattr__(self, "input_refs", [])
        if self.finalization_hint.mode == "auto" and not self.finalization_hint.reason:
            object.__setattr__(self, "finalization_hint", _default_finalization_hint(self.route))
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
    ) -> FastIntentDecision:
        text = (content or "").strip()
        if not text:
            raise ValueError("content must not be empty")

        session = session_state or ConversationSessionState()
        resolved = ModelRouter().resolve(LLMNode.INTENT_CLASSIFIER, _fast_intent_model_metadata())
        if not resolved.profile.api_key:
            logger.info("fast intent llm skipped reason=missing_api_key profile=%s", resolved.profile.id)
            return FastIntentDecision(route="needs_plan", confidence=0.5, reason="missing LLM API key")

        prompt = self._prompt_registry.load("fast_intent", self._prompt_version)
        response = resolved.client.chat_normalized(
            _fast_intent_messages(
                text,
                session_state=session,
                recent_artifacts=recent_artifacts or [],
                conversation_context=conversation_context,
                runtime_hints=runtime_hints,
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
    prompt_registry: PromptRegistry | None = None,
    prompt_version: str | None = None,
) -> list[LLMMessage]:
    registry = prompt_registry or PromptRegistry()
    prompt = registry.load("fast_intent", prompt_version)
    return prompt.render(
        {
            "input_json": json.dumps(
                {
                    "session_mode": session_state.session_mode,
                    "active_repo_id": session_state.active_repo_id,
                    "session_goal": session_state.session_goal,
                    "working_summary": session_state.working_summary,
                    "temporal_context": _temporal_context(runtime_hints or {}),
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


def _temporal_context(runtime_hints: dict[str, Any]) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "current_date": str(runtime_hints.get("current_date") or "").strip(),
            "current_time": str(runtime_hints.get("current_time") or "").strip(),
            "timezone": str(runtime_hints.get("timezone") or "").strip(),
        }.items()
        if value
    }


def _fast_intent_model_metadata() -> dict[str, Any]:
    return {"model_overrides": {LLMNode.INTENT_CLASSIFIER.value: FAST_INTENT_MODEL_PROFILE}}


def _decision_from_payload(payload: dict[str, Any]) -> FastIntentDecision:
    nested = payload.get("decision") if isinstance(payload.get("decision"), dict) else payload.get("fast_intent")
    candidate = dict(nested if isinstance(nested, dict) else payload)
    runtime = candidate.get("runtime")
    candidate["route"] = _normalize_route(candidate.get("route"), runtime=runtime)
    candidate["confidence"] = _coerce_confidence(candidate.get("confidence"))
    candidate["runtime"] = None
    candidate["tool_name"] = None
    candidate["input_refs"] = []
    candidate["finalization_hint"] = {"mode": "auto"}
    if isinstance(candidate.get("reply"), str):
        candidate["reply"] = candidate["reply"].strip()
    try:
        return FastIntentDecision.model_validate(candidate)
    except ValidationError as exc:
        logger.info("fast intent payload rejected; needs_plan error=%s", exc)
        return FastIntentDecision(route="needs_plan", confidence=0.5, reason="invalid fast intent payload")


def _decision_from_response(response: NormalizedLLMResponse) -> FastIntentDecision:
    if response.tool_calls:
        return _decision_from_tool_call(response.tool_calls[0])

    reply = (response.content or "").strip()
    if reply:
        payload = parse_json_content({"content": reply})
        if payload:
            return _decision_from_payload(payload)
        return FastIntentDecision(
            route="fast_reply",
            confidence=1.0,
            reply=reply,
            reason="fast intent returned assistant content",
        )

    logger.info("fast intent returned no content or tool call; needs_plan")
    return FastIntentDecision(route="needs_plan", confidence=0.0, reason="empty fast intent response")


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


def _normalize_route(value: Any, *, runtime: Any = None) -> FastIntentRoute:
    text = str(value or "").strip()
    if text in _ROUTE_RUNTIMES:
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


def _normalize_finalization_hint(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        hint = dict(value)
    elif isinstance(value, str):
        hint = {"mode": value}
    else:
        return {"mode": "auto"}
    mode = str(hint.get("mode") or "auto").strip().lower()
    if mode not in {"pass_through", "deterministic", "llm", "auto"}:
        mode = "auto"
    hint["mode"] = mode
    hint["reason"] = str(hint.get("reason") or "").strip()
    hint["user_facing"] = bool(hint.get("user_facing"))
    return hint


def _default_finalization_hint(route: FastIntentRoute) -> FinalizationHint:
    if route == "fast_reply":
        return FinalizationHint(
            mode="pass_through",
            user_facing=True,
            reason="fast intent produced a complete user-facing reply",
        )
    return FinalizationHint(mode="auto")


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
