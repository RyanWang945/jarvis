from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.agent import get_conversation_store
from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage
from app.main import create_app
from app.skills.bootstrap import reset_registries_for_tests


def unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def create_agent_test_client() -> TestClient:
    get_conversation_store.cache_clear()
    get_settings.cache_clear()
    reset_registries_for_tests()
    return TestClient(create_app())


def create_dm_turn(
    client: TestClient,
    content: str,
    *,
    chat_id: str | None = None,
    sender_id: str = "ou_1",
    display_name: str = "Ryan",
) -> dict[str, Any]:
    response = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id or unique_id("chat-dm"),
            "chat_type": "dm",
            "sender": {"platform_user_id": sender_id, "display_name": display_name},
            "content": content,
            "external_message_id": unique_id("msg"),
        },
    )
    assert response.status_code == 202
    return response.json()


def tool_call(name: str, args: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": call_id or unique_id(f"call-{name}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def tool_response(*calls: dict[str, Any], content: str = "") -> dict[str, Any]:
    return {"content": content, "tool_calls": list(calls)}


def final_response(content: str) -> dict[str, Any]:
    return {"content": content, "tool_calls": []}


@dataclass
class ScriptedChat:
    responses: list[dict[str, Any] | Callable[[list[LLMMessage], list[dict[str, Any]] | None], dict[str, Any]]]
    calls: list[tuple[list[LLMMessage], list[dict[str, Any]] | None]] = field(default_factory=list)

    def install(self, monkeypatch: Any) -> None:
        iterator = iter(self.responses)

        def _fake_chat(
            _client: ChatClient,
            messages: list[LLMMessage],
            tools: list[dict[str, Any]] | None = None,
            response_format: dict[str, str] | None = None,
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
