from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
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
from app.runtime_usage import append_usage_footer, collect_usage_records, usage_totals
from app.runtime_types import ChannelMessage, ConversationStore, TurnResult
from app.task_runtime.approval_types import approval_request_dicts
from app.task_runtime.node_execute_runtime import (
    CoderNodeExecuteRuntime,
    LLMNodeExecuteRuntime,
    ReactNodeExecuteRuntime,
)
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeResult
from app.task_runtime.planning_router import PlanningRouter
from app.task_runtime.result_aggregator import AggregationResult, ResultAggregator
from app.task_runtime.session_workspace import SessionWorkspaceManager, SessionWorkspaceRef
from app.tools.common import ToolArtifact

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
        session_workspace_manager: SessionWorkspaceManager | None = None,
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

        session_workspace: SessionWorkspaceRef | None = None
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
                usage_records = collect_usage_records(
                    router_result.fast_intent,
                    router_result.planner_usage_records,
                    report.node_results,
                )
                token_usage = usage_totals(usage_records)
                reply = append_usage_footer(router_result.fast_intent.reply, token_usage)
                aggregation = AggregationResult(
                    status="completed",
                    reply=reply,
                    data={"finalization": "fast_reply"},
                    usage_records=usage_records,
                )
                raw_payload = {
                    "source": "task_runtime",
                    "route": router_result.route,
                    "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                    "plan": router_result.plan.model_dump(mode="json"),
                    "execution_report": report.model_dump(mode="json", exclude_none=True),
                    "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
                }
                if usage_records:
                    raw_payload["usage_records"] = usage_records
                if token_usage is not None:
                    raw_payload["usage"] = token_usage
                self._store.finalize_turn_success(
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                    content=reply,
                    content_type="markdown",
                    raw_payload=raw_payload,
                )
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
            self._session_workspace_manager.update_status(session_workspace, "running")
            execution_runtime_hints = {
                **runtime_hints,
                **session_workspace.runtime_hints(),
            }
            execution_started = time.perf_counter()
            report = self._node_executor.execute(
                router_result.plan,
                artifacts=recent_artifacts,
                previous_node_results=[],
                runtime_hints=execution_runtime_hints,
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
            artifact_records = _publish_artifacts_from_report(
                report,
                turn_id=turn_id,
                session_workspace=session_workspace,
            )
            _persist_artifacts(self._store, turn.conversation_id, artifact_records)
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
                runtime_hints=execution_runtime_hints,
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
            raw_payload = {
                "source": "task_runtime",
                "route": router_result.route,
                "fast_intent": router_result.fast_intent.model_dump(mode="json"),
                "plan": router_result.plan.model_dump(mode="json"),
                "execution_report": report.model_dump(mode="json", exclude_none=True),
                "aggregation": aggregation.model_dump(mode="json", exclude_none=True),
                "session_workspace": session_workspace.metadata(),
            }
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
            reply = append_usage_footer(aggregation.reply, token_usage)
            if usage_records:
                raw_payload["usage_records"] = usage_records
            if token_usage is not None:
                raw_payload["usage"] = token_usage
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
                    content=reply if status == "completed" else "",
                    content_type="markdown",
                    summary=reply,
                    attachments=artifact_resolution.attachments,
                    metadata={
                        "conversation_id": turn.conversation_id,
                        "turn_id": turn_id,
                        "aggregation_status": aggregation.status,
                        **({"approval_requests": approval_requests} if approval_requests else {}),
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


def _publish_artifacts_from_report(
    report: ExecutionReport,
    *,
    turn_id: int,
    session_workspace: SessionWorkspaceRef,
) -> list[ToolArtifact]:
    artifacts = _artifact_records_from_report(report, turn_id=turn_id, session_workspace=session_workspace)
    return _promote_tool_artifacts_to_session(artifacts, session_workspace)


def _artifact_records_from_report(
    report: ExecutionReport,
    *,
    turn_id: int,
    session_workspace: SessionWorkspaceRef,
) -> list[ToolArtifact]:
    artifacts: list[ToolArtifact] = []
    seen: set[str] = set()
    for result in report.node_results:
        for node_artifact in result.artifacts:
            artifact = _artifact_record_from_node_artifact(
                node_artifact,
                result=result,
                turn_id=turn_id,
                session_workspace=session_workspace,
            )
            if artifact is None:
                continue
            if artifact.artifact_id not in seen:
                seen.add(artifact.artifact_id)
                artifacts.append(artifact)
        for raw in _tool_artifact_payloads(result):
            artifact = artifact_from_payload(raw)
            if artifact is None:
                continue
            artifact = _normalize_tool_artifact(
                artifact,
                result=result,
                turn_id=turn_id,
                session_workspace=session_workspace,
            )
            if artifact is not None and artifact.artifact_id not in seen:
                seen.add(artifact.artifact_id)
                artifacts.append(artifact)
    return artifacts


def _tool_artifact_payloads(result: NodeResult) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    payloads.extend(item for item in result.tool_artifacts if isinstance(item, dict))
    raw_items = result.data.get("tool_artifacts")
    if isinstance(raw_items, list):
        payloads.extend(item for item in raw_items if isinstance(item, dict))
    raw_calls = result.tool_calls or result.data.get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            call_artifacts = call.get("tool_artifacts")
            if isinstance(call_artifacts, list):
                payloads.extend(item for item in call_artifacts if isinstance(item, dict))
    return payloads


def _artifact_record_from_node_artifact(
    node_artifact: NodeArtifact,
    *,
    result: NodeResult,
    turn_id: int,
    session_workspace: SessionWorkspaceRef,
) -> ToolArtifact | None:
    if not node_artifact.publish:
        return None
    path_info = _resolve_session_artifact_path(
        node_artifact.session_relative_path or node_artifact.path,
        session_workspace=session_workspace,
        allow_absolute=False,
    )
    if path_info is None and node_artifact.kind in {"file", "image", "log", "directory"}:
        logger.warning(
            "node artifact skipped node_id=%s ref=%s reason=invalid_session_relative_path path=%s",
            result.node_id,
            node_artifact.ref,
            node_artifact.path,
        )
        return None
    absolute_path, relative_path = path_info if path_info is not None else (None, None)
    stat = _stat_file(absolute_path)
    metadata = dict(node_artifact.metadata)
    if relative_path:
        metadata.setdefault("session_relative_path", relative_path)
    metadata.setdefault("node_artifact_ref", node_artifact.ref)
    return ToolArtifact(
        artifact_id=node_artifact.artifact_id
        or _stable_session_artifact_id(session_workspace.session_id, result.node_id, node_artifact.ref, relative_path),
        kind=_artifact_kind(node_artifact.kind, absolute_path),
        turn_id=turn_id,
        tool_call_id=f"node:{result.node_id}",
        path=str(absolute_path) if absolute_path is not None else node_artifact.path,
        session_relative_path=relative_path,
        mime_type=node_artifact.mime_type or (_guess_mime(absolute_path) if absolute_path is not None else None),
        filename=node_artifact.filename or node_artifact.name or (absolute_path.name if absolute_path is not None else None),
        size_bytes=node_artifact.size_bytes or (stat.st_size if stat is not None else None),
        source_tool=node_artifact.source_tool or result.runtime,
        node_id=result.node_id,
        publish=node_artifact.publish,
        metadata=metadata,
    )


def _normalize_tool_artifact(
    artifact: ToolArtifact,
    *,
    result: NodeResult,
    turn_id: int,
    session_workspace: SessionWorkspaceRef,
) -> ToolArtifact | None:
    updates: dict[str, Any] = {}
    if artifact.turn_id is None:
        updates["turn_id"] = turn_id
    if not artifact.tool_call_id:
        updates["tool_call_id"] = f"node:{result.node_id}"
    if not artifact.source_tool:
        provider = str(result.debug.get("provider") or result.data.get("provider") or "")
        updates["source_tool"] = "coder" if result.runtime in {"coder", "codex"} and provider in {"", "codex"} else result.runtime
    if artifact.node_id is None:
        updates["node_id"] = result.node_id
    path_info = _resolve_session_artifact_path(
        artifact.session_relative_path or artifact.path,
        session_workspace=session_workspace,
        allow_absolute=True,
    )
    if path_info is not None:
        absolute_path, relative_path = path_info
        updates["path"] = str(absolute_path)
        updates["session_relative_path"] = relative_path
        metadata = dict(artifact.metadata)
        metadata.setdefault("session_relative_path", relative_path)
        updates["metadata"] = metadata
        stat = _stat_file(absolute_path)
        if artifact.size_bytes is None and stat is not None:
            updates["size_bytes"] = stat.st_size
        if not artifact.filename:
            updates["filename"] = absolute_path.name
        if not artifact.mime_type:
            updates["mime_type"] = _guess_mime(absolute_path)
    return replace(artifact, **updates) if updates else artifact


def _promote_tool_artifacts_to_session(
    artifacts: list[ToolArtifact],
    session_workspace: SessionWorkspaceRef,
) -> list[ToolArtifact]:
    return [_promote_tool_artifact_to_session(artifact, session_workspace) for artifact in artifacts]


def _promote_tool_artifact_to_session(
    artifact: ToolArtifact,
    session_workspace: SessionWorkspaceRef,
) -> ToolArtifact:
    if not artifact.publish:
        return artifact
    if artifact.kind not in {"image", "file"} or not artifact.path:
        return artifact
    try:
        source = Path(artifact.path).expanduser().resolve(strict=True)
    except OSError:
        return artifact
    if not source.is_file():
        return artifact
    artifacts_dir = session_workspace.artifacts_dir.resolve()
    try:
        source.relative_to(artifacts_dir)
        return _with_session_relative_path(artifact, source, session_workspace)
    except ValueError:
        pass

    target = (artifacts_dir / _session_artifact_filename(artifact, source)).resolve()
    try:
        target.relative_to(artifacts_dir)
    except ValueError:
        logger.warning("session artifact promotion target escaped artifacts dir artifact_id=%s", artifact.artifact_id)
        return artifact

    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        stat = target.stat()
    except OSError:
        logger.warning("session artifact promotion failed artifact_id=%s source=%s", artifact.artifact_id, source, exc_info=True)
        return artifact

    metadata = dict(artifact.metadata)
    metadata.update(
        {
            "session_id": session_workspace.session_id,
            "session_artifacts_dir": str(session_workspace.artifacts_dir),
            "source_path": str(source),
            "source_session_relative_path": artifact.session_relative_path,
            "promoted_to_session_artifacts": True,
        }
    )
    session_relative_path = _session_relative(target, session_workspace.root_path)
    return replace(
        artifact,
        path=str(target),
        session_relative_path=session_relative_path,
        filename=target.name,
        size_bytes=stat.st_size,
        metadata=metadata,
    )


def _session_artifact_filename(artifact: ToolArtifact, source: Path) -> str:
    raw_name = artifact.filename or source.name or "artifact"
    raw_path = Path(raw_name)
    suffix = raw_path.suffix or source.suffix
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix)[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_path.stem).strip("._-")[:80] or "artifact"
    digest_source = f"{artifact.artifact_id}|{source}"
    digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{stem}-{digest}{suffix}"


def _with_session_relative_path(
    artifact: ToolArtifact,
    path: Path,
    session_workspace: SessionWorkspaceRef,
) -> ToolArtifact:
    relative = _session_relative(path, session_workspace.root_path)
    if artifact.session_relative_path == relative:
        return artifact
    metadata = dict(artifact.metadata)
    metadata.setdefault("session_relative_path", relative)
    return replace(artifact, session_relative_path=relative, metadata=metadata)


def _resolve_session_artifact_path(
    path_text: str | None,
    *,
    session_workspace: SessionWorkspaceRef,
    allow_absolute: bool,
) -> tuple[Path, str] | None:
    text = str(path_text or "").strip()
    if not text:
        return None
    path = Path(text)
    session_root = session_workspace.root_path.resolve()
    try:
        if path.is_absolute():
            if not allow_absolute:
                return None
            resolved = path.expanduser().resolve(strict=True)
        else:
            if any(part == ".." for part in path.parts):
                return None
            resolved = (session_root / path).resolve(strict=True)
        relative = resolved.relative_to(session_root)
    except (OSError, ValueError):
        return None
    return resolved, relative.as_posix()


def _session_relative(path: Path, session_root: Path) -> str | None:
    try:
        return path.resolve().relative_to(session_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _stat_file(path: Path | None):
    if path is None:
        return None
    try:
        return path.stat() if path.is_file() else None
    except OSError:
        return None


def _guess_mime(path: Path | None) -> str | None:
    if path is None:
        return None
    return mimetypes.guess_type(str(path))[0]


def _artifact_kind(kind: str, path: Path | None) -> Any:
    normalized = str(kind or "").strip().lower()
    if normalized in {"image", "file", "directory", "log", "git_ref"}:
        return normalized
    if path is not None and path.is_dir():
        return "directory"
    if path is not None and _guess_mime(path or None) in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"}:
        return "image"
    return "file" if path is not None else "git_ref"


def _stable_session_artifact_id(session_id: str, node_id: str, ref: str, relative_path: str | None) -> str:
    identity = relative_path or ref
    digest = hashlib.sha256(f"{session_id}|{node_id}|{identity}".encode("utf-8", errors="replace")).hexdigest()[:16]
    safe_ref = re.sub(r"[^A-Za-z0-9._:-]+", "_", ref).strip("._:-")[:48] or "artifact"
    return f"{session_id}:{node_id}:{safe_ref}:{digest}"


def _persist_artifacts(store: ConversationStore, conversation_id: int, artifacts: list[Any]) -> None:
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


def _approval_requests_from_aggregation(aggregation: AggregationResult) -> list[dict[str, Any]]:
    if aggregation.approval_requests:
        return approval_request_dicts(aggregation.approval_requests)
    raw = aggregation.data.get("approval_requests")
    if not isinstance(raw, list):
        return []
    return approval_request_dicts(raw)
