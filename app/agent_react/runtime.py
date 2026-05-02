"""Agent runtime split into an outer orchestrator and an inner turn runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from typing_extensions import TypedDict

from app.agent_react.context_manager import ContextManager
from app.agent_react.react_graph import call_llm, execute_tools, should_continue
from app.config import get_settings

if TYPE_CHECKING:
    from app.persistence.models import TurnRecord

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
    """Store contract for the outer runtime orchestrator and inner turn runtime."""

    def get_turn(self, turn_id: int) -> TurnRecord | None: ...

    def list_messages(self, conversation_id: int) -> list: ...

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


class TurnRuntimeState(TypedDict):
    turn_id: int
    conversation_id: int
    trigger_message_id: int | None
    messages: list[BaseMessage]
    selected_skills: list[str]
    reply: str
    reply_message_id: int | None
    cancelled: bool
    status: str
    step_count: int
    token_budget: int | None
    error: str | None

class TurnRuntime:
    """Turn execution runtime with a local ReAct loop."""

    def __init__(self, store: ConversationStore, context_manager: ContextManager | None = None, token_budget: int | None = None) -> None:
        self._store = store
        self._context_manager = context_manager or ContextManager()
        self._token_budget = token_budget

    def invoke(self, state: TurnRuntimeState) -> TurnRuntimeState:
        prepared = self._prepare(state)
        if prepared.get("error"):
            return self._finalize(prepared)

        running = prepared
        while running.get("status") == "running" and not running.get("cancelled"):
            react_state = {
                "turn_id": running["turn_id"],
                "messages": running["messages"],
                "cancelled": running["cancelled"],
                "status": running["status"],
                "step_count": running["step_count"],
                "token_budget": running["token_budget"],
            }
            running = self._apply_react_step(running, react_state)
        return self._finalize(running)

    def _prepare(self, state: TurnRuntimeState) -> TurnRuntimeState:
        turn_id = state["turn_id"]
        turn = self._store.get_turn(turn_id)
        if turn is None:
            return {
                **state,
                "status": "failed",
                "error": f"Turn not found: {turn_id}",
            }

        lc_messages, skill_names = self._context_manager.build_initial_messages(
            self._store.list_messages(turn.conversation_id),
            getattr(turn, "trigger_message_id", None),
        )

        return {
            "turn_id": turn_id,
            "conversation_id": turn.conversation_id,
            "trigger_message_id": getattr(turn, "trigger_message_id", None),
            "messages": lc_messages,
            "selected_skills": skill_names,
            "reply": "",
            "reply_message_id": None,
            "cancelled": False,
            "status": "running",
            "step_count": 0,
            "token_budget": self._token_budget,
            "error": None,
        }

    def _apply_react_step(self, state: TurnRuntimeState, react_state: dict[str, Any]) -> TurnRuntimeState:
        try:
            llm_state = call_llm(react_state, self._store)
        except Exception as exc:
            logger.exception("turn runtime llm step failed")
            return {
                **state,
                "status": "failed",
                "error": str(exc),
            }

        if llm_state.get("status") == "failed":
            return {
                **state,
                "messages": llm_state["messages"],
                "step_count": llm_state["step_count"],
                "status": "failed",
                "error": "llm call failed",
            }

        if llm_state.get("cancelled"):
            return {
                **state,
                "messages": llm_state["messages"],
                "step_count": llm_state["step_count"],
                "cancelled": True,
                "status": "cancelled",
                "error": None,
            }

        route = should_continue(llm_state)
        if route == "execute_tools":
            try:
                next_state = execute_tools(llm_state, self._store)
            except Exception as exc:
                logger.exception("turn runtime tool step failed")
                return {
                    **state,
                    "messages": llm_state["messages"],
                    "step_count": llm_state["step_count"],
                    "status": "failed",
                    "error": str(exc),
                }
            return {
                **state,
                "messages": next_state["messages"],
                "step_count": next_state["step_count"],
                "cancelled": bool(next_state.get("cancelled")),
                "status": "cancelled" if next_state.get("cancelled") else "running",
                "error": state.get("error"),
            }

        return {
            **state,
            "messages": llm_state["messages"],
            "step_count": llm_state["step_count"],
            "cancelled": bool(llm_state.get("cancelled")),
            "status": "cancelled" if llm_state.get("cancelled") else "completed",
            "error": state.get("error"),
        }

    def _finalize(self, state: TurnRuntimeState) -> TurnRuntimeState:
        turn_id = state["turn_id"]
        if state.get("error"):
            self._store.finalize_turn_failure(
                turn_id,
                error_message=state["error"],
            )
            return {
                **state,
                "status": "failed",
                "reply": "",
            }

        if state.get("status") == "cancelled":
            self._store.finalize_turn_failure(
                turn_id,
                status="cancelled",
                error_message=None,
            )
            return {
                **state,
                "reply": "",
                "error": None,
            }

        assistant_messages = [
            m for m in state["messages"]
            if isinstance(m, AIMessage) and (m.content or "").strip()
        ]
        reply = assistant_messages[-1].content if assistant_messages else ""

        raw_payload: dict[str, Any] = {"source": "turn_runtime"}
        if assistant_messages:
            reasoning = assistant_messages[-1].response_metadata.get("reasoning_content") if assistant_messages[-1].response_metadata else None
            if reasoning is not None:
                raw_payload["reasoning_content"] = reasoning

        message = self._store.finalize_turn_success(
            turn_id=turn_id,
            conversation_id=state["conversation_id"],
            content=reply,
            content_type="markdown",
            raw_payload=raw_payload,
        )

        return {
            **state,
            "reply": reply,
            "reply_message_id": getattr(message, "id", None),
            "status": "completed",
            "error": None,
        }


class AgentRuntime:
    """Outer runtime orchestrator for a single turn execution."""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        settings = get_settings()
        token_budget = max(
            0,
            settings.llm_max_context_tokens - settings.llm_max_output_tokens - settings.llm_context_safety_buffer,
        )
        self._token_budget = token_budget
        # Kept as `_graph` temporarily for compatibility with existing tests.
        self._graph = TurnRuntime(store, token_budget=token_budget)

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
                "cancelled": False,
                "status": "running",
                "step_count": 0,
                "token_budget": self._token_budget,
                "error": None,
            })
        except Exception as exc:
            logger.exception("turn runtime failed turn_id=%s", turn_id)
            self._store.finalize_turn_failure(turn_id, error_message=str(exc))
            result = {
                "turn_id": turn_id,
                "conversation_id": turn.conversation_id,
                "trigger_message_id": getattr(turn, "trigger_message_id", None),
                "messages": [],
                "selected_skills": [],
                "reply": "",
                "reply_message_id": None,
                "cancelled": False,
                "status": "failed",
                "step_count": 0,
                "token_budget": self._token_budget,
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
