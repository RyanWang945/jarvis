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
from tests.helpers.mysql import prepare_test_mysql_database


def _client(monkeypatch) -> TestClient:
    prepare_test_mysql_database(monkeypatch)
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
                        "name": "delegate_to_codex",
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


def test_codex_tool_is_rejected_for_non_code_request(monkeypatch) -> None:
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
    assert "Rejected: tool not allowed by runtime policy" in body["reply"]
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "delegate_to_codex"
    assert tool_calls[0].status == "rejected"
    assert "Rejected: tool not allowed by runtime policy" in (tool_calls[0].error_message or "")


def test_codex_tool_runs_for_explicit_code_request(monkeypatch) -> None:
    client = _client(monkeypatch)
    store = get_conversation_store()
    chat_calls = 0

    def _fake_chat(self, messages, tools=None):
        nonlocal chat_calls
        chat_calls += 1
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
                        "name": "delegate_to_codex",
                        "arguments": json.dumps(
                            {
                                "instruction": "Fix the bug in app/channels/feishu_renderer.py and run related checks.",
                                "workdir": str(Path.cwd()),
                                "allow_commit": False,
                                "allow_push": False,
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

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
    assert chat_calls == 2
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "delegate_to_codex"
    assert tool_calls[0].status == "completed"
    assert tool_calls[0].output == {"result": "coder-ran"}


def test_codex_tool_reply_is_used_when_summary_llm_fails(monkeypatch) -> None:
    client = _client(monkeypatch)
    store = get_conversation_store()
    chat_calls = 0

    def _fake_chat(self, messages, tools=None):
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_delegation_1",
                        "type": "function",
                        "function": {
                            "name": "delegate_to_codex",
                            "arguments": json.dumps(
                                {
                                    "instruction": "Fix the bug in app/channels/feishu_renderer.py.",
                                    "workdir": str(Path.cwd()),
                                    "allow_commit": False,
                                    "allow_push": False,
                                }
                            ),
                        },
                    }
                ],
            }
        raise RuntimeError("summary model unavailable")

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(ok=True, exit_code=0, stdout="coder-ran", summary="coder-ran")

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": _unique_id("chat-dm-codex-summary-fail"),
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "Please fix the bug in feishu_renderer.py",
            "external_message_id": _unique_id("msg-codex-summary-fail"),
        },
    ).json()

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] == "coder-ran"
    assert chat_calls == 2
    turn = store.get_turn(created["turn_id"])
    assert turn is not None
    assert turn.status == "completed"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert tool_calls[0].status == "completed"


def test_codex_raw_numeric_output_is_summarized_before_reply(monkeypatch) -> None:
    client = _client(monkeypatch)
    captured_args: list[dict] = []

    def _fake_chat(self, messages, tools=None):
        last_tool = next((m for m in reversed(messages) if m.role == "tool"), None)
        if last_tool is not None and last_tool.tool_call_id == "call_delegation_1":
            assert str(last_tool.content).strip() == "21\n14 580 22"
            return {
                "content": (
                    "jarvis 当前有 21 个未提交条目。\n\n"
                    "diff 统计显示：14 个文件变化，新增 580 行，删除 22 行。"
                ),
                "tool_calls": [],
            }
        if last_tool is not None and last_tool.tool_call_id == "call_tool_search_1":
            tool_names = [
                tool["function"]["name"]
                for tool in (tools or [])
                if tool.get("type") == "function"
            ]
            assert "delegate_to_codex" in tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_delegation_1",
                        "type": "function",
                        "function": {
                            "name": "delegate_to_codex",
                            "arguments": json.dumps(
                                {
                                    "instruction": "Check jarvis uncommitted changes and explain the numbers.",
                                    "repo_id": "jarvis",
                                }
                            ),
                        },
                    }
                ],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_tool_search_1",
                    "type": "function",
                    "function": {
                        "name": "tool_search",
                        "arguments": json.dumps(
                            {
                                "query": "inspect jarvis repository uncommitted changes",
                                "original_user_request": "看一下jarvis当前分支有多少未提交的",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        if tool.name == "tool_search":
            from app.tools.runtime import execute_tool as real_execute_tool

            return real_execute_tool(tool, tool_args, timeout_seconds=timeout_seconds)
        captured_args.append(dict(tool_args))
        return ToolExecutionResult(ok=True, exit_code=0, stdout="21\n14 580 22", summary="21\n14 580 22")

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": _unique_id("chat-dm-codex-summary"),
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "看一下jarvis当前分支有多少未提交的",
            "external_message_id": _unique_id("msg-codex-summary"),
        },
    ).json()

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] != "21\n14 580 22"
    assert "14 个文件变化" in body["reply"]
    assert captured_args
    assert captured_args[0].get("allow_commit") is not True
