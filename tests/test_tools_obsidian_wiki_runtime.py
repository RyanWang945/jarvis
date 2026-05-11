import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.agent import get_conversation_store
from app.llm.client import ChatClient
from app.main import create_app
from app.skills.bootstrap import reset_registries_for_tests
from tests.helpers.mysql import prepare_test_mysql_database


def _client(monkeypatch) -> TestClient:
    prepare_test_mysql_database(monkeypatch)
    reset_registries_for_tests()
    return TestClient(create_app())


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _install_obsidian_wiki_chat(monkeypatch, *, vault_path: str) -> None:
    def _fake_chat(self, messages, tools=None):
        tool_messages = [m for m in messages if m.role == "tool"]
        if len(tool_messages) == 0:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_wiki_draft_1",
                        "type": "function",
                        "function": {
                            "name": "obsidian_wiki_draft",
                            "arguments": json.dumps(
                                {
                                    "vault_path": vault_path,
                                    "title": "Runtime Draft",
                                    "page_type": "design",
                                    "content": "This page was created through the ReAct runtime integration test.",
                                    "source_ids": [],
                                }
                            ),
                        },
                    }
                ],
            }
        if len(tool_messages) == 1:
            payload = json.loads(tool_messages[-1].content)
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_wiki_apply_1",
                        "type": "function",
                        "function": {
                            "name": "obsidian_wiki_apply",
                            "arguments": json.dumps(
                                {
                                    "vault_path": vault_path,
                                    "draft_id": payload["draft_id"],
                                }
                            ),
                        },
                    }
                ],
            }
        return {
            "content": "wiki-applied",
            "tool_calls": [],
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)


def test_obsidian_wiki_tools_run_inside_react_runtime(monkeypatch) -> None:
    vault_root = Path("sandbox") / _unique_id("obsidian-wiki-runtime")
    vault_path = vault_root / "JarvisWiki"
    client = _client(monkeypatch)
    store = get_conversation_store()
    _install_obsidian_wiki_chat(monkeypatch, vault_path=str(vault_path))

    try:
        created = client.post(
            "/messages",
            json={
                "platform": "feishu",
                "external_chat_id": _unique_id("chat-dm-wiki-runtime"),
                "chat_type": "dm",
                "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
                "content": "Please write this design note into the wiki",
                "external_message_id": _unique_id("msg-wiki-runtime"),
            },
        ).json()

        run = client.post(f"/turns/{created['turn_id']}/run")

        assert run.status_code == 200
        body = run.json()
        assert body["status"] == "completed"
        assert body["reply"] == "wiki-applied"

        tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
        assert [tool_call.tool_name for tool_call in tool_calls] == ["obsidian_wiki_draft", "obsidian_wiki_apply"]
        assert all(tool_call.status == "completed" for tool_call in tool_calls)
        assert (vault_path / "vault" / "projects" / "jarvis" / "designs" / "runtime-draft.md").exists()
    finally:
        shutil.rmtree(vault_root, ignore_errors=True)
