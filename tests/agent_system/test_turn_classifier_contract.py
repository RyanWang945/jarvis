from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent_react.session_state import ConversationSessionState
from app.agent_react.turn_classifier import classification_to_metadata, classify_turn
from app.config import get_settings
from app.llm.client import ChatClient
from app.repositories import RepositoryRef, RepositoryRegistry


def _enable_fake_llm(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "agent-system-test-key")
    get_settings.cache_clear()


class CapturingClassifierLLM:
    def __init__(self, content: str | Exception) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def install(self, monkeypatch) -> None:
        def _fake_chat(_client, messages, *, response_format=None, tools=None, tool_choice=None):
            self.calls.append(
                {
                    "messages": messages,
                    "response_format": response_format,
                    "tools": tools,
                    "tool_choice": tool_choice,
                }
            )
            if isinstance(self.content, Exception):
                raise self.content
            return {"content": self.content}

        monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def _registry(monkeypatch, tmp_path: Path, *repo_ids: str) -> RepositoryRegistry:
    refs: list[RepositoryRef] = []
    for repo_id in repo_ids:
        repo = tmp_path / repo_id
        repo.mkdir()
        refs.append(
            RepositoryRef(
                repo_id=repo_id,
                name=repo_id.title(),
                root_path=repo,
                canonical_root_path=repo.resolve(),
            )
        )
    registry = RepositoryRegistry(refs)
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: registry)
    return registry


def test_intent_resolver_prompt_and_payload_are_terminal_schema(monkeypatch, tmp_path: Path) -> None:
    _enable_fake_llm(monkeypatch)
    _registry(monkeypatch, tmp_path, "jarvis")
    llm = CapturingClassifierLLM(
        json.dumps(
            {
                "scene": "project",
                "access": "write",
                "deliver": True,
                "target_resources": [{"type": "repository", "id": "jarvis"}],
                "objective": "为 jarvis 项目生成架构图",
                "routing_basis": "contextual",
                "confidence": 0.88,
                "reason": "active repo artifact request",
            },
            ensure_ascii=False,
        )
    )
    llm.install(monkeypatch)

    classification = classify_turn(
        content="这个图不对，按 agent 流程改一下",
        session_state=ConversationSessionState(
            session_mode="coding",
            active_repo_id="jarvis",
            session_goal="完善 Jarvis agent 测试体系",
            working_summary="正在设计 context manager 和 eval。",
            last_turn_status="completed",
            last_assistant_summary="上轮已经生成 jarvis-architecture-v3.png，但工具路由关系表达不清楚。",
        ),
        conversation_metadata={"active_model_profile": "deepseek-v4-pro"},
        recent_artifacts=[
            {
                "artifact_id": "art-1",
                "kind": "image",
                "filename": "jarvis-architecture-v3.png",
            }
        ],
    )

    assert classification.scene == "project"
    assert classification.access == "write"
    assert classification.deliver is True
    assert classification.objective == "为 jarvis 项目生成架构图"
    assert len(llm.calls) == 1

    call = llm.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["tools"] is None
    assert call["tool_choice"] is None
    system_prompt = call["messages"][0].content
    assert "Return only these fields" in system_prompt
    assert "Do not return turn_type" in system_prompt
    assert "Do not return" in system_prompt and "task_plan" in system_prompt
    payload = json.loads(call["messages"][1].content)
    assert payload["active_repo_id"] == "jarvis"
    assert payload["last_assistant_summary"] == "上轮已经生成 jarvis-architecture-v3.png，但工具路由关系表达不清楚。"
    assert payload["recent_artifacts"][0]["filename"] == "jarvis-architecture-v3.png"
    get_settings.cache_clear()


def test_metadata_contract_has_no_legacy_fields(monkeypatch) -> None:
    _enable_fake_llm(monkeypatch)
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: RepositoryRegistry([]))
    CapturingClassifierLLM(
        json.dumps(
            {
                "scene": "research",
                "access": "read",
                "deliver": False,
                "target_resources": [{"type": "external_service", "id": "hermes", "name": "Hermes"}],
                "objective": "对比最新 Hermes agent 设计和 Jarvis",
                "routing_basis": "explicit",
                "confidence": 1.4,
                "reason": "multi-source comparison",
                "turn_type": "research",
                "requested_capabilities": ["web.search"],
                "task_plan": {"expected_steps": ["search", "compare"]},
                "session_mode_update": "research",
            },
            ensure_ascii=False,
        )
    ).install(monkeypatch)

    classification = classify_turn(
        content="对比最新 Hermes agent 设计和 Jarvis",
        session_state=ConversationSessionState(session_mode="chat"),
    )

    metadata = classification_to_metadata(classification)
    assert metadata == {
        "scene": "research",
        "access": "read",
        "deliver": False,
        "target_resources": [{"type": "external_service", "id": "hermes"}],
        "objective": "对比最新 Hermes agent 设计和 Jarvis",
        "routing_basis": "explicit",
        "confidence": 1.0,
        "reason": "multi-source comparison",
    }
    get_settings.cache_clear()


def test_invalid_or_low_confidence_llm_result_uses_hard_fallback(monkeypatch) -> None:
    _enable_fake_llm(monkeypatch)
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: RepositoryRegistry([]))
    CapturingClassifierLLM('{"turn_type":"coding","confidence":0.99}').install(monkeypatch)

    classification = classify_turn(
        content="这个是不是要改一下？",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="jarvis"),
    )

    assert classification.source == "fallback"
    assert classification.scene == "chat"
    assert classification.access == "none"
    assert classification.objective == "这个是不是要改一下？"
    get_settings.cache_clear()


def test_context_active_repo_is_used_when_llm_resolves_project_without_target(monkeypatch, tmp_path: Path) -> None:
    _enable_fake_llm(monkeypatch)
    _registry(monkeypatch, tmp_path, "nltk")
    CapturingClassifierLLM(
        json.dumps(
            {
                "scene": "project",
                "access": "read",
                "deliver": True,
                "target_resources": [],
                "objective": "为当前项目生成架构图",
                "routing_basis": "contextual",
                "confidence": 0.9,
                "reason": "short imperative resolved from active_repo_id",
            },
            ensure_ascii=False,
        )
    ).install(monkeypatch)

    classification = classify_turn(
        content="画个架构图吧",
        session_state=ConversationSessionState(session_mode="coding", active_repo_id="nltk"),
    )

    assert classification.scene == "project"
    assert classification.access == "read"
    assert classification.deliver is True
    assert classification.target_resources[0].id == "nltk"
    get_settings.cache_clear()


def test_registered_repo_status_fallback_when_llm_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "agent-system-disabled-llm")
    _registry(monkeypatch, tmp_path, "jarvis")

    classification = classify_turn(
        content="看下 jarvis 当前有没有未提交文件",
        session_state=ConversationSessionState(session_mode="chat"),
    )

    assert classification.source == "fallback"
    assert classification.scene == "project"
    assert classification.access == "read"
    assert classification.target_resources[0].id == "jarvis"
    assert classification.objective == "看下 jarvis 当前有没有未提交文件"


def test_unknown_slash_command_falls_through_to_llm(monkeypatch) -> None:
    _enable_fake_llm(monkeypatch)
    monkeypatch.setattr("app.agent_react.turn_classifier.get_repository_registry", lambda: RepositoryRegistry([]))
    llm = CapturingClassifierLLM(
        json.dumps(
            {
                "scene": "chat",
                "access": "none",
                "deliver": False,
                "target_resources": [],
                "objective": "/unknown 这是什么命令",
                "routing_basis": "fallback",
                "confidence": 0.8,
                "reason": "unknown slash text",
            },
            ensure_ascii=False,
        )
    )
    llm.install(monkeypatch)

    classification = classify_turn(
        content="/unknown 这是什么命令",
        session_state=ConversationSessionState(session_mode="chat"),
    )

    assert len(llm.calls) == 1
    assert classification.scene == "chat"
    assert classification.access == "none"
    assert classification.objective == "/unknown 这是什么命令"
    get_settings.cache_clear()
