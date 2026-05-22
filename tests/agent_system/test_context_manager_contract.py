from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent_react.context_manager import ContextManager
from app.agent_react.runtime_policy import RuntimePolicy
from app.agent_react.session_state import ConversationSessionState


def _record(
    record_id: int,
    role: str,
    content: str,
    *,
    turn_id: int | None = None,
    raw_payload: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=record_id,
        role=role,
        content=content,
        turn_id=turn_id,
        raw_payload=raw_payload or {},
    )


def test_context_contract_keeps_current_turn_contract_under_tight_budget() -> None:
    manager = ContextManager()
    messages, _skills = manager.build_initial_messages(
        [
            _record(1, "user", "old context " * 200, turn_id=1),
            _record(2, "assistant", "old answer " * 200, turn_id=1),
            _record(3, "user", "这个图不对，按路由关系改一下", turn_id=2),
        ],
        trigger_message_id=3,
        current_turn_id=2,
        turn_records=[
            SimpleNamespace(id=1, status="completed", started_at="2026-05-01T00:00:00Z"),
            SimpleNamespace(id=2, status="running", started_at="2026-05-01T00:01:00Z"),
        ],
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="jarvis"),
        runtime_policy=RuntimePolicy(
            mode="image_generation",
            allowed_tools=("load_skill_guidance", "delegate_to_codex"),
            context_sections=("session_state", "workspace_protocol"),
        ),
        task_plan={
            "objective": "修改 jarvis-architecture-v3.png 的路由关系",
        },
        recent_artifacts=[
            {
                "artifact_id": "art-1",
                "kind": "image",
                "filename": "jarvis-architecture-v3.png",
                "path": "E:\\pythonProject\\jarvis\\jarvis-architecture-v3.png",
                "source_tool": "delegate_to_codex",
                "turn_id": 1,
                "status": "available",
            }
        ],
    )

    model_messages = manager.render_for_model(messages, token_budget=64)
    system = model_messages[0].content

    assert model_messages[0].role == "system"
    assert "Conversation session state:" in system
    assert "Active repository: jarvis" in system
    assert "Current turn objective:" in system
    assert "修改 jarvis-architecture-v3.png 的路由关系" in system
    assert "Recent artifacts:" in system
    assert "filename=jarvis-architecture-v3.png" in system
    assert model_messages[-1].role == "user"
    assert model_messages[-1].content == "这个图不对，按路由关系改一下"


def test_context_contract_strips_historical_tool_protocol_and_usage_footer() -> None:
    manager = ContextManager()
    messages, _skills = manager.build_initial_messages(
        [
            _record(1, "user", "review repo", turn_id=1),
            _record(
                2,
                "assistant",
                "I will inspect it.",
                turn_id=1,
                raw_payload={"tool_calls": [{"id": "call-1", "name": "read_file", "args": {"path": "app.py"}}]},
            ),
            _record(3, "tool", "tool result", turn_id=1, raw_payload={"tool_call_id": "call-1"}),
            _record(4, "assistant", "<｜｜DSML｜｜tool_calls>\nraw provider syntax", turn_id=1),
            _record(
                5,
                "assistant",
                "final answer\n\n---\n- 模型：`old-model`\n- Token：输入 `1` / 输出 `2` / 合计 `3`",
                turn_id=1,
            ),
            _record(6, "user", "next request", turn_id=2),
        ],
        trigger_message_id=6,
        current_turn_id=2,
        turn_records=[
            SimpleNamespace(id=1, status="completed", started_at="2026-05-01T00:00:00Z"),
            SimpleNamespace(id=2, status="running", started_at="2026-05-01T00:01:00Z"),
        ],
    )

    model_messages = manager.render_for_model(messages)
    rendered = "\n".join(message.content for message in model_messages)

    assert not any(message.role == "tool" for message in model_messages)
    assert "raw provider syntax" not in rendered
    assert "old-model" not in rendered
    assert "Token：输入" not in rendered
    assert "final answer" in rendered
    assert model_messages[-1].content == "next request"


def test_context_contract_gates_policy_sections_by_runtime_policy() -> None:
    manager = ContextManager()
    records = [_record(1, "user", "hello")]

    chat_messages, _ = manager.build_initial_messages(
        records,
        trigger_message_id=1,
        runtime_policy=RuntimePolicy(mode="chat", allowed_tools=("obsidian_wiki_query",), context_sections=("session_state",)),
    )
    file_messages, _ = manager.build_initial_messages(
        records,
        trigger_message_id=1,
        runtime_policy=RuntimePolicy(
            mode="coding",
            allowed_tools=("read_file", "search_files"),
            context_sections=("session_state", "workspace_file_protocol"),
        ),
    )
    delivery_messages, _ = manager.build_initial_messages(
        records,
        trigger_message_id=1,
        runtime_policy=RuntimePolicy(
            mode="coding",
            allowed_tools=("deliver_file", "search_files"),
            context_sections=("session_state", "artifact_delivery_protocol"),
        ),
    )
    research_messages, _ = manager.build_initial_messages(
        records,
        trigger_message_id=1,
        runtime_policy=RuntimePolicy(
            mode="research",
            allowed_tools=("tavily_search",),
            context_sections=("session_state", "research_protocol"),
        ),
    )

    chat_system = str(chat_messages[0].content)
    file_system = str(file_messages[0].content)
    delivery_system = str(delivery_messages[0].content)
    research_system = str(research_messages[0].content)

    assert "Workspace protocol:" not in chat_system
    assert "Workspace file protocol:" in file_system
    assert "Workspace protocol:" not in file_system
    assert "Artifact delivery protocol:" in delivery_system
    assert "Research protocol:" in research_system


def test_context_contract_excludes_incomplete_prior_turns_when_building_current_context() -> None:
    manager = ContextManager()
    messages, _skills = manager.build_initial_messages(
        [
            _record(1, "user", "completed user", turn_id=1),
            _record(2, "assistant", "completed assistant", turn_id=1),
            _record(3, "user", "queued user should not be visible yet", turn_id=2),
            _record(4, "assistant", "failed assistant should not be visible", turn_id=3),
            _record(5, "user", "current user", turn_id=4),
        ],
        trigger_message_id=5,
        current_turn_id=4,
        turn_records=[
            SimpleNamespace(id=1, status="completed", started_at="2026-05-01T00:00:00Z"),
            SimpleNamespace(id=2, status="queued", started_at="2026-05-01T00:01:00Z"),
            SimpleNamespace(id=3, status="failed", started_at="2026-05-01T00:02:00Z"),
            SimpleNamespace(id=4, status="running", started_at="2026-05-01T00:03:00Z"),
        ],
    )

    rendered = "\n".join(str(message.content) for message in messages)

    assert "completed user" in rendered
    assert "completed assistant" in rendered
    assert "current user" in rendered
    assert "queued user should not be visible yet" not in rendered
    assert "failed assistant should not be visible" not in rendered


def test_context_contract_token_budget_keeps_current_trigger_even_when_oversized() -> None:
    manager = ContextManager()
    messages = [
        SystemMessage(content="system contract"),
        HumanMessage(content="old " * 200),
        HumanMessage(content="current trigger " * 200),
    ]

    fitted = manager.fit_messages_to_token_budget(messages, token_budget=8)

    assert isinstance(fitted[0], SystemMessage)
    assert fitted[0].content == "system contract"
    assert isinstance(fitted[-1], HumanMessage)
    assert "current trigger" in str(fitted[-1].content)


def test_context_contract_token_budget_keeps_ai_tool_block_atomic() -> None:
    manager = ContextManager()
    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-read",
                "name": "read_file",
                "args": {"path": "app/agent_react/runtime.py"},
            }
        ],
    )
    tool = ToolMessage(content="runtime content", tool_call_id="call-read")
    messages = [
        SystemMessage(content="system contract"),
        HumanMessage(content="old " * 200),
        ai,
        tool,
    ]

    fitted = manager.fit_messages_to_token_budget(messages, token_budget=16)

    assert fitted[0].content == "system contract"
    assert ai in fitted
    assert tool in fitted
    assert fitted.index(tool) == fitted.index(ai) + 1


def test_context_contract_render_for_model_serializes_current_turn_tool_protocol() -> None:
    manager = ContextManager()
    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-search",
                "name": "business_knowledge_search",
                "args": {"query": "agent testing"},
            }
        ],
    )
    messages = [
        SystemMessage(content="system contract"),
        HumanMessage(content="lookup"),
        ai,
        ToolMessage(content="search result", tool_call_id="call-search"),
    ]

    rendered = manager.render_for_model(messages)

    assert rendered[2].role == "assistant"
    assert rendered[2].tool_calls == [
        {
            "id": "call-search",
            "type": "function",
            "function": {
                "name": "business_knowledge_search",
                "arguments": '{"query": "agent testing"}',
            },
        }
    ]
    assert rendered[3].role == "tool"
    assert rendered[3].tool_call_id == "call-search"
