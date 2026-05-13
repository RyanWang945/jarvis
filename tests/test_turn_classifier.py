from app.agent_react.session_state import ConversationSessionState
from app.agent_react.turn_classifier import (
    classification_to_metadata,
    classify_turn,
    should_apply_repo_update,
    should_apply_session_mode_update,
)
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


def test_non_command_fallback_does_not_preserve_research_by_keyword() -> None:
    classification = classify_turn(
        content="继续",
        session_state=ConversationSessionState(session_mode="research"),
    )

    assert classification.turn_type == "chat"
    assert classification.session_mode_update is None
    assert classification.source == "fallback"


def test_non_command_fallback_does_not_preserve_coding_by_keyword() -> None:
    classification = classify_turn(
        content="把文件提交commit然后推送吧",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="nltk"),
    )

    assert classification.turn_type == "chat"
    assert classification.session_mode_update is None
    assert classification.active_repo_id_update is None
    assert classification.target_resources == ()
    assert classification.source == "fallback"
    assert classification.routing_basis == "fallback"


def test_low_confidence_fallback_does_not_force_session_update() -> None:
    classification = classify_turn(
        content="hello",
        session_state=ConversationSessionState(session_mode="research"),
    )

    assert classification.turn_type == "chat"
    assert classification.session_mode_update is None
    assert should_apply_session_mode_update(classification) is False


def test_current_info_request_uses_llm_classification(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        return {
            "content": (
                '{"turn_type":"chat","session_mode_update":"chat",'
                '"requested_capabilities":["web.search"],'
                '"routing_basis":"explicit","confidence":0.9,"reason":"current information"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

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
    get_settings.cache_clear()


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


def test_llm_classifier_accepts_missing_confidence_without_local_enrichment(monkeypatch) -> None:
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
    assert classification.requested_capabilities == ()
    get_settings.cache_clear()


def test_llm_classifier_accepts_reminder_capability(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        system_prompt = messages[0].content
        assert "reminder.manage" in system_prompt
        return {
            "content": (
                '{"turn_type":"command","session_mode_update":null,'
                '"requested_capabilities":["reminder.manage"],'
                '"routing_basis":"explicit","confidence":0.93,"reason":"explicit reminder"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="2 分钟之后提醒我喝水",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "command"
    assert classification.requested_capabilities == ("reminder.manage",)
    assert classification.routing_basis == "explicit"
    assert classification.source == "llm"
    get_settings.cache_clear()


def test_llm_classifier_accepts_workspace_file_capabilities(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        system_prompt = messages[0].content
        assert "workspace.read_file" in system_prompt
        assert "workspace.search_files" in system_prompt
        return {
            "content": (
                '{"turn_type":"coding","session_mode_update":"coding",'
                '"requested_capabilities":["workspace.read_file","workspace.search_files"],'
                '"routing_basis":"explicit","confidence":0.9,"reason":"lightweight workspace file lookup"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="inspect app/tools/runtime.py",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "coding"
    assert classification.requested_capabilities == ("workspace.read_file", "workspace.search_files")
    assert classification.source == "llm"
    get_settings.cache_clear()


def test_llm_classifier_accepts_task_plan_and_recent_artifacts(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        payload = messages[1].content
        assert "recent_artifacts" in payload
        assert "jarvis-architecture-v3.png" in payload
        return {
            "content": (
                '{"turn_type":"image_generation","session_mode_update":null,'
                '"requested_capabilities":["workspace.inspect","workspace.read_file","image.generate"],'
                '"task_plan":{"objective":"revise_existing_artifact",'
                '"target_artifacts":["jarvis-architecture-v3.png"],'
                '"evidence_policy":{"workspace_inspection":"light"},'
                '"final_deliverable":"updated_image_file"},'
                '"routing_basis":"contextual","confidence":0.88,"reason":"revise previous image"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="这个图不对，按工具路由和 agent 引擎关系改一下",
        session_state=ConversationSessionState(),
        recent_artifacts=[
            {
                "artifact_id": "art_1",
                "kind": "image",
                "filename": "jarvis-architecture-v3.png",
            }
        ],
    )

    assert classification.turn_type == "image_generation"
    assert classification.task_plan["objective"] == "revise_existing_artifact"
    assert classification.task_plan["target_artifacts"] == ["jarvis-architecture-v3.png"]
    metadata = classification_to_metadata(classification)
    assert metadata["task_plan"]["final_deliverable"] == "updated_image_file"
    get_settings.cache_clear()


def test_llm_classifier_accepts_artifact_delivery_capability(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_chat(self, messages, response_format=None, tools=None, tool_choice=None):
        system_prompt = messages[0].content
        assert "artifact.deliver" in system_prompt
        assert "Do not use workspace.read_file as the final capability for binary file delivery" in system_prompt
        return {
            "content": (
                '{"turn_type":"coding","session_mode_update":"coding",'
                '"requested_capabilities":["workspace.search_files","artifact.deliver"],'
                '"task_plan":{"objective":"deliver_existing_file",'
                '"targets":[{"kind":"local_file","path":"E:\\\\pythonProject\\\\jarvis\\\\jarvis-architecture-v2.png","artifact_type":"image"}],'
                '"output":{"type":"artifact","artifact_type":"image","delivery":"send_attachment"},'
                '"final_deliverable":"image_attachment"},'
                '"routing_basis":"explicit","confidence":0.9,"reason":"deliver local image file"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="看一下jarvis项目的E:\\pythonProject\\jarvis\\jarvis-architecture-v2.png这个文件，然后给我",
        session_state=ConversationSessionState(),
    )

    assert classification.turn_type == "coding"
    assert classification.requested_capabilities == ("workspace.search_files", "artifact.deliver")
    assert classification.task_plan["objective"] == "deliver_existing_file"
    assert classification.task_plan["final_deliverable"] == "image_attachment"
    get_settings.cache_clear()


def test_registered_repo_code_request_uses_llm_workspace_capabilities(monkeypatch, tmp_path) -> None:
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
                '{"turn_type":"coding","session_mode_update":"coding",'
                '"requested_capabilities":["workspace.edit"],'
                '"confidence":0.92,"reason":"repository code edit"}'
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
    assert classification.source == "llm"
    assert should_apply_session_mode_update(classification) is True
    assert should_apply_repo_update(classification) is False
    get_settings.cache_clear()


def test_registered_repo_design_request_uses_llm_workspace_inspect(monkeypatch, tmp_path) -> None:
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
                '{"turn_type":"coding","session_mode_update":"coding",'
                '"requested_capabilities":["workspace.inspect"],"target_resources":[]}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    classification = classify_turn(
        content="帮我看看jarvis里agent的设计",
        session_state=ConversationSessionState(session_mode="chat", active_repo_id="nltk"),
    )

    assert classification.turn_type == "coding"
    assert classification.session_mode_update == "coding"
    assert classification.active_repo_id_update is None
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
                '"requested_capabilities":["web.search","workspace.inspect"],"target_resources":[]}'
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
    assert classification.target_resources == ()
    get_settings.cache_clear()


def test_registered_repo_review_with_latest_release_notes_uses_llm_capabilities(monkeypatch, tmp_path) -> None:
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
                '{"turn_type":"coding","session_mode_update":"coding",'
                '"requested_capabilities":["workspace.inspect","web.search"],'
                '"routing_basis":"explicit","confidence":0.9,"reason":"repository review with current context"}'
            )
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

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
    get_settings.cache_clear()
