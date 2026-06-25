from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.api.agent import InMemoryConversationStore
from app.gateway.events import InboundEvent
from app.gateway.service import GatewayService
from app.progress import ProgressEvent
from app.task_runtime import TaskAgentRuntime
from app.task_runtime.fast_intent import FastIntentDecision
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import NodeArtifact, NodeResult
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.planning_router import PlanningRouterResult
from app.task_runtime.result_aggregator import ResultAggregator
from app.task_runtime.session_workspace import SessionWorkspaceManager


class StaticPlanningRouter:
    def __init__(self, plan: ExecutionPlan) -> None:
        self.plan_result = plan
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        progress = kwargs.get("progress")
        if progress is not None:
            progress.emit("planning_started", summary="正在生成执行计划")
        return PlanningRouterResult(
            route="planned",
            plan=self.plan_result,
            fast_intent=FastIntentDecision(route="needs_plan", confidence=0.99, reason="test"),
            elapsed_ms=1,
        )


class StaticFastReplyRouter:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        plan = ExecutionPlan(
            user_objective=kwargs["content"],
            nodes=[PlanNode(id="fast_reply", runtime="llm", objective=kwargs["content"])],
        )
        return PlanningRouterResult(
            route="fast_reply",
            plan=plan,
            fast_intent=FastIntentDecision(
                route="fast_reply",
                confidence=0.99,
                reply=self.reply,
                reason="simple chat",
            ),
            elapsed_ms=1,
        )


class EchoRuntime:
    def __init__(self) -> None:
        self.calls = []

    def run(self, context):
        self.calls.append(context)
        return NodeResult(
            node_id=context.node.id,
            runtime=context.node.runtime,
            status="completed",
            summary=f"node result: {context.user_objective}",
        )


class UsageRuntime:
    def run(self, context):
        return NodeResult(
            node_id=context.node.id,
            runtime=context.node.runtime,
            status="completed",
            summary="node result with usage",
            usage_records=[
                {
                    "source": "llm",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "stage": "llm_node",
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                },
                {
                    "source": "codex_app_server",
                    "provider": "codex",
                    "model": "codex",
                    "stage": "coder",
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "total_tokens": 250,
                },
            ],
        )


class ArtifactRuntime:
    def __init__(self, image_path: Path, artifact_id: str) -> None:
        self.image_path = image_path
        self.artifact_id = artifact_id

    def run(self, context):
        return NodeResult(
            node_id=context.node.id,
            runtime=context.node.runtime,
            status="completed",
            summary="generated image",
            tool_artifacts=[
                {
                    "artifact_id": self.artifact_id,
                    "kind": "image",
                    "path": str(self.image_path),
                    "mime_type": "image/png",
                    "filename": self.image_path.name,
                    "size_bytes": self.image_path.stat().st_size,
                    "source_tool": "delegate_to_codex",
                    "metadata": {"codex_item_id": "ig_test"},
                }
            ],
        )


class NestedToolArtifactRuntime(ArtifactRuntime):
    def run(self, context):
        return NodeResult(
            node_id=context.node.id,
            runtime=context.node.runtime,
            status="completed",
            summary="generated image",
            tool_calls=[
                {
                    "id": "call_1",
                    "tool_name": "write_image",
                    "status": "completed",
                    "tool_artifacts": [
                        {
                            "artifact_id": self.artifact_id,
                            "kind": "image",
                            "path": str(self.image_path),
                            "mime_type": "image/png",
                            "filename": self.image_path.name,
                            "size_bytes": self.image_path.stat().st_size,
                            "source_tool": "write_image",
                        }
                    ],
                }
            ],
        )


class NodeManifestArtifactRuntime:
    def __init__(self, *, absolute: bool = False) -> None:
        self.absolute = absolute

    def run(self, context):
        session_root = Path(context.legacy_hints["session_workspace_dir"])
        node_dir = Path(context.legacy_hints["node_workspace_dir"])
        report_path = node_dir / "report.md"
        report_path.write_text("# Report\n\nbody", encoding="utf-8")
        relative_path = report_path.relative_to(session_root).as_posix()
        return NodeResult(
            node_id=context.node.id,
            runtime=context.node.runtime,
            status="completed",
            summary="report generated",
            artifacts=[
                NodeArtifact(
                    ref="report",
                    kind="file",
                    path=str(report_path) if self.absolute else relative_path,
                    filename="report.md",
                    mime_type="text/markdown",
                    description="Generated report",
                )
            ],
        )


class RecordingProgress:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event_type: str, **payload):
        self.events.append(ProgressEvent(event_type=event_type, **payload))

    def close(self):
        pass


def test_feishu_gateway_can_run_task_runtime_e2e_without_network(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="hello from feishu",
        nodes=[PlanNode(id="answer", runtime="llm", objective="Answer simply")],
    )
    router = StaticPlanningRouter(plan)
    llm_runtime = EchoRuntime()
    runtime = TaskAgentRuntime(
        store,
        planning_router=router,
        node_executor=NodeExecutor(runtimes={"llm": llm_runtime}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime",
            external_message_id="msg-task-runtime-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="hello from feishu",
        )
    )
    run_result = runtime.run_turn(gateway_result.turn_id)

    assert gateway_result.should_run_agent is True
    assert run_result.status == "completed"
    assert run_result.reply == "node result: hello from feishu"
    assert router.calls[0]["content"] == "hello from feishu"
    assert router.calls[0]["runtime_context"].temporal.current_date
    assert router.calls[0]["runtime_context"].temporal.current_time
    assert router.calls[0]["runtime_context"].temporal.timezone == "Asia/Shanghai"
    assert llm_runtime.calls[0].user_objective == "hello from feishu"

    messages = store.list_messages(gateway_result.conversation_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].raw_payload["source"] == "task_runtime"
    assert messages[-1].raw_payload["plan"]["nodes"][0]["id"] == "answer"
    assert messages[-1].raw_payload["execution_report"]["status"] == "completed"
    assert messages[-1].raw_payload["aggregation"]["status"] == "completed"


def test_task_runtime_returns_combined_usage_metadata(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="hello with usage",
        nodes=[PlanNode(id="answer", runtime="llm", objective="Answer simply")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"llm": UsageRuntime()}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )
    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-usage",
            external_message_id="msg-task-runtime-usage-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="hello with usage",
        )
    )

    run_result = runtime.run_turn(gateway_result.turn_id)

    assert "node result with usage" in run_result.reply
    assert "- 模型：" not in run_result.reply
    assert "- Token：" not in run_result.reply
    assert run_result.message.metadata["usage"]["prompt_tokens"] == 300
    assert len(run_result.message.metadata["usage_records"]) == 2
    messages = store.list_messages(gateway_result.conversation_id)
    assert messages[-1].raw_payload["usage"]["prompt_tokens"] == 300
    assert len(messages[-1].raw_payload["usage_records"]) == 2


def test_task_runtime_emits_progress_events(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="hello from feishu",
        nodes=[PlanNode(id="answer", runtime="llm", objective="Answer simply")],
    )
    progress = RecordingProgress()
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"llm": EchoRuntime()}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )
    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-progress",
            external_message_id="msg-task-runtime-progress-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="hello from feishu",
        )
    )

    runtime.run_turn(gateway_result.turn_id, progress=progress)  # type: ignore[arg-type]

    event_types = [event.event_type for event in progress.events]
    assert event_types == [
        "turn_started",
        "planning_started",
        "plan_created",
        "node_started",
        "node_completed",
        "aggregation_started",
        "aggregation_completed",
        "turn_completed",
    ]
    assert progress.events[2].data["node_count"] == 1
    assert progress.events[3].node_id == "answer"


def test_task_runtime_fast_reply_bypasses_node_executor_and_aggregator(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    router = StaticFastReplyRouter("数学有时难，但能练会。")
    llm_runtime = EchoRuntime()
    chat = _CountingSummaryChat()
    runtime = TaskAgentRuntime(
        store,
        planning_router=router,
        node_executor=NodeExecutor(runtimes={"llm": llm_runtime}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _resolved_model(chat)),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-fast-reply",
            external_message_id="msg-task-runtime-fast-reply-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="你觉得数学难吗",
        )
    )
    progress = RecordingProgress()
    run_result = runtime.run_turn(gateway_result.turn_id, progress=progress)  # type: ignore[arg-type]

    assert run_result.status == "completed"
    assert run_result.reply == "数学有时难，但能练会。"
    assert llm_runtime.calls == []
    assert chat.calls == []
    assert [event.event_type for event in progress.events] == ["turn_started", "turn_completed"]

    messages = store.list_messages(gateway_result.conversation_id)
    raw_payload = messages[-1].raw_payload
    assert raw_payload["route"] == "fast_reply"
    assert raw_payload["execution_report"]["data"]["fast_path"] is True
    assert raw_payload["execution_report"]["node_results"][0]["node_id"] == "fast_reply"
    assert raw_payload["aggregation"]["data"]["finalization"] == "fast_reply"


def test_task_runtime_injects_history_context_only_into_planning(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="contextual task",
        nodes=[PlanNode(id="answer", runtime="llm", objective="Answer with resolved context")],
    )
    router = StaticPlanningRouter(plan)
    llm_runtime = EchoRuntime()
    runtime = TaskAgentRuntime(
        store,
        planning_router=router,
        node_executor=NodeExecutor(runtimes={"llm": llm_runtime}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    first = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-history",
            external_message_id="msg-task-runtime-history-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="我们决定让 ContextManager 维护历史和压缩摘要。",
        )
    )
    runtime.run_turn(first.turn_id)

    second = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-history",
            external_message_id="msg-task-runtime-history-2",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="继续刚才那个方案。",
        )
    )
    runtime.run_turn(second.turn_id)

    context = router.calls[-1]["conversation_context"]
    planner_payload = context.planner_payload()
    fast_payload = context.fast_payload()
    assert planner_payload["context_reference_detected"] is True
    assert fast_payload["context_reference_detected"] is True
    # History context is now embedded as messages (some compressed as role:system)
    assert any("ContextManager" in item["content"] for item in planner_payload["messages"])
    # No separate summary_node field — compression is baked into messages
    assert "summary_node" not in planner_payload
    assert not hasattr(llm_runtime.calls[-1], "conversation_context")


def test_task_runtime_persists_and_returns_node_tool_artifacts(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    image_path = Path("data") / "artifact_previews" / f"task-runtime-{uuid4().hex}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    artifact_id = f"task_runtime_image:{uuid4().hex}"
    plan = ExecutionPlan(
        user_objective="生成图片",
        nodes=[PlanNode(id="generate_image", runtime="coder", objective="Generate image")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"coder": ArtifactRuntime(image_path, artifact_id)}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    try:
        gateway_result = gateway.handle_inbound_event(
            InboundEvent(
                platform="feishu",
                external_chat_id="chat-task-runtime-artifact",
                external_message_id="msg-task-runtime-artifact-1",
                chat_type="dm",
                sender_id="ou_1",
                sender_name="Ryan",
                text="生成图片",
            )
        )
        run_result = runtime.run_turn(gateway_result.turn_id)

        assert run_result.status == "completed"
        assert len(run_result.message.attachments) == 1
        assert run_result.message.attachments[0].artifact_id == artifact_id
        assert store.get_artifact(artifact_id) is not None
        raw_payload = store.list_messages(gateway_result.conversation_id)[-1].raw_payload
        assert raw_payload["artifacts"][0]["artifact_id"] == artifact_id
        assert raw_payload["attachments"][0]["artifact_id"] == artifact_id
        promoted_path = Path(raw_payload["artifacts"][0]["path"])
        assert promoted_path.parent.name == "artifacts"
        assert promoted_path.parent.parent.parent == tmp_path / "sessions"
        assert raw_payload["attachments"][0]["path"] == str(promoted_path.resolve())
    finally:
        image_path.unlink(missing_ok=True)


def test_task_runtime_persists_nested_tool_call_artifacts(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    image_path = Path("data") / "artifact_previews" / f"task-runtime-nested-{uuid4().hex}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    artifact_id = f"task_runtime_nested_image:{uuid4().hex}"
    plan = ExecutionPlan(
        user_objective="生成图片",
        nodes=[PlanNode(id="generate_image", runtime="react", objective="Generate image")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"react": NestedToolArtifactRuntime(image_path, artifact_id)}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    try:
        gateway_result = gateway.handle_inbound_event(
            InboundEvent(
                platform="feishu",
                external_chat_id="chat-task-runtime-nested-artifact",
                external_message_id="msg-task-runtime-nested-artifact-1",
                chat_type="dm",
                sender_id="ou_1",
                sender_name="Ryan",
                text="生成图片",
            )
        )
        run_result = runtime.run_turn(gateway_result.turn_id)

        assert len(run_result.message.attachments) == 1
        assert run_result.message.attachments[0].artifact_id == artifact_id
        assert store.get_artifact(artifact_id) is not None
    finally:
        image_path.unlink(missing_ok=True)


def test_task_runtime_publishes_node_manifest_artifact_from_session_relative_path(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="生成报告",
        nodes=[PlanNode(id="write_report", runtime="react", objective="Write report")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"react": NodeManifestArtifactRuntime()}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-node-artifact",
            external_message_id="msg-task-runtime-node-artifact-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="生成报告",
        )
    )
    run_result = runtime.run_turn(gateway_result.turn_id)

    assert run_result.status == "completed"
    raw_payload = store.list_messages(gateway_result.conversation_id)[-1].raw_payload
    artifact = raw_payload["artifacts"][0]
    assert artifact["node_id"] == "write_report"
    assert artifact["session_relative_path"].startswith("artifacts/")
    assert artifact["metadata"]["source_session_relative_path"].startswith("nodes/write_report/")
    assert Path(artifact["path"]).parent.name == "artifacts"
    assert store.get_artifact(artifact["artifact_id"]) is not None


def test_task_runtime_rejects_absolute_path_in_node_manifest_artifact(tmp_path: Path) -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    plan = ExecutionPlan(
        user_objective="生成报告",
        nodes=[PlanNode(id="write_report", runtime="react", objective="Write report")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"react": NodeManifestArtifactRuntime(absolute=True)}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
        session_workspace_manager=SessionWorkspaceManager(workdir=tmp_path),
    )

    gateway_result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-node-artifact-absolute",
            external_message_id="msg-task-runtime-node-artifact-absolute-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="生成报告",
        )
    )
    run_result = runtime.run_turn(gateway_result.turn_id)

    assert run_result.status == "completed"
    raw_payload = store.list_messages(gateway_result.conversation_id)[-1].raw_payload
    assert "artifacts" not in raw_payload


def test_get_agent_runtime_returns_task_runtime_by_default(monkeypatch) -> None:
    import app.api.agent as agent_api

    store = InMemoryConversationStore()
    monkeypatch.setenv("JARVIS_AGENT_RUNTIME_PROVIDER", "react")
    monkeypatch.setattr(agent_api, "get_conversation_store", lambda: store)

    runtime = agent_api.get_agent_runtime()

    assert isinstance(runtime, TaskAgentRuntime)


def test_task_runtime_ingest_has_no_legacy_classifier(monkeypatch) -> None:
    import app.api.agent as agent_api

    assert not hasattr(agent_api, "classify" + "_turn")
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)

    result = gateway.handle_inbound_event(
        InboundEvent(
            platform="feishu",
            external_chat_id="chat-task-runtime-no-classifier",
            external_message_id="msg-task-runtime-no-classifier-1",
            chat_type="dm",
            sender_id="ou_1",
            sender_name="Ryan",
            text="你好",
        )
    )

    assert result.should_run_agent is True
    turn = store.get_turn(result.turn_id)
    assert turn is not None
    assert turn.metadata["classification"]["source"] == "task_runtime_ingest"


def _missing_key_model():
    class MissingKeyProfile:
        api_key = ""
        id = "missing"
        supports_json_object = True

    class MissingKeyModel:
        profile = MissingKeyProfile()

    return MissingKeyModel()


class _CountingSummaryChat:
    def __init__(self) -> None:
        self.calls = []

    def chat_normalized(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        raise AssertionError("aggregator should not be called for fast_reply")


def _resolved_model(chat):
    class Profile:
        api_key = "test-key"
        id = "test"
        supports_json_object = True

    class Model:
        profile = Profile()
        client = chat

    return Model()
