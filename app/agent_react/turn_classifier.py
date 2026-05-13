from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field, replace
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
    "response.text",
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
    "artifact.deliver",
    "artifact.generate",
    "artifact.revise",
    "image.generate",
    "reminder.manage",
]
TargetResourceType: TypeAlias = Literal["repository", "knowledge_base", "conversation", "external_service"]

_TURN_TYPES = {"chat", "research", "coding", "summary", "command", "image_generation"}
_SESSION_MODES = {"chat", "research", "coding"}
_CAPABILITIES = {
    "response.text",
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
    "artifact.deliver",
    "artifact.generate",
    "artifact.revise",
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
    task_plan: dict[str, Any] = field(default_factory=dict)
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
        "task_plan": dict(classification.task_plan),
        "routing_basis": classification.routing_basis,
    }


def classify_turn(
    *,
    content: str,
    session_state: ConversationSessionState | None,
    conversation_metadata: dict[str, Any] | None = None,
    recent_artifacts: list[dict[str, Any]] | None = None,
) -> TurnClassification:
    text = (content or "").strip()
    current = session_state or ConversationSessionState()
    active_repo_id = _detect_registered_repo_reference(text)

    hard = _hard_rule_classification(text)
    if hard is not None:
        return _with_target_resource(hard, active_repo_id)

    llm = _llm_classification(text, current, conversation_metadata, recent_artifacts=recent_artifacts)
    if llm is not None:
        return _with_target_resource(llm, active_repo_id)

    return _with_target_resource(_fallback_classification(text, current, active_repo_id=active_repo_id), active_repo_id)


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
    recent_artifacts: list[dict[str, Any]] | None = None,
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
                "summary, command, image_generation. Use command only for explicit Jarvis "
                "runtime control slash commands such as /status, /clear, /cancel, /repos, "
                "or /model. Do not use command for ordinary user tasks such as reminders, "
                "file delivery, web search, repository work, or knowledge-base operations; "
                "use chat/research/coding/image_generation plus requested_capabilities instead. "
                "Allowed session_mode_update values: "
                "chat, research, coding, or null. Return requested_capabilities as atomic labels "
                "from: response.text, web.search, kb.search, kb.write, workspace.read_file, workspace.search_files, "
                "workspace.inspect, workspace.edit, workspace.test, workspace.report, research.deep, "
                "artifact.deliver, artifact.generate, artifact.revise, image.generate, reminder.manage. "
                "Use reminder.manage for explicit reminder, timed notification, wake-up, "
                "reminder list, or reminder cancellation requests. "
                "Use workspace.read_file only when the user needs text content, excerpts, or metadata from a known local workspace file. "
                "Use workspace.search_files for lightweight workspace path lookup, file existence checks, "
                "or bounded text search. "
                "Use workspace.inspect for local repository understanding that needs multi-file reasoning, "
                "project/repository status, git status, code review, architecture analysis, runtime design, "
                "or prior local work products. "
                "Use workspace.report when the user asks for a local project report/review. "
                "Use artifact.deliver when the final user-visible output is an existing local file, image, document, "
                "or prior artifact delivered to the remote conversation. Do not use workspace.read_file as the "
                "final capability for binary file delivery. "
                "Use artifact.generate when the user asks to create a new user-visible file or artifact. "
                "Use artifact.revise when the user asks to modify a prior artifact and return the updated artifact. "
                "Use response.text when the final output is only a text answer and no special delivery is needed. "
                "Return target_resources only for clearly "
                "referenced repositories, knowledge bases, conversations, or external services. "
                "Prefer chat unless the user asks for "
                "multi-step research, repository/code work, summarization, or image generation. "
                "Messages that ask to switch to a repo/project, modify files, write code, fix bugs, "
                "run tests, inspect git status, or work inside a named repository are coding turns. "
                "Use web.search for latest/current/recent/time-sensitive facts. "
                "Also return task_plan as a compact object describing the current turn objective, "
                "targets, output shape, target artifacts, evidence policy, expected steps, final deliverable, and execution notes. "
                "Plan from the full conversational context and recent_artifacts semantically: resolve whether "
                "the user is continuing, revising, delivering, or replacing a prior artifact, and include the "
                "relevant artifact id or filename when it is part of the plan."
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
                        "recent_artifacts": recent_artifacts or [],
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

    return _classification_from_payload(payload, text=text)


def _classification_from_payload(payload: dict[str, Any], *, text: str = "") -> TurnClassification | None:
    raw_turn_type = payload.get("turn_type")
    if raw_turn_type not in _TURN_TYPES:
        return None

    confidence = _coerce_payload_confidence(payload)
    turn_type: TurnType = raw_turn_type  # type: ignore[assignment]
    if turn_type == "command" and not text.lstrip().startswith("/"):
        turn_type = "chat"
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
    task_plan = _coerce_task_plan(payload.get("task_plan"))
    routing_basis = _coerce_routing_basis(payload.get("routing_basis"), default="inferred")

    return TurnClassification(
        turn_type=turn_type,
        session_mode_update=session_update,
        requested_capabilities=requested_capabilities,
        target_resources=target_resources,
        task_plan=task_plan,
        routing_basis=routing_basis,
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip()[:160],
        source="llm",
    )


def _fallback_classification(
    text: str,
    session_state: ConversationSessionState,
    *,
    active_repo_id: str | None = None,
) -> TurnClassification:
    if text.startswith("/"):
        return TurnClassification(
            turn_type="command",
            routing_basis="fallback",
            confidence=0.7,
            reason="slash command fallback",
            source="fallback",
        )
    if active_repo_id and _looks_like_registered_repo_status_request(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            requested_capabilities=("workspace.inspect",),
            routing_basis="explicit",
            confidence=0.85,
            reason="registered repository status request",
            source="fallback",
        )
    return TurnClassification(turn_type="chat", confidence=0.6, reason="default chat", source="fallback")


def _with_target_resource(classification: TurnClassification, active_repo_id: str | None) -> TurnClassification:
    if not active_repo_id:
        return classification
    resources = classification.target_resources
    if not any(resource.type == "repository" and resource.id == active_repo_id for resource in resources):
        resources = (*resources, TargetResource(type="repository", id=active_repo_id))
    active_repo_id_update = classification.active_repo_id_update
    if active_repo_id_update is None and _classification_targets_workspace(classification):
        active_repo_id_update = active_repo_id
    if resources == classification.target_resources and active_repo_id_update == classification.active_repo_id_update:
        return classification
    return replace(classification, target_resources=resources, active_repo_id_update=active_repo_id_update)


def _target_resources_for_repo(repo_id: str | None) -> tuple[TargetResource, ...]:
    if not repo_id:
        return ()
    return (TargetResource(type="repository", id=repo_id),)


def _classification_targets_workspace(classification: TurnClassification) -> bool:
    if classification.turn_type == "coding":
        return True
    return any(str(capability).startswith("workspace.") for capability in classification.requested_capabilities)


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


def _looks_like_registered_repo_status_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "项目状态",
        "仓库状态",
        "当前状态",
        "现在状态",
        "状态",
        "git status",
        "repo status",
        "repository status",
        "working tree",
        "工作区",
        "分支",
        "branch",
        "未提交",
        "未暂存",
        "未跟踪",
        "uncommitted",
        "untracked",
        "dirty",
        "ahead",
        "behind",
        "最近提交",
        "latest commit",
        "recent commit",
    )
    return any(marker in lowered for marker in markers)


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


def _coerce_task_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_keys = {
        "objective",
        "targets",
        "target_artifacts",
        "output",
        "evidence_policy",
        "expected_steps",
        "final_deliverable",
        "execution_notes",
    }
    plan: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in value:
            continue
        coerced = _coerce_task_plan_value(value[key])
        if coerced not in (None, "", [], {}):
            plan[key] = coerced
    return plan


def _coerce_task_plan_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()[:1000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        result: list[Any] = []
        for item in value[:10]:
            coerced = _coerce_task_plan_value(item)
            if coerced not in (None, "", [], {}):
                result.append(coerced)
        return result
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            if not isinstance(key, str):
                continue
            coerced = _coerce_task_plan_value(item)
            if coerced not in (None, "", [], {}):
                result[key[:80]] = coerced
        return result
    return str(value).strip()[:1000]


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
