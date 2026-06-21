from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent_react.session_state import ConversationSessionState
from app.agent_react.context_manager import ConversationContext
from app.progress import ProgressReporter
from app.prompting import PromptRegistry
from app.task_runtime.fast_intent import FastIntentDecision, FastIntentNode
from app.task_runtime.planner import ExecutionPlan, FinalizationHint, PlanNode, TurnPlanner
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

RouterRoute = Literal["fast_reply", "planned", "fallback"]


@dataclass(frozen=True)
class PlanningRouterResult:
    route: RouterRoute
    plan: ExecutionPlan
    fast_intent: FastIntentDecision
    elapsed_ms: int
    planner_elapsed_ms: int | None = None
    planner_usage_records: list[dict[str, Any]] = field(default_factory=list)


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
        conversation_context: ConversationContext | None = None,
        previous_node_results: list[dict[str, Any]] | None = None,
        runtime_hints: dict[str, Any] | None = None,
        runtime_context: RuntimeContext | None = None,
        instructions: list[str] | None = None,
        progress: ProgressReporter | None = None,
    ) -> PlanningRouterResult:
        started = time.perf_counter()
        resolved_runtime_context = runtime_context or RuntimeContext.from_hints(runtime_hints)
        artifact_plan = _artifact_delivery_plan(content, recent_artifacts or [])
        if artifact_plan is not None:
            fast_decision = FastIntentDecision(
                route="needs_plan",
                confidence=1.0,
                reason="deterministic existing-artifact delivery plan",
            )
            logger.info(
                "planning router deterministic artifact delivery selected node_count=%s elapsed_ms=%s",
                len(artifact_plan.nodes),
                int((time.perf_counter() - started) * 1000),
            )
            return PlanningRouterResult(
                route="planned",
                plan=artifact_plan,
                fast_intent=fast_decision,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                planner_elapsed_ms=0,
            )
        if _should_skip_fast_intent(
            content,
            runtime_context=resolved_runtime_context,
            session_state=session_state,
            previous_node_results=previous_node_results,
            conversation_context=conversation_context,
        ):
            fast_decision = FastIntentDecision(
                route="needs_plan",
                confidence=1.0,
                reason="deterministic planner-required request",
            )
            logger.info("planning router skipped fast intent reason=%s", fast_decision.reason)
        else:
            try:
                fast_decision = self._fast_intent.decide(
                    content=content,
                    session_state=session_state,
                    conversation_metadata=conversation_metadata,
                    recent_artifacts=recent_artifacts,
                    conversation_context=conversation_context,
                    runtime_hints=runtime_hints,
                    runtime_context=resolved_runtime_context,
                )
            except Exception:
                logger.exception("fast intent failed; falling back to planner")
                fast_decision = FastIntentDecision(route="needs_plan", confidence=0.0, reason="fast intent failed")
        try:
            logger.info(
                "planning router fast intent completed route=%s confidence=%.2f reply_len=%s reason=%s",
                fast_decision.route,
                fast_decision.confidence,
                len(fast_decision.reply),
                fast_decision.reason,
            )

            if _can_use_fast_reply(
                fast_decision,
                self._fast_reply_confidence_threshold,
                has_previous_node_results=bool(previous_node_results),
                has_context_reference=bool(
                    conversation_context is not None and conversation_context.context_reference_detected
                ),
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

            if progress is not None:
                progress.emit(
                    "planning_started",
                    turn_id=resolved_runtime_context.turn.turn_id,
                    conversation_id=resolved_runtime_context.turn.conversation_id,
                    stage="planning",
                    status="running",
                    summary="正在生成执行计划",
                    data={
                        "fast_route": fast_decision.route,
                        "fast_confidence": fast_decision.confidence,
                        "reason": fast_decision.reason,
                    },
                )
            plan, planner_elapsed_ms, planner_usage_records = _timed_planner_call(
                self._planner,
                content=content,
                session_state=session_state,
                conversation_metadata=conversation_metadata,
                recent_artifacts=recent_artifacts,
                conversation_context=conversation_context,
                previous_node_results=previous_node_results,
                runtime_hints=runtime_hints,
                runtime_context=resolved_runtime_context,
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
                planner_usage_records=planner_usage_records,
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
        ),
        nodes=[
            PlanNode(
                id="main",
                objective=content,
                runtime="llm",
                input_refs=[],
                output_hint="User-facing answer.",
            )
        ],
    )


def fast_reply_plan(content: str, decision: FastIntentDecision) -> ExecutionPlan:
    return ExecutionPlan(
        user_objective=content,
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True),
        nodes=[
            PlanNode(
                id="fast_reply",
                objective=content,
                runtime="llm",
                input_refs=[],
                output_hint="User-facing fast reply.",
            )
        ],
    )


def _artifact_delivery_plan(content: str, artifacts: list[dict[str, Any]]) -> ExecutionPlan | None:
    if not artifacts or not _looks_like_artifact_delivery(content):
        return None
    artifact_ref = _artifact_ref(artifacts[0], default_index=1)
    if not artifact_ref:
        return None
    return ExecutionPlan(
        user_objective=content,
        finalization_hint=FinalizationHint(
            mode="llm",
            user_facing=False,
        ),
        nodes=[
            PlanNode(
                id="deliver_artifact",
                runtime="react",
                objective="Deliver the requested existing artifact to the user by calling the deliver_file tool.",
                input_refs=[artifact_ref],
                output_hint="Artifact delivered to the user.",
            )
        ],
    )


def _artifact_ref(artifact: dict[str, Any], *, default_index: int) -> str:
    for key in ("ref", "artifact_ref", "id", "artifact_id"):
        value = artifact.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text if text.startswith("artifact:") else f"artifact:{text}"
    return f"artifact:A{default_index}"


def _looks_like_artifact_delivery(content: str) -> bool:
    text = str(content or "").strip().lower()
    delivery_terms = ("发我", "发给我", "发送", "交付", "deliver", "send", "resend")
    artifact_terms = ("刚刚", "刚才", "那个", "报告", "文件", "产物", "artifact", "file", "report")
    return any(term in text for term in delivery_terms) and any(term in text for term in artifact_terms)


def _should_skip_fast_intent(
    content: str,
    *,
    runtime_context: RuntimeContext,
    session_state: ConversationSessionState | None,
    previous_node_results: list[dict[str, Any]] | None,
    conversation_context: ConversationContext | None,
) -> bool:
    if previous_node_results:
        return True
    if conversation_context is not None and conversation_context.context_reference_detected:
        return True
    active_repo = (
        runtime_context.repo.active_repo or str(getattr(session_state, "active_repo_id", None) or "").strip()
    ).lower()
    if active_repo and _looks_like_repo_or_action_plan(content, active_repo):
        return True
    return False


def _looks_like_repo_or_action_plan(content: str, active_repo: str) -> bool:
    text = str(content or "").strip().lower()
    if not text:
        return False
    markers = (
        active_repo,
        "repo",
        "repository",
        "项目",
        "仓库",
        "代码",
        "review",
        "重构",
        "风险",
        "评估",
        "调整",
        "报告",
        "markdown",
        "提醒",
        "remind",
        "agent runtime",
        "task graph",
    )
    return any(marker and marker in text for marker in markers)


def _can_use_fast_reply(
    decision: FastIntentDecision,
    threshold: float,
    *,
    has_previous_node_results: bool = False,
    has_context_reference: bool = False,
) -> bool:
    if has_previous_node_results:
        return False
    if has_context_reference:
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
    conversation_context: ConversationContext | None,
    previous_node_results: list[dict[str, Any]] | None,
    runtime_hints: dict[str, Any] | None,
    runtime_context: RuntimeContext | None,
    instructions: list[str] | None,
) -> tuple[ExecutionPlan, int, list[dict[str, Any]]]:
    started = time.perf_counter()
    kwargs = {
        "content": content,
        "session_state": session_state,
        "conversation_metadata": conversation_metadata,
        "recent_artifacts": recent_artifacts,
        "conversation_context": conversation_context,
        "previous_node_results": previous_node_results,
        "runtime_hints": runtime_hints,
        "runtime_context": runtime_context,
        "instructions": instructions,
    }
    if hasattr(planner, "plan_with_usage"):
        result = planner.plan_with_usage(**kwargs)
        return result.plan, int((time.perf_counter() - started) * 1000), result.usage_records
    plan = planner.plan(**kwargs)
    return plan, int((time.perf_counter() - started) * 1000), []
