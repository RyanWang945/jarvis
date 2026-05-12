from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, TypeAlias

from app.agent_react.session_state import ConversationSessionState, SessionMode
from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.repositories import RepositoryRegistryError, get_repository_registry

logger = logging.getLogger(__name__)

TurnType: TypeAlias = Literal["chat", "research", "coding", "summary", "command", "image_generation"]
RoutingBasis: TypeAlias = Literal["explicit", "contextual", "inferred", "fallback"]
Capability: TypeAlias = Literal[
    "web.search",
    "kb.search",
    "kb.read",
    "kb.write",
    "workspace.inspect",
    "workspace.read_file",
    "workspace.search_files",
    "workspace.edit",
    "workspace.test",
    "workspace.report",
    "code.inspect",
    "code.edit",
    "code.test",
    "research.deep",
    "image.generate",
    "reminder.manage",
]
TargetResourceType: TypeAlias = Literal["repository", "knowledge_base", "conversation", "external_service"]

_TURN_TYPES = {"chat", "research", "coding", "summary", "command", "image_generation"}
_SESSION_MODES = {"chat", "research", "coding"}
_CAPABILITIES = {
    "web.search",
    "kb.search",
    "kb.read",
    "kb.write",
    "workspace.inspect",
    "workspace.read_file",
    "workspace.search_files",
    "workspace.edit",
    "workspace.test",
    "workspace.report",
    "code.inspect",
    "code.edit",
    "code.test",
    "research.deep",
    "image.generate",
    "reminder.manage",
}
_TARGET_RESOURCE_TYPES = {"repository", "knowledge_base", "conversation", "external_service"}
_CONFIDENCE_THRESHOLD = 0.65
_SESSION_UPDATE_THRESHOLD = 0.75


@dataclass(frozen=True)
class TargetResource:
    type: TargetResourceType
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class TurnClassification:
    turn_type: TurnType
    session_mode_update: SessionMode | None = None
    active_repo_id_update: str | None = None
    requested_capabilities: tuple[Capability, ...] = ()
    target_resources: tuple[TargetResource, ...] = ()
    routing_basis: RoutingBasis = "fallback"
    confidence: float = 1.0
    reason: str = ""
    source: str = "fallback"


def classification_to_metadata(classification: TurnClassification) -> dict[str, Any]:
    return {
        "source": classification.source,
        "confidence": classification.confidence,
        "reason": classification.reason,
        "session_mode_update": classification.session_mode_update,
        "active_repo_id_update": classification.active_repo_id_update,
        "requested_capabilities": list(classification.requested_capabilities),
        "target_resources": [asdict(resource) for resource in classification.target_resources],
        "routing_basis": classification.routing_basis,
    }


def classify_turn(
    *,
    content: str,
    session_state: ConversationSessionState | None,
    conversation_metadata: dict[str, Any] | None = None,
) -> TurnClassification:
    text = (content or "").strip()
    current = session_state or ConversationSessionState()
    active_repo_id = _detect_registered_repo_reference(text)

    hard = _hard_rule_classification(text)
    if hard is not None:
        return _with_target_resource(hard, active_repo_id)

    llm = _llm_classification(text, current, conversation_metadata)
    if llm is not None:
        return _with_target_resource(llm, active_repo_id)

    return _with_target_resource(_fallback_classification(text, current), active_repo_id)


def should_apply_session_mode_update(classification: TurnClassification) -> bool:
    if classification.session_mode_update is None:
        return False
    if classification.source == "hard_rule":
        return True
    return classification.confidence >= _SESSION_UPDATE_THRESHOLD


def should_apply_repo_update(classification: TurnClassification) -> bool:
    if classification.active_repo_id_update is None:
        return False
    return classification.confidence >= _SESSION_UPDATE_THRESHOLD or classification.source == "hard_rule"


def _hard_rule_classification(text: str) -> TurnClassification | None:
    lowered = text.lower()
    command = lowered.split(maxsplit=1)[0] if lowered.startswith("/") else ""
    if command in {"/status", "/cancel", "/clear", "/repos", "/model"}:
        return TurnClassification(
            turn_type="command",
            confidence=1.0,
            reason=command,
            source="hard_rule",
            routing_basis="explicit",
        )
    if command == "/research":
        return TurnClassification(
            turn_type="research",
            session_mode_update="research",
            requested_capabilities=("research.deep", "web.search", "kb.search"),
            confidence=1.0,
            reason="/research command",
            source="hard_rule",
            routing_basis="explicit",
        )
    if command == "/chat":
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            confidence=1.0,
            reason="/chat command",
            source="hard_rule",
            routing_basis="explicit",
        )
    if command == "/coding":
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            requested_capabilities=("workspace.inspect",),
            confidence=1.0,
            reason="/coding command",
            source="hard_rule",
            routing_basis="explicit",
        )
    return None


def _llm_classification(
    text: str,
    session_state: ConversationSessionState,
    conversation_metadata: dict[str, Any] | None = None,
) -> TurnClassification | None:
    settings = get_settings()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None

    resolved_llm = ModelRouter(settings).resolve(LLMNode.INTENT_CLASSIFIER, conversation_metadata)
    if not resolved_llm.profile.api_key:
        return None
    client = resolved_llm.client
    messages = [
        LLMMessage(
            role="system",
            content=(
                "Classify the next Jarvis turn. Return compact JSON only. "
                "Do not answer the user. Allowed turn_type values: chat, research, coding, "
                "summary, command, image_generation. Allowed session_mode_update values: "
                "chat, research, coding, or null. Return requested_capabilities as labels "
                "from: web.search, kb.search, kb.write, workspace.read_file, workspace.search_files, "
                "workspace.inspect, workspace.edit, workspace.test, workspace.report, research.deep, "
                "image.generate, reminder.manage. "
                "Use reminder.manage for explicit reminder, timed notification, wake-up, "
                "reminder list, or reminder cancellation requests. "
                "Use workspace.read_file for lightweight reading of a known local workspace file. "
                "Use workspace.search_files for lightweight workspace path lookup, file existence checks, "
                "or bounded text search. "
                "Use workspace.inspect for local repository understanding that needs multi-file reasoning, "
                "code review, architecture analysis, runtime design, or prior local work products. "
                "Use workspace.report when the user asks for a local project report/review. "
                "Return target_resources only for clearly "
                "referenced repositories, knowledge bases, conversations, or external services. "
                "Prefer chat unless the user asks for "
                "multi-step research, repository/code work, summarization, or image generation. "
                "Messages that ask to switch to a repo/project, modify files, write code, fix bugs, "
                "run tests, inspect git status, or work inside a named repository are coding turns. "
                "Use web.search for latest/current/recent/time-sensitive facts."
            ),
        ),
        LLMMessage(
            role="user",
            content=(
                json.dumps(
                    {
                        "session_mode": session_state.session_mode,
                        "active_repo_id": session_state.active_repo_id,
                        "registered_repositories": _registered_repositories_for_router(),
                        "session_goal": session_state.session_goal,
                        "working_summary": session_state.working_summary,
                        "last_turn_status": session_state.last_turn_status,
                        "message": text,
                    },
                    ensure_ascii=False,
                )
            ),
        ),
    ]

    try:
        response_format = {"type": "json_object"} if resolved_llm.profile.supports_json_object else None
        response = client.chat_normalized(messages, response_format=response_format)
        payload = parse_json_content({"content": response.content})
        logger.info("turn classifier llm payload=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("turn classifier llm call failed")
        return None

    return _classification_from_payload(payload)


def _classification_from_payload(payload: dict[str, Any]) -> TurnClassification | None:
    raw_turn_type = payload.get("turn_type")
    if raw_turn_type not in _TURN_TYPES:
        return None

    confidence = _coerce_payload_confidence(payload)
    turn_type: TurnType = raw_turn_type  # type: ignore[assignment]
    if confidence < _CONFIDENCE_THRESHOLD:
        return TurnClassification(
            turn_type="chat",
            session_mode_update=None,
            confidence=confidence,
            reason="low classifier confidence",
            source="llm",
            routing_basis="fallback",
        )

    raw_session_update = payload.get("session_mode_update")
    session_update: SessionMode | None = None
    if raw_session_update in _SESSION_MODES:
        session_update = raw_session_update  # type: ignore[assignment]

    requested_capabilities = _coerce_capabilities(payload.get("requested_capabilities"))
    target_resources = _coerce_target_resources(payload.get("target_resources"))
    routing_basis = _coerce_routing_basis(payload.get("routing_basis"), default="inferred")

    return TurnClassification(
        turn_type=turn_type,
        session_mode_update=session_update,
        requested_capabilities=requested_capabilities,
        target_resources=target_resources,
        routing_basis=routing_basis,
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip()[:160],
        source="llm",
    )


def _fallback_classification(text: str, session_state: ConversationSessionState) -> TurnClassification:
    if text.startswith("/"):
        return TurnClassification(
            turn_type="command",
            routing_basis="fallback",
            confidence=0.7,
            reason="slash command fallback",
            source="fallback",
        )
    return TurnClassification(turn_type="chat", confidence=0.6, reason="default chat", source="fallback")


def _with_target_resource(classification: TurnClassification, active_repo_id: str | None) -> TurnClassification:
    if not active_repo_id:
        return classification
    resources = classification.target_resources
    if not any(resource.type == "repository" and resource.id == active_repo_id for resource in resources):
        resources = (*resources, TargetResource(type="repository", id=active_repo_id))
    if resources == classification.target_resources:
        return classification
    return replace(classification, target_resources=resources)


def _target_resources_for_repo(repo_id: str | None) -> tuple[TargetResource, ...]:
    if not repo_id:
        return ()
    return (TargetResource(type="repository", id=repo_id),)


def _detect_registered_repo_reference(text: str) -> str | None:
    lowered = text.lower()
    try:
        repositories = get_repository_registry().list_repositories()
    except (RepositoryRegistryError, OSError):
        logger.exception("failed to load repository registry for turn classification")
        return None
    for repo in sorted(repositories, key=lambda item: len(item.repo_id), reverse=True):
        repo_id = repo.repo_id.lower()
        repo_name = repo.name.lower()
        if _contains_identifier(lowered, repo_id) or (repo_name and repo_name in lowered):
            return repo.repo_id
    return None


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    return bool(re.search(rf"(?<![a-z0-9_-]){re.escape(identifier)}(?![a-z0-9_-])", text))


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(confidence, 0.0), 1.0)


def _coerce_payload_confidence(payload: dict[str, Any]) -> float:
    if "confidence" in payload:
        return _coerce_confidence(payload.get("confidence"))
    if "model_confidence" in payload:
        return _coerce_confidence(payload.get("model_confidence"))
    # DeepSeek often returns compact JSON without optional confidence. Treat a
    # structurally valid classification as usable instead of forcing fallback.
    return 0.8


def _coerce_capabilities(value: Any) -> tuple[Capability, ...]:
    if not isinstance(value, list):
        return ()
    capabilities: list[Capability] = []
    for item in value:
        if isinstance(item, str) and item in _CAPABILITIES and item not in capabilities:
            capabilities.append(item)  # type: ignore[arg-type]
    return tuple(capabilities)


def _coerce_target_resources(value: Any) -> tuple[TargetResource, ...]:
    if not isinstance(value, list):
        return ()
    resources: list[TargetResource] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type")
        if raw_type not in _TARGET_RESOURCE_TYPES:
            continue
        resource = TargetResource(
            type=raw_type,  # type: ignore[arg-type]
            id=_optional_payload_str(item.get("id")),
            name=_optional_payload_str(item.get("name")),
        )
        if resource not in resources:
            resources.append(resource)
    return tuple(resources)


def _coerce_routing_basis(value: Any, *, default: RoutingBasis = "fallback") -> RoutingBasis:
    if value in {"explicit", "contextual", "inferred", "fallback"}:
        return value  # type: ignore[return-value]
    return default


def _optional_payload_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _registered_repositories_for_router() -> list[dict[str, str | None]]:
    try:
        repositories = get_repository_registry().list_repositories()
    except (RepositoryRegistryError, OSError):
        logger.exception("failed to load repository registry for router context")
        return []
    return [{"repo_id": repo.repo_id, "name": repo.name} for repo in repositories]


def _json_str(value: str | None) -> str:
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)
