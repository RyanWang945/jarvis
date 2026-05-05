import json
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pathlib import Path
from uuid import uuid4

from app.agent_react import AgentRuntime
from app.agent_react import react_graph as react_graph_module
from app.agent_react.session_state import ConversationSessionState, load_session_state
from app.api.agent import InMemoryConversationStore, get_conversation_store
from app.api.schemas import MessageCreateRequest, SenderInput
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


def _install_session_echo_chat(monkeypatch) -> None:
    def _fake_chat(self, messages, tools=None):
        system_messages = [m.content for m in messages if m.role == "system"]
        session = next((content for content in system_messages if "Conversation session state:" in content), "")
        if "Goal: compare agent runtime designs" in session and "Working summary: Keep context lightweight." in session:
            return {"content": "session-loaded", "tool_calls": []}
        return {"content": "session-missing", "tool_calls": []}

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


def test_same_conversation_message_waits_behind_active_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-queue")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "first question",
            "external_message_id": _unique_id("msg-queue"),
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
            "external_message_id": _unique_id("msg-queue"),
        },
    ).json()

    assert first["should_respond"] is True
    assert second["should_respond"] is False
    assert second["turn_id"] is not None
    assert second["status"] == "queued"
    assert "已排队" in second["reset_message"]

    run = client.post(f"/turns/{first['turn_id']}/run").json()
    assert run["status"] == "completed"

    claimed = get_conversation_store().claim_next_queued_turn(first["conversation_id"])
    assert claimed is not None
    assert claimed.id == second["turn_id"]
    assert claimed.status == "running"


def test_queued_turn_context_waits_for_previous_assistant_reply(monkeypatch) -> None:
    store = InMemoryConversationStore()
    snapshots: list[list[tuple[str, str]]] = []

    def _fake_chat(self, messages, tools=None):
        snapshots.append(
            [
                (message.role, str(message.content))
                for message in messages
                if message.role in {"user", "assistant"}
            ]
        )
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return {"content": f"reply:{last_user}", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    first = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_queue_context",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_queue_user", display_name="Ryan"),
            content="first question",
            external_message_id="msg_queue_context_1",
        )
    )
    second = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_queue_context",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_queue_user", display_name="Ryan"),
            content="second question",
            external_message_id="msg_queue_context_2",
        )
    )

    assert first.should_respond is True
    assert second.should_respond is False

    runtime = AgentRuntime(store)
    runtime.run_turn(first.turn_id)
    claimed = store.claim_next_queued_turn(first.conversation_id)
    assert claimed is not None
    runtime.run_turn(claimed.id)

    assert snapshots[-1] == [
        ("user", "first question"),
        ("assistant", "reply:first question"),
        ("user", "second question"),
    ]


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


def test_session_state_is_injected_into_model_context(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_session_echo_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-session-context")

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "continue the design review",
            "external_message_id": _unique_id("msg-session-context"),
        },
    ).json()

    get_conversation_store().update_conversation_session(
        created["conversation_id"],
        ConversationSessionState(
            session_mode="research",
            session_goal="compare agent runtime designs",
            working_summary="Keep context lightweight.",
            last_turn_id=999,
            last_turn_status="completed",
            last_assistant_summary="Do not inject this debug summary.",
            updated_by_turn_id=999,
        ),
    )

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["reply"] == "session-loaded"


def test_turn_completion_writes_back_session_debug_state_conservatively(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install_fake_chat(monkeypatch)
    chat_id = _unique_id("chat-dm-session-writeback")

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "continue the design review",
            "external_message_id": _unique_id("msg-session-writeback"),
        },
    ).json()
    store = get_conversation_store()
    store.update_conversation_session(
        created["conversation_id"],
        ConversationSessionState(
            session_mode="research",
            session_goal="compare agent runtime designs",
            working_summary="Do not overwrite this working summary.",
            waiting_for="tool",
        ),
    )

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    state = load_session_state(store.get_conversation(created["conversation_id"]).metadata)
    assert state.session_mode == "research"
    assert state.session_goal == "compare agent runtime designs"
    assert state.working_summary == "Do not overwrite this working summary."
    assert state.waiting_for is None
    assert state.last_turn_id == created["turn_id"]
    assert state.last_turn_status == "completed"
    assert state.last_assistant_summary == "reply:continue the design review"
    assert state.updated_by_turn_id == created["turn_id"]


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


def test_clear_command_preserves_runtime_model_preferences(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-clear-model")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello",
            "external_message_id": _unique_id("msg-clear-model-1"),
        },
    ).json()
    store = get_conversation_store()
    store.update_conversation_metadata(
        first["conversation_id"],
        {
            "active_model_profile": "deepseek-v4-pro",
            "runtime_profile": {
                "loop_provider": "react",
                "model_overrides": {"intent_classifier": "deepseek-v4-flash"},
            },
        },
    )
    store.update_conversation_session(
        first["conversation_id"],
        ConversationSessionState(session_mode="research", working_summary="clear this context"),
    )

    cleared = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/clear",
            "external_message_id": _unique_id("msg-clear-model"),
        },
    ).json()

    new_metadata = store.get_conversation(cleared["conversation_id"]).metadata
    assert new_metadata["active_model_profile"] == "deepseek-v4-pro"
    assert new_metadata["runtime_profile"]["loop_provider"] == "react"
    assert new_metadata["runtime_profile"]["model_overrides"]["intent_classifier"] == "deepseek-v4-flash"
    assert "session" not in new_metadata
    assert new_metadata["cleared_from_conversation_id"] == first["conversation_id"]


def test_duplicate_message_after_clear_does_not_trigger_new_turn(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-dup-after-clear")
    msg_id = _unique_id("msg-review")

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "review 下nltk项目的代码",
            "external_message_id": msg_id,
        },
    ).json()
    assert first["should_respond"] is True

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
    assert cleared["conversation_id"] != first["conversation_id"]

    duplicate = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "review 下nltk项目的代码",
            "external_message_id": msg_id,
        },
    ).json()

    assert duplicate["status"] == "duplicate"
    assert duplicate["should_respond"] is False
    assert duplicate["turn_id"] == first["turn_id"]


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

    created = client.post(
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

    get_conversation_store().update_conversation_session(
        created["conversation_id"],
        ConversationSessionState(
            session_mode="research",
            session_goal="compare agent runtime designs",
            working_summary="Need a lightweight session state before heavier long-run features.",
            last_turn_id=created["turn_id"],
            last_turn_status="queued",
            last_assistant_summary="No assistant response yet.",
            updated_by_turn_id=created["turn_id"],
        ),
    )

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
    assert "Session State" in status["reset_message"]
    assert "Mode: research" in status["reset_message"]
    assert "Goal: compare agent runtime designs" in status["reset_message"]
    assert "Working summary: Need a lightweight session state" in status["reset_message"]
    assert "消息数:" in status["reset_message"]
    assert "会话代数:" in status["reset_message"]
    assert "Loop: react" in status["reset_message"]
    assert "Agent step:" in status["reset_message"]


def test_model_command_lists_and_switches_active_profile(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("JARVIS_KIMI_API_KEY", "kimi-key")
    get_settings.cache_clear()
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-model")

    listing = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/model",
            "external_message_id": _unique_id("msg-model-list"),
        },
    ).json()

    assert listing["status"] == "model_report"
    assert "moonshot-v1-8k" in listing["reset_message"]

    switched = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/model moonshot-v1-8k",
            "external_message_id": _unique_id("msg-model-switch"),
        },
    ).json()

    assert switched["status"] == "model_updated"
    conversation = get_conversation_store().get_conversation(switched["conversation_id"])
    assert conversation is not None
    assert conversation.metadata["active_model_profile"] == "moonshot-v1-8k"
    get_settings.cache_clear()


def test_model_command_hides_unconfigured_providers_and_switches_deepseek_pro(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.delenv("JARVIS_KIMI_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-model-deepseek")

    listing = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/model",
            "external_message_id": _unique_id("msg-model-list-deepseek"),
        },
    ).json()

    assert "deepseek-v4-flash" in listing["reset_message"]
    assert "deepseek-v4-pro" in listing["reset_message"]
    assert "moonshot" not in listing["reset_message"]
    assert "gemini" not in listing["reset_message"]

    switched = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/model deepseek-v4-pro",
            "external_message_id": _unique_id("msg-model-switch-deepseek"),
        },
    ).json()

    assert switched["status"] == "model_updated"
    conversation = get_conversation_store().get_conversation(switched["conversation_id"])
    assert conversation is not None
    assert conversation.metadata["active_model_profile"] == "deepseek-v4-pro"
    get_settings.cache_clear()


def test_repos_command_returns_registered_repositories(monkeypatch) -> None:
    client = _client(monkeypatch)
    chat_id = _unique_id("chat-dm-repos")

    repos = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/repos",
            "external_message_id": _unique_id("msg-repos"),
        },
    ).json()

    assert repos["status"] == "repos_report"
    assert repos["should_respond"] is False
    assert "Registered repositories:" in repos["reset_message"]
    assert "- jarvis" in repos["reset_message"]


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
    assert tool_call.tool_name == "delegate_to_codex"
    assert tool_call.assistant_message_id is not None
    assert tool_call.provider_tool_call_id == "call_delegation_1"
    assert tool_call.step_index == 1
    assert tool_call.status == "rejected"
    assert "Rejected: tool not allowed by runtime policy" in (tool_call.error_message or "")

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


def test_scheduled_task_tool_gets_turn_runtime_context(monkeypatch) -> None:
    store = InMemoryConversationStore()
    captured_args = []
    seen_tool_sets = []

    def _fake_chat(self, messages, tools=None):
        tool_names = [
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ]
        seen_tool_sets.append(tool_names)
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            assert "scheduled_task" not in tool_names
            assert "tool_search" in tool_names
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
                                    "query": "create a reminder in 10 minutes",
                                    "original_user_request": "10分钟后提醒我喝水",
                                }
                            ),
                        },
                    }
                ],
            }
        if tool_messages[-1].tool_call_id == "call_tool_search_1":
            assert "scheduled_task" in tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_reminder_1",
                        "type": "function",
                        "function": {
                            "name": "scheduled_task",
                            "arguments": json.dumps(
                                {
                                    "action": "create",
                                    "title": "提醒喝水",
                                    "prompt": "提醒我喝水",
                                    "time_text": "10分钟后",
                                }
                            ),
                        },
                    }
                ],
            }
        return {"content": "已设置提醒", "tool_calls": []}

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        if tool.name == "tool_search":
            from app.tools.runtime import execute_tool as real_execute_tool

            return real_execute_tool(tool, tool_args, timeout_seconds=timeout_seconds)
        assert tool.name == "scheduled_task"
        captured_args.append(dict(tool_args))
        return ToolExecutionResult(ok=True, exit_code=0, stdout="Reminder created.", summary="Reminder created.")

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)
    monkeypatch.setattr(react_graph_module, "execute_tool", _fake_execute_tool)

    ingest = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_runtime_context",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_runtime_user", display_name="Ryan"),
            content="10分钟后提醒我喝水",
            external_message_id="msg_runtime_context",
        )
    )

    result = AgentRuntime(store).run_turn(ingest.turn_id)

    assert result.status == "completed"
    assert result.reply == "已设置提醒"
    assert "scheduled_task" not in seen_tool_sets[0]
    assert "tool_search" in seen_tool_sets[0]
    assert "scheduled_task" in seen_tool_sets[1]
    assert captured_args == [
        {
            "action": "create",
            "title": "提醒喝水",
            "prompt": "提醒我喝水",
            "time_text": "10分钟后",
            "conversation_id": ingest.conversation_id,
            "created_by_user_id": 1,
            "platform": "feishu",
            "external_chat_id": "oc_runtime_context",
        }
    ]


def test_tool_search_no_capable_tool_does_not_unlock_action_tools(monkeypatch) -> None:
    store = InMemoryConversationStore()
    seen_tool_sets = []

    def _fake_chat(self, messages, tools=None):
        tool_names = [
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ]
        seen_tool_sets.append(tool_names)
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
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
                                    "query": "git diff stat numbers meaning",
                                    "original_user_request": "21\n14 580 22啥意思",
                                }
                            ),
                        },
                    }
                ],
            }
        assert "scheduled_task" not in tool_names
        assert "delegate_to_codex" not in tool_names
        return {"content": "这是一段 git 统计输出。", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    ingest = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_tool_search_none",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_tool_search_user", display_name="Ryan"),
            content="21\n14 580 22啥意思",
            external_message_id="msg_tool_search_none",
        )
    )

    result = AgentRuntime(store).run_turn(ingest.turn_id)

    assert result.status == "completed"
    assert result.reply == "这是一段 git 统计输出。"
    assert "tool_search" in seen_tool_sets[0]
    assert "scheduled_task" not in seen_tool_sets[0]
    assert "delegate_to_codex" not in seen_tool_sets[1]


def test_ask_user_completes_turn_and_records_pending_question(monkeypatch) -> None:
    store = InMemoryConversationStore()
    seen_tool_sets = []

    def _fake_chat(self, messages, tools=None):
        tool_names = [
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ]
        seen_tool_sets.append(tool_names)
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ask_user_1",
                    "type": "function",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps(
                            {
                                "question": "你想检查 jarvis 还是 nltk？",
                                "reason": "The repository target is ambiguous.",
                                "expected_answer_type": "choice",
                                "choices": ["jarvis", "nltk"],
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    ingest = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_ask_user",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_ask_user", display_name="Ryan"),
            content="检查一下当前项目 diff",
            external_message_id="msg_ask_user",
        )
    )

    result = AgentRuntime(store).run_turn(ingest.turn_id)

    assert result.status == "completed"
    assert result.reply == "你想检查 jarvis 还是 nltk？"
    assert "ask_user" in seen_tool_sets[0]

    state = load_session_state(store.get_conversation(ingest.conversation_id).metadata)
    assert state.waiting_for == "user"
    assert state.pending_user_question == "你想检查 jarvis 还是 nltk？"
    assert state.pending_user_expected_answer_type == "choice"
    assert state.pending_user_choices == ("jarvis", "nltk")
    assert state.pending_user_turn_id == ingest.turn_id


def test_pending_user_question_is_visible_to_next_turn(monkeypatch) -> None:
    store = InMemoryConversationStore()
    saw_pending = []

    def _fake_chat(self, messages, tools=None):
        system_text = "\n".join(m.content for m in messages if m.role == "system")
        saw_pending.append("Question: Which repository should I inspect?" in system_text)
        return {"content": "using jarvis", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    first = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_ask_user_next",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_ask_user_next", display_name="Ryan"),
            content="which repo?",
            external_message_id="msg_ask_user_next_1",
        )
    )
    store.update_conversation_session(
        first.conversation_id,
        ConversationSessionState(
            waiting_for="user",
            pending_user_question="Which repository should I inspect?",
            pending_user_expected_answer_type="choice",
            pending_user_choices=("jarvis", "nltk"),
            pending_user_turn_id=123,
        ),
    )

    second = store.ingest_message(
        MessageCreateRequest(
            platform="feishu",
            external_chat_id="oc_ask_user_next",
            chat_type="dm",
            sender=SenderInput(platform_user_id="ou_ask_user_next", display_name="Ryan"),
            content="jarvis",
            external_message_id="msg_ask_user_next_2",
        )
    )

    result = AgentRuntime(store).run_turn(second.turn_id)

    assert result.status == "completed"
    assert saw_pending == [True]
    state = load_session_state(store.get_conversation(second.conversation_id).metadata)
    assert state.waiting_for is None
    assert state.pending_user_question is None
