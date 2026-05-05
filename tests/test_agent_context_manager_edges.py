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


def test_render_for_model_strips_historical_model_usage_footer() -> None:
    manager = ContextManager()
    rendered = manager.render_for_model(
        [
            AIMessage(
                content=(
                    "prior answer\n\n"
                    "---\n"
                    "- 模型：`deepseek-v4-flash`\n"
                    "- Token：输入 `2500` / 输出 `37` / 合计 `2537`"
                )
            )
        ]
    )

    assert len(rendered) == 1
    assert rendered[0].role == "assistant"
    assert rendered[0].content == "prior answer"
