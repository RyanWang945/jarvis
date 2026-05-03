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


def test_classifier_fallback_preserves_active_coding_session() -> None:
    classification = classify_turn(
        content="把文件提交commit然后推送吧",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="nltk"),
    )

    assert classification.turn_type == "coding"
    assert classification.session_mode_update == "coding"
    assert classification.active_repo_id_update == "nltk"
    assert classification.target_resources[0].id == "nltk"
    assert classification.source == "fallback"
    assert classification.routing_basis == "contextual"


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
    assert classification.requested_capabilities == ("web.search",)
    assert classification.routing_basis == "explicit"
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
                '"requested_capabilities":["research.deep","web.search"],'
                '"routing_basis":"explicit","confidence":0.91,"reason":"multi-source comparison"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="Please compare these agent architectures in depth.",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "research"
    assert classification.session_mode_update == "research"
    assert classification.requested_capabilities == ("research.deep", "web.search")
    assert classification.routing_basis == "explicit"
    assert classification.source == "llm"
    assert should_apply_session_mode_update(classification) is True
    get_settings.cache_clear()


def test_llm_classifier_accepts_missing_confidence_and_enriches_current_info(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"chat","session_mode_update":null,'
                '"requested_capabilities":[],"target_resources":[]}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="帮我看看伊朗最新局势",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "chat"
    assert classification.confidence == 0.8
    assert classification.requested_capabilities == ("web.search",)
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
    assert "workspace.edit" in classification.requested_capabilities
    assert classification.target_resources[0].type == "repository"
    assert classification.target_resources[0].id == "nltk"
    assert classification.source == "local_override"
    assert should_apply_session_mode_update(classification) is True
    assert should_apply_repo_update(classification) is True
    get_settings.cache_clear()


def test_registered_repo_design_request_overrides_chat_llm(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
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
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: registry)

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"chat","session_mode_update":null,'
                '"requested_capabilities":[],"target_resources":[]}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="帮我看看jarvis里agent的设计",
        session_state=ConversationSessionState(session_mode="chat", active_repo_id="nltk"),
    )

    assert classification.turn_type == "coding"
    assert classification.session_mode_update == "coding"
    assert classification.active_repo_id_update == "jarvis"
    assert "workspace.inspect" in classification.requested_capabilities
    assert classification.target_resources[0].id == "jarvis"
    get_settings.cache_clear()


def test_registered_repo_mention_does_not_update_active_repo_without_code_action(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
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
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: registry)

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"chat","session_mode_update":null,'
                '"requested_capabilities":[],"target_resources":[]}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="jarvis 这个名字是什么意思？",
        session_state=ConversationSessionState(session_mode="chat", active_repo_id="nltk"),
    )

    assert classification.turn_type == "chat"
    assert classification.active_repo_id_update is None
    assert classification.target_resources[0].id == "jarvis"
    assert should_apply_repo_update(classification) is False
    get_settings.cache_clear()


def test_research_followup_inherits_active_workspace_for_design_comparison(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"research","session_mode_update":"research",'
                '"requested_capabilities":["web.search"],"target_resources":[]}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="你觉得这个设计对比最新的hermes的设计怎样，hermes有什么可以借鉴的吗",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="jarvis"),
    )

    assert classification.turn_type == "research"
    assert classification.session_mode_update == "research"
    assert "web.search" in classification.requested_capabilities
    assert "workspace.inspect" in classification.requested_capabilities
    assert classification.target_resources[0].id == "jarvis"
    get_settings.cache_clear()


def test_registered_repo_review_with_latest_release_notes_requests_code_and_web(monkeypatch, tmp_path) -> None:
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

    classification = classify_turn(
        content="review nltk 并结合最新 release note 看兼容风险",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "coding"
    assert classification.session_mode_update == "coding"
    assert classification.active_repo_id_update == "nltk"
    assert "workspace.inspect" in classification.requested_capabilities
    assert "web.search" in classification.requested_capabilities
    assert classification.target_resources[0].id == "nltk"
