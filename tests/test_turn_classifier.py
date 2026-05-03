from app.agent_react.session_state import ConversationSessionState
from app.agent_react.turn_classifier import classify_turn, should_apply_repo_update, should_apply_session_mode_update
from app.config import get_settings
from app.llm.client import ChatClient
from app.repositories import RepositoryRef, RepositoryRegistry


def test_hard_rule_research_command_updates_session_mode() -> None:
    classification = classify_turn(
        content="/research compare agent runtimes",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "research"
    assert classification.session_mode_update == "research"
    assert classification.source == "hard_rule"
    assert should_apply_session_mode_update(classification) is True


def test_continue_uses_current_research_session_mode() -> None:
    classification = classify_turn(
        content="继续",
        session_state=ConversationSessionState(session_mode="research"),
    )

    assert classification.turn_type == "research"
    assert classification.session_mode_update is None
    assert classification.source == "fallback"


def test_low_confidence_fallback_does_not_force_session_update() -> None:
    classification = classify_turn(
        content="hello",
        session_state=ConversationSessionState(session_mode="research"),
    )

    assert classification.turn_type == "chat"
    assert classification.session_mode_update is None
    assert should_apply_session_mode_update(classification) is False


def test_current_info_request_exits_coding_session() -> None:
    classification = classify_turn(
        content="不看项目了，查查最新国际金价",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="nltk"),
    )

    assert classification.turn_type == "chat"
    assert classification.session_mode_update == "chat"
    assert classification.active_repo_id_update is None
    assert classification.confidence >= 0.75
    assert should_apply_session_mode_update(classification) is True


def test_llm_classifier_json_result_is_used(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"research","session_mode_update":"research",'
                '"confidence":0.91,"reason":"multi-source comparison"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="Please compare these agent architectures in depth.",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "research"
    assert classification.session_mode_update == "research"
    assert classification.source == "llm"
    assert should_apply_session_mode_update(classification) is True
    get_settings.cache_clear()


def test_registered_repo_code_request_overrides_chat_llm(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
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
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: registry)

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"chat","session_mode_update":null,'
                '"confidence":0.92,"reason":"misclassified"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="切换到nltk项目，然后在其中写个python代码的快排",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "coding"
    assert classification.session_mode_update == "coding"
    assert classification.active_repo_id_update == "nltk"
    assert classification.source == "local_override"
    assert should_apply_session_mode_update(classification) is True
    assert should_apply_repo_update(classification) is True
    get_settings.cache_clear()
