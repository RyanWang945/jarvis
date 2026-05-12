from __future__ import annotations

from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from types import SimpleNamespace

from app.agent_react import react_graph
from app.agent_react.artifacts import resolve_channel_attachments
from app.agent_react.runtime import TurnRuntime
from app.agent_react.turn_classifier import TurnClassification
from app.tools.common import ToolArtifact
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


def test_llm_input_log_helpers_include_tool_names_and_args() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_read",
                    "name": "read_file",
                    "args": {"path": "app/agent_react/react_graph.py"},
                }
            ],
        ),
        ToolMessage(content='{"content": "result"}', tool_call_id="call_read"),
    ]

    assert react_graph._tool_call_names_by_id(messages) == {"call_read": "read_file"}
    assert react_graph._tool_calls_for_log(messages[0]) == [
        {
            "id": "call_read",
            "name": "read_file",
            "args": '{"path": "app/agent_react/react_graph.py"}',
        }
    ]
    assert react_graph._tool_calls_for_log(messages[1]) == []


def test_multiple_tool_calls_in_one_assistant_message_share_step_index(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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


def test_direct_tool_artifacts_are_bound_to_current_turn(monkeypatch) -> None:
    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout="created image",
            tool_artifacts=[
                ToolArtifact(
                    artifact_id="image-1",
                    kind="image",
                    path="diagram.png",
                    source_tool="",
                )
            ],
        )

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)

    outcome = react_graph._execute_single_tool(
        "delegate_to_codex",
        {"instruction": "generate image"},
        turn_id=123,
        tool_call_id="call_image",
    )

    assert outcome.artifacts[0].turn_id == 123
    assert outcome.artifacts[0].tool_call_id == "call_image"
    assert outcome.artifacts[0].source_tool == "delegate_to_codex"


def test_tool_artifacts_flow_to_channel_message_attachments(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
    store = get_conversation_store()
    artifact_dir = Path(".pytest_tmp_artifacts_flow") / unique_id("run")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_path = artifact_dir / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
    chat = ScriptedChat([
        tool_response(
            tool_call(
                "delegate_to_codex",
                {"instruction": "Generate a PNG diagram.", "repo_id": "jarvis"},
                call_id="call_codex_image",
            )
        ),
        final_response("generated image"),
    ])
    chat.install(monkeypatch)

    def _fake_registry():
        return SimpleNamespace(
            list_repositories=lambda: [
                SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
            ],
            resolve_repo=lambda repo_id: SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
        )

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout="created diagram",
            artifacts=[str(image_path)],
            summary="created diagram",
        )

    monkeypatch.setattr("app.agent_react.artifacts.get_repository_registry", _fake_registry)
    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_dm_turn(client, "Please fix the bug in app/channels/feishu.py and generate a PNG.")

    try:
        run = client.post(f"/turns/{created['turn_id']}/run")
    finally:
        try:
            image_path.unlink()
            artifact_dir.rmdir()
            artifact_dir.parent.rmdir()
        except OSError:
            pass

    assert run.status_code == 200
    body = run.json()
    assert body["reply"] == "generated image"
    assert body["attachments"][0]["kind"] == "image"
    assert body["attachments"][0]["path"] == str(image_path.resolve())
    assert body["attachments"][0]["mime_type"] == "image/png"

    tool_message = next(
        message
        for message in store.list_messages(created["conversation_id"])
        if message.role == "tool" and message.turn_id == created["turn_id"]
    )
    assert tool_message.raw_payload["artifacts"][0]["path"] == str(image_path.resolve())
    artifact_record = store.get_artifact(tool_message.raw_payload["artifacts"][0]["artifact_id"])
    assert artifact_record is not None
    assert artifact_record.conversation_id == created["conversation_id"]
    assert artifact_record.turn_id == created["turn_id"]
    assert artifact_record.path == str(image_path.resolve())


def test_svg_artifact_resolves_to_png_preview_attachment(monkeypatch) -> None:
    artifact_dir = Path(".pytest_tmp_svg_preview") / unique_id("run")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    svg_path = artifact_dir / "diagram.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>", encoding="utf-8")

    def _fake_registry():
        return SimpleNamespace(
            list_repositories=lambda: [
                SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
            ],
        )

    class FakeCairoSvg:
        @staticmethod
        def svg2png(*, url: str, write_to: str) -> None:
            assert url == str(svg_path.resolve())
            Path(write_to).write_bytes(b"\x89PNG\r\n\x1a\npreview")

    monkeypatch.setattr("app.agent_react.artifacts.get_repository_registry", _fake_registry)
    monkeypatch.setattr("app.agent_react.artifacts.importlib.import_module", lambda name: FakeCairoSvg)

    try:
        result = resolve_channel_attachments([
            ToolArtifact(
                artifact_id="turn:call:svg",
                kind="file",
                path=str(svg_path),
                mime_type="image/svg+xml",
                filename="diagram.svg",
                size_bytes=svg_path.stat().st_size,
                source_tool="delegate_to_codex",
            )
        ])
    finally:
        try:
            svg_path.unlink()
            artifact_dir.rmdir()
            artifact_dir.parent.rmdir()
        except OSError:
            pass

    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.artifact_id == "turn:call:svg:preview:png"
    assert attachment.kind == "image"
    assert attachment.mime_type == "image/png"
    assert attachment.filename == "diagram.preview.png"
    assert attachment.metadata["source_path"].endswith("diagram.svg")
    assert result.rejected == ()
    try:
        Path(attachment.path).unlink()
    except OSError:
        pass


def test_artifact_resolver_rejects_artifacts_from_other_turn(monkeypatch) -> None:
    artifact_dir = Path(".pytest_tmp_turn_artifact_scope") / unique_id("run")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_path = artifact_dir / "old-turn.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")

    def _fake_registry():
        return SimpleNamespace(
            list_repositories=lambda: [
                SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
            ],
        )

    monkeypatch.setattr("app.agent_react.artifacts.get_repository_registry", _fake_registry)

    try:
        result = resolve_channel_attachments(
            [
                ToolArtifact(
                    artifact_id="old-turn:image",
                    kind="image",
                    turn_id=10,
                    tool_call_id="call_old",
                    path=str(image_path),
                    mime_type="image/png",
                    filename="old-turn.png",
                    size_bytes=image_path.stat().st_size,
                    source_tool="delegate_to_codex",
                )
            ],
            turn_id=11,
        )
    finally:
        try:
            image_path.unlink()
            artifact_dir.rmdir()
            artifact_dir.parent.rmdir()
        except OSError:
            pass

    assert result.attachments == ()
    assert result.rejected[0].reason == "artifact_turn_mismatch"


def test_svg_artifact_rejects_when_preview_renderer_unavailable(monkeypatch) -> None:
    artifact_dir = Path(".pytest_tmp_svg_preview_missing") / unique_id("run")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    svg_path = artifact_dir / "diagram.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")

    def _fake_registry():
        return SimpleNamespace(
            list_repositories=lambda: [
                SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
            ],
        )

    def _missing_module(name: str):
        raise ImportError(name)

    monkeypatch.setattr("app.agent_react.artifacts.get_repository_registry", _fake_registry)
    monkeypatch.setattr("app.agent_react.artifacts.importlib.import_module", _missing_module)
    monkeypatch.setattr("app.agent_react.artifacts._find_svg_preview_browser", lambda: None)

    try:
        result = resolve_channel_attachments([
            ToolArtifact(
                artifact_id="turn:call:svg",
                kind="file",
                path=str(svg_path),
                mime_type="image/svg+xml",
                filename="diagram.svg",
                size_bytes=svg_path.stat().st_size,
                source_tool="delegate_to_codex",
            )
        ])
    finally:
        try:
            svg_path.unlink()
            artifact_dir.rmdir()
            artifact_dir.parent.rmdir()
        except OSError:
            pass

    assert result.attachments == ()
    assert result.rejected[0].reason == "svg_preview_unavailable"


def test_svg_artifact_uses_browser_preview_fallback(monkeypatch) -> None:
    artifact_dir = Path(".pytest_tmp_svg_preview_browser") / unique_id("run")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    svg_path = artifact_dir / "diagram.svg"
    svg_path.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'></svg>",
        encoding="utf-8",
    )
    browser_path = artifact_dir / "browser.exe"
    browser_path.write_text("", encoding="utf-8")

    def _fake_registry():
        return SimpleNamespace(
            list_repositories=lambda: [
                SimpleNamespace(canonical_root_path=artifact_dir.resolve()),
            ],
        )

    def _missing_module(name: str):
        raise ImportError(name)

    def _fake_run(command, **kwargs):
        screenshot_arg = next(item for item in command if str(item).startswith("--screenshot="))
        Path(str(screenshot_arg).split("=", 1)[1]).write_bytes(b"\x89PNG\r\n\x1a\npreview")
        assert "--window-size=640,360" in command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.agent_react.artifacts.get_repository_registry", _fake_registry)
    monkeypatch.setattr("app.agent_react.artifacts.importlib.import_module", _missing_module)
    monkeypatch.setattr("app.agent_react.artifacts._find_svg_preview_browser", lambda: browser_path)
    monkeypatch.setattr("app.agent_react.artifacts.subprocess.run", _fake_run)

    try:
        result = resolve_channel_attachments([
            ToolArtifact(
                artifact_id="turn:call:svg",
                kind="file",
                path=str(svg_path),
                mime_type="image/svg+xml",
                filename="diagram.svg",
                size_bytes=svg_path.stat().st_size,
                source_tool="delegate_to_codex",
            )
        ])
    finally:
        try:
            svg_path.unlink()
            browser_path.unlink()
            artifact_dir.rmdir()
            artifact_dir.parent.rmdir()
        except OSError:
            pass

    assert len(result.attachments) == 1
    assert result.attachments[0].artifact_id == "turn:call:svg:preview:png"
    assert result.attachments[0].mime_type == "image/png"
    assert result.rejected == ()
    try:
        Path(result.attachments[0].path).unlink()
    except OSError:
        pass


def test_codex_delegation_preserves_edit_commit_push_contract(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
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


def test_codex_summary_step_injects_fact_only_guardrails(monkeypatch) -> None:
    captured_messages = []

    def _fake_call_llm(state, store):
        del store
        captured_messages.extend(state["messages"])
        return {
            **state,
            "messages": [*state["messages"], AIMessage(content="summary")],
            "status": "running",
            "step_count": int(state.get("step_count", 0) or 0) + 1,
        }

    monkeypatch.setattr("app.agent_react.runtime.call_llm", _fake_call_llm)
    runtime = TurnRuntime(store=object())
    base_state = {
        "turn_id": 123,
        "conversation_id": 456,
        "trigger_message_id": 1,
        "messages": [],
        "artifacts": [],
        "session_state": None,
        "runtime_policy": None,
        "allowed_tools": [],
        "reply": "",
        "cancelled": False,
        "status": "running",
        "step_count": 1,
        "token_budget": None,
        "token_usage": None,
        "model": None,
        "loop_provider": "react",
        "error": None,
    }
    next_state = {
        **base_state,
        "messages": [
            SystemMessage(content="base system"),
            HumanMessage(content="看下nltk项目的状态"),
            AIMessage(
                content="",
                tool_calls=[{"id": "call_codex", "name": "delegate_to_codex", "args": {}}],
            ),
            ToolMessage(
                content="Remote status: ahead of origin by 1 commit\nWorking tree: clean.",
                tool_call_id="call_codex",
            ),
        ],
    }

    result = runtime._summarize_after_coder_tool(base_state, next_state)

    assert result["status"] == "completed"
    assert captured_messages
    system_content = captured_messages[0].content
    assert "Summarize only facts explicitly present in the delegate_to_codex tool output" in system_content
    assert "Do not offer to commit, push, edit, or run follow-up actions" in system_content
    assert "unless the original user request explicitly asked for that action" in system_content


def test_tavily_search_budget_rejects_eleventh_call_in_same_turn(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
    store = get_conversation_store()
    monkeypatch.setattr(
        "app.persistence.conversation_store.classify_turn",
        lambda **_kwargs: TurnClassification(
            turn_type="chat",
            requested_capabilities=("web.search",),
            confidence=0.9,
            source="llm",
        ),
    )
    chat = ScriptedChat([
        tool_response(
            *[
                tool_call(
                    "tavily_search",
                    {"query": f"Jarvis agent tests {index}"},
                    call_id=f"call_search_{index}",
                )
                for index in range(1, 12)
            ]
        ),
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
    assert executed_queries == [f"Jarvis agent tests {index}" for index in range(1, 11)]

    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert [record.provider_tool_call_id for record in tool_calls] == [
        f"call_search_{index}" for index in range(1, 12)
    ]
    assert [record.step_index for record in tool_calls] == [1] * 11
    assert [record.status for record in tool_calls] == ["completed"] * 10 + ["rejected"]
    assert "tavily_search budget exceeded" in (tool_calls[-1].error_message or "")


def test_tavily_intent_in_new_turn_gets_fresh_budget(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
    store = get_conversation_store()
    chat = ScriptedChat([
        tool_response(tool_call("tavily_search", {"query": "US Iran gold conflict"}, call_id="call_search")),
        final_response("used fresh search budget"),
    ])
    chat.install(monkeypatch)
    executed_queries: list[str] = []

    def _fake_execute_tool(tool, tool_args, *, timeout_seconds=30):
        executed_queries.append(tool_args["query"])
        return ToolExecutionResult(ok=True, exit_code=0, stdout="fresh result", summary="ok")

    monkeypatch.setattr(react_graph, "execute_tool", _fake_execute_tool)
    created = create_dm_turn(client, "你觉得美伊战争和这个有关系吗")
    store.update_conversation_metadata(created["conversation_id"], {"active_tool_intents": ["tavily_search"]})

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert executed_queries == ["US Iran gold conflict"]
    tool_calls = store.list_tool_calls_by_turn(created["turn_id"])
    assert tool_calls[0].status == "completed"


def test_tool_search_grant_expands_allowed_tools_with_audit_log(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
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
    client = create_agent_test_client(monkeypatch)
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


def test_load_skill_guidance_injects_turn_scoped_reminder(monkeypatch) -> None:
    skill_root = Path("sandbox") / unique_id("skill-guidance")
    skill_dir = skill_root / "release-checklist"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: release-checklist\n"
        "description: Release workflow guidance.\n"
        "when_to_use: User asks for release workflow.\n"
        "capabilities:\n"
        "  - release\n"
        "---\n\n"
        "Use this release checklist before delegating work.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_SKILL_PATH", str(skill_root))
    client = create_agent_test_client(monkeypatch)
    seen_tool_sets: list[list[str]] = []

    def _first(messages, tools):
        del messages
        tool_names = [
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function"
        ]
        seen_tool_sets.append(tool_names)
        assert "load_skill_guidance" in tool_names
        return tool_response(
            tool_call(
                "load_skill_guidance",
                {"query": "release workflow", "intent": "repository workflow"},
                call_id="call_skill_guidance",
            )
        )

    def _second(messages, tools):
        del tools
        reminder_messages = [
            message.content
            for message in messages
            if message.role == "user" and "<system-reminder>" in str(message.content)
        ]
        assert any("[Skill: release-checklist]" in content for content in reminder_messages)
        assert any("Use this release checklist before delegating work." in content for content in reminder_messages)
        return final_response("skill guidance loaded")

    chat = ScriptedChat([_first, _second])
    chat.install(monkeypatch)
    created = create_dm_turn(client, "做一下这个任务")

    run = client.post(f"/turns/{created['turn_id']}/run")

    assert run.status_code == 200
    assert run.json()["reply"] == "skill guidance loaded"


def test_conversation_tool_intents_append_across_turns(monkeypatch) -> None:
    client = create_agent_test_client(monkeypatch)
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
