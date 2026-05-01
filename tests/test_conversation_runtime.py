import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.agent_react import AgentRuntime
from app.api.agent import get_conversation_store
from app.config import get_settings
from app.llm.client import ChatClient
from app.main import create_app


def _client(monkeypatch) -> TestClient:
    get_conversation_store.cache_clear()
    get_settings.cache_clear()

    store = get_conversation_store()
    with store._engine.begin() as conn:
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
        conn.execute(sa.text("TRUNCATE TABLE tool_calls"))
        conn.execute(sa.text("TRUNCATE TABLE turns"))
        conn.execute(sa.text("TRUNCATE TABLE messages"))
        conn.execute(sa.text("TRUNCATE TABLE conversations"))
        conn.execute(sa.text("TRUNCATE TABLE users"))
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))

    return TestClient(create_app())


def _install_fake_chat(monkeypatch) -> None:
    def _fake_chat(self, messages, tools=None):
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return {"content": f"reply:{last_user}", "tool_calls": []}

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def test_message_api_handles_dm_turn(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-dm-1",
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "hello, remember my name is Ryan",
            "external_message_id": "msg-1",
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

    response = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-group-1",
            "chat_type": "group",
            "sender": {"platform_user_id": "ou_alice", "display_name": "Alice"},
            "content": "this plan may be too complex",
            "external_message_id": "msg-bg-1",
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

    background = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-group-2",
            "chat_type": "group",
            "sender": {"platform_user_id": "ou_bob", "display_name": "Bob"},
            "content": "we can simplify first and keep only the turn table",
            "external_message_id": "msg-bg-2",
        },
    ).json()
    triggered = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-group-2",
            "chat_type": "group",
            "sender": {"channel_user_id": "ou_ryan", "display_name": "Ryan"},
            "content": "@Jarvis summarize the discussion above",
            "external_message_id": "msg-mention-1",
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

    first = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-dm-boundary",
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "first question",
            "external_message_id": "msg-boundary-1",
        },
    ).json()
    second = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-dm-boundary",
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "second question",
            "external_message_id": "msg-boundary-2",
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

    created = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": "chat-dm-cancelled",
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "please stop",
            "external_message_id": "msg-cancelled-1",
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


def test_runtime_marks_turn_failed_when_graph_crashes(monkeypatch) -> None:
    _client(monkeypatch)
    store = get_conversation_store()
    _install_fake_chat(monkeypatch)

    ingest = store.ingest_message(
        type("Req", (), {
            "platform": "feishu",
            "external_chat_id": "chat-dm-fail",
            "chat_type": "dm",
            "sender": type("Sender", (), {
                "platform_user_id": "ou_1",
                "display_name": "Ryan",
                "metadata": {},
            })(),
            "content": "crash me",
            "content_type": "text",
            "external_message_id": "msg-fail-1",
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
