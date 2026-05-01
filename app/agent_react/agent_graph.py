"""Turn lifecycle graph (main graph): load context -> ReAct loop -> persist results.

The graph is built via `build_turn_graph(store)` so that MySQL-dependent nodes
receive the store instance through closures without polluting the state schema.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.agent_react.react_graph import build_react_graph

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    turn_id: int
    conversation_id: int
    messages: list[BaseMessage]
    reply: str
    status: str
    error: str | None


def _records_to_lc_messages(messages: list) -> list[BaseMessage]:
    """Convert persistence MessageRecords to LangChain messages."""
    lc: list[BaseMessage] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", "") or ""
        if role == "user":
            lc.append(HumanMessage(content=content))
        elif role == "assistant":
            lc.append(AIMessage(content=content))
        elif role == "tool":
            raw = getattr(msg, "raw_payload", {}) or {}
            tool_call_id = raw.get("tool_call_id", "unknown")
            lc.append(ToolMessage(content=content, tool_call_id=tool_call_id))
        elif role == "system":
            lc.append(SystemMessage(content=content))
    return lc


def _slice_records_through_trigger(records: list, trigger_message_id: int | None) -> list:
    if trigger_message_id is None:
        return records

    bounded: list = []
    for record in records:
        bounded.append(record)
        if getattr(record, "id", None) == trigger_message_id:
            break
    return bounded


def build_turn_graph(store):
    """Build the compiled Turn graph with store injected via closures."""
    react_graph = build_react_graph(store)

    # --------------------------------------------------------------
    # prepare: load MySQL context into LangChain messages
    # --------------------------------------------------------------
    def prepare(state: AgentState) -> AgentState:
        turn_id = state["turn_id"]
        turn = store.get_turn(turn_id)
        if turn is None:
            return {
                **state,
                "status": "failed",
                "error": f"Turn not found: {turn_id}",
            }

        records = _slice_records_through_trigger(
            store.list_messages(turn.conversation_id),
            getattr(turn, "trigger_message_id", None),
        )
        lc_messages = _records_to_lc_messages(records)

        return {
            "turn_id": turn_id,
            "conversation_id": turn.conversation_id,
            "messages": lc_messages,
            "reply": "",
            "status": "running",
            "error": None,
        }

    # --------------------------------------------------------------
    # react: run the ReAct subgraph
    # --------------------------------------------------------------
    def react(state: AgentState) -> AgentState:
        if state.get("error"):
            return state

        try:
            result = react_graph.invoke({
                "turn_id": state["turn_id"],
                "messages": state["messages"],
                "cancelled": False,
            })
        except Exception as exc:
            logger.exception("react subgraph failed")
            return {
                **state,
                "status": "failed",
                "error": str(exc),
            }

        if result.get("cancelled"):
            return {
                **state,
                "status": "cancelled",
                "reply": "",
                "error": None,
                "messages": result["messages"],
            }

        return {
            **state,
            "messages": result["messages"],
        }

    # --------------------------------------------------------------
    # persist: write assistant reply and complete the turn
    # --------------------------------------------------------------
    def persist(state: AgentState) -> AgentState:
        turn_id = state["turn_id"]
        if state.get("error"):
            store.finalize_turn_failure(
                turn_id,
                error_message=state["error"],
            )
            return {
                **state,
                "status": "failed",
                "reply": "",
            }

        if state.get("status") == "cancelled":
            store.finalize_turn_failure(
                turn_id,
                status="cancelled",
                error_message=None,
            )
            return {
                **state,
                "reply": "",
                "error": None,
            }

        # Find the last assistant message with content as the reply
        assistant_messages = [
            m for m in state["messages"]
            if isinstance(m, AIMessage) and (m.content or "").strip()
        ]
        reply = assistant_messages[-1].content if assistant_messages else ""

        store.finalize_turn_success(
            turn_id=turn_id,
            conversation_id=state["conversation_id"],
            content=reply,
            content_type="markdown",
            raw_payload={"source": "agent_react"},
        )

        return {
            **state,
            "reply": reply,
            "status": "completed",
            "error": None,
        }

    # --------------------------------------------------------------
    # Conditional routing after prepare
    # --------------------------------------------------------------
    def _route_after_prepare(state: AgentState) -> str:
        if state.get("error"):
            return "persist"
        return "react"

    # --------------------------------------------------------------
    # Build graph
    # --------------------------------------------------------------
    builder = StateGraph(AgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("react", react)
    builder.add_node("persist", persist)

    builder.set_entry_point("prepare")
    builder.add_conditional_edges(
        "prepare",
        _route_after_prepare,
        {"react": "react", "persist": "persist"},
    )
    builder.add_edge("react", "persist")
    builder.add_edge("persist", END)

    return builder.compile()
