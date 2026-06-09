from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent_react.artifact_context import artifact_records_to_context
from app.agent_react.artifacts import artifact_from_payload, artifact_to_payload, resolve_channel_attachments
from app.agent_react.context_manager import ContextManager
from app.agent_react.session_state import (
    build_session_state_after_turn,
    load_session_state,
)
from app.config import get_settings
from app.progress import ProgressReporter, ensure_progress
from app.runtime_types import ChannelMessage, ConversationStore, TurnResult
from app.task_runtime.node_execute_runtime import (
    CoderNodeExecuteRuntime,
    LLMNodeExecuteRuntime,
    ReactNodeExecuteRuntime,
)
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import ExecutionReport, NodeResult
from app.task_runtime.planning_router import PlanningRouter
from app.task_runtime.result_aggregator import AggregationResult, ResultAggregator

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._store = store
        self._context_manager = context_manager or ContextManager()
        self._planning_router = planning_router or PlanningRouter()
        self._node_executor = node_executor or NodeExecutor(
            runtimes={
                "llm": LLMNodeExecuteRuntime(),
                "react": ReactNodeExecuteRuntime(),
                "coder": CoderNodeExecuteRuntime(),
            }
        )
        self._result_aggregator = result_aggregator or ResultAggregator()

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
        runtime_hints = {
            "active_repo": session_state.active_repo_id,
            "platform": conversation.platform,
            "conversation_id": turn.conversation_id,
            "turn_id": turn_id,
            "external_chat_id": conversation.external_chat_id,
            "available_runtimes": ["llm", "react", "coder"],
            "coder_runtime_provider": get_settings().coder_runtime_provider,
            **_runtime_temporal_hints(),
        }
        started = time.perf_counter()
        logger.info(
            "task runtime turn start turn_id=%s conversation_id=%s trigger_type=%s user_input_len=%s recent_artifact_count=%s active_repo=%s",
            turn_id,
            turn.conversation_id,
            getattr(turn, "trigger_type", None),
            len(user_input),
            len(recent_artifacts),
            session_state.active_repo_id,
        )
        progress.emit(
            "turn_started",
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            stage="turn",
            status="running",
            summary="开始处理用户请求",
            data={"trigger_type": getattr(turn, "trigger_type", None), "recent_artifact_count": len(recent_artifacts)},
        )

        try:
            router_result = self._planning_router.plan(
                content=user_input,
                session_state=session_state,
                conversation_metadata=conversation.metadata,
                recent_artifacts=recent_artifacts,
                conversation_context=conversation_context,
                previous_node_results=[],
                runtime_hints=runtime_hints,
                instructions=[],
                progress=progress,
            )
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
                aggregation = AggregationResult(
                    status="completed",
                    reply=router_result.fast_intent.reply,
                    data={"finalization": "fast_reply"},
                )
                raw_payload = {
                    "source": "task_runtime",
                    "route": router_result.route,
                    "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                    "plan": router_result.plan.model_dump(mode="json"),
                    "execution_report": report.model_dump(mode="json", exclude_none=True),
                    "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
                }
                self._store.finalize_turn_success(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    content=aggregation.reply,
                    content_type="markdown",
                    raw_payload=raw_payload,
                )
                logger.info(
                    "task runtime fast reply finished turn_id=%s reply_len=%s elapsed_ms=%s",
                    turn_id,
                    len(aggregation.reply),
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
                    reply=aggregation.reply,
                    current_user_input=user_input,
                )
                return TurnResult(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    status="completed",
                    message=ChannelMessage(
                        content=aggregation.reply,
                        content_type="markdown",
                        summary=aggregation.reply,
                        metadata={
                            "conversation_id": turn.conversation_id,
                            "turn_id": turn_id,
                            "aggregation_status": aggregation.status,
                            "route": router_result.route,
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
            execution_started = time.perf_counter()
            report = self._node_executor.execute(
                router_result.plan,
                artifacts=recent_artifacts,
                previous_node_results=[],
                runtime_hints=runtime_hints,
                instructions=[],
                progress=progress,
            )
            logger.info(
                "task runtime execution completed turn_id=%s status=%s node_count=%s completed_order=%s elapsed_ms=%s",
                turn_id,
                report.status,
                len(report.node_results),
                report.data.get("completed_order"),
                int((time.perf_counter() - execution_started) * 1000),
            )
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
                artifacts=recent_artifacts,
                runtime_hints=runtime_hints,
                instructions=[],
                conversation_metadata=conversation.metadata,
            )
            tool_artifacts = _tool_artifacts_from_report(report, turn_id=turn_id)
            _persist_tool_artifacts(self._store, turn.conversation_id, tool_artifacts)
            artifact_resolution = resolve_channel_attachments(tool_artifacts, turn_id=turn_id)
            logger.info(
                "task runtime aggregation completed turn_id=%s status=%s finalization=%s reply_len=%s artifact_refs=%s elapsed_ms=%s",
                turn_id,
                aggregation.status,
                router_result.plan.finalization_hint.mode,
                len(aggregation.reply),
                aggregation.artifact_refs,
                int((time.perf_counter() - aggregation_started) * 1000),
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
                len(tool_artifacts),
                len(artifact_resolution.attachments),
                len(artifact_resolution.rejected),
            )
            raw_payload = {
                "source": "task_runtime",
                "route": router_result.route,
                "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                "plan": router_result.plan.model_dump(mode="json"),
                "execution_report": report.model_dump(mode="json", exclude_none=True),
                "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
            }
            if tool_artifacts:
                raw_payload["artifacts"] = [artifact_to_payload(item) for item in tool_artifacts]
            if artifact_resolution.attachments:
                raw_payload["attachments"] = [attachment.__dict__ for attachment in artifact_resolution.attachments]
            if artifact_resolution.rejected:
                raw_payload["artifact_rejections"] = [item.__dict__ for item in artifact_resolution.rejected]
            reply = aggregation.reply
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
                    content=reply if status == "completed" else "",
                    content_type="markdown",
                    summary=reply,
                    attachments=artifact_resolution.attachments,
                    metadata={
                        "conversation_id": turn.conversation_id,
                        "turn_id": turn_id,
                        "aggregation_status": aggregation.status,
                    },
                ),
            )
        except Exception as exc:
            logger.exception(
                "task runtime failed turn_id=%s elapsed_ms=%s",
                turn_id,
                int((time.perf_counter() - started) * 1000),
            )
            self._store.finalize_turn_failure(turn_id, error_message=str(exc))
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
            return TurnResult(
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                status="failed",
                message=ChannelMessage(
                    content="",
                    content_type="markdown",
                    summary=str(exc),
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


def _tool_artifacts_from_report(report: ExecutionReport, *, turn_id: int) -> list[Any]:
    artifacts: list[Any] = []
    seen: set[str] = set()
    for result in report.node_results:
        raw_items = result.data.get("tool_artifacts")
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            artifact = artifact_from_payload(raw)
            if artifact is None:
                continue
            updates: dict[str, Any] = {}
            if artifact.turn_id is None:
                updates["turn_id"] = turn_id
            if not artifact.tool_call_id:
                updates["tool_call_id"] = f"node:{result.node_id}"
            if not artifact.source_tool:
                provider = str(result.data.get("provider") or "")
                updates["source_tool"] = "coder" if result.runtime in {"coder", "codex"} and provider in {"", "codex"} else result.runtime
            if updates:
                artifact = replace(artifact, **updates)
            if artifact.artifact_id in seen:
                continue
            seen.add(artifact.artifact_id)
            artifacts.append(artifact)
    return artifacts


def _persist_tool_artifacts(store: ConversationStore, conversation_id: int, artifacts: list[Any]) -> None:
    upsert = getattr(store, "upsert_artifact", None)
    if not callable(upsert):
        return
    for artifact in artifacts:
        try:
            upsert(artifact, conversation_id=conversation_id)
        except Exception:
            logger.exception(
                "task runtime artifact persistence failed conversation_id=%s artifact_id=%s",
                conversation_id,
                getattr(artifact, "artifact_id", ""),
            )


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
