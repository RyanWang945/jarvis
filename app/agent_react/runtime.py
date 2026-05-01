"""AgentRuntime: driver that loads MySQL state, invokes the Turn graph,
and persists tool_call audit records after execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage

from app.agent_react.agent_graph import build_turn_graph

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


class ConversationStore(Protocol):
    def get_turn(self, turn_id: int): ...

    def list_messages(self, conversation_id: int): ...

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
    ): ...

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
    ): ...

    def finalize_turn_failure(
        self,
        turn_id: int,
        *,
        status: str = "failed",
        error_message: str | None = None,
    ) -> None: ...

    # tool_calls (Phase 1 persistence)
    def create_tool_call(self, *, turn_id: int, tool_name: str, input: dict): ...

    def update_tool_call(self, tool_call_id: int, *, status: str | None = None, output: dict | None = None, error_message: str | None = None): ...

    def list_tool_calls_by_turn(self, turn_id: int) -> list: ...


class AgentRuntime:
    """LangGraph-based ReAct runtime for one turn.

    Responsibilities:
    - Turn lifecycle (running / completed / failed)
    - Load MySQL context into graph state
    - Invoke the compiled Turn graph
    - Audit tool_calls after execution
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
                "messages": [],
                "reply": "",
                "status": "running",
                "error": None,
            })
        except Exception as exc:
            logger.exception("turn graph failed turn_id=%s", turn_id)
            self._store.finalize_turn_failure(turn_id, error_message=str(exc))
            result = {
                "turn_id": turn_id,
                "conversation_id": turn.conversation_id,
                "messages": [],
                "reply": "",
                "status": "failed",
                "error": str(exc),
            }

        status = result.get("status", "failed")
        reply = result.get("reply", "")
        error = result.get("error")

        if status == "failed" and error:
            logger.error("turn failed turn_id=%s error=%s", turn_id, error)

        # Audit tool_calls: walk new messages and record each tool execution
        self._persist_tool_calls(turn_id, result.get("messages", []))

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist_tool_calls(self, turn_id: int, messages: list[BaseMessage]) -> None:
        """Walk messages to find tool_call pairs and write them to MySQL."""
        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            for tc in msg.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]
                tool_call_id = tc["id"]

                # Find the corresponding ToolMessage by tool_call_id
                output = ""
                for follow in messages:
                    if (
                        isinstance(follow, BaseMessage)
                        and getattr(follow, "tool_call_id", None) == tool_call_id
                    ):
                        output = str(follow.content)
                        break

                try:
                    record = self._store.create_tool_call(
                        turn_id=turn_id,
                        tool_name=tool_name,
                        input=tool_args,
                    )
                    self._store.update_tool_call(
                        record.id,
                        status="completed",
                        output={"result": output[:2000]} if output else {},
                    )
                except Exception:
                    logger.exception("failed to persist tool_call turn=%s tool=%s", turn_id, tool_name)
