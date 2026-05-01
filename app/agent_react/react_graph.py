"""ReAct subgraph: LLM reasoning + tool execution loop.

This subgraph is stateless regarding persistence; it only operates on
messages and produces new messages. The parent Turn graph is responsible
for loading MySQL context before entering here and persisting results after.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage
from app.skills.base import SkillRequest
from app.skills.bootstrap import get_skill_registry, get_tool_registry
from app.tools.specs import ToolSpec

logger = logging.getLogger(__name__)


class TurnStore(Protocol):
    def get_turn(self, turn_id: int): ...


class ReActState(TypedDict):
    turn_id: int
    messages: list[BaseMessage]
    cancelled: bool


def _build_tools_for_llm() -> list[dict[str, Any]]:
    """Convert exposed ToolSpecs to OpenAI function-calling format."""
    registry = get_tool_registry()
    tools = registry.list(exposed_to_llm=True)
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.args_schema,
            },
        }
        for tool in tools
    ]


def _lc_messages_to_llm(messages: list[BaseMessage]) -> list[LLMMessage]:
    result: list[LLMMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append(LLMMessage(role="system", content=str(msg.content)))
        elif isinstance(msg, HumanMessage):
            result.append(LLMMessage(role="user", content=str(msg.content)))
        elif isinstance(msg, AIMessage):
            tool_calls: list[dict[str, Any]] | None = None
            if msg.tool_calls:
                tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"]),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            reasoning = None
            if msg.response_metadata:
                reasoning = msg.response_metadata.get("reasoning_content")
            result.append(
                LLMMessage(
                    role="assistant",
                    content=str(msg.content or ""),
                    tool_calls=tool_calls,
                    reasoning_content=reasoning,
                )
            )
        elif isinstance(msg, ToolMessage):
            result.append(
                LLMMessage(
                    role="tool",
                    content=str(msg.content),
                    tool_call_id=msg.tool_call_id,
                )
            )
    return result


def _llm_response_to_ai_message(response: dict[str, Any]) -> AIMessage:
    content = response.get("content") or ""
    tool_calls: list[dict[str, Any]] = []
    for tc in response.get("tool_calls", []):
        if tc.get("type") == "function":
            func = tc["function"]
            try:
                args = json.loads(func["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append({
                "id": tc["id"],
                "name": func["name"],
                "args": args,
            })
    reasoning_content = response.get("reasoning_content")
    response_metadata = {}
    if reasoning_content is not None:
        response_metadata["reasoning_content"] = reasoning_content
    return AIMessage(content=content, tool_calls=tool_calls, response_metadata=response_metadata)


def _is_turn_cancelled(store: TurnStore, turn_id: int) -> bool:
    turn = store.get_turn(turn_id)
    return turn is not None and getattr(turn, "status", None) == "cancelled"


def call_llm(state: ReActState, store: TurnStore) -> ReActState:
    if _is_turn_cancelled(store, state["turn_id"]):
        return {
            **state,
            "cancelled": True,
        }

    settings = get_settings()
    client = ChatClient(
        api_key=settings.deepseek_api_key or "",
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    llm_messages = _lc_messages_to_llm(state["messages"])

    if not llm_messages or llm_messages[0].role != "system":
        system_msg = LLMMessage(
            role="system",
            content=(
                "You are Jarvis, an AI programming assistant. "
                "You can help with coding, shell commands, knowledge base searches, and general questions. "
                "You have access to tools when needed. Be concise and helpful. "
                "Reply in the same language the user uses (Chinese or English)."
            ),
        )
        llm_messages.insert(0, system_msg)

    tools = _build_tools_for_llm()

    for idx, m in enumerate(llm_messages):
        logger.info(
            "llm_input msg[%s] role=%s content=%s tool_call_id=%s has_tool_calls=%s",
            idx,
            m.role,
            repr(m.content[:120]) if m.content else "",
            m.tool_call_id,
            bool(m.tool_calls),
        )

    try:
        response = client.chat(llm_messages, tools=tools)
    except Exception:
        logger.exception("llm call failed messages_count=%s", len(llm_messages))
        for idx, m in enumerate(llm_messages):
            logger.debug(
                "msg[%s] role=%s content=%r tool_call_id=%s has_tool_calls=%s",
                idx, m.role, m.content[:200], m.tool_call_id, bool(m.tool_calls),
            )
        return {
            "turn_id": state["turn_id"],
            "cancelled": False,
            "messages": state["messages"] + [AIMessage(content="抱歉，调用模型时出错了，请稍后再试。")],
        }

    ai_message = _llm_response_to_ai_message(response)
    return {
        "turn_id": state["turn_id"],
        "cancelled": False,
        "messages": state["messages"] + [ai_message],
    }


def _execute_single_tool(tool_spec: ToolSpec, tool_args: dict[str, Any]) -> str:
    skill_registry = get_skill_registry()
    skill = skill_registry.get(tool_spec.skill)

    request = SkillRequest(
        skill=tool_spec.skill,
        action=tool_spec.action,
        workdir=None,
        args=tool_args,
        risk_level=tool_spec.risk_level,
        timeout_seconds=30,
    )
    result = skill.run(request)

    if result.ok:
        return result.stdout or result.summary or "Completed successfully."
    return f"Error (exit_code={result.exit_code}): {result.stderr or result.summary}"


def execute_tools(state: ReActState, store: TurnStore) -> ReActState:
    if _is_turn_cancelled(store, state["turn_id"]):
        return {
            **state,
            "cancelled": True,
        }

    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        return state

    tool_registry = get_tool_registry()
    tool_messages: list[BaseMessage] = []

    for tc in last_message.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        tool_call_id = tc["id"]

        try:
            tool_spec = tool_registry.get(tool_name)
            output = _execute_single_tool(tool_spec, tool_args)
        except Exception as exc:
            output = f"Tool execution error: {exc}"
            logger.exception("tool execution failed tool=%s", tool_name)

        tool_messages.append(ToolMessage(content=output, tool_call_id=tool_call_id))

    return {
        "turn_id": state["turn_id"],
        "cancelled": False,
        "messages": state["messages"] + tool_messages,
    }


def should_continue(state: ReActState) -> str:
    if state.get("cancelled"):
        return END
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "execute_tools"
    return END


def build_react_graph(store: TurnStore) -> StateGraph:
    builder = StateGraph(ReActState)
    builder.add_node("call_llm", lambda state: call_llm(state, store))
    builder.add_node("execute_tools", lambda state: execute_tools(state, store))
    builder.set_entry_point("call_llm")
    builder.add_conditional_edges(
        "call_llm",
        should_continue,
        {"execute_tools": "execute_tools", END: END},
    )
    builder.add_edge("execute_tools", "call_llm")
    return builder.compile()
