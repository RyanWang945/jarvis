from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.agent_react import react_graph
from app.api.agent import get_conversation_store
from app.config import get_settings
from app.llm.client import ChatClient
from app.tools.common import ToolExecutionResult
from tests.helpers.agent_harness import (
    ScriptedChat,
    create_agent_test_client,
    create_dm_turn,
    final_response,
    tool_call,
    tool_response,
    unique_id,
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


def test_turn_reply_appends_model_and_token_usage_when_provider_reports_usage(monkeypatch) -> None:
    client = create_agent_test_client()
    chat = ScriptedChat([
        {
            "content": "usage reported",
            "tool_calls": [],
            "_model": "deepseek-test",
            "_usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ])
    chat.install(monkeypatch)
    created = create_dm_turn(client, "Report usage.")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    reply = run.json()["reply"]
    assert reply.startswith("usage reported")
    assert "---" in reply
    assert "**本轮调用信息**" not in reply
    assert "- 模型：`deepseek-test`" in reply
    assert "- Token：输入 `10` / 输出 `3` / 合计 `13`" in reply


def test_turn_reply_replaces_model_generated_usage_footer(monkeypatch) -> None:
    client = create_agent_test_client()
    chat = ScriptedChat([
        {
            "content": (
                "usage reported\n\n"
                "---\n"
                "- 模型：`deepseek-v4-flash`\n"
                "- Token：输入 `2500` / 输出 `37` / 合计 `2537`"
            ),
            "tool_calls": [],
            "_model": "deepseek-v4-pro",
            "_usage": {"prompt_tokens": 2501, "completion_tokens": 96, "total_tokens": 2597},
        },
    ])
    chat.install(monkeypatch)
    created = create_dm_turn(client, "Report usage.")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    reply = run.json()["reply"]
    assert "deepseek-v4-flash" not in reply
    assert "2500" not in reply
    assert "- 模型：`deepseek-v4-pro`" in reply
    assert "- Token：输入 `2501` / 输出 `96` / 合计 `2597`" in reply


def test_agent_step_uses_active_model_profile(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "deepseek-key")
    get_settings.cache_clear()
    client = create_agent_test_client()
    chat_id = unique_id("chat-active-model-profile")
    observed_models: list[str] = []

    switched = client.post(
        "/messages",
        json={
            "platform": "feishu",
            "external_chat_id": chat_id,
            "chat_type": "dm",
            "sender": {"platform_user_id": "ou_1", "display_name": "Ryan"},
            "content": "/model deepseek-v4-pro",
            "external_message_id": unique_id("msg-switch-active-model"),
        },
    )
    assert switched.status_code == 202
    assert switched.json()["status"] == "model_updated"

    def _fake_chat(
        self,
        messages,
        tools=None,
        response_format=None,
        tool_choice=None,
    ):
        del messages, tools, response_format, tool_choice
        observed_models.append(self._model)
        return {
            "content": "active model reply",
            "tool_calls": [],
            "_model": self._model,
            "_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }

    monkeypatch.setattr(ChatClient, "chat", _fake_chat)
    created = create_dm_turn(client, "hello after switch", chat_id=chat_id)

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert observed_models == ["deepseek-v4-pro"]
    assert "- 模型：`deepseek-v4-pro`" in run.json()["reply"]
    get_settings.cache_clear()


def test_unsupported_loop_provider_fails_closed_without_llm_call(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    created = create_dm_turn(client, "run with unsupported loop")
    store.update_conversation_metadata(
        created["conversation_id"],
        {"runtime_profile": {"loop_provider": "plan_execute"}},
    )

    def _raise_if_called(self, messages, tools=None, response_format=None, tool_choice=None):
        del self, messages, tools, response_format, tool_choice
        raise AssertionError("unsupported loop provider should not call LLM")

    monkeypatch.setattr(ChatClient, "chat", _raise_if_called)

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert run.json()["status"] == "failed"
    assert run.json()["reply"] == ""
    turn = store.get_turn(created["turn_id"])
    assert turn is not None
    assert turn.status == "failed"
    assert turn.error_message == "Unsupported turn loop provider: plan_execute"


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


def test_codex_contract_repair_does_not_upgrade_diagnosis_plan_request() -> None:
    original_args = {
        "instruction": (
            "在 jarvis 项目中，查看回复渲染/格式化相关的代码。"
            "返回找到的所有相关文件和问题的根因。"
        ),
        "repo_id": "jarvis",
    }

    repaired = react_graph._strengthen_codex_contract(
        original_args,
        [
            HumanMessage(
                content=(
                    "现在你，也就是jarvis的回复中会有符号没有正常显示，"
                    "比如:`pyproject.toml`、`uv.lock`。"
                    "你看看具体是什么问题，先告诉我然后再告诉我修改的计划。"
                )
            )
        ],
    )

    assert repaired == original_args


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


def test_tool_search_grant_expands_allowed_tools_with_audit_log(monkeypatch) -> None:
    client = create_agent_test_client()
    log_messages: list[str] = []
    original_info = react_graph.logger.info

    def _capture_info(message, *args, **kwargs):
        log_messages.append(message % args if args else str(message))
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(react_graph.logger, "info", _capture_info)
    chat = ScriptedChat([
        tool_response(
            tool_call(
                "tool_search",
                {
                    "query": "search Twitter posts about Jarvis",
                    "original_user_request": "X 上大家怎么说 Jarvis",
                },
                call_id="call_tool_search",
            )
        ),
        final_response("grant logged"),
    ])
    chat.install(monkeypatch)
    created = create_dm_turn(client, "X 上大家怎么说 Jarvis")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert any("tool_search grant evaluation" in message and "x_search" in message for message in log_messages)
    assert any("runtime allowed_tools expanded" in message and "x_search" in message for message in log_messages)


def test_tool_search_grant_uses_original_request_for_continuation_message(monkeypatch) -> None:
    client = create_agent_test_client()
    seen_tool_sets: list[list[str]] = []

    def _chat(messages, tools):
        tool_names = [
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ]
        seen_tool_sets.append(tool_names)
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return tool_response(
                tool_call(
                    "tool_search",
                    {
                        "query": "delegate_to_codex inspect file",
                        "original_user_request": "查看 app/channels/feishu_renderer.py 文件内容",
                    },
                    call_id="call_tool_search",
                )
            )
        if tool_messages[-1].tool_call_id == "call_tool_search":
            assert "delegate_to_codex" in tool_names
            return final_response("delegate now available")
        return final_response("done")

    chat = ScriptedChat([_chat, _chat])
    chat.install(monkeypatch)
    created = create_dm_turn(client, "好的")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert "delegate_to_codex" not in seen_tool_sets[0]
    assert "delegate_to_codex" in seen_tool_sets[1]


def test_conversation_tool_intents_append_across_turns(monkeypatch) -> None:
    client = create_agent_test_client()
    store = get_conversation_store()
    chat_id = "chat-tool-intent-append"
    seen_tool_sets: list[list[str]] = []

    def _chat(_messages, tools):
        seen_tool_sets.append([
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ])
        return final_response("done")

    chat = ScriptedChat([_chat, _chat])
    chat.install(monkeypatch)

    created = create_dm_turn(client, "Please inspect app/channels/feishu_renderer.py code.", chat_id=chat_id)
    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    conversation = store.get_conversation(created["conversation_id"])
    assert conversation is not None
    assert "delegate_to_codex" in conversation.metadata.get("active_tool_intents", [])

    continued = create_dm_turn(client, "ok", chat_id=chat_id)
    run = client.post(f"/turns/{continued['turn_id']}/run")

    assert run.status_code == 200
    assert "delegate_to_codex" in seen_tool_sets[0]
    assert "delegate_to_codex" in seen_tool_sets[1]
