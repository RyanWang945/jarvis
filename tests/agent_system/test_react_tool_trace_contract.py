from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent_react import react_graph
from app.agent_react.turn_classifier import TurnClassification
from app.tools.common import ToolExecutionResult
from tests.agent_system.harness import (
    ScriptedChat,
    create_turn,
    final_response,
    isolated_store,
    run_turn,
    tool_call,
    tool_response,
)


def test_react_contract_records_tool_call_trace_and_passes_observation_to_model(monkeypatch) -> None:
    store = isolated_store()
    chat = ScriptedChat(
        [
            tool_response(
                tool_call("business_knowledge_search", {"query": "agent testing"}, call_id="call-business")
            ),
            final_response("基于业务知识检索结果，agent 测试要看 trace。"),
        ]
    )
    chat.install(monkeypatch)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        del timeout_seconds
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout=f"tool={tool.name}; query={tool_args['query']}",
            summary="searched",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_turn(
        monkeypatch,
        store,
        "从业务知识里查一下 agent 测试应该关注什么",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    assert "agent 测试要看 trace" in result.reply
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "business_knowledge_search"
    assert tool_calls[0].provider_tool_call_id == "call-business"
    assert tool_calls[0].status == "completed"
    assert tool_calls[0].step_index == 1

    persisted_roles = [
        message.role
        for message in store.list_messages(created["conversation_id"])
        if message.turn_id == created["turn_id"]
    ]
    assert persisted_roles == ["user", "assistant", "tool", "assistant"]
    assert any("query=agent testing" in message.content for message in chat.tool_messages)


def test_react_contract_enforces_allowed_tool_surface_from_runtime_policy(monkeypatch) -> None:
    store = isolated_store()

    def _assert_search_tool_surface(messages, tools):
        del messages
        names = {tool["function"]["name"] for tool in tools or []}
        assert "tavily_search" in names
        assert "delegate_to_codex" not in names
        assert "scheduled_task" not in names
        return final_response("已确认工具面。")

    chat = ScriptedChat([_assert_search_tool_surface])
    chat.install(monkeypatch)

    created = create_turn(
        monkeypatch,
        store,
        "查一下最近 agent eval 的动态",
        classification=TurnClassification(
            scene="research",
            access="read",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    assert result.reply.startswith("已确认工具面。")
    assert chat.tool_names_by_call[0].count("tavily_search") == 1


def test_react_contract_rejects_tool_not_allowed_by_runtime_policy(monkeypatch) -> None:
    store = isolated_store()
    chat = ScriptedChat(
        [
            tool_response(
                tool_call("scheduled_task", {"action": "create", "title": "drink water"}, call_id="call-reminder")
            ),
            final_response("我不能在当前工具策略下创建提醒。"),
        ]
    )
    chat.install(monkeypatch)
    created = create_turn(
        monkeypatch,
        store,
        "随便聊聊，不需要提醒",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "scheduled_task"
    assert tool_calls[0].status == "rejected"
    assert "tool not allowed by runtime policy" in (tool_calls[0].error_message or "")
    assert any("tool not allowed by runtime policy" in message.content for message in chat.tool_messages)


def test_react_contract_caps_tavily_search_budget(monkeypatch) -> None:
    store = isolated_store()
    calls = [
        tool_call("tavily_search", {"query": f"agent eval item {index}"}, call_id=f"call-search-{index}")
        for index in range(11)
    ]
    chat = ScriptedChat(
        [
            tool_response(*calls),
            final_response("已基于预算内搜索结果总结。"),
        ]
    )
    chat.install(monkeypatch)

    executed_queries: list[str] = []

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        del timeout_seconds
        executed_queries.append(str(tool_args["query"]))
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout=f"result for {tool_args['query']}",
            summary="searched",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_turn(
        monkeypatch,
        store,
        "查最近 agent eval 动态",
        classification=TurnClassification(
            scene="research",
            access="read",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(tool_calls) == 11
    assert [record.status for record in tool_calls].count("completed") == 10
    assert tool_calls[-1].status == "rejected"
    assert "budget exceeded" in (tool_calls[-1].error_message or "")
    assert len(executed_queries) == 10


def test_react_contract_ask_user_waiting_payload_completes_turn(monkeypatch) -> None:
    store = isolated_store()
    chat = ScriptedChat(
        [
            tool_response(
                tool_call(
                    "ask_user",
                    {
                        "question": "你希望我只检查测试，还是也补实现？",
                        "expected_answer_type": "choice",
                        "choices": ["只检查测试", "也补实现"],
                    },
                    call_id="call-ask",
                )
            )
        ]
    )
    chat.install(monkeypatch)
    created = create_turn(
        monkeypatch,
        store,
        "看看这个任务要不要继续",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    assert result.reply.startswith("你希望我只检查测试")
    assert len(chat.calls) == 1
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert tool_calls[0].status == "completed"
    conversation = store.get_conversation(created["conversation_id"])
    assert conversation is not None
    assert conversation.metadata["session"]["waiting_for"] == "user"
    assert conversation.metadata["session"]["pending_user_turn_id"] == created["turn_id"]


def test_react_contract_codex_approval_request_is_final_reply_without_summary(monkeypatch) -> None:
    store = isolated_store()
    chat = ScriptedChat(
        [
            tool_response(
                tool_call(
                    "delegate_to_codex",
                    {
                        "instruction": "Commit and push the current branch.",
                        "repo_id": "jarvis",
                        "allow_commit": True,
                        "allow_push": True,
                    },
                    call_id="call-codex",
                )
            )
        ]
    )
    chat.install(monkeypatch)

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        del tool, tool_args, timeout_seconds
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout="Codex requested approval (item/commandExecution/requestApproval).\nApproval ID: codex_123",
            stderr="ignored stderr",
            summary="Codex requested approval.",
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_turn(
        monkeypatch,
        store,
        "直接帮我 commit 并 push",
        classification=TurnClassification(
            scene="project",
            access="push",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    assert result.reply.startswith("Codex requested approval")
    assert "ignored stderr" not in result.reply
    assert len(chat.calls) == 1
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert tool_calls[0].status == "completed"


def test_react_contract_final_llm_call_omits_tools_at_max_step_boundary(monkeypatch) -> None:
    store = isolated_store()

    def _assert_no_tools_on_force_final(messages, tools):
        assert tools is None
        assert messages[-1].role == "user"
        return final_response("达到步数边界，给出阶段性结论。")

    chat = ScriptedChat([_assert_no_tools_on_force_final])
    chat.install(monkeypatch)
    created = create_turn(
        monkeypatch,
        store,
        "给个阶段性结论",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )

    # Summary mode has max_steps=4. Pre-seed the runtime state at step 3 via a
    # narrow monkeypatch so call_llm exercises force_final without a long loop.
    from app.agent_react.runtime import TurnRuntime

    original_prepare = TurnRuntime._prepare

    def _prepare_at_boundary(self, state):
        prepared = original_prepare(self, state)
        return {**prepared, "step_count": 5}

    monkeypatch.setattr(TurnRuntime, "_prepare", _prepare_at_boundary)

    result = run_turn(store, created["turn_id"])

    assert result.status == "completed"
    assert result.reply.startswith("达到步数边界")


def test_react_contract_unknown_allowed_tool_is_audited_as_failed(monkeypatch) -> None:
    store = isolated_store()
    created = create_turn(
        monkeypatch,
        store,
        "trigger direct execute_tools",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )
    state = {
        "turn_id": created["turn_id"],
        "messages": [
            SystemMessage(content="system"),
            HumanMessage(content="trigger direct execute_tools"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-ghost",
                        "name": "ghost_tool",
                        "args": {"x": 1},
                    }
                ],
            ),
        ],
        "artifacts": [],
        "cancelled": False,
        "status": "running",
        "step_count": 1,
        "token_budget": None,
        "token_usage": None,
        "model": None,
        "allowed_tools": ["ghost_tool"],
        "max_steps": 8,
        "search_budget": 0,
    }

    next_state = react_graph.execute_tools(state, store)

    assert next_state["status"] == "running"
    records = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(records) == 1
    assert records[0].tool_name == "ghost_tool"
    assert records[0].status == "failed"
    assert "unknown tool: ghost_tool" in (records[0].error_message or "")


def test_react_contract_shell_run_policy_rejects_dangerous_git_push_even_if_allowed(monkeypatch) -> None:
    store = isolated_store()
    created = create_turn(
        monkeypatch,
        store,
        "trigger direct shell policy",
        classification=TurnClassification(
            scene="chat",
            access="none",
            confidence=0.9,
            source="agent_system_test",
            routing_basis="explicit",
        ),
    )
    state = {
        "turn_id": created["turn_id"],
        "messages": [
            SystemMessage(content="system"),
            HumanMessage(content="直接 git push"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-push",
                        "name": "shell_run_command",
                        "args": {"command": "git push origin master"},
                    }
                ],
            ),
        ],
        "artifacts": [],
        "cancelled": False,
        "status": "running",
        "step_count": 1,
        "token_budget": None,
        "token_usage": None,
        "model": None,
        "allowed_tools": ["shell_run_command"],
        "max_steps": 8,
        "search_budget": 0,
    }

    next_state = react_graph.execute_tools(state, store)

    assert next_state["status"] == "running"
    records = store.list_tool_calls_by_turn(created["turn_id"])
    assert len(records) == 1
    assert records[0].status == "rejected"
    assert "too risky" in (records[0].error_message or "")
    assert "delegate_to_codex" in (records[0].error_message or "")
