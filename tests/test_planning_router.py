from __future__ import annotations

import time

from app.agent_react.context_manager import ContextMessage, ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.task_runtime.fast_intent import FastIntentDecision
from app.task_runtime.planning_router import PlanningRouter
from app.task_runtime.planner import ExecutionPlan, PlanNode, TurnPlannerResult
from app.task_runtime.runtime_context import RuntimeContext


class StaticFastIntent:
    def __init__(self, decision: FastIntentDecision) -> None:
        self.decision = decision
        self.calls = 0

    def decide(self, **kwargs):
        del kwargs
        self.calls += 1
        return self.decision


class SlowPlanner:
    def __init__(self, plan: ExecutionPlan, *, delay_seconds: float = 0.0) -> None:
        self.plan_result = plan
        self.delay_seconds = delay_seconds
        self.calls = 0

    def plan(self, **kwargs):
        del kwargs
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return self.plan_result


class UsagePlanner(SlowPlanner):
    def plan_with_usage(self, **kwargs):
        return TurnPlannerResult(
            plan=self.plan(**kwargs),
            usage_records=[
                {
                    "source": "llm",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "stage": "planner",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            ],
        )


class RecordingProgress:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type: str, **payload):
        self.events.append((event_type, payload))


class FailingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, **kwargs):
        del kwargs
        self.calls += 1
        raise RuntimeError("planner boom")


def _planned_plan() -> ExecutionPlan:
    return ExecutionPlan(
        user_objective="planned complex task",
        nodes=[
            PlanNode(id="research", objective="research", runtime="react"),
            PlanNode(id="review", objective="review repo", runtime="coder", input_refs=["node:research"]),
        ],
    )


def test_planning_router_fast_reply_returns_without_planner() -> None:
    fast = StaticFastIntent(
        FastIntentDecision(
            route="fast_reply",
            confidence=0.95,
            reply="数学有时难，但能练会。",
            reason="simple chat",
        )
    )
    slow_planner = SlowPlanner(_planned_plan(), delay_seconds=1.0)
    router = PlanningRouter(fast_intent=fast, planner=slow_planner)

    result = router.plan(content="你觉得数学难吗")

    assert result.route == "fast_reply"
    assert result.fast_intent.reply == "数学有时难，但能练会。"
    assert result.plan.nodes[0].id == "fast_reply"
    assert result.planner_elapsed_ms is None
    assert slow_planner.calls == 0


def test_planning_router_deterministically_plans_existing_artifact_delivery() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="would call llm"))
    planner = SlowPlanner(_planned_plan(), delay_seconds=1.0)
    router = PlanningRouter(fast_intent=fast, planner=planner)

    result = router.plan(
        content="把刚刚那个报告发我",
        recent_artifacts=[{"ref": "A1", "kind": "report", "name": "rag_eval_report.md"}],
    )

    assert result.route == "planned"
    assert result.planner_elapsed_ms == 0
    assert result.plan.nodes[0].runtime == "react"
    assert result.plan.nodes[0].input_refs == ["artifact:A1"]
    assert fast.calls == 0
    assert planner.calls == 0


def test_planning_router_needs_plan_always_waits_for_planner() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="needs execution"))
    planned = _planned_plan()
    planner = SlowPlanner(planned, delay_seconds=0.01)
    router = PlanningRouter(fast_intent=fast, planner=planner)

    result = router.plan(content="看看特朗普访华后说了什么")

    assert result.route == "planned"
    assert result.plan is planned
    assert result.planner_elapsed_ms is not None
    assert fast.calls == 1
    assert planner.calls == 1


def test_planning_router_emits_planning_started_before_heavy_planner() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="needs execution"))
    progress = RecordingProgress()

    class AssertingPlanner(SlowPlanner):
        def plan(self, **kwargs):
            assert [event[0] for event in progress.events] == ["planning_started"]
            return super().plan(**kwargs)

    router = PlanningRouter(fast_intent=fast, planner=AssertingPlanner(_planned_plan()))

    result = router.plan(
        content="查资料",
        runtime_context=RuntimeContext.from_hints({"turn_id": 42, "conversation_id": 7}),
        progress=progress,  # type: ignore[arg-type]
    )

    assert result.route == "planned"
    assert progress.events[0][0] == "planning_started"
    assert progress.events[0][1]["turn_id"] == 42
    assert progress.events[0][1]["conversation_id"] == 7


def test_planning_router_needs_plan_waits_for_planner() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="multi goal"))
    planned = _planned_plan()
    router = PlanningRouter(fast_intent=fast, planner=SlowPlanner(planned))

    result = router.plan(
        content="先查资料再 review jarvis",
        session_state=ConversationSessionState(session_mode="coding"),
    )

    assert result.route == "planned"
    assert [node.runtime for node in result.plan.nodes] == ["react", "coder"]
    assert result.plan.nodes[1].input_refs == ["node:research"]
    assert fast.calls == 1


def test_planning_router_previous_node_results_force_planner() -> None:
    fast = StaticFastIntent(
        FastIntentDecision(
            route="fast_reply",
            confidence=0.95,
            reply="这个可以直接答，但有上游结果时不能短路。",
            reason="simple chat",
        )
    )
    planned = _planned_plan()
    planner = SlowPlanner(planned)
    router = PlanningRouter(fast_intent=fast, planner=planner)

    result = router.plan(
        content="根据刚才的调研结果评估 jarvis",
        previous_node_results=[{"node_id": "research", "status": "completed"}],
    )

    assert result.route == "planned"
    assert result.plan is planned
    assert planner.calls == 1


def test_planning_router_surfaces_planner_usage_outside_plan() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="needs execution"))
    router = PlanningRouter(fast_intent=fast, planner=UsagePlanner(_planned_plan()))

    result = router.plan(content="查资料")

    assert result.route == "planned"
    assert result.planner_usage_records[0]["stage"] == "planner"
    assert "usage_records" not in result.plan.model_dump(mode="json")


def test_planning_router_context_reference_forces_planner() -> None:
    fast = StaticFastIntent(
        FastIntentDecision(
            route="fast_reply",
            confidence=0.95,
            reply="可以继续。",
            reason="simple acknowledgement",
        )
    )
    planned = _planned_plan()
    planner = SlowPlanner(planned)
    router = PlanningRouter(fast_intent=fast, planner=planner)

    result = router.plan(
        content="继续刚才那个方案",
        conversation_context=ConversationContext(
            messages=(
                ContextMessage(
                    role="system",
                    content="[对话历史] User and assistant discussed the ContextManager history plan.",
                    is_compressed=True,
                    compression_level="batch",
                ),
            ),
            context_reference_detected=True,
        ),
    )

    assert result.route == "planned"
    assert result.plan is planned
    assert planner.calls == 1
    assert fast.calls == 0


def test_planning_router_falls_back_to_llm_plan_when_planner_fails() -> None:
    fast = StaticFastIntent(FastIntentDecision(route="needs_plan", confidence=0.95, reason="needs execution"))
    planner = FailingPlanner()
    router = PlanningRouter(fast_intent=fast, planner=planner)

    result = router.plan(content="解释一下 lightweight plan")

    assert result.route == "fallback"
    assert result.plan.nodes[0].runtime == "llm"
    assert result.plan.finalization_hint.mode == "pass_through"
    assert planner.calls == 1
