from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.agent_react.session_state import ConversationSessionState
from app.prompting import PromptRegistry
from app.task_runtime.fast_intent import FastIntentDecision, FastIntentNode
from app.task_runtime.planner import ExecutionPlan, FinalizationHint, PlanNode, TurnPlanner

logger = logging.getLogger(__name__)

RouterRoute = Literal["fast_reply", "planned", "fallback"]


@dataclass(frozen=True)
class PlanningRouterResult:
    route: RouterRoute
    plan: ExecutionPlan
    fast_intent: FastIntentDecision
    elapsed_ms: int
    planner_elapsed_ms: int | None = None


class PlanningRouter:
    """Routes a turn through FastIntent or the full Planner."""

    def __init__(
        self,
        *,
        fast_intent: FastIntentNode | None = None,
        planner: TurnPlanner | None = None,
        prompt_registry: PromptRegistry | None = None,
        fast_intent_prompt_version: str | None = None,
        planner_prompt_version: str | None = None,
        fast_reply_confidence_threshold: float = 0.9,
    ) -> None:
        registry = prompt_registry or PromptRegistry()
        self._fast_intent = fast_intent or FastIntentNode(
            prompt_registry=registry,
            prompt_version=fast_intent_prompt_version,
        )
        self._planner = planner or TurnPlanner(
            prompt_registry=registry,
            prompt_version=planner_prompt_version,
        )
        self._fast_reply_confidence_threshold = fast_reply_confidence_threshold

    def prompt_metadata(self) -> dict[str, dict[str, Any]]:
        return {
            "fast_intent": self._fast_intent.prompt_metadata(),
            "planner": self._planner.prompt_metadata(),
        }

    def plan(
        self,
        *,
        content: str,
        session_state: ConversationSessionState | None = None,
        conversation_metadata: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
        previous_node_results: list[dict[str, Any]] | None = None,
        runtime_hints: dict[str, Any] | None = None,
        instructions: list[str] | None = None,
    ) -> PlanningRouterResult:
        started = time.perf_counter()
        try:
            fast_decision = self._fast_intent.decide(
                content=content,
                session_state=session_state,
                conversation_metadata=conversation_metadata,
                recent_artifacts=recent_artifacts,
            )
        except Exception:
            logger.exception("fast intent failed; falling back to planner")
            fast_decision = FastIntentDecision(route="needs_plan", confidence=0.0, reason="fast intent failed")
        try:
            logger.info(
                "planning router fast intent completed route=%s runtime=%s confidence=%.2f tool_name=%s finalization=%s reply_len=%s reason=%s",
                fast_decision.route,
                fast_decision.runtime,
                fast_decision.confidence,
                fast_decision.tool_name,
                fast_decision.finalization_hint.mode,
                len(fast_decision.reply),
                fast_decision.reason,
            )

            if _can_use_fast_reply(
                fast_decision,
                self._fast_reply_confidence_threshold,
                has_previous_node_results=bool(previous_node_results),
            ):
                logger.info(
                    "planning router fast reply selected confidence=%.2f reply_len=%s elapsed_ms=%s",
                    fast_decision.confidence,
                    len(fast_decision.reply),
                    int((time.perf_counter() - started) * 1000),
                )
                return PlanningRouterResult(
                    route="fast_reply",
                    plan=fast_reply_plan(content, fast_decision),
                    fast_intent=fast_decision,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    planner_elapsed_ms=None,
                )

            plan, planner_elapsed_ms = _timed_planner_call(
                self._planner,
                content=content,
                session_state=session_state,
                conversation_metadata=conversation_metadata,
                recent_artifacts=recent_artifacts,
                previous_node_results=previous_node_results,
                runtime_hints=runtime_hints,
                instructions=instructions,
            )
            logger.info(
                "planning router planned route selected node_count=%s runtimes=%s planner_elapsed_ms=%s elapsed_ms=%s finalization=%s",
                len(plan.nodes),
                [node.runtime for node in plan.nodes],
                planner_elapsed_ms,
                int((time.perf_counter() - started) * 1000),
                plan.finalization_hint.mode,
            )
            return PlanningRouterResult(
                route="planned",
                plan=plan,
                fast_intent=fast_decision,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                planner_elapsed_ms=planner_elapsed_ms,
            )
        except Exception:
            logger.exception("planner failed; falling back to llm single-node plan")
            return PlanningRouterResult(
                route="fallback",
                plan=fallback_llm_plan(content),
                fast_intent=fast_decision,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                planner_elapsed_ms=None,
            )


def fallback_llm_plan(content: str) -> ExecutionPlan:
    return ExecutionPlan(
        user_objective=content,
        finalization_hint=FinalizationHint(
            mode="pass_through",
            user_facing=True,
            reason="planner failed; fallback LLM node should answer the user directly",
        ),
        nodes=[
            PlanNode(
                id="main",
                objective=content,
                runtime="llm",
                input_refs=[],
                expected_output="User-facing answer.",
            )
        ],
    )


def fast_reply_plan(content: str, decision: FastIntentDecision) -> ExecutionPlan:
    return ExecutionPlan(
        user_objective=content,
        finalization_hint=decision.finalization_hint,
        nodes=[
            PlanNode(
                id="fast_reply",
                objective=content,
                runtime="llm",
                input_refs=[],
                expected_output="User-facing fast reply.",
            )
        ],
    )


def _can_use_fast_reply(
    decision: FastIntentDecision,
    threshold: float,
    *,
    has_previous_node_results: bool = False,
) -> bool:
    if has_previous_node_results:
        return False
    if decision.route != "fast_reply":
        return False
    if decision.confidence < threshold:
        return False
    return bool(decision.reply.strip())


def _timed_planner_call(
    planner: TurnPlanner,
    *,
    content: str,
    session_state: ConversationSessionState | None,
    conversation_metadata: dict[str, Any] | None,
    recent_artifacts: list[dict[str, Any]] | None,
    previous_node_results: list[dict[str, Any]] | None,
    runtime_hints: dict[str, Any] | None,
    instructions: list[str] | None,
) -> tuple[ExecutionPlan, int]:
    started = time.perf_counter()
    plan = planner.plan(
        content=content,
        session_state=session_state,
        conversation_metadata=conversation_metadata,
        recent_artifacts=recent_artifacts,
        previous_node_results=previous_node_results,
        runtime_hints=runtime_hints,
        instructions=instructions,
    )
    return plan, int((time.perf_counter() - started) * 1000)
