"""Agent runtime split into an outer orchestrator and an inner turn runtime."""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from typing_extensions import TypedDict

from app.agent_react.context_manager import ContextManager
from app.agent_react.react_graph import call_llm, execute_tools, should_continue
from app.agent_react.runtime_policy import RuntimePolicy, resolve_runtime_policy
from app.agent_react.session_state import (
    ConversationSessionState,
    build_session_state_after_turn,
    load_session_state,
)
from app.config import get_settings

if TYPE_CHECKING:
    from app.persistence.models import ConversationRecord
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
    session_state: ConversationSessionState | None
    runtime_policy: RuntimePolicy | None
    selected_skills: list[str]
    allowed_tools: list[str]
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
            runtime_policy = running.get("runtime_policy")
            allowed_tools = running.get("allowed_tools")
            if allowed_tools is None:
                allowed_tools = list(runtime_policy.allowed_tools) if runtime_policy is not None else []
            react_state = {
                "turn_id": running["turn_id"],
                "messages": running["messages"],
                "cancelled": running["cancelled"],
                "status": running["status"],
                "step_count": running["step_count"],
                "token_budget": running["token_budget"],
                "allowed_tools": list(allowed_tools),
                "max_steps": runtime_policy.max_steps if runtime_policy is not None else 8,
                "search_budget": runtime_policy.search_budget if runtime_policy is not None else None,
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

        conversation = self._store.get_conversation(turn.conversation_id)
        session_state = load_session_state(conversation.metadata if conversation is not None else None)
        runtime_policy = resolve_runtime_policy(
            session_mode=session_state.session_mode,
            turn_type=getattr(turn, "turn_type", "chat"),
            requested_capabilities=_requested_capabilities_from_turn(turn),
        )
        logger.info(
            "runtime policy resolved turn_id=%s mode=%s allowed_tools=%s requested_capabilities=%s",
            turn_id,
            runtime_policy.mode,
            list(runtime_policy.allowed_tools),
            _requested_capabilities_from_turn(turn),
        )

        lc_messages, skill_names = self._context_manager.build_initial_messages(
            self._store.list_messages(turn.conversation_id),
            getattr(turn, "trigger_message_id", None),
            turn_records=self._store.list_turns(turn.conversation_id),
            current_turn_id=turn.id,
            session_state=session_state,
            runtime_policy=runtime_policy,
        )

        return {
            "turn_id": turn_id,
            "conversation_id": turn.conversation_id,
            "trigger_message_id": getattr(turn, "trigger_message_id", None),
            "messages": lc_messages,
            "session_state": session_state,
            "runtime_policy": runtime_policy,
            "selected_skills": skill_names,
            "allowed_tools": list(runtime_policy.allowed_tools),
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
            if self._should_complete_after_ask_user_tool(next_state):
                ask_payload = self._latest_ask_user_payload(next_state["messages"]) or {}
                question = str(ask_payload.get("question") or "").strip()
                if question:
                    return {
                        **state,
                        "messages": next_state["messages"] + [AIMessage(content=question)],
                        "step_count": next_state["step_count"],
                        "allowed_tools": next_state.get("allowed_tools", state.get("allowed_tools")),
                        "cancelled": bool(next_state.get("cancelled")),
                        "status": "completed",
                        "error": state.get("error"),
                    }
            if self._should_complete_after_coder_tool(state, next_state):
                coder_reply = self._latest_tool_message_content(next_state["messages"])
                if not self._looks_like_codex_approval_request(coder_reply):
                    return self._summarize_after_coder_tool(state, next_state)
                return {
                    **state,
                    "messages": next_state["messages"] + [AIMessage(content=coder_reply)],
                    "step_count": next_state["step_count"],
                    "allowed_tools": next_state.get("allowed_tools", state.get("allowed_tools")),
                    "cancelled": bool(next_state.get("cancelled")),
                    "status": "completed",
                    "error": state.get("error"),
                }
            return {
                **state,
                "messages": next_state["messages"],
                "step_count": next_state["step_count"],
                "allowed_tools": next_state.get("allowed_tools", state.get("allowed_tools")),
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

    def _summarize_after_coder_tool(self, state: TurnRuntimeState, next_state: dict[str, Any]) -> TurnRuntimeState:
        summary_state = call_llm(
            {
                **next_state,
                "allowed_tools": [],
                "max_steps": int(next_state.get("step_count", 0) or 0) + 1,
            },
            self._store,
        )
        if summary_state.get("status") == "failed":
            return {
                **state,
                "messages": summary_state["messages"],
                "step_count": summary_state["step_count"],
                "cancelled": bool(summary_state.get("cancelled")),
                "status": "failed",
                "error": summary_state.get("error") or "failed to summarize Codex result",
            }
        return {
            **state,
            "messages": summary_state["messages"],
            "step_count": summary_state["step_count"],
            "allowed_tools": summary_state.get("allowed_tools", state.get("allowed_tools")),
            "cancelled": bool(summary_state.get("cancelled")),
            "status": "completed",
            "error": state.get("error"),
        }

    def _should_complete_after_coder_tool(self, state: TurnRuntimeState, next_state: dict[str, Any]) -> bool:
        if not self._latest_ai_message_called_tool(next_state["messages"], "delegate_to_codex"):
            return False
        return bool(self._latest_tool_message_content(next_state["messages"]))

    def _should_complete_after_ask_user_tool(self, next_state: dict[str, Any]) -> bool:
        if not self._latest_ai_message_called_tool(next_state["messages"], "ask_user"):
            return False
        payload = self._latest_ask_user_payload(next_state["messages"])
        return bool(payload and payload.get("status") == "waiting_for_user" and payload.get("question"))

    @staticmethod
    def _looks_like_codex_approval_request(content: str) -> bool:
        return content.strip().startswith("Codex requested approval")

    @staticmethod
    def _latest_ai_message_called_tool(messages: list[BaseMessage], tool_name: str) -> bool:
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                return any(tool_call.get("name") == tool_name for tool_call in message.tool_calls)
        return False

    @staticmethod
    def _latest_tool_message_content(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                content = str(message.content or "").strip()
                if content:
                    return content
        return ""

    @staticmethod
    def _latest_ask_user_payload(messages: list[BaseMessage]) -> dict[str, Any] | None:
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                continue
            content = str(message.content or "").strip()
            if not content:
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("status") == "waiting_for_user":
                return payload
        return None

    def _finalize(self, state: TurnRuntimeState) -> TurnRuntimeState:
        turn_id = state["turn_id"]
        if state.get("error"):
            self._store.finalize_turn_failure(
                turn_id,
                error_message=state["error"],
            )
            session_state = self._writeback_session_state(state, status="failed")
            return {
                **state,
                "status": "failed",
                "reply": "",
                "session_state": session_state,
            }

        if state.get("status") == "cancelled":
            self._store.finalize_turn_failure(
                turn_id,
                status="cancelled",
                error_message=None,
            )
            session_state = self._writeback_session_state(state, status="cancelled")
            return {
                **state,
                "reply": "",
                "error": None,
                "session_state": session_state,
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
        session_state = self._writeback_session_state(
            state,
            status="completed",
            assistant_reply=reply,
        )

        return {
            **state,
            "reply": reply,
            "reply_message_id": getattr(message, "id", None),
            "status": "completed",
            "error": None,
            "session_state": session_state,
        }

    def _writeback_session_state(
        self,
        state: TurnRuntimeState,
        *,
        status: str,
        assistant_reply: str | None = None,
    ) -> ConversationSessionState:
        session_state = build_session_state_after_turn(
            state.get("session_state"),
            turn_id=state["turn_id"],
            status=status,
            assistant_reply=assistant_reply,
        )
        ask_payload = self._latest_ask_user_payload(state.get("messages", []))
        if status == "completed" and ask_payload is not None:
            choices = ask_payload.get("choices")
            if not isinstance(choices, list):
                choices = []
            session_state = replace(
                session_state,
                waiting_for="user",
                pending_user_question=str(ask_payload.get("question") or "").strip() or None,
                pending_user_reason=str(ask_payload.get("reason") or "").strip() or None,
                pending_user_expected_answer_type=str(ask_payload.get("expected_answer_type") or "").strip() or None,
                pending_user_choices=tuple(str(item).strip() for item in choices if str(item).strip())[:8],
                pending_user_turn_id=state["turn_id"],
            )
        try:
            self._store.update_conversation_session(state["conversation_id"], session_state)
        except Exception:
            logger.exception("session state writeback failed turn_id=%s", state["turn_id"])
        return session_state


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
                "session_state": None,
                "runtime_policy": None,
                "selected_skills": [],
                "allowed_tools": [],
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
                "session_state": None,
                "runtime_policy": None,
                "selected_skills": [],
                "allowed_tools": [],
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


def _requested_capabilities_from_turn(turn: TurnRecord) -> tuple[str, ...]:
    metadata = getattr(turn, "metadata", {}) or {}
    classification = metadata.get("classification")
    if not isinstance(classification, dict):
        return ()
    raw = classification.get("requested_capabilities")
    if not isinstance(raw, list):
        return ()
    capabilities: list[str] = []
    for item in raw:
        if isinstance(item, str) and item not in capabilities:
            capabilities.append(item)
    return tuple(capabilities)
