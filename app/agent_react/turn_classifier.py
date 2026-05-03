from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from app.agent_react.session_state import ConversationSessionState, SessionMode
from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage, parse_json_content

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

    hard = _hard_rule_classification(text)
    if hard is not None:
        return hard

    llm = _llm_classification(text, current)
    if llm is not None:
        return llm

    return _fallback_classification(text, current)


def should_apply_session_mode_update(classification: TurnClassification) -> bool:
    if classification.session_mode_update is None:
        return False
    if classification.source == "hard_rule":
        return True
    return classification.confidence >= _SESSION_UPDATE_THRESHOLD


def _hard_rule_classification(text: str) -> TurnClassification | None:
    lowered = text.lower()
    command = lowered.split(maxsplit=1)[0] if lowered.startswith("/") else ""
    if command in {"/status", "/cancel", "/clear"}:
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
                "multi-step research, coding, summarization, or image generation."
            ),
        ),
        LLMMessage(
            role="user",
            content=(
                "{"
                f'"session_mode":"{session_state.session_mode}",'
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
    if any(
        marker in lowered
        for marker in (
            "code",
            "代码",
            "重构",
            "bug",
            "repo",
            "repository",
            "inspect",
            "workspace",
            "git",
            "test",
            "测试",
            ".py",
            ".ts",
            ".js",
        )
    ):
        return TurnClassification(
            turn_type="coding",
            session_mode_update="coding",
            confidence=0.7,
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
