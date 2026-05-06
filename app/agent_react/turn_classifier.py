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

    local = _pre_llm_local_classification(text, active_repo_id)
    if local is not None:
        return local

    llm = _llm_classification(text, current, conversation_metadata)
    if llm is not None:
        return _apply_local_overrides(llm, text, active_repo_id, current)

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
    return classification.confidence >= _SESSION_UPDATE_THRESHOLD or classification.source in {"hard_rule", "local_override"}


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
                "from: web.search, kb.search, kb.write, workspace.inspect, workspace.edit, "
                "workspace.test, workspace.report, research.deep, image.generate, reminder.manage. "
                "Use reminder.manage for explicit reminder, timed notification, wake-up, "
                "reminder list, or reminder cancellation requests. "
                "Use workspace.inspect for reading local repositories, source code, logs, "
                "project docs, architecture, runtime design, or prior local work products. "
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


def _pre_llm_local_classification(text: str, active_repo_id: str | None) -> TurnClassification | None:
    if active_repo_id and _looks_like_repository_work(text) and not _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            requested_capabilities=_capabilities_for_text(text, turn_type="coding"),
            target_resources=_target_resources_for_repo(active_repo_id),
            routing_basis="explicit",
            confidence=0.85,
            reason=f"explicit registered repo coding request: {active_repo_id}",
            source="local_override",
        )
    return None


def _fallback_classification(text: str, session_state: ConversationSessionState) -> TurnClassification:
    lowered = text.lower()
    compact = lowered.strip()

    if session_state.session_mode in {"research", "coding"} and compact in {
        "继续",
        "继续吧",
        "下一步",
        "展开",
        "continue",
        "next",
    }:
        return TurnClassification(
            turn_type=session_state.session_mode,
            requested_capabilities=_capabilities_for_text(text, turn_type=session_state.session_mode),
            routing_basis="contextual",
            confidence=0.7,
            reason="continue current session mode",
            source="fallback",
        )

    if _looks_like_workspace_followup(text) and session_state.active_repo_id:
        capabilities = _capabilities_for_text(text, turn_type="research")
        if "workspace.inspect" not in capabilities:
            capabilities = (*capabilities, "workspace.inspect")
        return TurnClassification(
            turn_type="research",
            session_mode_update="research",
            requested_capabilities=capabilities,
            target_resources=_target_resources_for_repo(session_state.active_repo_id),
            routing_basis="contextual",
            confidence=0.75,
            reason="workspace follow-up fallback",
            source="fallback",
        )

    if _looks_like_current_info_request(text) or _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            requested_capabilities=("web.search",),
            routing_basis="explicit",
            confidence=0.85,
            reason="current-info or non-code topic switch fallback",
            source="fallback",
        )

    if session_state.session_mode == "coding" and session_state.active_repo_id:
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=session_state.active_repo_id,
            requested_capabilities=_capabilities_for_text(text, turn_type="coding"),
            target_resources=_target_resources_for_repo(session_state.active_repo_id),
            routing_basis="contextual",
            confidence=0.72,
            reason="preserve coding session after classifier fallback",
            source="fallback",
        )

    if any(marker in lowered for marker in ("research", "deep research", "调研", "研究", "对比", "竞品", "报告")):
        return TurnClassification(
            turn_type="research",
            session_mode_update="research",
            requested_capabilities=_capabilities_for_text(text, turn_type="research"),
            routing_basis="inferred",
            confidence=0.7,
            reason="research fallback marker",
            source="fallback",
        )
    if any(marker in lowered for marker in ("search", "搜索", "查找", "查一下")):
        return TurnClassification(
            turn_type="chat",
            requested_capabilities=("web.search",),
            routing_basis="inferred",
            confidence=0.7,
            reason="search fallback marker",
            source="fallback",
        )
    if _looks_like_knowledge_write_request(text):
        return TurnClassification(
            turn_type="chat",
            requested_capabilities=("kb.write",),
            target_resources=(TargetResource(type="knowledge_base", name="wiki"),),
            routing_basis="inferred",
            confidence=0.75,
            reason="knowledge write fallback marker",
            source="fallback",
        )
    if _looks_like_code_request(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            requested_capabilities=_capabilities_for_text(text, turn_type="coding"),
            routing_basis="inferred",
            confidence=0.8,
            reason="coding fallback marker",
            source="fallback",
        )
    if any(marker in lowered for marker in ("总结", "summary", "summarize")):
        return TurnClassification(
            turn_type="summary",
            routing_basis="inferred",
            confidence=0.7,
            reason="summary fallback marker",
            source="fallback",
        )
    if any(marker in lowered for marker in ("画图", "image", "图片", "生成图")):
        return TurnClassification(
            turn_type="image_generation",
            requested_capabilities=("image.generate",),
            routing_basis="inferred",
            confidence=0.7,
            reason="image fallback marker",
            source="fallback",
        )
    if lowered.startswith("/"):
        return TurnClassification(
            turn_type="command",
            routing_basis="fallback",
            confidence=0.7,
            reason="slash command fallback",
            source="fallback",
        )
    return TurnClassification(turn_type="chat", confidence=0.6, reason="default chat", source="fallback")


def _apply_local_overrides(
    classification: TurnClassification,
    text: str,
    active_repo_id: str | None,
    session_state: ConversationSessionState,
) -> TurnClassification:
    if _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            active_repo_id_update=None,
            requested_capabilities=("web.search",),
            routing_basis="explicit",
            confidence=max(classification.confidence, 0.85),
            reason="explicit non-code topic switch override",
            source="local_override",
        )
    if active_repo_id and _looks_like_repository_work(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            requested_capabilities=_capabilities_for_text(text, turn_type="coding"),
            target_resources=_target_resources_for_repo(active_repo_id),
            routing_basis="explicit",
            confidence=max(classification.confidence, 0.85),
            reason=f"explicit registered repo coding request: {active_repo_id}",
            source="local_override",
        )
    if _looks_like_code_request(text) and classification.turn_type == "chat":
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            requested_capabilities=_capabilities_for_text(text, turn_type="coding"),
            target_resources=_target_resources_for_repo(active_repo_id),
            routing_basis="explicit" if active_repo_id else "inferred",
            confidence=max(classification.confidence, 0.8),
            reason="explicit code request override",
            source="local_override",
        )
    if _looks_like_workspace_followup(text) and session_state.active_repo_id:
        capabilities = classification.requested_capabilities
        if "workspace.inspect" not in capabilities:
            capabilities = (*capabilities, "workspace.inspect")
        if _looks_like_current_info_request(text) and "web.search" not in capabilities:
            capabilities = (*capabilities, "web.search")
        resources = classification.target_resources
        if not any(
            resource.type == "repository" and resource.id == session_state.active_repo_id for resource in resources
        ):
            resources = (*resources, TargetResource(type="repository", id=session_state.active_repo_id))
        classification = replace(
            classification,
            requested_capabilities=capabilities,
            target_resources=resources,
        )
    if _looks_like_current_info_request(text) and "web.search" not in classification.requested_capabilities:
        classification = replace(
            classification,
            requested_capabilities=(*classification.requested_capabilities, "web.search"),
        )
    return _with_target_resource(classification, active_repo_id)


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


def _capabilities_for_text(text: str, *, turn_type: str) -> tuple[Capability, ...]:
    capabilities: list[Capability] = []
    if turn_type == "research":
        capabilities.append("research.deep")
    if turn_type == "image_generation":
        capabilities.append("image.generate")
    if turn_type == "coding":
        capabilities.append("workspace.inspect")
        lowered = text.lower()
        if any(marker in lowered for marker in ("修改", "修复", "fix", "写个", "写一个", "重构", "edit", "implement")):
            capabilities.append("workspace.edit")
        if any(marker in lowered for marker in ("test", "测试", "pytest", "验证")):
            capabilities.append("workspace.test")
        if any(marker in lowered for marker in ("报告", "review", "评审", "总结", "设计", "架构")):
            capabilities.append("workspace.report")
    if _looks_like_current_info_request(text):
        capabilities.append("web.search")
    return tuple(dict.fromkeys(capabilities))


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


def _looks_like_repository_work(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "切换",
            "项目",
            "仓库",
            "repo",
            "repository",
            "代码",
            "code",
            "review",
            "兼容",
            "设计",
            "架构",
            "实现",
            "模块",
            "流程",
            "写个",
            "写一个",
            "修改",
            "修复",
            "fix",
            "test",
            "git",
        )
    )


def _looks_like_code_request(text: str) -> bool:
    lowered = text.lower()
    if _looks_like_current_info_request(text) or _explicitly_leaves_code_context(text):
        return False
    return any(
        marker in lowered
        for marker in (
            "code",
            "代码",
            "review",
            "设计",
            "架构",
            "实现",
            "bug",
            "fix",
            "修改",
            "修复",
            "重构",
            "写个",
            "写一个",
            "repo",
            "repository",
            "仓库",
            "项目",
            "git",
            "test",
            "测试",
            ".py",
            ".ts",
            ".js",
            ".md",
        )
    )


def _looks_like_current_info_request(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "最新",
            "实时",
            "今天",
            "今日",
            "现在",
            "最近",
            "新闻",
            "价格",
            "报价",
            "行情",
            "金价",
            "股价",
            "汇率",
            "查查",
            "查一下",
            "搜索",
            "search",
        )
    )


def _looks_like_knowledge_write_request(text: str) -> bool:
    lowered = text.lower()
    write_marker = any(
        marker in lowered
        for marker in (
            "write",
            "save",
            "record",
            "draft",
            "apply",
            "写入",
            "保存",
            "记录",
            "记到",
            "整理到",
        )
    )
    target_marker = any(
        marker in lowered
        for marker in (
            "wiki",
            "obsidian",
            "knowledge base",
            "memory",
            "note",
            "知识库",
            "长期记忆",
            "记忆",
            "笔记",
        )
    )
    return write_marker and target_marker


def _looks_like_workspace_followup(text: str) -> bool:
    lowered = text.lower()
    referential = any(
        marker in lowered
        for marker in (
            "这个设计",
            "这个方案",
            "这个架构",
            "刚才",
            "上面",
            "上一轮",
            "前面",
            "该设计",
            "这个实现",
            "这个项目",
        )
    )
    workspace_topic = any(
        marker in lowered
        for marker in (
            "设计",
            "架构",
            "实现",
            "代码",
            "源码",
            "runtime",
            "agent",
            "react",
            "对比",
            "借鉴",
            "评审",
            "hermes",
            "claude",
            "codex",
            "openclaw",
        )
    )
    return referential and workspace_topic


def _explicitly_leaves_code_context(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "不看项目",
            "不看仓库",
            "不看代码",
            "不是项目",
            "不是仓库",
            "不是代码",
            "别看项目",
            "别看仓库",
            "别看代码",
            "不用看项目",
            "不用看仓库",
            "不用看代码",
            "no code",
            "not code",
            "not repo",
            "not repository",
        )
    )


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
