from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.agent_react.artifacts import ChannelAttachment
from app.agent_react.session_state import ConversationSessionState

if TYPE_CHECKING:
    from app.persistence.models import ConversationRecord, TurnRecord


@dataclass(frozen=True)
class ChannelMessage:
    content: str
    content_type: Literal["text", "markdown"] = "text"
    summary: str | None = None
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnResult:
    turn_id: int
    conversation_id: int
    status: str
    message: ChannelMessage

    @property
    def reply(self) -> str:
        return self.message.content


class ConversationStore(Protocol):
    """Store contract for the DAG turn runtime."""

    def get_conversation(self, conversation_id: int) -> ConversationRecord | None: ...

    def get_turn(self, turn_id: int) -> TurnRecord | None: ...

    def list_messages(self, conversation_id: int) -> list: ...

    def list_turns(self, conversation_id: int) -> list: ...

    def update_conversation_session(
        self,
        conversation_id: int,
        session_state: ConversationSessionState,
    ) -> None: ...

    def update_conversation_metadata(self, conversation_id: int, patch: dict[str, Any]) -> None: ...

    def mark_turn_running(self, turn_id: int) -> None: ...

    def append_assistant_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "markdown",
        external_message_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> Any: ...

    def append_tool_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "text",
        external_message_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> Any: ...

    def create_tool_call(
        self,
        *,
        turn_id: int,
        tool_name: str,
        input: dict,
        assistant_message_id: int | None = None,
        provider_tool_call_id: str | None = None,
        step_index: int = 0,
    ) -> Any: ...

    def update_tool_call(
        self,
        tool_call_id: int,
        *,
        status: str | None = None,
        output: dict | None = None,
        error_message: str | None = None,
    ) -> Any: ...

    def list_tool_calls_by_turn(self, turn_id: int) -> list: ...

    def list_recent_artifacts_by_conversation(self, conversation_id: int, *, limit: int = 5) -> list: ...

    def finalize_turn_success(
        self,
        *,
        turn_id: int,
        conversation_id: int,
        content: str,
        content_type: str = "markdown",
        raw_payload: dict | None = None,
    ) -> Any: ...

    def finalize_turn_failure(
        self,
        turn_id: int,
        *,
        status: str = "failed",
        error_message: str | None = None,
    ) -> None: ...
