import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent_react import react_graph
from app.api.agent import get_conversation_store
from app.llm.client import ChatClient
from app.main import create_app
from app.skills.bootstrap import reset_registries_for_tests
from app.tools.common import ToolExecutionResult


def _client(monkeypatch) -> TestClient:
    get_conversation_store.cache_clear()
    reset_registries_for_tests()
    return TestClient(create_app())


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _install_delegation_chat(monkeypatch, *, instruction: str, workdir: str, allow_commit: bool = False, allow_push: bool = False) -> None:
    def _fake_chat(self, messages, tools=None):
        last_tool = next((m for m in reversed(messages) if m.role == "tool"), None)
        if last_tool is not None:
            return {"content": str(last_tool.content), "tool_calls": []}
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_delegation_1",
                    "type": "function",
                    "function": {
                        "name": "delegate_to_claude_code",
                        "arguments": json.dumps(
                            {
                                "instruction": instruction,
                                "workdir": workdir,
                                "allow_commit": allow_commit,
                                "allow_push": allow_push,
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def test_claude_code_tool_is_rejected_for_non_code_request(monkeypatch) -> None:
    client = _client(monkeypatch)
    store = get_conversation_store()
    _install_delegation_chat(
        monkeypatch,
        instruction="Check the repository and change files as needed.",
        workdir=str(Path.cwd()),
    )

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": _unique_id("chat-dm-proposal-reject"),
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "What is the capital of France?",
            "external_message_id": _unique_id("msg-proposal-reject"),
        },
    ).json()

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert "Rejected: high-privilege delegation" in body["reply"]
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "delegate_to_claude_code"
    assert tool_calls[0].status == "rejected"
    assert "Rejected: high-privilege delegation" in (tool_calls[0].error_message or "")


def test_claude_code_tool_runs_for_explicit_code_request(monkeypatch) -> None:
    client = _client(monkeypatch)
    store = get_conversation_store()
    _install_delegation_chat(
        monkeypatch,
        instruction="Fix the bug in app/channels/feishu_renderer.py and run related checks.",
        workdir=str(Path.cwd()),
        allow_commit=False,
        allow_push=False,
    )

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(ok=True, exit_code=0, stdout="coder-ran", summary="coder-ran")

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": _unique_id("chat-dm-proposal-accept"),
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "Please fix the bug in feishu_renderer.py",
            "external_message_id": _unique_id("msg-proposal-accept"),
        },
    ).json()

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] == "coder-ran"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "delegate_to_claude_code"
    assert tool_calls[0].status == "completed"
    assert tool_calls[0].output == {"result": "coder-ran"}
