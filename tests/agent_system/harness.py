from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.agent_react import AgentRuntime
from app.agent_react.turn_classifier import TurnClassification
from app.api.agent import InMemoryConversationStore
from app.api.schemas import MessageCreateRequest, SenderInput
from app.llm.client import ChatClient, LLMMessage


def unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def isolated_store() -> InMemoryConversationStore:
    store = InMemoryConversationStore()
    # InMemoryConversationStore creates turns while holding its lock. Avoid
    # re-entering artifact listing in this independent harness.
    store.list_recent_artifacts_by_conversation = lambda conversation_id, *, limit=5: []  # type: ignore[method-assign]
    return store


def create_turn(
    monkeypatch: Any,
    store: InMemoryConversationStore,
    content: str,
    *,
    classification: TurnClassification | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    if classification is not None:
        monkeypatch.setattr("app.api.agent.classify_turn", lambda **kwargs: classification)

    response = store.ingest_message(
        MessageCreateRequest(
            platform="system",
            external_chat_id=chat_id or unique_id("chat"),
            chat_type="dm",
            sender=SenderInput(platform_user_id="agent-system-user", display_name="Agent System Test"),
            content=content,
            external_message_id=unique_id("msg"),
        )
    )
    assert response.turn_id is not None
    return {
        "conversation_id": response.conversation_id,
        "message_id": response.message_id,
        "turn_id": response.turn_id,
        "should_respond": response.should_respond,
    }


def run_turn(store: InMemoryConversationStore, turn_id: int):
    return AgentRuntime(store).run_turn(turn_id)


def tool_call(name: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": call_id or unique_id(f"call-{name}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def tool_response(*calls: dict[str, Any], content: str = "") -> dict[str, Any]:
    return {"content": content, "tool_calls": list(calls)}


def final_response(content: str, *, model: str = "agent-system-test-model") -> dict[str, Any]:
    return {
        "content": content,
        "tool_calls": [],
        "_model": model,
        "_usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


@dataclass
class ScriptedChat:
    responses: list[dict[str, Any] | Callable[[list[LLMMessage], list[dict[str, Any]] | None], dict[str, Any]]]
    calls: list[tuple[list[LLMMessage], list[dict[str, Any]] | None]] = field(default_factory=list)

    def install(self, monkeypatch: Any) -> None:
        iterator = iter(self.responses)

        def _fake_chat(
            _client: ChatClient,
            messages: list[LLMMessage],
            *,
            response_format: dict[str, str] | None = None,
            tools: list[dict[str, Any]] | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del response_format, tool_choice
            self.calls.append((messages, tools))
            try:
                response = next(iterator)
            except StopIteration as exc:
                raise AssertionError("ScriptedChat received more calls than expected.") from exc
            if callable(response):
                return response(messages, tools)
            return response

        monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    @property
    def tool_messages(self) -> Iterator[LLMMessage]:
        for messages, _tools in self.calls:
            yield from (message for message in messages if message.role == "tool")

    @property
    def tool_names_by_call(self) -> list[list[str]]:
        names: list[list[str]] = []
        for _messages, tools in self.calls:
            names.append([str(tool.get("function", {}).get("name")) for tool in tools or []])
        return names
