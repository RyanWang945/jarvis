from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, TypeAlias

SessionMode: TypeAlias = Literal["chat", "coding", "research"]
WaitingFor: TypeAlias = Literal["user", "approval", "tool", "external"] | None

SESSION_METADATA_KEY = "session"
_SESSION_MODES = {"chat", "coding", "research"}
_WAITING_FOR = {"user", "approval", "tool", "external"}


@dataclass(frozen=True)
class ConversationSessionState:
    version: int = 1
    session_mode: SessionMode = "chat"
    active_repo_id: str | None = None
    session_goal: str | None = None
    working_summary: str | None = None
    waiting_for: WaitingFor = None
    last_turn_id: int | None = None
    last_turn_status: str | None = None
    last_assistant_summary: str | None = None
    updated_by_turn_id: int | None = None


def load_session_state(metadata: dict[str, Any] | None) -> ConversationSessionState:
    if not isinstance(metadata, dict):
        return ConversationSessionState()
    raw = metadata.get(SESSION_METADATA_KEY)
    if not isinstance(raw, dict):
        return ConversationSessionState()

    return ConversationSessionState(
        version=_coerce_int(raw.get("version"), default=1) or 1,
        session_mode=_coerce_session_mode(raw.get("session_mode")),
        active_repo_id=_coerce_optional_str(raw.get("active_repo_id")),
        session_goal=_coerce_optional_str(raw.get("session_goal")),
        working_summary=_coerce_optional_str(raw.get("working_summary")),
        waiting_for=_coerce_waiting_for(raw.get("waiting_for")),
        last_turn_id=_coerce_int(raw.get("last_turn_id")),
        last_turn_status=_coerce_optional_str(raw.get("last_turn_status")),
        last_assistant_summary=_coerce_optional_str(raw.get("last_assistant_summary")),
        updated_by_turn_id=_coerce_int(raw.get("updated_by_turn_id")),
    )


def dump_session_state(state: ConversationSessionState) -> dict[str, Any]:
    return {SESSION_METADATA_KEY: asdict(state)}


def render_session_state(state: ConversationSessionState) -> str:
    return (
        "Session State\n"
        f"Mode: {state.session_mode}\n"
        f"Active repo: {_display_value(state.active_repo_id)}\n"
        f"Goal: {_display_value(state.session_goal)}\n"
        f"Waiting: {_display_value(state.waiting_for)}\n"
        f"Working summary: {_display_value(state.working_summary)}\n"
        f"Last turn: {_display_value(state.last_turn_id)} / {_display_value(state.last_turn_status)}\n"
        f"Last assistant: {_display_value(state.last_assistant_summary)}"
    )


def render_session_state_for_model(state: ConversationSessionState) -> str | None:
    lines = [f"Mode: {state.session_mode}"]
    if state.active_repo_id:
        lines.append(f"Active repository: {state.active_repo_id}")
    if state.session_goal:
        lines.append(f"Goal: {state.session_goal}")
    if state.working_summary:
        lines.append(f"Working summary: {state.working_summary}")
    if len(lines) == 1 and state.session_mode == "chat":
        return None
    return "Conversation session state:\n" + "\n".join(lines)


def build_session_state_after_turn(
    state: ConversationSessionState | None,
    *,
    turn_id: int,
    status: str,
    assistant_reply: str | None = None,
) -> ConversationSessionState:
    current = state or ConversationSessionState()
    return replace(
        current,
        waiting_for=None,
        last_turn_id=turn_id,
        last_turn_status=status,
        last_assistant_summary=_summarize_assistant_reply(assistant_reply),
        updated_by_turn_id=turn_id,
    )


def _coerce_session_mode(value: Any) -> SessionMode:
    if isinstance(value, str) and value in _SESSION_MODES:
        return value  # type: ignore[return-value]
    return "chat"


def _coerce_waiting_for(value: Any) -> WaitingFor:
    if value is None:
        return None
    if isinstance(value, str) and value in _WAITING_FOR:
        return value  # type: ignore[return-value]
    return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _summarize_assistant_reply(reply: str | None, *, max_chars: int = 500) -> str | None:
    if not reply:
        return None
    text = " ".join(str(reply).split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
