"""AgentRuntime: driver that loads MySQL state, invokes the Turn graph,
and persists tool_call audit records after execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.agent_react.agent_graph import build_turn_graph
from app.agent_react.react_graph import TurnStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelMessage:
    content: str
    content_type: Literal["text", "markdown"] = "text"
    summary: str | None = None
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


class ConversationStore(TurnStore, Protocol):
    """Full store interface for the outer turn lifecycle graph.

    Extends TurnStore (used by the inner ReAct subgraph) with turn-level
    lifecycle and conversation-context methods.
    """

    def list_messages(self, conversation_id: int) -> list: ...

    def mark_turn_running(self, turn_id: int) -> None: ...

    def complete_turn(
        self,
        turn_id: int,
        *,
        status: str,
        summary: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

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


class AgentRuntime:
    """LangGraph-based ReAct runtime for one turn.

    Responsibilities:
    - Turn lifecycle (running / completed / failed)
    - Load MySQL context into graph state
    - Invoke the compiled Turn graph
    - Return TurnResult to callers (API, CLI, Feishu)
    """

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        self._graph = build_turn_graph(store)

    def run_turn(self, turn_id: int) -> TurnResult:
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise ValueError(f"Turn not found: {turn_id}")

        self._store.mark_turn_running(turn_id)

        try:
            result = self._graph.invoke({
                "turn_id": turn_id,
                "conversation_id": turn.conversation_id,
                "trigger_message_id": getattr(turn, "trigger_message_id", None),
                "messages": [],
                "selected_skills": [],
                "reply": "",
                "reply_message_id": None,
                "status": "running",
                "error": None,
            })
        except Exception as exc:
            logger.exception("turn graph failed turn_id=%s", turn_id)
            self._store.finalize_turn_failure(turn_id, error_message=str(exc))
            result = {
                "turn_id": turn_id,
                "conversation_id": turn.conversation_id,
                "trigger_message_id": getattr(turn, "trigger_message_id", None),
                "messages": [],
                "selected_skills": [],
                "reply": "",
                "reply_message_id": None,
                "status": "failed",
                "error": str(exc),
            }

        status = result.get("status", "failed")
        reply = result.get("reply", "")
        error = result.get("error")

        if status == "failed" and error:
            logger.error("turn failed turn_id=%s error=%s", turn_id, error)

        return TurnResult(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            status=status,
            message=ChannelMessage(
                content=reply,
                content_type="markdown",
                summary=reply,
            ),
        )
