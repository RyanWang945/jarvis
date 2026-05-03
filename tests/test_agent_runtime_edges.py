from __future__ import annotations

from app.agent_react import react_graph
from app.api.agent import get_conversation_store
from app.tools.common import ToolExecutionResult
from tests.helpers.agent_harness import (
    ScriptedChat,
    create_agent_test_client,
    create_dm_turn,
    final_response,
    tool_call,
    tool_response,
)


def test_multiple_tool_calls_in_one_assistant_message_share_step_index(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    chat = ScriptedChat([
        tool_response(
            tool_call("obsidian_wiki_query", {"query": "runtime"}, call_id="call_runtime"),
            tool_call("business_knowledge_search", {"query": "agent"}, call_id="call_agent"),
        ),
        final_response("inspected both"),
    ])
    chat.install(monkeypatch)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout=f"out:{tool.name}:{tool_args['query']}",
            summary="ok",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_dm_turn(client, "Inspect the workspace notes.")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert run.json()["reply"] == "inspected both"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert [record.tool_name for record in tool_calls] == ["obsidian_wiki_query", "business_knowledge_search"]
    assert [record.provider_tool_call_id for record in tool_calls] == ["call_runtime", "call_agent"]
    assert [record.step_index for record in tool_calls] == [1, 1]
    assert [record.status for record in tool_calls] == ["completed", "completed"]

    messages = [message for message in store.list_messages(created["conversation_id"]) if message.turn_id == created["turn_id"]]
    assert [message.role for message in messages] == ["user", "assistant", "tool", "tool", "assistant"]


def test_tool_exception_is_audited_and_returned_to_model(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    chat = ScriptedChat([
        tool_response(tool_call("obsidian_wiki_query", {"query": "runtime"}, call_id="call_explodes")),
        final_response("handled tool failure"),
    ])
    chat.install(monkeypatch)

    def _raise_execute_tool(tool, tool_args, *, timeout_seconds=30):
        raise RuntimeError("inspect exploded")

    monkeypatch.setattr(react_graph, "execute_tool", _raise_execute_tool)
    created = create_dm_turn(client, "Inspect the workspace.")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert run.json()["reply"] == "handled tool failure"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].status == "failed"
    assert "Tool execution error: inspect exploded" in (tool_calls[0].error_message or "")
    assert tool_calls[0].output == {"result": "Tool execution error: inspect exploded"}

    tool_messages = [
        message
        for message in store.list_messages(created["conversation_id"])
        if message.turn_id == created["turn_id"] and message.role == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].raw_payload["tool_call_id"] == "call_explodes"
    assert "inspect exploded" in tool_messages[0].content


def test_codex_tool_result_is_passed_through_without_error_wrapper(monkeypatch) -> None:
    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout="Codex requested approval (item/commandExecution/requestApproval).\nApproval ID: codex_1",
            stderr="older command stderr",
            summary="Codex requested approval.",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)

    ok, output = react_graph._execute_single_tool("delegate_to_codex", {"instruction": "commit and push"})

    assert ok is True
    assert output.startswith("Codex requested approval")
    assert "Error (exit_code" not in output
    assert "older command stderr" not in output


def test_codex_delegation_preserves_edit_commit_push_contract(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    captured_args: list[dict] = []
    chat = ScriptedChat([
        tool_response(
            tool_call(
                "delegate_to_codex",
                {
                    "instruction": "Read the current README.md file from the repository root. Show me the full content so I can decide what to update.",
                    "repo_id": "jarvis",
                },
                call_id="call_codex_readonly",
            )
        ),
        final_response("delegated"),
    ])
    chat.install(monkeypatch)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        captured_args.append(dict(tool_args))
        return ToolExecutionResult(ok=True, exit_code=0, stdout="codex-ran", summary="codex-ran")

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_dm_turn(client, "jarvis项目中更新一下README，然后创建commit 并push")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert captured_args
    repaired = captured_args[0]
    assert repaired["allow_commit"] is True
    assert repaired["allow_push"] is True
    assert "Original user request:" in repaired["instruction"]
    assert "jarvis项目中更新一下README，然后创建commit 并push" in repaired["instruction"]
    assert "Do not downgrade" in repaired["instruction"]

    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert tool_calls[0].input["allow_commit"] is True
    assert tool_calls[0].input["allow_push"] is True


def test_tavily_search_budget_rejects_third_call_in_same_turn(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    chat = ScriptedChat([
        tool_response(tool_call("tavily_search", {"query": "Jarvis agent tests"}, call_id="call_search_1")),
        tool_response(tool_call("tavily_search", {"query": "Jarvis ReAct audit"}, call_id="call_search_2")),
        tool_response(tool_call("tavily_search", {"query": "Jarvis runtime edges"}, call_id="call_search_3")),
        final_response("search budget handled"),
    ])
    chat.install(monkeypatch)
    executed_queries: list[str] = []

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        executed_queries.append(tool_args["query"])
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout=f"result:{tool_args['query']}",
            summary="ok",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_dm_turn(client, "Search a few sources about Jarvis agent tests.")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert run.json()["reply"] == "search budget handled"
    assert executed_queries == ["Jarvis agent tests", "Jarvis ReAct audit"]

    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert [record.provider_tool_call_id for record in tool_calls] == [
        "call_search_1",
        "call_search_2",
        "call_search_3",
    ]
    assert [record.step_index for record in tool_calls] == [1, 2, 3]
    assert [record.status for record in tool_calls] == ["completed", "completed", "rejected"]
    assert "tavily_search budget exceeded" in (tool_calls[-1].error_message or "")
