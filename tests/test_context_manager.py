from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from app.agent_react.context_manager import ContextManager
from app.agent_react.session_state import ConversationSessionState


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
