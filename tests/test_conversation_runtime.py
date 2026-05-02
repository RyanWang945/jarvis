import json
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pathlib import Path
from uuid import uuid4

from app.agent_react import AgentRuntime
from app.agent_react import react_graph as react_graph_module
from app.api.agent import get_conversation_store
from app.config import get_settings
from app.llm.client import ChatClient
from app.main import create_app
from app.skills.bootstrap import reset_registries_for_tests
from app.tools.common import ToolExecutionResult


def _client(monkeypatch) -> TestClient:
    get_conversation_store.cache_clear()
    get_settings.cache_clear()
    reset_registries_for_tests()
    return TestClient(create_app())


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _install_fake_chat(monkeypatch) -> None:
    def _fake_chat(self, messages, tools=None):
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return {"content": f"reply:{last_user}", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def _install_history_count_chat(monkeypatch) -> None:
    def _fake_chat(self, messages, tools=None):
        user_messages = [m.content for m in messages if m.role == "user"]
        last_user = user_messages[-1] if user_messages else ""
        return {
            "content": f"history_users:{len(user_messages)} last:{last_user}",
            "tool_calls": [],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def _install_skill_echo_chat(monkeypatch) -> None:
    def _fake_chat(self, messages, tools=None):
        system_messages = [m.content for m in messages if m.role == "system"]
        selected = next((content for content in system_messages if "Selected skills for this turn." in content), "")
        return {"content": "skill-loaded" if "release checklist" in selected.lower() else "skill-missing", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def _install_delegation_chat(
    monkeypatch,
    *,
    instruction: str,
    workdir: str,
    allow_commit: bool = False,
    allow_push: bool = False,
) -> None:
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


def test_message_api_handles_dm_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm")
    msg_id = _unique_id("msg")

    response = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello, remember my name is Ryan",
            "external_message_id": msg_id,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["should_respond"] is True
    assert body["trigger_type"] == "dm_message"
    assert body["turn_id"] is not None

    messages = client.get(f"/conversations/{body['conversation_id']}/messages").json()
    turns = client.get(f"/conversations/{body['conversation_id']}/turns").json()
    assert messages[0]["turn_id"] == body["turn_id"]
    assert messages[0]["user_id"] is not None
    assert turns[0]["trigger_message_id"] == body["message_id"]


def test_group_background_message_does_not_create_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-group")

    response = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "group",
            "sender": {"platform_user_id": "ou_alice", "display_name": "Alice"},
            "content": "this plan may be too complex",
            "external_message_id": _unique_id("msg-bg"),
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["should_respond"] is False
    assert body["turn_id"] is None
    assert body["status"] == "stored"

    messages = client.get(f"/conversations/{body['conversation_id']}/messages").json()
    turns = client.get(f"/conversations/{body['conversation_id']}/turns").json()
    assert messages[0]["turn_id"] is None
    assert turns == []


def test_group_mention_triggers_turn_and_runtime_reply(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-group")

    background = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "group",
            "sender": {"platform_user_id": "ou_bob", "display_name": "Bob"},
            "content": "we can simplify first and keep only the turn table",
            "external_message_id": _unique_id("msg-bg"),
        },
    ).json()
    triggered = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "group",
            "sender": {"channel_user_id": "ou_ryan", "display_name": "Ryan"},
            "content": "@Jarvis summarize the discussion above",
            "external_message_id": _unique_id("msg-mention"),
            "mentions": ["jarvis"],
        },
    ).json()

    assert triggered["conversation_id"] == background["conversation_id"]
    assert triggered["should_respond"] is True
    assert triggered["trigger_type"] == "mention"
    assert triggered["turn_id"] is not None

    run = client.post(f"/turns/{triggered['turn_id']}/run")

    assert run.status_code == 200
    run_body = run.json()
    assert run_body["status"] == "completed"
    assert run_body["reply"]
    assert run_body["content_type"] == "markdown"

    messages = client.get(f"/conversations/{triggered['conversation_id']}/messages").json()
    assert [message["role"] for message in messages] == ["user", "user", "assistant"]
    assert [message["turn_id"] for message in messages] == [None, triggered["turn_id"], triggered["turn_id"]]
    assert messages[-1]["content_type"] == "markdown"

    turn = client.get(f"/turns/{triggered['turn_id']}").json()
    assert turn["status"] == "completed"


def test_run_turn_uses_trigger_message_boundary(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-boundary")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "first question",
            "external_message_id": _unique_id("msg-boundary"),
        },
    ).json()
    second = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "second question",
            "external_message_id": _unique_id("msg-boundary"),
        },
    ).json()

    run = client.post(f"/turns/{first['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] == "reply:first question"

    second_turn = client.get(f"/turns/{second['turn_id']}").json()
    assert second_turn["status"] == "queued"


def test_cancelled_turn_does_not_generate_reply(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-cancelled")

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "please stop",
            "external_message_id": _unique_id("msg-cancelled"),
        },
    ).json()

    cancelled = client.post(f"/turns/{created['turn_id']}/cancel")
    assert cancelled.status_code == 200

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "cancelled"
    assert body["reply"] == ""

    messages = client.get(f"/conversations/{created['conversation_id']}/messages").json()
    assert [message["role"] for message in messages] == ["user"]


def test_conversation_history_survives_store_recreation(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_history_count_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-restart")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "first question",
            "external_message_id": _unique_id("msg-restart"),
        },
    ).json()
    first_run = client.post(f"/turns/{first['turn_id']}/run").json()

    assert first_run["reply"] == "history_users:1 last:first question"

    get_conversation_store.cache_clear()

    second_client = TestClient(create_app())
    second = second_client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "second question",
            "external_message_id": _unique_id("msg-restart"),
        },
    ).json()
    second_run = second_client.post(f"/turns/{second['turn_id']}/run").json()

    assert second["conversation_id"] == first["conversation_id"]
    assert second_run["reply"] == "history_users:2 last:second question"


def test_runtime_marks_turn_failed_when_graph_crashes(monkeypatch) -> None:
    _client(monkeypatch)
    store = get_conversation_store()
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-fail")

    ingest = store.ingest_message(
        type("Req", (), {
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": type("Sender", (), {
                "platform_user_id": "ou_1",
                "display_name": "Ryan",
                "metadata": {},
            })(),
            "content": "crash me",
            "content_type": "text",
            "external_message_id": _unique_id("msg-fail"),
            "reply_to_message_id": None,
            "reply_to_external_message_id": None,
            "mentions": [],
            "raw_payload": {},
            "metadata": {},
        })()
    )
    runtime = AgentRuntime(store)
    monkeypatch.setattr(runtime._graph, "invoke", lambda state: (_ for _ in ()).throw(RuntimeError("boom")))

    result = runtime.run_turn(ingest.turn_id)

    assert result.status == "failed"
    turn = store.get_turn(ingest.turn_id)
    assert turn is not None
    assert turn.status == "failed"
    assert turn.error_message == "boom"


def test_skill_body_is_injected_when_selected(monkeypatch, tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "release-checklist"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: release-checklist\n"
        "description: Use for release deployment checklist and release verification steps.\n"
        "capabilities:\n"
        "  - release\n"
        "  - deploy\n"
        "---\n\n"
        "# Release checklist\n\n"
        "Run the release checklist before deployment.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_SKILL_PATH", str(skill_root))
    _install_skill_echo_chat(monkeypatch)

    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-skill")
    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "Please help me with the release deploy checklist",
            "external_message_id": _unique_id("msg-skill"),
        },
    ).json()

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] == "skill-loaded"


def test_clear_command_in_dm_creates_new_conversation_generation(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-clear")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-1"),
        },
    ).json()
    assert first["status"] == "queued"

    cleared = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": _unique_id("msg-clear"),
        },
    ).json()

    assert cleared["status"] == "reset"
    assert cleared["should_respond"] is False
    assert cleared["reset_message"] == "已开始新对话。"
    assert cleared["conversation_id"] != first["conversation_id"]

    old_conv = client.get(f"/conversations/{first['conversation_id']}").json()
    assert old_conv["status"] == "archived"

    new_conv = client.get(f"/conversations/{cleared['conversation_id']}").json()
    assert new_conv["status"] == "active"
    assert new_conv["clear_generation"] == old_conv["clear_generation"] + 1

    new_messages = client.get(f"/conversations/{cleared['conversation_id']}/messages").json()
    assert len(new_messages) == 1
    assert new_messages[0]["role"] == "system"
    assert "cleared from" in new_messages[0]["content"]

    old_messages = client.get(f"/conversations/{first['conversation_id']}/messages").json()
    assert any(msg["content"] == "/clear" for msg in old_messages)


def test_clear_command_rejected_when_turn_running(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-clear-busy")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-busy"),
        },
    ).json()

    store = get_conversation_store()
    store.mark_turn_running(first["turn_id"])

    cleared = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": _unique_id("msg-clear-busy"),
        },
    ).json()

    assert cleared["status"] == "reset"
    assert cleared["should_respond"] is False
    assert "正在生成中" in cleared["reset_message"]


def test_clear_command_ignored_in_group(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-group-clear")

    result = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "group",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": _unique_id("msg-group-clear"),
            "mentions": ["jarvis"],
        },
    ).json()

    assert result["should_respond"] is True
    assert result["trigger_type"] == "command"


def test_clear_command_is_idempotent_by_external_message_id(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-clear-dup")
    msg_id = _unique_id("msg-clear-dup")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": msg_id,
        },
    ).json()
    assert first["status"] == "reset"

    second = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": msg_id,
        },
    ).json()
    assert second["status"] == "duplicate"


def test_cancel_command_cancels_running_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-cancel")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-1"),
        },
    ).json()
    store = get_conversation_store()
    store.mark_turn_running(first["turn_id"])

    cancelled = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/cancel",
            "external_message_id": _unique_id("msg-cancel"),
        },
    ).json()

    assert cancelled["status"] == "cancelled"
    assert cancelled["should_respond"] is False
    assert cancelled["reset_message"] == "已取消当前生成。"
    turn = client.get(f"/turns/{first['turn_id']}").json()
    assert turn["status"] == "cancelled"


def test_cancel_command_reports_none_when_no_running_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-cancel-none")

    client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-1"),
        },
    ).json()

    cancel = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/cancel",
            "external_message_id": _unique_id("msg-cancel"),
        },
    ).json()

    assert cancel["status"] == "cancelled"
    assert cancel["should_respond"] is False
    assert "没有正在进行的对话" in cancel["reset_message"]


def test_status_command_returns_conversation_stats(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-status")

    client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-1"),
        },
    ).json()

    status = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/status",
            "external_message_id": _unique_id("msg-status"),
        },
    ).json()

    assert status["status"] == "status_report"
    assert status["should_respond"] is False
    assert "消息数:" in status["reset_message"]
    assert "会话代数:" in status["reset_message"]

def test_tool_call_audit_records_message_relationship_for_rejected_proposal(monkeypatch) -> None:
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
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    tool_call = tool_calls[0]
    assert tool_call.tool_name == "delegate_to_claude_code"
    assert tool_call.assistant_message_id is not None
    assert tool_call.provider_tool_call_id == "call_delegation_1"
    assert tool_call.step_index == 1
    assert tool_call.status == "rejected"
    assert "Rejected: high-privilege delegation" in (tool_call.error_message or "")

    messages = store.list_messages(created["conversation_id"])
    assistant_message = next(message for message in messages if message.id == tool_call.assistant_message_id)
    assert assistant_message.role == "assistant"
    assert assistant_message.raw_payload["tool_calls"][0]["id"] == "call_delegation_1"
    tool_messages = [message for message in messages if message.role == "tool" and message.turn_id == created["turn_id"]]
    assert len(tool_messages) == 1
    assert tool_messages[0].raw_payload["tool_call_id"] == "call_delegation_1"


def test_tool_call_audit_records_message_relationship_for_completed_proposal(monkeypatch) -> None:
    client = _client(monkeypatch)
    store = get_conversation_store()
    _install_delegation_chat(
        monkeypatch,
        instruction="Fix the bug in app/channels/feishu_renderer.py and run related checks.",
        workdir=str(Path.cwd()),
    )

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(ok=True, exit_code=0, stdout="coder-ran", summary="coder-ran")

    monkeypatch.setattr(react_graph_module, "execute_tool", _fake_execute_tool)

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
    tool_call = tool_calls[0]
    assert tool_call.assistant_message_id is not None
    assert tool_call.provider_tool_call_id == "call_delegation_1"
    assert tool_call.step_index == 1
    assert tool_call.status == "completed"
    assert tool_call.output == {"result": "coder-ran"}

    messages = [message for message in store.list_messages(created["conversation_id"]) if message.turn_id == created["turn_id"]]
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
