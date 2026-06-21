from __future__ import annotations

from app.task_runtime.node_execute_runtime import NodeExecutionContext
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import NodeResult, ResolvedInput
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.runtime_context import RuntimeContext
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.skills.loader import SkillPackageLoader
from app.skills.registry import SkillRegistry
from app.tools.common import ToolExecutionResult


class RecordingRuntime:
    def __init__(self, runtime: str, summary: str = "ok", *, status: str = "completed") -> None:
        self.runtime = runtime
        self.summary = summary
        self.status = status
        self.calls: list[NodeExecutionContext] = []

    def run(self, context: NodeExecutionContext) -> NodeResult:
        self.calls.append(context)
        return NodeResult(
            node_id=context.node.id,
            runtime=self.runtime,
            status=self.status,
            summary=f"{self.summary}:{context.node.id}",
            data={"input_refs": [item.ref for item in context.resolved_inputs]},
        )


class FakeProfile:
    api_key = "test-key"
    supports_json_object = True


class FakeResolvedModel:
    def __init__(self, client) -> None:
        self.client = client
        self.profile = FakeProfile()


class ScriptedNodeChat:
    def __init__(self, responses: list[NormalizedLLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat_normalized(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        return self.responses.pop(0)


def llm_response(content: str = "", *, tool_calls: tuple[NormalizedToolCall, ...] = ()) -> NormalizedLLMResponse:
    return NormalizedLLMResponse(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        usage=None,
        model="fake",
        finish_reason=None,
        raw={},
    )


def test_node_executor_runs_ready_nodes_and_passes_node_result_inputs() -> None:
    react = RecordingRuntime("react", "research")
    coder = RecordingRuntime("coder", "review")
    executor = NodeExecutor(runtimes={"react": react, "coder": coder})
    plan = ExecutionPlan(
        user_objective="research then review",
        nodes=[
            PlanNode(id="research", runtime="react", objective="Research agent runtime"),
            PlanNode(id="review", runtime="coder", objective="Review repo", input_refs=["node:research"]),
        ],
    )

    report = executor.execute(plan, runtime_context=RuntimeContext.from_hints({"active_repo": "jarvis"}))

    assert report.status == "completed"
    assert [result.node_id for result in report.node_results] == ["research", "review"]
    assert coder.calls[0].resolved_inputs[0].ref == "node:research"
    assert coder.calls[0].resolved_inputs[0].summary == "research:research"


def test_node_executor_resolves_artifact_inputs() -> None:
    react = RecordingRuntime("react", "deliver")
    executor = NodeExecutor(runtimes={"react": react})
    plan = ExecutionPlan(
        user_objective="deliver report",
        nodes=[
            PlanNode(
                id="deliver",
                runtime="react",
                objective="Deliver report",
                input_refs=["artifact:A1"],
            )
        ],
    )

    report = executor.execute(
        plan,
        artifacts=[
            {
                "ref": "A1",
                "kind": "report",
                "name": "rag_eval_report.md",
                "description": "RAG eval report",
            }
        ],
    )

    assert report.status == "completed"
    assert react.calls[0].resolved_inputs[0].kind == "artifact"
    assert react.calls[0].resolved_inputs[0].artifacts[0].name == "rag_eval_report.md"


def test_node_executor_uses_previous_node_results() -> None:
    coder = RecordingRuntime("coder", "review")
    executor = NodeExecutor(runtimes={"coder": coder})
    plan = ExecutionPlan(
        user_objective="evaluate jarvis",
        nodes=[
            PlanNode(
                id="evaluate",
                runtime="coder",
                objective="Evaluate jarvis",
                input_refs=["node:research_agent_sdk"],
            )
        ],
    )

    report = executor.execute(
        plan,
        previous_node_results=[
            {
                "node_id": "research_agent_sdk",
                "runtime": "react",
                "status": "completed",
                "summary": "SDK tracing and handoff changed.",
            }
        ],
    )

    assert report.status == "completed"
    assert coder.calls[0].resolved_inputs[0].summary == "SDK tracing and handoff changed."


def test_node_executor_blocks_nodes_with_missing_inputs() -> None:
    executor = NodeExecutor(runtimes={"coder": RecordingRuntime("coder")})
    plan = ExecutionPlan(
        user_objective="bad inputs",
        nodes=[PlanNode(id="review", runtime="coder", objective="Review", input_refs=["node:missing"])],
    )

    report = executor.execute(plan)

    assert report.status == "blocked"
    assert report.node_results[0].status == "blocked"
    assert report.node_results[0].error is not None
    assert report.node_results[0].error.code == "unresolved_input_refs"


def test_node_executor_blocks_downstream_when_dependency_failed() -> None:
    react = RecordingRuntime("react", "research", status="failed")
    coder = RecordingRuntime("coder", "review")
    executor = NodeExecutor(runtimes={"react": react, "coder": coder})
    plan = ExecutionPlan(
        user_objective="research then review",
        nodes=[
            PlanNode(id="research", runtime="react", objective="Research"),
            PlanNode(id="review", runtime="coder", objective="Review", input_refs=["node:research"]),
        ],
    )

    report = executor.execute(plan)

    assert report.status == "failed"
    assert [result.status for result in report.node_results] == ["failed", "blocked"]
    assert coder.calls == []


def test_react_node_execute_runtime_runs_tool_loop() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_1",
                        name="business_knowledge_search",
                        args={"query": "agent testing"},
                    ),
                )
            ),
            llm_response(
                '{"summary":"found evidence","findings":["trace matters"],"sources":["business_kb"],"data":{"confidence":"high"}}'
            ),
        ]
    )
    executed = []

    def _tool_runner(tool, tool_args, *, timeout_seconds=60):
        executed.append((tool.name, tool_args, timeout_seconds))
        return ToolExecutionResult(ok=True, exit_code=0, stdout="trace evidence", summary="searched kb")

    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=_tool_runner,
        max_steps=4,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="research agent tests",
            node=PlanNode(id="research", runtime="react", objective="Research agent testing"),
            legacy_hints={
                "current_date": "2026-05-25",
                "current_time": "2026-05-25T10:30:00+08:00",
                "timezone": "Asia/Shanghai",
            },
        )
    )

    assert result.status == "completed"
    assert result.summary == "found evidence"
    assert result.data["findings"] == ["trace matters"]
    assert result.tool_calls[0]["tool_name"] == "business_knowledge_search"
    assert executed == [("business_knowledge_search", {"query": "agent testing"}, 60)]
    tool_names = {tool["function"]["name"] for tool in chat.calls[0]["tools"]}
    assert "business_knowledge_search" in tool_names
    assert "deliver_file" in tool_names
    assert chat.calls[0]["messages"][0].role == "system"
    assert "最新" in chat.calls[0]["messages"][0].content
    assert "temporal_context" in chat.calls[0]["messages"][1].content
    assert "2026-05-25" in chat.calls[0]["messages"][1].content
    assert chat.calls[1]["messages"][-1].role == "tool"
    assert "trace evidence" in chat.calls[1]["messages"][-1].content


def test_react_node_execute_runtime_exposes_all_llm_tools() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat([llm_response('{"summary":"done"}')])
    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=lambda tool, tool_args, *, timeout_seconds=60: (_ for _ in ()).throw(AssertionError("should not run")),
        max_steps=2,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="把刚刚那个报告发我",
            node=PlanNode(id="deliver", runtime="react", objective="Deliver report"),
        )
    )

    tool_names = {tool["function"]["name"] for tool in chat.calls[0]["tools"]}
    assert result.status == "completed"
    assert {"read_file", "shell_run_command", "scheduled_task", "deliver_file", "tavily_search"} <= tool_names
    assert "delegate_to_codex" not in tool_names


def test_react_node_execute_runtime_rejects_coder_only_tool_calls_before_runner() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(
                    NormalizedToolCall(id="call_1", name="shell_inspect", args={"command": "git status"}),
                    NormalizedToolCall(
                        id="call_2",
                        name="delegate_to_codex",
                        args={"repo_id": "jarvis", "instruction": "Review the repo"},
                    ),
                )
            ),
            llm_response('{"summary":"used available evidence","findings":[],"sources":[]}'),
        ]
    )
    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=lambda tool, tool_args, *, timeout_seconds=60: (_ for _ in ()).throw(AssertionError("should not run")),
        max_steps=4,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="research",
            node=PlanNode(id="research", runtime="react", objective="Research"),
        )
    )

    assert result.status == "completed"
    assert result.tool_calls[0]["status"] == "rejected"
    assert result.tool_calls[1]["status"] == "rejected"
    assert "cannot execute coder-only actions" in result.tool_calls[0]["summary"]


def test_react_node_execute_runtime_allows_lightweight_file_tools() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(NormalizedToolCall(id="call_1", name="read_file", args={"path": "docs/example.md"}),)
            ),
            llm_response('{"summary":"document inspected","findings":[],"sources":[]}'),
        ]
    )
    executed = []

    def _tool_runner(tool, tool_args, *, timeout_seconds=60):
        executed.append((tool.name, tool_args))
        return ToolExecutionResult(ok=True, exit_code=0, stdout="# Example", summary="Read file: docs/example.md")

    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=_tool_runner,
        max_steps=4,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="快速看下这个文档",
            node=PlanNode(id="inspect_doc", runtime="react", objective="Inspect a non-code document"),
        )
    )

    assert result.status == "completed"
    assert result.tool_calls[0]["status"] == "completed"
    assert executed == [("read_file", {"path": "docs/example.md"})]


def test_react_node_execute_runtime_falls_back_to_tool_summary() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_1",
                        name="tavily_search",
                        args={"query": "agent swarm latest", "max_results": 3},
                    ),
                )
            ),
            llm_response("{}"),
        ]
    )
    executed = []

    def _tool_runner(tool, tool_args, *, timeout_seconds=60):
        executed.append(tool.name)
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout="{}",
            summary="Tavily search returned 3 results. Source URLs: https://example.com/swarm",
        )

    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=_tool_runner,
        max_steps=4,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="research",
            node=PlanNode(id="research", runtime="react", objective="Research"),
        )
    )

    assert result.status == "completed"
    assert "tavily_search" in result.summary
    assert "Source URLs" in result.summary
    assert executed == ["tavily_search"]


def test_react_node_execute_runtime_preserves_structured_payload_without_summary() -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                '{"candidates":[{"name":"Elden Ring","summary":"Radahn Festival lets the player summon many allied NPCs."}],"sources":["source1"]}'
            ),
        ]
    )
    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        tool_runner=lambda tool, tool_args, *, timeout_seconds=60: (_ for _ in ()).throw(AssertionError("should not run")),
        max_steps=2,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="这个游戏可能是啥",
            node=PlanNode(id="research", runtime="react", objective="Research candidate games"),
        )
    )

    assert "Elden Ring" in result.summary
    assert result.data["candidates"][0]["name"] == "Elden Ring"
    assert result.data["sources"] == ["source1"]


def test_react_node_execute_runtime_lets_model_load_skill(monkeypatch, tmp_path) -> None:
    from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

    registry = _install_test_skill_registry(monkeypatch, tmp_path, "react-skill")
    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_skill_1",
                        name="Skill",
                        args={"skill": "react-skill", "args": "topic"},
                    ),
                )
            ),
            llm_response('{"summary":"used react skill"}'),
        ]
    )
    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        max_steps=3,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="use a skill",
            node=PlanNode(id="react", runtime="react", objective="Use procedural guidance"),
        )
    )

    assert registry.get("react-skill").skill_id == "react-skill"
    assert result.status == "completed"
    assert result.summary == "used react skill"
    assert result.tool_calls[0]["tool_name"] == "Skill"
    assert result.tool_calls[0]["loaded_skill"]["name"] == "react-skill"
    assert {tool["function"]["name"] for tool in chat.calls[0]["tools"]} >= {"Skill", "tavily_search"}
    second_call_text = "\n\n".join(str(message.content) for message in chat.calls[1]["messages"])
    assert "[Skill: react-skill]" in second_call_text
    assert "Skill guidance marker for react-skill." in second_call_text
    assert "<system-reminder>\nLoaded skills for this turn." not in second_call_text
    tool_message_text = str([message for message in chat.calls[1]["messages"] if message.role == "tool"][0].content)
    assert "[injected as turn-scoped skill guidance]" in tool_message_text


def test_llm_node_execute_runtime_can_load_skill_for_own_call(monkeypatch, tmp_path) -> None:
    from app.task_runtime.node_execute_runtime import LLMNodeExecuteRuntime

    _install_test_skill_registry(monkeypatch, tmp_path, "llm-skill")
    chat = ScriptedNodeChat(
        [
            llm_response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_skill_1",
                        name="Skill",
                        args={"skill": "llm-skill"},
                    ),
                )
            ),
            llm_response('{"summary":"used llm skill"}'),
        ]
    )
    runtime = LLMNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        max_skill_steps=3,
    )

    result = runtime.run(
        NodeExecutionContext(
            user_objective="answer with a skill",
            node=PlanNode(id="answer", runtime="llm", objective="Answer with optional procedural guidance"),
        )
    )

    assert result.status == "completed"
    assert result.summary == "used llm skill"
    assert result.tool_calls[0]["tool_name"] == "Skill"
    assert {tool["function"]["name"] for tool in chat.calls[0]["tools"]} == {"Skill"}
    first_call_text = "\n\n".join(str(message.content) for message in chat.calls[0]["messages"])
    assert "- llm-skill:" in first_call_text
    assert "Skill guidance marker for llm-skill." not in first_call_text
    second_call_text = "\n\n".join(str(message.content) for message in chat.calls[1]["messages"])
    assert "[Skill: llm-skill]" in second_call_text
    assert "Skill guidance marker for llm-skill." in second_call_text
    assert "<system-reminder>\nLoaded skills for this turn." not in second_call_text


def test_llm_node_execute_runtime_preserves_explicit_reply() -> None:
    from app.task_runtime.node_execute_runtime import LLMNodeExecuteRuntime

    chat = ScriptedNodeChat(
        [
            llm_response(
                '{"summary":"短摘要","reply":"这是完整回复。","data":{"confidence":"high"}}'
            )
        ]
    )
    runtime = LLMNodeExecuteRuntime(model_resolver=lambda context: FakeResolvedModel(chat))

    result = runtime.run(
        NodeExecutionContext(
            user_objective="解释关系",
            node=PlanNode(id="answer", runtime="llm", objective="Answer directly"),
        )
    )

    assert result.status == "completed"
    assert result.summary == "短摘要"
    assert result.data["reply"] == "这是完整回复。"
    assert result.data["confidence"] == "high"


def _install_test_skill_registry(monkeypatch, tmp_path, skill_id: str) -> SkillRegistry:
    skill_dir = tmp_path / skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Guidance for {skill_id}.\n"
        "---\n\n"
        f"# {skill_id}\n\n"
        f"Skill guidance marker for {skill_id}.\n",
        encoding="utf-8",
    )
    package = SkillPackageLoader([]).load_package(skill_dir)
    registry = SkillRegistry([package.skill])
    monkeypatch.setattr("app.agent_react.context_manager.get_skill_registry", lambda: registry)
    monkeypatch.setattr("app.tools.skill_guidance.get_skill_registry", lambda: registry)
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_skill_registry", lambda: registry)
    return registry
