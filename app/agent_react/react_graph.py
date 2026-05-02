"""ReAct subgraph: LLM reasoning + tool execution loop."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage
from app.tools.runtime import build_llm_tools, check_tool_policy, execute_tool, get_tool_definition

logger = logging.getLogger(__name__)


class TurnStore(Protocol):
    def get_turn(self, turn_id: int): ...

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

    def append_tool_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "text",
        external_message_id: str | None = None,
        raw_payload: dict | None = None,
    ): ...

    def create_tool_call(
        self,
        *,
        turn_id: int,
        tool_name: str,
        input: dict,
        assistant_message_id: int | None = None,
        provider_tool_call_id: str | None = None,
        step_index: int = 0,
    ): ...

    def update_tool_call(
        self,
        tool_call_id: int,
        *,
        status: str | None = None,
        output: dict | None = None,
        error_message: str | None = None,
    ): ...

    def list_tool_calls_by_turn(self, turn_id: int) -> list: ...


class ReActState(TypedDict):
    turn_id: int
    messages: list[BaseMessage]
    cancelled: bool
    status: str


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
            "status": "cancelled",
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
        llm_messages.insert(
            0,
            LLMMessage(
                role="system",
                content=(
                    "You are Jarvis, an AI programming assistant. "
                    "You can help with coding, shell commands, and general questions. "
                    "You have access to tools when needed. Be concise and helpful. "
                    "Reply in the same language the user uses (Chinese or English)."
                ),
            ),
        )

    tools = build_llm_tools()
    for idx, message in enumerate(llm_messages):
        logger.info(
            "llm_input msg[%s] role=%s content=%s tool_call_id=%s has_tool_calls=%s",
            idx,
            message.role,
            repr(message.content[:120]) if message.content else "",
            message.tool_call_id,
            bool(message.tool_calls),
        )

    try:
        response = client.chat(llm_messages, tools=tools)
    except Exception:
        logger.exception("llm call failed messages_count=%s", len(llm_messages))
        return {
            "turn_id": state["turn_id"],
            "cancelled": False,
            "status": "running",
            "messages": state["messages"] + [AIMessage(content="抱歉，调用模型时出错了，请稍后再试。")],
        }

    ai_message = _llm_response_to_ai_message(response)
    return {
        "turn_id": state["turn_id"],
        "cancelled": False,
        "status": "running",
        "messages": state["messages"] + [ai_message],
    }
def _execute_single_tool(tool_name: str, tool_args: dict[str, Any]) -> tuple[bool, str]:
    tool = get_tool_definition(tool_name)
    result = execute_tool(tool, tool_args, timeout_seconds=30)
    if result.ok:
        return True, result.stdout or result.summary or "Completed successfully."
    return False, f"Error (exit_code={result.exit_code}): {result.stderr or result.summary}"


def _tool_output_payload(output: str) -> dict[str, Any]:
    trimmed = output[:2000]
    return {"result": trimmed} if trimmed else {}


def _serialize_tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    return [
        {
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": tool_call["args"],
        }
        for tool_call in message.tool_calls
    ]


def _next_step_index(store: TurnStore, turn_id: int) -> int:
    records = store.list_tool_calls_by_turn(turn_id)
    if not records:
        return 1
    return max(int(getattr(record, "step_index", 0) or 0) for record in records) + 1


def execute_tools(state: ReActState, store: TurnStore) -> ReActState:
    if _is_turn_cancelled(store, state["turn_id"]):
        return {
            **state,
            "cancelled": True,
            "status": "cancelled",
        }

    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        return state

    tool_messages: list[BaseMessage] = []
    turn = store.get_turn(state["turn_id"])
    if turn is None:
        raise ValueError(f"Turn not found: {state['turn_id']}")
    step_index = _next_step_index(store, state["turn_id"])
    assistant_message = store.append_assistant_message(
        conversation_id=turn.conversation_id,
        turn_id=state["turn_id"],
        content=str(last_message.content or ""),
        content_type="markdown",
        raw_payload={
            "source": "agent_react.tool_call",
            "tool_calls": _serialize_tool_calls(last_message),
            "step_index": step_index,
        },
    )
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        tool_call_id = tool_call["id"]
        record = None

        try:
            tool = get_tool_definition(tool_name)
            record = store.create_tool_call(
                turn_id=state["turn_id"],
                tool_name=tool_name,
                input=tool_args,
                assistant_message_id=getattr(assistant_message, "id", None),
                provider_tool_call_id=tool_call_id,
                step_index=step_index,
            )
            rejection = check_tool_policy(tool, tool_args, state["messages"])
            if rejection is not None:
                output = rejection
                store.update_tool_call(
                    record.id,
                    status="rejected",
                    output=_tool_output_payload(output),
                    error_message=rejection,
                )
            else:
                store.update_tool_call(record.id, status="running")
                ok, output = _execute_single_tool(tool_name, tool_args)
                if not ok:
                    store.update_tool_call(
                        record.id,
                        status="failed",
                        output=_tool_output_payload(output),
                        error_message=output,
                    )
                else:
                    store.update_tool_call(
                        record.id,
                        status="completed",
                        output=_tool_output_payload(output),
                    )
        except Exception as exc:
            output = f"Tool execution error: {exc}"
            logger.exception("tool execution failed tool=%s", tool_name)
            if record is None:
                try:
                    record = store.create_tool_call(
                        turn_id=state["turn_id"],
                        tool_name=tool_name,
                        input=tool_args,
                        assistant_message_id=getattr(assistant_message, "id", None),
                        provider_tool_call_id=tool_call_id,
                        step_index=step_index,
                    )
                except Exception:
                    logger.exception("tool call audit create failed tool=%s", tool_name)
                    record = None
            if record is not None:
                try:
                    store.update_tool_call(
                        record.id,
                        status="failed",
                        output=_tool_output_payload(output),
                        error_message=output,
                    )
                except Exception:
                    logger.exception("tool call audit update failed tool=%s", tool_name)

        store.append_tool_message(
            conversation_id=turn.conversation_id,
            turn_id=state["turn_id"],
            content=output,
            content_type="text",
            raw_payload={
                "source": "agent_react.tool_result",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "assistant_message_id": getattr(assistant_message, "id", None),
                "step_index": step_index,
            },
        )
        tool_messages.append(ToolMessage(content=output, tool_call_id=tool_call_id))

    return {
        "turn_id": state["turn_id"],
        "cancelled": False,
        "status": "running",
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

