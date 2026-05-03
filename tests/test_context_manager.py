from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from app.agent_react.context_manager import ContextManager
from app.agent_react.runtime_policy import RuntimePolicy
from app.agent_react.session_state import ConversationSessionState
from app.repositories import RepositoryRef, RepositoryRegistry


def test_context_header_keeps_session_state_in_protected_system_message() -> None:
    manager = ContextManager()
    records = [
        SimpleNamespace(
            id=1,
            role="user",
            content="continue the research",
            raw_payload={},
        )
    ]

    messages, _ = manager.build_initial_messages(
        records,
        trigger_message_id=1,
        session_state=ConversationSessionState(
            session_mode="research",
            session_goal="compare agent runtime designs",
            working_summary="Keep context lightweight.",
        ),
    )

    system_messages = [message for message in messages if isinstance(message, SystemMessage)]
    assert len(system_messages) == 1
    assert messages[0] == system_messages[0]
    assert "Conversation session state:" in str(messages[0].content)
    assert "Goal: compare agent runtime designs" in str(messages[0].content)
    assert "Working summary: Keep context lightweight." in str(messages[0].content)

    fitted = manager.fit_messages_to_token_budget(messages, token_budget=1)

    assert isinstance(fitted[0], SystemMessage)
    assert "Conversation session state:" in str(fitted[0].content)


def test_coding_context_includes_active_registered_repository(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "nltk"
    repo.mkdir()
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="nltk",
                name="NLTK",
                root_path=repo,
                canonical_root_path=repo.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.agent_react.context_manager.get_repository_registry", lambda: registry)

    messages, _ = ContextManager().build_initial_messages(
        [
            SimpleNamespace(
                id=1,
                role="user",
                content="修改当前项目",
                raw_payload={},
            )
        ],
        trigger_message_id=1,
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="nltk"),
        runtime_policy=RuntimePolicy(
            mode="coding",
            allowed_tools=("delegate_to_codex",),
            context_sections=("coding_protocol", "session_state"),
        ),
    )

    content = str(messages[0].content)
    assert "Repository context:" in content
    assert "Active repository: nltk" in content
    assert "- nltk (active):" in content
    assert "Prefer delegate_to_codex with repo_id" in content


def test_workspace_context_includes_active_registered_repository(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "jarvis"
    repo.mkdir()
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=repo,
                canonical_root_path=repo.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.agent_react.context_manager.get_repository_registry", lambda: registry)

    messages, _ = ContextManager().build_initial_messages(
        [
            SimpleNamespace(
                id=1,
                role="user",
                content="对比这个设计和 Hermes",
                raw_payload={},
            )
        ],
        trigger_message_id=1,
        session_state=ConversationSessionState(session_mode="research", active_repo_id="jarvis"),
        runtime_policy=RuntimePolicy(
            mode="research",
            allowed_tools=("delegate_to_codex", "tavily_search"),
            context_sections=("workspace_protocol", "research_protocol", "session_state"),
        ),
    )

    content = str(messages[0].content)
    assert "Workspace protocol:" in content
    assert "Repository context:" in content
    assert "Active repository: jarvis" in content
    assert "- jarvis (active):" in content


def test_initial_context_strips_persisted_tool_protocol() -> None:
    records = [
        SimpleNamespace(id=1, role="user", content="review repo", raw_payload={}),
        SimpleNamespace(
            id=2,
            role="assistant",
            content="I will inspect it.",
            raw_payload={"tool_calls": [{"id": "call_1", "name": "shell_inspect", "args": {"command": "pwd"}}]},
        ),
        SimpleNamespace(
            id=3,
            role="tool",
            content="tool result",
            raw_payload={"tool_call_id": "call_1"},
        ),
        SimpleNamespace(
            id=4,
            role="assistant",
            content="<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name=\"delegate_to_codex\">",
            raw_payload={},
        ),
        SimpleNamespace(id=5, role="user", content="next request", raw_payload={}),
    ]

    messages, _ = ContextManager().build_initial_messages(records, trigger_message_id=5)

    assert not any(isinstance(message, ToolMessage) for message in messages)
    assert not any(isinstance(message, AIMessage) and message.tool_calls for message in messages)
    assert any(isinstance(message, AIMessage) and message.content == "I will inspect it." for message in messages)
    assert not any("DSML" in str(message.content) for message in messages)
