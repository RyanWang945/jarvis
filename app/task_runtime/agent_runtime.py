from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent_react.artifact_context import artifact_records_to_context
from app.agent_react.artifacts import artifact_to_payload, resolve_channel_attachments
from app.agent_react.context_manager import ContextManager
from app.agent_react.session_state import (
    build_session_state_after_turn,
    load_session_state,
)
from app.config import get_settings
from app.observability import (
    add_event,
    content_capture_enabled,
    current_trace_ids,
    record_exception,
    set_attributes,
    span_context,
    trace_preview,
)
from app.progress import ProgressReporter, ensure_progress
from app.runtime_usage import collect_usage_records, usage_totals
from app.runtime_types import ChannelMessage, ConversationStore, TurnResult
from app.task_runtime.approval_types import approval_request_dicts
from app.task_runtime.artifacts import ArtifactPublisher
from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime, is_claude_agent_sdk_available
from app.task_runtime.node_execute_runtime import (
    CoderNodeExecuteRuntime,
    LLMNodeExecuteRuntime,
    ReactNodeExecuteRuntime,
)
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import ExecutionReport, NodeResult
from app.task_runtime.planning_router import PlanningRouter
from app.task_runtime.result_aggregator import AggregationResult, ResultAggregator
from app.task_runtime.runtime_context import RuntimeContext
from app.task_runtime.session_workspace import SessionWorkspaceManager, SessionWorkspaceRef

logger = logging.getLogger(__name__)


def _build_default_runtimes() -> dict:
    """Build the default runtime dict.

    The ``react`` runtime backend is controlled by ``JARVIS_REACT_RUNTIME_BACKEND``:
    - ``builtin`` (default) – the hand-rolled ReAct loop
    - ``claude_agent_sdk`` – Claude Agent SDK, if installed
    """
    settings = get_settings()
    backend = (settings.react_runtime_backend or "builtin").strip().lower()

    runtimes: dict = {
        "llm": LLMNodeExecuteRuntime(),
        "coder": CoderNodeExecuteRuntime(),
    }

    if backend == "claude_agent_sdk":
        if is_claude_agent_sdk_available():
            runtimes["react"] = ClaudeReactNodeExecuteRuntime()
        else:
            logger.warning(
                "react_runtime_backend=claude_agent_sdk but SDK is not installed; falling back to builtin"
            )
            runtimes["react"] = ReactNodeExecuteRuntime()
    else:
        runtimes["react"] = ReactNodeExecuteRuntime()

    return runtimes


def _default_available_runtimes() -> list[str]:
    """List of runtime names the planner may assign. Always returns standard names."""
    return ["react", "coder"]


class TaskAgentRuntime:
    """Outer turn runtime backed by PlanningRouter, NodeExecutor, and ResultAggregator."""

    def __init__(
        self,
        store: ConversationStore,
        *,
        planning_router: PlanningRouter | None = None,
        node_executor: NodeExecutor | None = None,
        result_aggregator: ResultAggregator | None = None,
        context_manager: ContextManager | None = None,
        session_workspace_manager: SessionWorkspaceManager | None = None,
    ) -> None:
        self._store = store
        self._context_manager = context_manager or ContextManager()
        self._planning_router = planning_router or PlanningRouter()
        self._node_executor = node_executor or NodeExecutor(
            runtimes=_build_default_runtimes(),
        )
        self._result_aggregator = result_aggregator or ResultAggregator()
        self._session_workspace_manager = session_workspace_manager or SessionWorkspaceManager()

    def run_turn(self, turn_id: int, progress: ProgressReporter | None = None) -> TurnResult:
        progress = ensure_progress(progress)
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise ValueError(f"Turn not found: {turn_id}")
        conversation = self._store.get_conversation(turn.conversation_id)
        if conversation is None:
            raise ValueError(f"Conversation not found: {turn.conversation_id}")

        self._store.mark_turn_running(turn_id)
        user_input = _trigger_user_input(self._store, turn)
        session_state = load_session_state(conversation.metadata)
        recent_artifacts = _recent_artifacts(self._store, turn.conversation_id)
        conversation_context = self._context_manager.build_conversation_context(
            self._store.list_messages(turn.conversation_id),
            getattr(turn, "trigger_message_id", None),
            turn_records=self._store.list_turns(turn.conversation_id),
            current_turn_id=turn.id,
            session_state=session_state,
            current_user_input=user_input,
        )
        runtime_context = RuntimeContext.from_hints(
            {
                "platform": conversation.platform,
                "conversation_id": turn.conversation_id,
                "turn_id": turn_id,
                "external_chat_id": conversation.external_chat_id,
                "available_runtimes": _default_available_runtimes(),
                "coder_runtime_provider": get_settings().coder_runtime_provider,
                **_runtime_temporal_hints(),
            }
        )
        started = time.perf_counter()
        with span_context(
            "turn.run",
            **{
                "jarvis.platform": conversation.platform,
                "jarvis.conversation_id": turn.conversation_id,
                "jarvis.turn_id": turn_id,
                "langfuse.trace.name": f"turn.run:{turn_id}",
                "langfuse.session.id": f"conversation:{turn.conversation_id}",
            },
        ):
            return self._run_turn_inner(
                turn=turn,
                conversation=conversation,
                turn_id=turn_id,
                user_input=user_input,
                session_state=session_state,
                recent_artifacts=recent_artifacts,
                conversation_context=conversation_context,
                runtime_context=runtime_context,
                progress=progress,
                started=started,
            )

    def _run_turn_inner(
        self,
        *,
        turn: Any,
        conversation: Any,
        turn_id: int,
        user_input: str,
        session_state: Any,
        recent_artifacts: list[dict[str, Any]],
        conversation_context: Any,
        runtime_context: RuntimeContext,
        progress: ProgressReporter,
        started: float,
    ) -> TurnResult:
        logger.info(
            "task runtime turn start turn_id=%s conversation_id=%s trigger_type=%s user_input_len=%s recent_artifact_count=%s",
            turn_id,
            turn.conversation_id,
            getattr(turn, "trigger_type", None),
            len(user_input),
            len(recent_artifacts),
        )
        turn_attributes: dict[str, Any] = {
            "jarvis.trigger_type": getattr(turn, "trigger_type", None),
            "jarvis.user_input_len": len(user_input),
            "jarvis.user_input_preview": trace_preview(user_input),
            "jarvis.recent_artifact_count": len(recent_artifacts),
        }
        previous_node_results = _previous_node_results_from_messages(
            self._store.list_messages(turn.conversation_id),
            current_turn_id=turn_id,
        ) if _should_include_previous_node_results(user_input, conversation_context) else []
        if previous_node_results:
            turn_attributes["jarvis.previous_node_result_count"] = len(previous_node_results)
        if content_capture_enabled():
            turn_attributes["langfuse.trace.input"] = trace_preview(user_input, limit=1200)
        set_attributes(**turn_attributes)
        progress.emit(
            "turn_started",
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            stage="turn",
            status="running",
            summary="开始处理用户请求",
            data={"trigger_type": getattr(turn, "trigger_type", None), "recent_artifact_count": len(recent_artifacts)},
        )

        session_workspace: SessionWorkspaceRef | None = None
        try:
            router_result = self._planning_router.plan(
                content=user_input,
                session_state=session_state,
                conversation_metadata=conversation.metadata,
                recent_artifacts=recent_artifacts,
                conversation_context=conversation_context,
                previous_node_results=previous_node_results,
                runtime_context=runtime_context,
                instructions=[],
                progress=progress,
            )
            set_attributes(**{"jarvis.route": router_result.route, "langfuse.trace.metadata.route": router_result.route})
            planner_attributes: dict[str, Any] = {
                "jarvis.route": router_result.route,
                "jarvis.fast_route": router_result.fast_intent.route,
                "jarvis.fast_confidence": router_result.fast_intent.confidence,
                "jarvis.node_count": len(router_result.plan.nodes),
                "jarvis.finalization_mode": router_result.plan.finalization_hint.mode,
                "jarvis.planner_elapsed_ms": router_result.planner_elapsed_ms,
            }
            set_attributes(**planner_attributes)
            planner_event: dict[str, Any] = {
                **planner_attributes,
                "jarvis.node_ids": [node.id for node in router_result.plan.nodes],
                "jarvis.node_runtimes": [node.runtime for node in router_result.plan.nodes],
            }
            if content_capture_enabled():
                planner_event["jarvis.node_objective_previews"] = [
                    trace_preview(node.objective, limit=180) for node in router_result.plan.nodes
                ]
            add_event("planner.completed", **planner_event)
            logger.info(
                "task runtime planning completed turn_id=%s route=%s fast_route=%s fast_confidence=%.2f node_count=%s runtimes=%s finalization=%s elapsed_ms=%s planner_elapsed_ms=%s",
                turn_id,
                router_result.route,
                router_result.fast_intent.route,
                router_result.fast_intent.confidence,
                len(router_result.plan.nodes),
                [node.runtime for node in router_result.plan.nodes],
                router_result.plan.finalization_hint.mode,
                router_result.elapsed_ms,
                router_result.planner_elapsed_ms,
            )
            if router_result.route == "fast_reply":
                report = _fast_reply_report(router_result.fast_intent.reply)
                usage_records = collect_usage_records(
                    router_result.fast_intent,
                    router_result.planner_usage_records,
                    report.node_results,
                )
                token_usage = usage_totals(usage_records)
                reply = router_result.fast_intent.reply
                aggregation = AggregationResult(
                    status="completed",
                    reply=reply,
                    data={"finalization": "fast_reply"},
                    usage_records=usage_records,
                )
                raw_payload = _trace_enriched_payload(
                    {
                        "source": "task_runtime",
                        "route": router_result.route,
                        "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                        "plan": router_result.plan.model_dump(mode="json"),
                        "execution_report": report.model_dump(mode="json", exclude_none=True),
                        "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
                    }
                )
                if usage_records:
                    raw_payload["usage_records"] = usage_records
                if token_usage is not None:
                    raw_payload["usage"] = token_usage
                    _set_usage_attributes(token_usage)
                self._store.finalize_turn_success(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    content=reply,
                    content_type="markdown",
                    raw_payload=raw_payload,
                )
                completed_attributes: dict[str, Any] = {
                    "jarvis.status": "completed",
                    "langfuse.trace.metadata.status": "completed",
                }
                if content_capture_enabled():
                    completed_attributes["langfuse.trace.output"] = trace_preview(reply, limit=1200)
                set_attributes(**completed_attributes)
                logger.info(
                    "task runtime fast reply finished turn_id=%s reply_len=%s elapsed_ms=%s",
                    turn_id,
                    len(reply),
                    int((time.perf_counter() - started) * 1000),
                )
                progress.emit(
                    "turn_completed",
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    stage="turn",
                    status="completed",
                    summary="已生成直接回复",
                )
                self._writeback_session(
                    conversation_id=turn.conversation_id,
                    turn_id=turn_id,
                    status="completed",
                    reply=reply,
                    current_user_input=user_input,
                )
                return TurnResult(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    status="completed",
                    message=ChannelMessage(
                        content=reply,
                        content_type="markdown",
                        summary=reply,
                        metadata={
                            "conversation_id": turn.conversation_id,
                            "turn_id": turn_id,
                            "aggregation_status": aggregation.status,
                            "route": router_result.route,
                            **({"usage_records": usage_records} if usage_records else {}),
                            **({"usage": token_usage} if token_usage is not None else {}),
                        },
                    ),
                )

            progress.emit(
                "plan_created",
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                stage="planning",
                status="completed",
                summary=f"已生成 {len(router_result.plan.nodes)} 个执行节点",
                data={
                    "route": router_result.route,
                    "fast_route": router_result.fast_intent.route,
                    "node_count": len(router_result.plan.nodes),
                    "runtimes": [node.runtime for node in router_result.plan.nodes],
                    "nodes": [
                        {
                            "id": node.id,
                            "runtime": node.runtime,
                            "objective": node.objective,
                        }
                        for node in router_result.plan.nodes
                    ],
                    "planner_elapsed_ms": router_result.planner_elapsed_ms,
                },
            )
            session_workspace = self._session_workspace_manager.create_for_plan(
                router_result.plan,
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
            )
            set_attributes(
                **{
                    "jarvis.session_id": session_workspace.session_id,
                    "jarvis.session_path": str(session_workspace.session_path),
                    "jarvis.dag_path": str(session_workspace.dag_path),
                    "jarvis.session_workspace_dir": str(session_workspace.root_path),
                }
            )
            add_event(
                "session_workspace.created",
                **{
                    "jarvis.session_id": session_workspace.session_id,
                    "jarvis.session_path": str(session_workspace.session_path),
                    "jarvis.dag_path": str(session_workspace.dag_path),
                    "jarvis.node_count": len(session_workspace.nodes),
                },
            )
            self._session_workspace_manager.update_status(session_workspace, "running")
            execution_context = runtime_context.with_hints(session_workspace.to_legacy_hints())
            execution_started = time.perf_counter()
            report = self._node_executor.execute(
                router_result.plan,
                artifacts=recent_artifacts,
                previous_node_results=previous_node_results,
                runtime_context=execution_context,
                instructions=[],
                progress=progress,
                session_workspace=session_workspace,
            )
            logger.info(
                "task runtime execution completed turn_id=%s status=%s node_count=%s completed_order=%s elapsed_ms=%s",
                turn_id,
                report.status,
                len(report.node_results),
                report.data.get("completed_order"),
                int((time.perf_counter() - execution_started) * 1000),
            )
            artifact_records = ArtifactPublisher(self._store, session_workspace).publish(
                report, turn_id=turn_id, conversation_id=turn.conversation_id,
            )
            current_artifact_context = [
                *recent_artifacts,
                *[artifact_to_payload(item) for item in artifact_records],
            ]
            aggregation_started = time.perf_counter()
            progress.emit(
                "aggregation_started",
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                stage="aggregation",
                status="running",
                summary="正在汇总执行结果",
                data={"report_status": report.status, "node_count": len(report.node_results)},
            )
            aggregation = self._result_aggregator.aggregate(
                plan=router_result.plan,
                report=report,
                current_user_input=user_input,
                route=router_result.route,
                fast_intent=router_result.fast_intent.model_dump(mode="json"),
                artifacts=current_artifact_context,
                runtime_context=execution_context,
                instructions=[],
                conversation_metadata=conversation.metadata,
            )
            artifact_resolution = resolve_channel_attachments(
                artifact_records,
                turn_id=turn_id,
                extra_allowed_roots=[session_workspace.artifacts_dir],
            )
            logger.info(
                "task runtime aggregation completed turn_id=%s status=%s finalization=%s reply_len=%s artifact_refs=%s elapsed_ms=%s",
                turn_id,
                aggregation.status,
                router_result.plan.finalization_hint.mode,
                len(aggregation.reply),
                aggregation.artifact_refs,
                int((time.perf_counter() - aggregation_started) * 1000),
            )
            set_attributes(
                **{
                    "jarvis.aggregation_status": aggregation.status,
                    "jarvis.reply_len": len(aggregation.reply),
                }
            )
            progress.emit(
                "aggregation_completed",
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                stage="aggregation",
                status=aggregation.status,
                summary="汇总完成" if aggregation.status != "failed" else "汇总失败",
                data={
                    "artifact_refs": aggregation.artifact_refs,
                    "reply_len": len(aggregation.reply),
                    "elapsed_ms": int((time.perf_counter() - aggregation_started) * 1000),
                },
            )
            logger.info(
                "task runtime artifacts resolved turn_id=%s artifact_count=%s attachment_count=%s rejected_count=%s",
                turn_id,
                len(artifact_records),
                len(artifact_resolution.attachments),
                len(artifact_resolution.rejected),
            )
            raw_payload = _trace_enriched_payload(
                {
                    "source": "task_runtime",
                    "route": router_result.route,
                    "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                    "plan": router_result.plan.model_dump(mode="json"),
                    "execution_report": report.model_dump(mode="json", exclude_none=True),
                    "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
                    "session_workspace": session_workspace.metadata(),
                }
            )
            if artifact_records:
                raw_payload["artifacts"] = [artifact_to_payload(item) for item in artifact_records]
            if artifact_resolution.attachments:
                raw_payload["attachments"] = [attachment.__dict__ for attachment in artifact_resolution.attachments]
            if artifact_resolution.rejected:
                raw_payload["artifact_rejections"] = [item.__dict__ for item in artifact_resolution.rejected]
            approval_requests = _approval_requests_from_aggregation(aggregation)
            if approval_requests:
                raw_payload["approval_requests"] = approval_requests
            usage_records = collect_usage_records(
                router_result.fast_intent,
                router_result.planner_usage_records,
                report.node_results,
                aggregation,
            )
            token_usage = usage_totals(usage_records)
            reply = aggregation.reply
            if usage_records:
                raw_payload["usage_records"] = usage_records
            if token_usage is not None:
                raw_payload["usage"] = token_usage
                _set_usage_attributes(token_usage)
            if aggregation.status == "failed":
                self._store.finalize_turn_failure(turn_id, error_message=reply)
                status = "failed"
            else:
                self._store.finalize_turn_success(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    content=reply,
                    content_type="markdown",
                    raw_payload=raw_payload,
                )
                status = "completed"
            status_attributes: dict[str, Any] = {
                "jarvis.status": status,
                "langfuse.trace.metadata.status": status,
            }
            if content_capture_enabled():
                status_attributes["langfuse.trace.output"] = trace_preview(reply, limit=1200)
            set_attributes(**status_attributes)
            self._session_workspace_manager.update_status(session_workspace, status)
            logger.info(
                "task runtime turn finished turn_id=%s status=%s elapsed_ms=%s",
                turn_id,
                status,
                int((time.perf_counter() - started) * 1000),
            )
            progress.emit(
                "turn_failed" if status == "failed" else "turn_completed",
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                stage="turn",
                status=status,
                summary="任务执行失败" if status == "failed" else "任务已完成",
                data={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            )
            self._writeback_session(
                conversation_id=turn.conversation_id,
                turn_id=turn_id,
                status=status,
                reply=reply if status == "completed" else None,
                current_user_input=user_input,
            )
            return TurnResult(
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                status=status,
                message=ChannelMessage(
                    content=reply,
                    content_type="markdown",
                    summary=reply,
                    attachments=artifact_resolution.attachments,
                    metadata={
                        "conversation_id": turn.conversation_id,
                        "turn_id": turn_id,
                        "aggregation_status": aggregation.status,
                        **({"usage_records": usage_records} if usage_records else {}),
                        **({"usage": token_usage} if token_usage is not None else {}),
                        **({"approval_requests": approval_requests} if approval_requests else {}),
                    },
                ),
            )
        except Exception as exc:
            set_attributes(**{"jarvis.status": "failed", "langfuse.trace.metadata.status": "failed"})
            record_exception(exc, **{"jarvis.turn_id": turn_id, "jarvis.conversation_id": turn.conversation_id})
            logger.exception(
                "task runtime failed turn_id=%s elapsed_ms=%s",
                turn_id,
                int((time.perf_counter() - started) * 1000),
            )
            self._store.finalize_turn_failure(turn_id, error_message=str(exc))
            if session_workspace is not None:
                self._session_workspace_manager.update_status(session_workspace, "failed")
            progress.emit(
                "turn_failed",
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                stage="turn",
                status="failed",
                summary=str(exc),
                data={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            )
            self._writeback_session(
                conversation_id=turn.conversation_id,
                turn_id=turn_id,
                status="failed",
                reply=None,
                current_user_input=user_input,
            )
            error_text = str(exc)
            return TurnResult(
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                status="failed",
                message=ChannelMessage(
                    content=f"任务执行异常: {error_text}",
                    content_type="markdown",
                    summary=error_text,
                    metadata={"conversation_id": turn.conversation_id, "turn_id": turn_id},
                ),
            )

    def _writeback_session(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        status: str,
        reply: str | None,
        current_user_input: str = "",
    ) -> None:
        try:
            conversation = self._store.get_conversation(conversation_id)
            previous = load_session_state(conversation.metadata if conversation is not None else None)
            summary = self._context_manager.update_working_summary(
                previous,
                current_user_input=current_user_input,
                assistant_reply=reply,
            )
            session_state = replace(
                build_session_state_after_turn(
                    previous,
                    turn_id=turn_id,
                    status=status,
                    assistant_reply=reply,
                ),
                working_summary=summary,
            )
            self._store.update_conversation_session(conversation_id, session_state)
        except Exception:
            logger.exception("task runtime session state writeback failed turn_id=%s", turn_id)


def _trigger_user_input(store: ConversationStore, turn: Any) -> str:
    trigger_id = getattr(turn, "trigger_message_id", None)
    messages = store.list_messages(turn.conversation_id)
    if trigger_id is not None:
        for message in messages:
            if getattr(message, "id", None) == trigger_id:
                return str(getattr(message, "content", "") or "").strip()
    for message in reversed(messages):
        if getattr(message, "role", None) == "user" and getattr(message, "turn_id", None) == turn.id:
            return str(getattr(message, "content", "") or "").strip()
    return ""


def _recent_artifacts(store: ConversationStore, conversation_id: int) -> list[dict[str, Any]]:
    list_recent = getattr(store, "list_recent_artifacts_by_conversation", None)
    if not callable(list_recent):
        return []
    try:
        return artifact_records_to_context(list_recent(conversation_id))
    except Exception:
        logger.warning("task runtime failed to load recent artifacts conversation_id=%s", conversation_id, exc_info=True)
        return []


def _previous_node_results_from_messages(
    messages: list[Any],
    *,
    current_turn_id: int | None = None,
    max_results: int = 12,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in reversed(messages):
        if current_turn_id is not None and getattr(message, "turn_id", None) == current_turn_id:
            continue
        if getattr(message, "role", None) != "assistant":
            continue
        raw_payload = getattr(message, "raw_payload", None)
        if not isinstance(raw_payload, dict) or raw_payload.get("source") != "task_runtime":
            continue
        workspace_nodes = _workspace_nodes_by_id(raw_payload.get("session_workspace"))
        report = raw_payload.get("execution_report")
        node_results = report.get("node_results") if isinstance(report, dict) else None
        if not isinstance(node_results, list):
            continue
        for item in reversed(node_results):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or item.get("id") or "").strip()
            runtime = item.get("runtime")
            if not node_id or node_id in seen or runtime not in {"llm", "react", "coder"}:
                continue
            if item.get("status") != "completed":
                continue
            result = _compact_previous_node_result(item, workspace_nodes.get(node_id))
            if result is None:
                continue
            results.append(result)
            seen.add(node_id)
            if len(results) >= max_results:
                return results
    return results


def _should_include_previous_node_results(user_input: str, conversation_context: Any) -> bool:
    if conversation_context is not None and getattr(conversation_context, "context_reference_detected", False):
        return True
    text = str(user_input or "").strip().lower()
    if not text:
        return False
    continuation_terms = (
        "继续",
        "接着",
        "刚才",
        "刚刚",
        "上次",
        "之前",
        "那个",
        "这个",
        "再",
        "补",
        "另开",
        "合并",
        "发布",
        "continue",
        "resume",
        "previous",
        "last",
        "same",
        "that",
        "fork",
        "publish",
        "merge",
    )
    return any(term in text for term in continuation_terms)


def _workspace_nodes_by_id(session_workspace: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(session_workspace, dict):
        return {}
    nodes = session_workspace.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    return {str(node_id): dict(value) for node_id, value in nodes.items() if isinstance(value, dict)}


def _compact_previous_node_result(item: dict[str, Any], workspace_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    node_id = str(item.get("node_id") or item.get("id") or "").strip()
    runtime = item.get("runtime")
    if not node_id or runtime not in {"llm", "react", "coder"}:
        return None
    data = dict(item.get("data")) if isinstance(item.get("data"), dict) else {}
    workspace = dict(data.get("workspace")) if isinstance(data.get("workspace"), dict) else {}
    if workspace_meta:
        _fill_workspace_from_meta(workspace, workspace_meta)
    if workspace:
        data["workspace"] = workspace
        if workspace.get("workspace_path"):
            data.setdefault("workspace_path", workspace["workspace_path"])
    result: dict[str, Any] = {
        "node_id": node_id,
        "runtime": runtime,
        "status": "completed",
        "summary": str(item.get("summary") or ""),
        "artifacts": item.get("artifacts") if isinstance(item.get("artifacts"), list) else [],
        "data": data,
    }
    if isinstance(item.get("git"), dict) and item["git"]:
        result["git"] = item["git"]
    return result


def _fill_workspace_from_meta(workspace: dict[str, Any], meta: dict[str, Any]) -> None:
    mapping = {
        "root_path": "workspace_path",
        "task_path": "task_path",
        "progress_path": "progress_path",
        "result_markdown_path": "result_markdown_path",
        "state_path": "state_path",
        "artifacts_dir": "artifacts_dir",
        "repo_path": "repo_path",
    }
    for source_key, target_key in mapping.items():
        value = meta.get(source_key)
        if value is not None and not workspace.get(target_key):
            workspace[target_key] = str(value)


def _runtime_temporal_hints(now: datetime | None = None) -> dict[str, str]:
    timezone_name = get_settings().default_timezone
    tz = _resolve_timezone(timezone_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return {
        "current_date": current.date().isoformat(),
        "current_time": current.isoformat(timespec="seconds"),
        "timezone": timezone_name,
    }


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name in {"Asia/Shanghai", "Asia/Chongqing"}:
            return timezone(timedelta(hours=8), name=timezone_name)
        return UTC


def _fast_reply_report(reply: str) -> ExecutionReport:
    return ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="fast_reply",
                runtime="llm",
                status="completed",
                summary=reply,
            )
        ],
        data={"completed_order": ["fast_reply"], "fast_path": True},
    )


def _approval_requests_from_aggregation(aggregation: AggregationResult) -> list[dict[str, Any]]:
    if aggregation.approval_requests:
        return approval_request_dicts(aggregation.approval_requests)
    return []


def _trace_enriched_payload(payload: dict[str, Any]) -> dict[str, Any]:
    trace_id, span_id = current_trace_ids()
    if not trace_id and not span_id:
        return payload
    enriched = dict(payload)
    enriched["trace"] = {"trace_id": trace_id, "span_id": span_id}
    return enriched


def _set_usage_attributes(token_usage: dict[str, Any]) -> None:
    set_attributes(
        **{
            "jarvis.usage.model": token_usage.get("model"),
            "jarvis.usage.prompt_tokens": token_usage.get("prompt_tokens"),
            "jarvis.usage.completion_tokens": token_usage.get("completion_tokens"),
            "jarvis.usage.total_tokens": token_usage.get("total_tokens"),
        }
    )
