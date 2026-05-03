from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

from app.agent_react.session_state import ConversationSessionState, SessionMode
from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage, parse_json_content
from app.repositories import RepositoryRegistryError, get_repository_registry

logger = logging.getLogger(__name__)

TurnType: TypeAlias = Literal["chat", "research", "coding", "summary", "command", "image_generation"]

_TURN_TYPES = {"chat", "research", "coding", "summary", "command", "image_generation"}
_SESSION_MODES = {"chat", "research", "coding"}
_CONFIDENCE_THRESHOLD = 0.65
_SESSION_UPDATE_THRESHOLD = 0.75


@dataclass(frozen=True)
class TurnClassification:
    turn_type: TurnType
    session_mode_update: SessionMode | None = None
    active_repo_id_update: str | None = None
    confidence: float = 1.0
    reason: str = ""
    source: str = "fallback"


def classify_turn(
    *,
    content: str,
    session_state: ConversationSessionState | None,
) -> TurnClassification:
    text = (content or "").strip()
    current = session_state or ConversationSessionState()
    active_repo_id = _detect_registered_repo_reference(text)

    hard = _hard_rule_classification(text)
    if hard is not None:
        return _with_repo_update(hard, active_repo_id)

    local = _pre_llm_local_classification(text)
    if local is not None:
        return local

    llm = _llm_classification(text, current)
    if llm is not None:
        return _apply_local_overrides(llm, text, active_repo_id)

    return _with_repo_update(_fallback_classification(text, current), active_repo_id)


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
    if command in {"/status", "/cancel", "/clear", "/repos"}:
        return TurnClassification(turn_type="command", confidence=1.0, reason=command, source="hard_rule")
    if command == "/research":
        return TurnClassification(
            turn_type="research",
            session_mode_update="research",
            confidence=1.0,
            reason="/research command",
            source="hard_rule",
        )
    if command == "/chat":
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            confidence=1.0,
            reason="/chat command",
            source="hard_rule",
        )
    if command == "/coding":
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            confidence=1.0,
            reason="/coding command",
            source="hard_rule",
        )
    return None


def _llm_classification(text: str, session_state: ConversationSessionState) -> TurnClassification | None:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None

    client = ChatClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=min(float(settings.llm_timeout_seconds), 10.0),
    )
    messages = [
        LLMMessage(
            role="system",
            content=(
                "Classify the next Jarvis turn. Return compact JSON only. "
                "Do not answer the user. Allowed turn_type values: chat, research, coding, "
                "summary, command, image_generation. Allowed session_mode_update values: "
                "chat, research, coding, or null. Prefer chat unless the user asks for "
                "multi-step research, repository/code work, summarization, or image generation. "
                "Messages that ask to switch to a repo/project, modify files, write code, fix bugs, "
                "run tests, inspect git status, or work inside a named repository are coding turns."
            ),
        ),
        LLMMessage(
            role="user",
            content=(
                "{"
                f'"session_mode":"{session_state.session_mode}",'
                f'"active_repo_id":{_json_str(session_state.active_repo_id)},'
                f'"session_goal":{_json_str(session_state.session_goal)},'
                f'"working_summary":{_json_str(session_state.working_summary)},'
                f'"last_turn_status":{_json_str(session_state.last_turn_status)},'
                f'"message":{_json_str(text)}'
                "}"
            ),
        ),
    ]

    try:
        response = client.chat(messages, response_format={"type": "json_object"})
        payload = parse_json_content(response)
    except Exception:
        logger.exception("turn classifier llm call failed")
        return None

    return _classification_from_payload(payload)


def _classification_from_payload(payload: dict[str, Any]) -> TurnClassification | None:
    raw_turn_type = payload.get("turn_type")
    if raw_turn_type not in _TURN_TYPES:
        return None

    confidence = _coerce_confidence(payload.get("confidence"))
    turn_type: TurnType = raw_turn_type  # type: ignore[assignment]
    if confidence < _CONFIDENCE_THRESHOLD:
        return TurnClassification(
            turn_type="chat",
            session_mode_update=None,
            confidence=confidence,
            reason="low classifier confidence",
            source="llm",
        )

    raw_session_update = payload.get("session_mode_update")
    session_update: SessionMode | None = None
    if raw_session_update in _SESSION_MODES:
        session_update = raw_session_update  # type: ignore[assignment]

    return TurnClassification(
        turn_type=turn_type,
        session_mode_update=session_update,
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip()[:160],
        source="llm",
    )


def _pre_llm_local_classification(text: str) -> TurnClassification | None:
    if _looks_like_current_info_request(text) or _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            active_repo_id_update=None,
            confidence=0.85,
            reason="current-info or non-code topic switch local rule",
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
            confidence=0.7,
            reason="continue current session mode",
            source="fallback",
        )

    if _looks_like_current_info_request(text) or _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            confidence=0.85,
            reason="current-info or non-code topic switch fallback",
            source="fallback",
        )

    if any(marker in lowered for marker in ("research", "deep research", "调研", "研究", "对比", "竞品", "报告")):
        return TurnClassification(
            turn_type="research",
            session_mode_update="research",
            confidence=0.7,
            reason="research fallback marker",
            source="fallback",
        )
    if any(marker in lowered for marker in ("search", "搜索", "查找", "查一下")):
        return TurnClassification(turn_type="chat", confidence=0.7, reason="search fallback marker", source="fallback")
    if _looks_like_code_request(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            confidence=0.8,
            reason="coding fallback marker",
            source="fallback",
        )
    if any(marker in lowered for marker in ("总结", "summary", "summarize")):
        return TurnClassification(turn_type="summary", confidence=0.7, reason="summary fallback marker", source="fallback")
    if any(marker in lowered for marker in ("画图", "image", "图片", "生成图")):
        return TurnClassification(
            turn_type="image_generation",
            confidence=0.7,
            reason="image fallback marker",
            source="fallback",
        )
    if lowered.startswith("/"):
        return TurnClassification(turn_type="command", confidence=0.7, reason="slash command fallback", source="fallback")
    return TurnClassification(turn_type="chat", confidence=0.6, reason="default chat", source="fallback")


def _apply_local_overrides(
    classification: TurnClassification,
    text: str,
    active_repo_id: str | None,
) -> TurnClassification:
    if _looks_like_current_info_request(text) or _explicitly_leaves_code_context(text):
        return TurnClassification(
            turn_type="chat",
            session_mode_update="chat",
            active_repo_id_update=None,
            confidence=max(classification.confidence, 0.85),
            reason="current-info or non-code topic switch override",
            source="local_override",
        )
    if active_repo_id and _looks_like_repository_work(text):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            confidence=max(classification.confidence, 0.85),
            reason=f"explicit registered repo coding request: {active_repo_id}",
            source="local_override",
        )
    if _looks_like_code_request(text) and classification.turn_type == "chat":
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            active_repo_id_update=active_repo_id,
            confidence=max(classification.confidence, 0.8),
            reason="explicit code request override",
            source="local_override",
        )
    return _with_repo_update(classification, active_repo_id)


def _with_repo_update(classification: TurnClassification, active_repo_id: str | None) -> TurnClassification:
    if not active_repo_id:
        return classification
    if classification.active_repo_id_update == active_repo_id:
        return classification
    return replace(classification, active_repo_id_update=active_repo_id)


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


def _json_str(value: str | None) -> str:
    if value is None:
        return "null"
    import json

    return json.dumps(value, ensure_ascii=False)
