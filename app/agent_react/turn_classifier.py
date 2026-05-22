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
Scene: TypeAlias = Literal["chat", "project", "research", "reminder", "control"]
AccessLevel: TypeAlias = Literal["none", "read", "write", "commit", "push"]
TargetResourceType: TypeAlias = Literal["repository", "knowledge_base", "conversation", "external_service"]

_TURN_TYPES = {"chat", "research", "coding", "summary", "command", "image_generation"}
_SCENES = {"chat", "project", "research", "reminder", "control"}
_ACCESS_LEVELS = {"none", "read", "write", "commit", "push"}
_SESSION_MODES = {"chat", "research", "coding"}
_LEGACY_CAPABILITY_LABELS = {
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


@dataclass(frozen=True, init=False)
class TurnClassification:
    turn_type: TurnType
    scene: Scene = "chat"
    access: AccessLevel = "none"
    deliver: bool = False
    session_mode_update: SessionMode | None = None
    active_repo_id_update: str | None = None
    target_resources: tuple[TargetResource, ...] = ()
    task_plan: dict[str, Any] = field(default_factory=dict)
    routing_basis: RoutingBasis = "fallback"
    confidence: float = 1.0
    reason: str = ""
    source: str = "fallback"
    _legacy_capabilities: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def __init__(
        self,
        turn_type: TurnType,
        scene: Scene = "chat",
        access: AccessLevel = "none",
        deliver: bool = False,
        session_mode_update: SessionMode | None = None,
        active_repo_id_update: str | None = None,
        requested_capabilities: tuple[str, ...] = (),
        target_resources: tuple[TargetResource, ...] = (),
        task_plan: dict[str, Any] | None = None,
        routing_basis: RoutingBasis = "fallback",
        confidence: float = 1.0,
        reason: str = "",
        source: str = "fallback",
        _legacy_capabilities: tuple[str, ...] | None = None,
    ) -> None:
        raw_legacy_capabilities = _legacy_capabilities if _legacy_capabilities is not None else requested_capabilities
        legacy_capabilities = tuple(dict.fromkeys(str(item) for item in raw_legacy_capabilities if isinstance(item, str)))
        resolved_scene = scene
        if resolved_scene == "chat" and (turn_type != "chat" or legacy_capabilities):
            resolved_scene = _scene_from_legacy(turn_type, legacy_capabilities)
        resolved_access = access
        if resolved_access == "none":
            resolved_access = _access_from_legacy(turn_type, legacy_capabilities)
        resolved_deliver = deliver or _deliver_from_legacy(legacy_capabilities)

        object.__setattr__(self, "turn_type", turn_type)
        object.__setattr__(self, "scene", resolved_scene)
        object.__setattr__(self, "access", resolved_access)
        object.__setattr__(self, "deliver", resolved_deliver)
        object.__setattr__(self, "session_mode_update", session_mode_update)
        object.__setattr__(self, "active_repo_id_update", active_repo_id_update)
        object.__setattr__(self, "target_resources", target_resources)
        object.__setattr__(self, "task_plan", task_plan or {})
        object.__setattr__(self, "routing_basis", routing_basis)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "_legacy_capabilities", legacy_capabilities)

    @property
    def requested_capabilities(self) -> tuple[str, ...]:
        if self._legacy_capabilities:
            return self._legacy_capabilities
        return _legacy_capabilities_from_scene(self.scene, self.access, self.deliver)


def classification_to_metadata(classification: TurnClassification) -> dict[str, Any]:
    return {
        "source": classification.source,
        "scene": classification.scene,
        "access": classification.access,
        "deliver": classification.deliver,
        "confidence": classification.confidence,
        "reason": classification.reason,
        "session_mode_update": classification.session_mode_update,
        "active_repo_id_update": classification.active_repo_id_update,
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
    logger.info(
        "turn classifier start content_length=%s session_mode=%s active_repo_id=%s metadata_keys=%s recent_artifact_count=%s",
        len(text),
        current.session_mode,
        current.active_repo_id,
        sorted((conversation_metadata or {}).keys()),
        len(recent_artifacts or []),
    )
    active_repo_id = _detect_registered_repo_reference(text)

    hard = _hard_rule_classification(text)
    if hard is not None:
        classification = _with_target_resource(hard, active_repo_id)
        _log_classification_result("hard_rule", classification, detected_repo_id=active_repo_id)
        return classification

    llm = _llm_classification(text, current, conversation_metadata, recent_artifacts=recent_artifacts)
    if llm is not None:
        classification = _with_target_resource(llm, active_repo_id)
        _log_classification_result("llm", classification, detected_repo_id=active_repo_id)
        return classification

    fallback_repo_id = active_repo_id or current.active_repo_id
    fallback = _fallback_classification(text, current, active_repo_id=fallback_repo_id)
    classification = _with_target_resource(fallback, active_repo_id or fallback_repo_id)
    _log_classification_result("fallback", classification, detected_repo_id=active_repo_id)
    return classification


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
            scene="control",
            access="none",
            confidence=1.0,
            reason=command,
            source="hard_rule",
            routing_basis="explicit",
        )
    if command == "/research":
        return TurnClassification(
            turn_type="research",
            scene="research",
            access="read",
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
            scene="chat",
            access="none",
            session_mode_update="chat",
            confidence=1.0,
            reason="/chat command",
            source="hard_rule",
            routing_basis="explicit",
        )
    if command == "/coding":
        return TurnClassification(
            turn_type="coding",
            scene="project",
            access="read",
            session_mode_update="coding",
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
        logger.info("turn classifier llm skipped reason=pytest")
        return None

    resolved_llm = ModelRouter(settings).resolve(LLMNode.INTENT_CLASSIFIER, conversation_metadata)
    if not resolved_llm.profile.api_key:
        logger.info(
            "turn classifier llm skipped reason=missing_api_key profile=%s provider=%s",
            resolved_llm.profile.id,
            resolved_llm.profile.provider,
        )
        return None
    client = resolved_llm.client
    messages = [
        LLMMessage(
            role="system",
            content=(
                "Classify the next Jarvis turn. Return compact JSON only. "
                "Do not answer the user. Prefer the simple routing fields scene, access, and deliver. "
                "Allowed scene values: chat, project, research, reminder, control. "
                "Allowed access values: none, read, write, commit, push. "
                "deliver is a boolean for sending an existing or generated file/image/document back to the user. "
                "Use scene=control only for explicit Jarvis "
                "runtime control slash commands such as /status, /clear, /cancel, /repos, "
                "or /model. Do not use control for ordinary user tasks such as reminders, "
                "file delivery, web search, repository work, or knowledge-base operations; "
                "use chat, project, research, or reminder instead. "
                "Use scene=project for local repository/project work: file lookup, file reading, project status, "
                "git status, architecture analysis, integration planning, code review, edits, tests, commits, pushes, "
                "and generated project artifacts. "
                "If a request combines external/current research with how to use, integrate, adapt, or evaluate the result "
                "inside a named or active local repository, classify it as scene=project with access=read; the task_plan "
                "can still mention that external research evidence is needed. "
                "Use access=read for project lookup, file reading, status checks, review, and architecture understanding. "
                "Use access=write for modifying or generating files, access=commit for commit requests, and access=push for push requests. "
                "Use scene=research for deep/current/external research. Use scene=reminder for reminders and scheduled tasks. "
                "Use scene=chat for ordinary conversation that does not need local project, research, reminder, or control behavior. "
                "For compatibility also return turn_type when obvious, but keep routing based on scene/access/deliver. "
                "Allowed turn_type values: chat, research, coding, summary, command, image_generation. "
                "Allowed session_mode_update values: "
                "chat, research, coding, or null. "
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
        logger.info(
            "turn classifier llm request profile=%s provider=%s response_format=%s",
            resolved_llm.profile.id,
            resolved_llm.profile.provider,
            "json_object" if response_format else "default",
        )
        response = client.chat_normalized(messages, response_format=response_format)
        payload = parse_json_content({"content": response.content})
        logger.info("turn classifier llm payload=%s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("turn classifier llm call failed")
        return None

    classification = _classification_from_payload(payload, text=text)
    if classification is None:
        logger.info("turn classifier llm payload rejected payload_keys=%s", sorted(payload.keys()))
    return classification


def _log_classification_result(
    path: str,
    classification: TurnClassification,
    *,
    detected_repo_id: str | None,
) -> None:
    logger.info(
        "turn classifier result path=%s source=%s turn_type=%s scene=%s access=%s deliver=%s "
        "confidence=%.2f routing_basis=%s detected_repo_id=%s classification=%s",
        path,
        classification.source,
        classification.turn_type,
        classification.scene,
        classification.access,
        classification.deliver,
        classification.confidence,
        classification.routing_basis,
        detected_repo_id,
        json.dumps(classification_to_metadata(classification), ensure_ascii=False),
    )


def _classification_from_payload(payload: dict[str, Any], *, text: str = "") -> TurnClassification | None:
    raw_scene = payload.get("scene")
    scene = raw_scene if raw_scene in _SCENES else None
    raw_turn_type = payload.get("turn_type")
    if raw_turn_type not in _TURN_TYPES and scene is None:
        return None

    confidence = _coerce_payload_confidence(payload)
    legacy_capabilities = _coerce_legacy_capabilities(payload.get("requested_capabilities"))
    access = _coerce_access(payload.get("access"))
    deliver = bool(payload.get("deliver"))
    if scene is None:
        scene = _scene_from_legacy(raw_turn_type, legacy_capabilities)  # type: ignore[arg-type]
    if access is None:
        access = _access_from_legacy(raw_turn_type, legacy_capabilities)
    if not deliver:
        deliver = _deliver_from_legacy(legacy_capabilities)

    turn_type = _turn_type_from_scene(scene, raw_turn_type)
    if (turn_type == "command" or scene == "control") and not text.lstrip().startswith("/"):
        turn_type = "chat"
        scene = "chat"
        access = "none"
    if confidence < _CONFIDENCE_THRESHOLD:
        return TurnClassification(
            turn_type="chat",
            scene="chat",
            access="none",
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

    target_resources = _coerce_target_resources(payload.get("target_resources"))
    task_plan = _coerce_task_plan(payload.get("task_plan"))
    routing_basis = _coerce_routing_basis(payload.get("routing_basis"), default="inferred")

    return TurnClassification(
        turn_type=turn_type,
        scene=scene,
        access=access,
        deliver=deliver,
        session_mode_update=session_update,
        requested_capabilities=legacy_capabilities,
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
            scene="control",
            access="none",
            routing_basis="fallback",
            confidence=0.7,
            reason="slash command fallback",
            source="fallback",
        )
    if active_repo_id and _looks_like_registered_repo_status_request(text):
        return TurnClassification(
            turn_type="coding",
            scene="project",
            access="read",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            routing_basis="explicit",
            confidence=0.85,
            reason="registered repository status request",
            source="fallback",
        )
    project_access = _fallback_project_access(text)
    if project_access is not None:
        return TurnClassification(
            turn_type="coding",
            scene="project",
            access=project_access,
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            routing_basis="fallback",
            confidence=0.75,
            reason="project work fallback",
            source="fallback",
        )
    return TurnClassification(
        turn_type="chat",
        scene="chat",
        access="none",
        confidence=0.6,
        reason="default chat",
        source="fallback",
    )


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
    if classification.scene == "project":
        return True
    if classification.turn_type == "coding":
        return True
    return False


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


def _fallback_project_access(text: str) -> AccessLevel | None:
    lowered = text.lower()
    path_markers = (
        "app/",
        "app\\",
        "tests/",
        "tests\\",
        ".py",
        ".md",
        ".json",
        ".yaml",
        ".yml",
        "readme",
        "dockerfile",
    )
    project_markers = (
        "项目",
        "仓库",
        "repo",
        "repository",
        "code",
        "bug",
        "文件",
        "file",
    )
    if not any(marker in lowered for marker in (*path_markers, *project_markers)):
        return None
    if any(marker in lowered for marker in ("push", "推送")):
        return "push"
    if any(marker in lowered for marker in ("commit", "提交")):
        return "commit"
    if any(marker in lowered for marker in ("fix", "修改", "更新", "改", "edit", "write", "generate", "生成", "创建")):
        return "write"
    if any(marker in lowered for marker in ("inspect", "查看", "看下", "读取", "read", "review", "分析")):
        return "read"
    return None


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


def _coerce_legacy_capabilities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    capabilities: list[str] = []
    for item in value:
        if isinstance(item, str) and item in _LEGACY_CAPABILITY_LABELS and item not in capabilities:
            capabilities.append(item)
    return tuple(capabilities)


def _coerce_access(value: Any) -> AccessLevel | None:
    if value in _ACCESS_LEVELS:
        return value  # type: ignore[return-value]
    return None


def _scene_from_legacy(turn_type: TurnType | str | None, capabilities: tuple[str, ...]) -> Scene:
    capability_set = set(capabilities)
    if turn_type == "command":
        if "reminder.manage" in capability_set:
            return "reminder"
        return "control"
    if "reminder.manage" in capability_set:
        return "reminder"
    if turn_type == "research":
        return "research"
    if turn_type in {"coding", "image_generation"}:
        return "project"
    if any(str(capability).startswith(("workspace.", "code.")) for capability in capability_set) or capability_set & {
        "artifact.generate",
        "artifact.revise",
        "image.generate",
    }:
        return "project"
    if "research.deep" in capability_set or "web.search" in capability_set:
        return "research"
    return "chat"


def _access_from_legacy(turn_type: TurnType | str | None, capabilities: tuple[str, ...]) -> AccessLevel:
    capability_set = set(capabilities)
    if capability_set & {"workspace.edit", "workspace.test", "code.edit", "code.test", "artifact.generate", "artifact.revise", "image.generate"}:
        return "write"
    if (
        turn_type in {"coding", "research", "image_generation"}
        or capability_set & {"workspace.inspect", "workspace.read_file", "workspace.search_files", "workspace.report", "code.inspect"}
    ):
        return "read"
    return "none"


def _deliver_from_legacy(capabilities: tuple[str, ...]) -> bool:
    return "artifact.deliver" in set(capabilities)


def _legacy_capabilities_from_scene(scene: Scene, access: AccessLevel, deliver: bool) -> tuple[str, ...]:
    capabilities: list[str] = []
    if scene == "research":
        capabilities.extend(["research.deep", "web.search"])
    elif scene == "reminder":
        capabilities.append("reminder.manage")
    elif scene == "project":
        if access == "read":
            capabilities.append("workspace.inspect")
        elif access in {"write", "commit", "push"}:
            capabilities.append("workspace.edit")
    if deliver:
        capabilities.append("artifact.deliver")
    return tuple(dict.fromkeys(capabilities))


def _turn_type_from_scene(scene: Scene, legacy_turn_type: Any) -> TurnType:
    if legacy_turn_type in _TURN_TYPES:
        if legacy_turn_type == "command" and scene != "control":
            return "chat"
        return legacy_turn_type  # type: ignore[return-value]
    if scene == "control":
        return "command"
    if scene == "research":
        return "research"
    if scene == "project":
        return "coding"
    return "chat"


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
