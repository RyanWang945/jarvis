from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.api.agent import InMemoryConversationStore
from app.config import get_settings
from app.gateway.events import InboundEvent
from app.gateway.service import GatewayService
from app.progress import ProgressEvent
from app.task_runtime import TaskAgentRuntime
from app.task_runtime.fast_intent import FastIntentDecision
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import NodeResult
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.planning_router import PlanningRouterResult
from app.task_runtime.result_aggregator import ResultAggregator


class StaticPlanningRouter:
    def __init__(self, plan: ExecutionPlan) -> None:
        self.plan_result = plan
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
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
            data={
                "tool_artifacts": [
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
                ]
            },
        )


class RecordingProgress:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event_type: str, **payload):
        self.events.append(ProgressEvent(event_type=event_type, **payload))

    def close(self):
        pass


def test_feishu_gateway_can_run_task_runtime_e2e_without_network() -> None:
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
    assert llm_runtime.calls[0].user_objective == "hello from feishu"

    messages = store.list_messages(gateway_result.conversation_id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].raw_payload["source"] == "task_runtime"
    assert messages[-1].raw_payload["plan"]["nodes"][0]["id"] == "answer"
    assert messages[-1].raw_payload["execution_report"]["status"] == "completed"
    assert messages[-1].raw_payload["aggregation"]["status"] == "completed"


def test_task_runtime_emits_progress_events() -> None:
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


def test_task_runtime_fast_reply_bypasses_node_executor_and_aggregator() -> None:
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
    run_result = runtime.run_turn(gateway_result.turn_id)

    assert run_result.status == "completed"
    assert run_result.reply == "数学有时难，但能练会。"
    assert llm_runtime.calls == []
    assert chat.calls == []

    messages = store.list_messages(gateway_result.conversation_id)
    raw_payload = messages[-1].raw_payload
    assert raw_payload["route"] == "fast_reply"
    assert raw_payload["execution_report"]["data"]["fast_path"] is True
    assert raw_payload["execution_report"]["node_results"][0]["node_id"] == "fast_reply"
    assert raw_payload["aggregation"]["data"]["finalization"] == "fast_reply"


def test_task_runtime_persists_and_returns_node_tool_artifacts() -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    image_path = Path("data") / "artifact_previews" / f"task-runtime-{uuid4().hex}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    artifact_id = f"task_runtime_image:{uuid4().hex}"
    plan = ExecutionPlan(
        user_objective="生成图片",
        nodes=[PlanNode(id="generate_image", runtime="codex", objective="Generate image")],
    )
    runtime = TaskAgentRuntime(
        store,
        planning_router=StaticPlanningRouter(plan),
        node_executor=NodeExecutor(runtimes={"codex": ArtifactRuntime(image_path, artifact_id)}),
        result_aggregator=ResultAggregator(model_resolver=lambda metadata: _missing_key_model()),
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
    finally:
        image_path.unlink(missing_ok=True)


def test_get_agent_runtime_can_switch_to_task_runtime(monkeypatch) -> None:
    import app.api.agent as agent_api

    store = InMemoryConversationStore()
    monkeypatch.setenv("JARVIS_AGENT_RUNTIME_PROVIDER", "task")
    get_settings.cache_clear()
    monkeypatch.setattr(agent_api, "get_conversation_store", lambda: store)

    try:
        runtime = agent_api.get_agent_runtime()
    finally:
        get_settings.cache_clear()

    assert isinstance(runtime, TaskAgentRuntime)


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
