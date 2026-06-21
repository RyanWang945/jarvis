from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.progress import ProgressReporter, ensure_progress
from app.task_runtime.node_execute_runtime import NodeExecuteRuntime, NodeExecutionContext
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeError, NodeResult, ResolvedInput
from app.task_runtime.planner import ExecutionPlan, NodeRuntime, PlanNode
from app.task_runtime.runtime_context import RuntimeContext
from app.task_runtime.session_workspace import (
    SessionWorkspaceRef,
    node_workspace_legacy_hints,
    write_node_input_snapshot,
    write_node_result,
)

logger = logging.getLogger(__name__)


@dataclass
class NodeExecutor:
    runtimes: dict[NodeRuntime, NodeExecuteRuntime] = field(default_factory=dict)

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        artifacts: list[dict[str, Any]] | None = None,
        previous_node_results: list[dict[str, Any] | NodeResult] | None = None,
        runtime_hints: dict[str, Any] | None = None,
        instructions: list[str] | None = None,
        progress: ProgressReporter | None = None,
        session_workspace: SessionWorkspaceRef | None = None,
    ) -> ExecutionReport:
        progress = ensure_progress(progress)
        base_runtime_context = RuntimeContext.from_hints(runtime_hints)
        artifact_index = _artifact_index(artifacts or [])
        result_index = _previous_result_index(previous_node_results or [])
        completed_order: list[str] = []
        pending = {node.id: node for node in plan.nodes}
        results: list[NodeResult] = []
        logger.info(
            "node executor start objective_len=%s node_count=%s artifact_count=%s previous_result_count=%s",
            len(plan.user_objective),
            len(plan.nodes),
            len(artifact_index),
            len(result_index),
        )

        while pending:
            progressed = False
            for node in list(pending.values()):
                resolved_inputs, missing_refs, blocked_refs = _resolve_inputs(node, artifact_index, result_index)
                if missing_refs or blocked_refs:
                    logger.info(
                        "node executor waiting node_id=%s runtime=%s missing_refs=%s blocked_refs=%s",
                        node.id,
                        node.runtime,
                        missing_refs,
                        blocked_refs,
                    )
                    continue
                runtime = self.runtimes.get(node.runtime)
                node_started = time.perf_counter()
                logger.info(
                    "node executor node start node_id=%s runtime=%s input_refs=%s resolved_input_count=%s",
                    node.id,
                    node.runtime,
                    node.input_refs,
                    len(resolved_inputs),
                )
                progress.emit(
                    "node_started",
                    turn_id=base_runtime_context.turn.turn_id,
                    conversation_id=base_runtime_context.turn.conversation_id,
                    stage="execution",
                    node_id=node.id,
                    status="running",
                    summary=f"开始执行 {node.runtime} 节点：{node.objective}",
                    data={
                        "runtime": node.runtime,
                        "input_refs": list(node.input_refs),
                        "resolved_input_count": len(resolved_inputs),
                    },
                )
                node_runtime_context = _node_runtime_context(base_runtime_context, session_workspace, node.id)
                merged_runtime_hints = node_runtime_context.to_legacy_hints()
                node_workspace = session_workspace.node(node.id) if session_workspace is not None else None
                if node_workspace is not None:
                    write_node_input_snapshot(
                        node_workspace,
                        user_objective=plan.user_objective,
                        node=node,
                        resolved_inputs=resolved_inputs,
                        runtime_hints=merged_runtime_hints,
                        instructions=list(instructions or []),
                    )
                if runtime is None:
                    result = _blocked_result(
                        node,
                        "runtime_not_available",
                        f"No NodeExecuteRuntime registered for runtime: {node.runtime}",
                    )
                else:
                    result = runtime.run(
                        NodeExecutionContext(
                            user_objective=plan.user_objective,
                            node=node,
                            resolved_inputs=resolved_inputs,
                            runtime_hints=merged_runtime_hints,
                            instructions=list(instructions or []),
                        )
                    )
                if node_workspace is not None:
                    write_node_result(node_workspace, result)
                logger.info(
                    "node executor node finished node_id=%s runtime=%s status=%s artifact_count=%s elapsed_ms=%s summary_preview=%s",
                    result.node_id,
                    result.runtime,
                    result.status,
                    len(result.artifacts),
                    int((time.perf_counter() - node_started) * 1000),
                    _preview(result.summary),
                )
                progress.emit(
                    "node_failed" if result.status != "completed" else "node_completed",
                    turn_id=base_runtime_context.turn.turn_id,
                    conversation_id=base_runtime_context.turn.conversation_id,
                    stage="execution",
                    node_id=result.node_id,
                    status=result.status,
                    summary=f"{node.runtime} 节点 {result.status}: {_preview(result.summary, limit=120)}",
                    data={
                        "runtime": result.runtime,
                        "artifact_count": len(result.artifacts),
                        "elapsed_ms": int((time.perf_counter() - node_started) * 1000),
                    },
                )
                results.append(result)
                result_index[f"node:{node.id}"] = result
                completed_order.append(node.id)
                pending.pop(node.id)
                progressed = True
            if not progressed:
                for node in list(pending.values()):
                    _, missing_refs, blocked_refs = _resolve_inputs(node, artifact_index, result_index)
                    message = _blocked_message(missing_refs, blocked_refs)
                    result = _blocked_result(node, "unresolved_input_refs", message)
                    if session_workspace is not None:
                        node_workspace = session_workspace.node(node.id)
                        merged_runtime_hints = _node_runtime_context(base_runtime_context, session_workspace, node.id).to_legacy_hints()
                        write_node_input_snapshot(
                            node_workspace,
                            user_objective=plan.user_objective,
                            node=node,
                            resolved_inputs=[],
                            runtime_hints=merged_runtime_hints,
                            instructions=list(instructions or []),
                            missing_refs=missing_refs,
                            blocked_refs=blocked_refs,
                        )
                        write_node_result(node_workspace, result)
                    logger.warning(
                        "node executor node blocked node_id=%s runtime=%s missing_refs=%s blocked_refs=%s message=%s",
                        node.id,
                        node.runtime,
                        missing_refs,
                        blocked_refs,
                        message,
                    )
                    progress.emit(
                        "node_failed",
                        turn_id=base_runtime_context.turn.turn_id,
                        conversation_id=base_runtime_context.turn.conversation_id,
                        stage="execution",
                        node_id=node.id,
                        status="blocked",
                        summary=message,
                        data={"runtime": node.runtime, "missing_refs": missing_refs, "blocked_refs": blocked_refs},
                    )
                    results.append(result)
                    result_index[f"node:{node.id}"] = result
                    pending.pop(node.id)
                break

        report = ExecutionReport(
            status=_execution_status(results),
            node_results=results,
            data={"completed_order": completed_order},
        )
        logger.info(
            "node executor finished status=%s completed_order=%s node_count=%s",
            report.status,
            completed_order,
            len(results),
        )
        return report


def _resolve_inputs(
    node: PlanNode,
    artifact_index: dict[str, NodeArtifact],
    result_index: dict[str, NodeResult],
) -> tuple[list[ResolvedInput], list[str], list[str]]:
    resolved: list[ResolvedInput] = []
    missing: list[str] = []
    blocked: list[str] = []
    for ref in node.input_refs:
        if ref.startswith("artifact:"):
            artifact = artifact_index.get(ref)
            if artifact is None:
                missing.append(ref)
            else:
                resolved.append(
                    ResolvedInput(
                        ref=ref,
                        kind="artifact",
                        summary=artifact.description,
                        artifacts=[artifact],
                        data=artifact.metadata,
                    )
                )
            continue
        if ref.startswith("node:"):
            result = result_index.get(ref)
            if result is None:
                missing.append(ref)
            elif result.status != "completed":
                blocked.append(ref)
            else:
                resolved.append(
                    ResolvedInput(
                        ref=ref,
                        kind="node_result",
                        summary=result.summary,
                        artifacts=result.artifacts,
                        data=result.data,
                        source_status=result.status,
                    )
                )
            continue
        missing.append(ref)
    return resolved, missing, blocked


def _node_runtime_context(
    base_context: RuntimeContext,
    session_workspace: SessionWorkspaceRef | None,
    node_id: str,
) -> RuntimeContext:
    if session_workspace is None:
        return base_context
    return base_context.with_hints(session_workspace.to_legacy_hints()).with_hints(
        node_workspace_legacy_hints(session_workspace, node_id)
    )


def _artifact_index(artifacts: list[dict[str, Any]]) -> dict[str, NodeArtifact]:
    result: dict[str, NodeArtifact] = {}
    for index, artifact in enumerate(artifacts, start=1):
        if not isinstance(artifact, dict):
            continue
        ref = str(artifact.get("ref") or artifact.get("id") or artifact.get("artifact_id") or f"A{index}").strip().removeprefix("artifact:")
        if not ref:
            continue
        node_artifact = NodeArtifact(
            ref=ref,
            artifact_id=_optional_str(artifact.get("artifact_id") or artifact.get("id")),
            kind=str(artifact.get("kind") or artifact.get("type") or "artifact"),
            name=_optional_str(artifact.get("name") or artifact.get("filename") or artifact.get("title")),
            description=str(artifact.get("description") or artifact.get("summary") or ""),
            path=_optional_str(artifact.get("path")),
            session_relative_path=_optional_str(artifact.get("session_relative_path")),
            mime_type=_optional_str(artifact.get("mime_type")),
            filename=_optional_str(artifact.get("filename")),
            size_bytes=_optional_int(artifact.get("size_bytes")),
            source_tool=_optional_str(artifact.get("source_tool")) or "",
            publish=_optional_bool(artifact.get("publish"), default=True),
            metadata={
                key: value
                for key, value in artifact.items()
                if key
                not in {
                    "ref",
                    "id",
                    "artifact_id",
                    "kind",
                    "type",
                    "name",
                    "filename",
                    "title",
                    "description",
                    "summary",
                    "path",
                    "session_relative_path",
                    "mime_type",
                    "size_bytes",
                    "source_tool",
                    "publish",
                }
            },
        )
        result[f"artifact:{node_artifact.ref}"] = node_artifact
    return result


def _previous_result_index(values: list[dict[str, Any] | NodeResult]) -> dict[str, NodeResult]:
    result: dict[str, NodeResult] = {}
    for value in values:
        node_result = value if isinstance(value, NodeResult) else _node_result_from_mapping(value)
        if node_result is not None:
            result[f"node:{node_result.node_id}"] = node_result
    return result


def _node_result_from_mapping(value: dict[str, Any]) -> NodeResult | None:
    if not isinstance(value, dict):
        return None
    node_id = value.get("node_id") or value.get("id")
    runtime = value.get("runtime")
    if not isinstance(node_id, str) or not node_id.strip() or runtime not in {"llm", "react", "coder"}:
        return None
    status = value.get("status") if value.get("status") in {"completed", "failed", "blocked"} else "completed"
    return NodeResult(
        node_id=node_id.strip(),
        runtime=runtime,
        status=status,
        summary=str(value.get("summary") or ""),
        artifacts=[_artifact_from_mapping(item) for item in value.get("artifacts", []) if isinstance(item, dict)],
        approval_requests=value.get("approval_requests") if isinstance(value.get("approval_requests"), list) else [],
        tool_calls=value.get("tool_calls") if isinstance(value.get("tool_calls"), list) else [],
        usage_records=value.get("usage_records") if isinstance(value.get("usage_records"), list) else [],
        git=value.get("git") if isinstance(value.get("git"), dict) else {},
        debug=value.get("debug") if isinstance(value.get("debug"), dict) else {},
        data=value.get("data") if isinstance(value.get("data"), dict) else {},
    )


def _artifact_from_mapping(value: dict[str, Any]) -> NodeArtifact:
    return NodeArtifact(
        ref=str(value.get("ref") or value.get("id") or value.get("artifact_id") or "artifact"),
        artifact_id=_optional_str(value.get("artifact_id") or value.get("id")),
        kind=str(value.get("kind") or value.get("type") or "artifact"),
        name=_optional_str(value.get("name") or value.get("filename")),
        description=str(value.get("description") or value.get("summary") or ""),
        path=_optional_str(value.get("path")),
        session_relative_path=_optional_str(value.get("session_relative_path")),
        mime_type=_optional_str(value.get("mime_type")),
        filename=_optional_str(value.get("filename")),
        size_bytes=_optional_int(value.get("size_bytes")),
        source_tool=_optional_str(value.get("source_tool")) or "",
        publish=_optional_bool(value.get("publish"), default=True),
        metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
    )


def _blocked_result(node: PlanNode, code: str, message: str) -> NodeResult:
    return NodeResult(
        node_id=node.id,
        runtime=node.runtime,
        status="blocked",
        summary=message,
        error=NodeError(code=code, message=message),
    )


def _blocked_message(missing_refs: list[str], blocked_refs: list[str]) -> str:
    parts: list[str] = []
    if missing_refs:
        parts.append("missing input refs: " + ", ".join(missing_refs))
    if blocked_refs:
        parts.append("blocked input refs: " + ", ".join(blocked_refs))
    return "; ".join(parts) or "node inputs are not ready"


def _execution_status(results: list[NodeResult]) -> str:
    if any(result.status == "failed" for result in results):
        return "failed"
    if any(result.status == "blocked" for result in results):
        return "blocked"
    return "completed"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "external", "publish"}:
        return True
    if text in {"0", "false", "no", "internal", "none"}:
        return False
    return default


def _preview(value: Any, *, limit: int = 240) -> str:
    text = str(value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
