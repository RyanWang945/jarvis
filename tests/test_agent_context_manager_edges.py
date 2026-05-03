from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent_react.context_manager import ContextManager


def test_token_budget_keeps_ai_tool_call_with_matching_tool_result() -> None:
    manager = ContextManager()
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="old context " + ("x" * 500)),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "shell_inspect", "args": {"command": "pwd"}}],
        ),
        ToolMessage(content="tool result", tool_call_id="call_1"),
    ]

    fitted = manager.fit_messages_to_token_budget(messages, token_budget=20)

    assert isinstance(fitted[0], SystemMessage)
    assert all(not (isinstance(message, HumanMessage) and "old context" in str(message.content)) for message in fitted)
    assert any(isinstance(message, AIMessage) and message.tool_calls[0]["id"] == "call_1" for message in fitted)
    assert any(isinstance(message, ToolMessage) and message.tool_call_id == "call_1" for message in fitted)
