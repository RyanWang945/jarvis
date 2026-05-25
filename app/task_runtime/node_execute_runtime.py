from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult, ResolvedInput
from app.task_runtime.planner import PlanNode
from app.tools.codex import run_codex_coder_tool
from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.runtime import build_llm_tools, execute_tool, get_tool_definition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeExecutionContext:
    user_objective: str
    node: PlanNode
    resolved_inputs: list[ResolvedInput] = field(default_factory=list)
    runtime_hints: dict[str, Any] = field(default_factory=dict)
    instructions: list[str] = field(default_factory=list)


class NodeExecuteRuntime(Protocol):
    def run(self, context: NodeExecutionContext) -> NodeResult: ...


class LLMNodeExecuteRuntime:
    """Plain LLM runtime for one node without tool execution."""

    def __init__(self, *, model_resolver=None) -> None:
        self._model_resolver = model_resolver or (lambda context: ModelRouter().resolve(LLMNode.AGENT_STEP, None))

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        resolved = self._model_resolver(context)
        if not resolved.profile.api_key:
            logger.info("llm node skipped node_id=%s reason=missing_api_key profile=%s", context.node.id, getattr(resolved.profile, "id", None))
            return _blocked(context.node, "missing_api_key", "LLM runtime API key is not configured.")
        messages = _llm_messages(context)
        response_format = {"type": "json_object"} if resolved.profile.supports_json_object else None
        try:
            logger.info(
                "llm node request node_id=%s model_profile=%s response_format=%s resolved_input_count=%s",
                context.node.id,
                getattr(resolved.profile, "id", None),
                response_format,
                len(context.resolved_inputs),
            )
            response = resolved.client.chat_normalized(messages, response_format=response_format)
            payload = parse_json_content({"content": response.content}) if response_format else {}
        except Exception as exc:
            logger.exception("llm node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "llm_runtime_error", str(exc), retryable=True)
        summary = str(payload.get("summary") or payload.get("answer") or response.content or "").strip()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        logger.info(
            "llm node completed node_id=%s model=%s finish_reason=%s summary_len=%s elapsed_ms=%s",
            context.node.id,
            response.model,
            response.finish_reason,
            len(summary),
            int((time.perf_counter() - started) * 1000),
        )
        return NodeResult(
            node_id=context.node.id,
            runtime="llm",
            status="completed",
            summary=summary or "LLM runtime completed.",
            data=data,
            artifacts=_artifacts_from_payload(payload),
        )


class ReactNodeExecuteRuntime:
    """Tool-using research runtime for one plan node."""

    DEFAULT_ALLOWED_TOOLS = ("tavily_search", "business_knowledge_search", "obsidian_wiki_query")

    def __init__(
        self,
        *,
        model_resolver=None,
        tool_runner=execute_tool,
        allowed_tools: tuple[str, ...] | None = None,
        max_steps: int = 6,
        tool_timeout_seconds: int = 60,
    ) -> None:
        self._model_resolver = model_resolver or (lambda context: ModelRouter().resolve(LLMNode.AGENT_STEP, None))
        self._tool_runner = tool_runner
        self._allowed_tools = tuple(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self._max_steps = max(2, int(max_steps))
        self._tool_timeout_seconds = max(1, int(tool_timeout_seconds))

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        resolved = self._model_resolver(context)
        if not resolved.profile.api_key:
            logger.info("react node skipped node_id=%s reason=missing_api_key profile=%s", context.node.id, getattr(resolved.profile, "id", None))
            return _blocked(context.node, "missing_api_key", "React runtime LLM API key is not configured.")
        messages = _react_messages(context)
        tools = build_llm_tools(allowed_tools=self._allowed_tools)
        tool_calls: list[dict[str, Any]] = []
        response: NormalizedLLMResponse | None = None
        try:
            for step_index in range(1, self._max_steps + 1):
                force_final = step_index == self._max_steps
                response_format = {"type": "json_object"} if force_final and resolved.profile.supports_json_object else None
                logger.info(
                    "react node llm step start node_id=%s step=%s force_final=%s allowed_tools=%s tool_observation_count=%s",
                    context.node.id,
                    step_index,
                    force_final,
                    self._allowed_tools,
                    len(tool_calls),
                )
                response = resolved.client.chat_normalized(
                    messages,
                    tools=None if force_final else tools,
                    tool_choice=None if force_final else "auto",
                    response_format=response_format,
                )
                if response.tool_calls:
                    logger.info(
                        "react node llm proposed tools node_id=%s step=%s tools=%s",
                        context.node.id,
                        step_index,
                        [{"id": item.id, "name": item.name, "args": item.args} for item in response.tool_calls],
                    )
                    messages.append(_assistant_tool_call_message(response))
                    for tool_call in response.tool_calls:
                        observation, record = self._run_tool_call(tool_call)
                        tool_calls.append(record)
                        messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                content=json.dumps(observation, ensure_ascii=False),
                            )
                        )
                    continue
                result = _react_result_from_response(context, response, tool_calls)
                logger.info(
                    "react node completed node_id=%s status=%s tool_call_count=%s summary_len=%s elapsed_ms=%s",
                    context.node.id,
                    result.status,
                    len(tool_calls),
                    len(result.summary),
                    int((time.perf_counter() - started) * 1000),
                )
                return result
        except Exception as exc:
            logger.exception("react node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "react_runtime_error", str(exc), retryable=True)
        if response is None:
            return _failed(context.node, "react_runtime_no_response", "React runtime finished without an LLM response.", retryable=True)
        result = _react_result_from_response(context, response, tool_calls)
        logger.info(
            "react node completed node_id=%s status=%s tool_call_count=%s summary_len=%s elapsed_ms=%s",
            context.node.id,
            result.status,
            len(tool_calls),
            len(result.summary),
            int((time.perf_counter() - started) * 1000),
        )
        return result

    def _run_tool_call(self, tool_call: NormalizedToolCall) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "id": tool_call.id,
            "tool_name": tool_call.name,
            "args": tool_call.args,
        }
        if tool_call.name not in self._allowed_tools:
            message = f"tool not allowed for ReactNodeExecuteRuntime: {tool_call.name}"
            record.update({"status": "rejected", "summary": message})
            logger.info("react node tool rejected tool=%s tool_call_id=%s reason=%s", tool_call.name, tool_call.id, message)
            return {"ok": False, "status": "rejected", "error": message}, record
        try:
            tool = get_tool_definition(tool_call.name)
        except Exception as exc:
            message = str(exc)
            record.update({"status": "failed", "summary": message})
            logger.warning("react node tool definition failed tool=%s tool_call_id=%s error=%s", tool_call.name, tool_call.id, message)
            return {"ok": False, "status": "failed", "error": message}, record
        try:
            logger.info("react node tool start tool=%s tool_call_id=%s args=%s", tool_call.name, tool_call.id, tool_call.args)
            result = self._tool_runner(tool, tool_call.args, timeout_seconds=self._tool_timeout_seconds)
        except Exception as exc:
            message = str(exc)
            record.update({"status": "failed", "summary": message})
            logger.exception("react node tool failed tool=%s tool_call_id=%s elapsed_ms=%s", tool_call.name, tool_call.id, int((time.perf_counter() - started) * 1000))
            return {"ok": False, "status": "failed", "error": message}, record

        status = "completed" if result.ok else "failed"
        logger.info(
            "react node tool finished tool=%s tool_call_id=%s status=%s exit_code=%s artifact_count=%s elapsed_ms=%s summary=%s",
            tool_call.name,
            tool_call.id,
            status,
            result.exit_code,
            len(result.tool_artifacts) + len(result.artifacts),
            int((time.perf_counter() - started) * 1000),
            _truncate(result.summary, limit=300),
        )
        record.update(
            {
                "status": status,
                "summary": result.summary,
                "exit_code": result.exit_code,
                "artifacts": list(result.artifacts),
                "tool_artifacts": [_tool_artifact_dict(item) for item in result.tool_artifacts],
            }
        )
        observation = {
            "ok": result.ok,
            "status": status,
            "tool_name": tool.name,
            "summary": result.summary,
            "stdout": _truncate(result.stdout),
            "stderr": _truncate(result.stderr),
            "artifacts": list(result.artifacts),
            "tool_artifacts": [_tool_artifact_dict(item) for item in result.tool_artifacts],
        }
        return observation, record


class CodexNodeExecuteRuntime:
    """Adapter from the new node-runtime interface to the existing Codex tool."""

    def __init__(self, runner=run_codex_coder_tool) -> None:
        self._runner = runner

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        repo_id = _active_repo(context)
        if not repo_id:
            logger.info("codex node blocked node_id=%s reason=missing_active_repo", context.node.id)
            return _blocked(context.node, "missing_active_repo", "Codex runtime requires runtime_hints.active_repo.")
        request = ToolExecutionRequest(
            tool_name="delegate_to_codex",
            workdir=None,
            args={
                "instruction": _codex_instruction(context),
                "repo_id": repo_id,
                "allow_commit": bool(context.runtime_hints.get("allow_commit")),
                "allow_push": bool(context.runtime_hints.get("allow_push")),
                "_read_only": bool(context.runtime_hints.get("codex_read_only", True)),
            },
            timeout_seconds=int(getattr(get_settings(), "coder_timeout_seconds", 1800)),
        )
        try:
            logger.info(
                "codex node start node_id=%s repo_id=%s allow_commit=%s allow_push=%s read_only=%s",
                context.node.id,
                repo_id,
                request.args.get("allow_commit"),
                request.args.get("allow_push"),
                request.args.get("_read_only"),
            )
            result = self._runner(request)
        except Exception as exc:
            logger.exception("codex node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "codex_runtime_error", str(exc), retryable=True)
        node_result = _node_result_from_tool(context.node, result)
        logger.info(
            "codex node finished node_id=%s status=%s exit_code=%s artifact_count=%s elapsed_ms=%s summary=%s",
            context.node.id,
            node_result.status,
            result.exit_code,
            len(result.artifacts) + len(result.tool_artifacts),
            int((time.perf_counter() - started) * 1000),
            _truncate(node_result.summary, limit=300),
        )
        return node_result


class ToolNodeExecuteRuntime:
    """Adapter for low-level single-tool plan nodes."""

    def __init__(self, *, tool_runner=execute_tool, timeout_seconds: int = 60) -> None:
        self._tool_runner = tool_runner
        self._timeout_seconds = max(1, int(timeout_seconds))

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        tool_name = context.node.tool_name
        if not tool_name:
            logger.info("tool node blocked node_id=%s reason=missing_tool_name", context.node.id)
            return _blocked(context.node, "missing_tool_name", "Tool runtime requires node.tool_name.")
        try:
            tool = get_tool_definition(tool_name)
        except Exception as exc:
            logger.warning("tool node unknown tool node_id=%s tool=%s error=%s", context.node.id, tool_name, exc)
            return _failed(context.node, "unknown_tool", str(exc), retryable=False)
        args = _tool_args(context)
        try:
            logger.info("tool node start node_id=%s tool=%s args=%s", context.node.id, tool_name, args)
            result = self._tool_runner(tool, args, timeout_seconds=self._timeout_seconds)
        except Exception as exc:
            logger.exception("tool node failed node_id=%s tool=%s elapsed_ms=%s", context.node.id, tool_name, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "tool_runtime_error", str(exc), retryable=True)
        logger.info(
            "tool node finished node_id=%s tool=%s ok=%s exit_code=%s artifact_count=%s elapsed_ms=%s summary=%s",
            context.node.id,
            tool_name,
            result.ok,
            result.exit_code,
            len(result.artifacts) + len(result.tool_artifacts),
            int((time.perf_counter() - started) * 1000),
            _truncate(result.summary, limit=300),
        )
        return NodeResult(
            node_id=context.node.id,
            runtime="tool",
            status="completed" if result.ok else "failed",
            summary=result.summary or result.stdout or result.stderr or f"Tool {tool_name} finished.",
            artifacts=[_artifact_from_tool_string(item) for item in result.artifacts],
            data={
                "tool_name": tool_name,
                "args": args,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "tool_artifacts": [_tool_artifact_dict(item) for item in result.tool_artifacts],
            },
            error=None if result.ok else NodeError(code="tool_failed", message=result.summary or result.stderr, retryable=False),
        )


def _llm_messages(context: NodeExecutionContext) -> list[LLMMessage]:
    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
        "temporal_context": _temporal_context(context.runtime_hints),
        "runtime_hints": context.runtime_hints,
        "instructions": context.instructions,
    }
    return [
        LLMMessage(
            role="system",
            content=(
                "You are Jarvis LLMNodeExecuteRuntime. Execute one plan node without tools. "
                "Do not produce a final user reply unless the node objective itself is the whole answer. "
                "Use the temporal_context payload as the authoritative current date/time for relative-time wording. "
                "Write summary in the user's language when the node output is user-facing. "
                "Return JSON with summary, optional data, and optional artifacts."
            ),
        ),
        LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def _react_messages(context: NodeExecutionContext) -> list[LLMMessage]:
    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
        "temporal_context": _temporal_context(context.runtime_hints),
        "runtime_hints": context.runtime_hints,
        "instructions": context.instructions,
    }
    return [
        LLMMessage(
            role="system",
            content=(
                "You are Jarvis ReactNodeExecuteRuntime. Execute one research/lookup node only. "
                "Use tools when external, business, or project-memory evidence is needed. "
                "Use the temporal_context payload as the authoritative current date/time; convert relative terms "
                "such as today, current, latest, recent, 今天, 当前, 最新, 最近 into concrete date constraints when searching. "
                "Do not perform code edits or repository workflows. Do not produce a final user reply. "
                "After tool use, return JSON with summary, findings, sources, and data. "
                "Be concise and preserve useful evidence for downstream nodes."
            ),
        ),
        LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def _assistant_tool_call_message(response: NormalizedLLMResponse) -> LLMMessage:
    return LLMMessage(
        role="assistant",
        content=response.content or "",
        tool_calls=[
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.args, ensure_ascii=False),
                },
            }
            for tool_call in response.tool_calls
        ],
        reasoning_content=response.reasoning_content,
    )


def _react_result_from_response(
    context: NodeExecutionContext,
    response: NormalizedLLMResponse,
    tool_calls: list[dict[str, Any]],
) -> NodeResult:
    payload = parse_json_content({"content": response.content})
    summary = _react_summary(payload, response.content)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data.update(_react_extra_data(payload))
    findings = payload.get("findings")
    sources = payload.get("sources")
    if isinstance(findings, list):
        data.setdefault("findings", findings)
    else:
        data.setdefault("findings", [])
    if isinstance(sources, list):
        data.setdefault("sources", sources)
    else:
        data.setdefault("sources", [])
    data["tool_calls"] = tool_calls
    return NodeResult(
        node_id=context.node.id,
        runtime="react",
        status="completed",
        summary=summary or "React runtime completed.",
        data=data,
        artifacts=_artifacts_from_payload(payload),
    )


def _react_summary(payload: dict[str, Any], response_content: str) -> str:
    for key in ("summary", "answer", "result", "final_answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    findings = payload.get("findings")
    if isinstance(findings, list):
        summary = _summary_from_list(findings)
        if summary:
            return summary
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("summary", "answer", "result", "final_answer"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        summary = _summary_from_mapping(data)
        if summary:
            return summary
    summary = _summary_from_mapping(_react_extra_data(payload))
    if summary:
        return summary
    return str(response_content or "").strip()


def _react_extra_data(payload: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "summary",
        "answer",
        "result",
        "final_answer",
        "data",
        "findings",
        "sources",
        "artifacts",
    }
    return {key: value for key, value in payload.items() if key not in reserved}


def _summary_from_mapping(value: dict[str, Any]) -> str:
    for key in ("findings", "candidates", "items", "results"):
        items = value.get(key)
        if isinstance(items, list):
            summary = _summary_from_list(items)
            if summary:
                return summary
    if not value:
        return ""
    return _truncate(json.dumps(value, ensure_ascii=False), limit=1200)


def _summary_from_list(items: list[Any]) -> str:
    lines: list[str] = []
    for item in items[:5]:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = _summary_from_item(item)
        else:
            text = str(item).strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _summary_from_item(item: dict[str, Any]) -> str:
    label = _first_item_text(item, ("name", "title", "candidate"))
    summary = _first_item_text(item, ("summary", "answer", "claim", "result"))
    if label and summary and label != summary:
        return f"{label}: {summary}"
    if label or summary:
        return label or summary or ""
    for key in ("summary", "answer", "title", "name", "candidate", "claim", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    compact = {key: value for key, value in item.items() if key not in {"url", "source_url"}}
    return _truncate(json.dumps(compact or item, ensure_ascii=False), limit=400)


def _first_item_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _codex_instruction(context: NodeExecutionContext) -> str:
    sections = [
        "Execute one Jarvis plan node as CodexNodeExecuteRuntime.",
        "",
        _temporal_context_text(context.runtime_hints),
        "",
        f"User objective: {context.user_objective}",
        f"Node id: {context.node.id}",
        f"Node objective: {context.node.objective}",
        f"Expected output: {context.node.expected_output or 'Repository task result.'}",
    ]
    if context.resolved_inputs:
        sections.extend(["", "Resolved inputs:"])
        for item in context.resolved_inputs:
            sections.append(f"- {item.ref} ({item.kind}, status={item.source_status or 'n/a'}): {item.summary}")
            if item.data:
                sections.append(f"  data: {json.dumps(item.data, ensure_ascii=False)[:2000]}")
            if item.artifacts:
                artifact_refs = ", ".join(f"artifact:{artifact.ref}" for artifact in item.artifacts)
                sections.append(f"  artifacts: {artifact_refs}")
    if context.instructions:
        sections.extend(["", "Additional instructions:", *[f"- {item}" for item in context.instructions]])
    sections.extend(
        [
            "",
            "Return a concise result suitable for a NodeResult summary.",
            "Do not ask for routine confirmation. Respect permission limits and request approval through Codex only when required.",
        ]
    )
    return "\n".join(sections)


def _temporal_context(runtime_hints: dict[str, Any]) -> dict[str, str]:
    current_date = str(runtime_hints.get("current_date") or "").strip()
    current_time = str(runtime_hints.get("current_time") or "").strip()
    timezone = str(runtime_hints.get("timezone") or "").strip()
    return {
        key: value
        for key, value in {
            "current_date": current_date,
            "current_time": current_time,
            "timezone": timezone,
        }.items()
        if value
    }


def _temporal_context_text(runtime_hints: dict[str, Any]) -> str:
    temporal = _temporal_context(runtime_hints)
    if not temporal:
        return "Temporal context: unavailable; do not infer current dates from model memory."
    lines = ["Temporal context:"]
    if temporal.get("current_date"):
        lines.append(f"- Current date: {temporal['current_date']}")
    if temporal.get("current_time"):
        lines.append(f"- Current time: {temporal['current_time']}")
    if temporal.get("timezone"):
        lines.append(f"- Timezone: {temporal['timezone']}")
    lines.append("- Interpret today/current/latest/recent and 今天/当前/最新/最近 relative to this context.")
    return "\n".join(lines)


def _tool_args(context: NodeExecutionContext) -> dict[str, Any]:
    args = {
        "user_objective": context.user_objective,
        "node_objective": context.node.objective,
        "expected_output": context.node.expected_output,
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
    }
    if context.runtime_hints:
        args.update({key: value for key, value in context.runtime_hints.items() if key not in args})
    return args


def _node_result_from_tool(node: PlanNode, result: ToolExecutionResult) -> NodeResult:
    status = "completed" if result.ok else "failed"
    summary = _codex_node_summary(result)
    return NodeResult(
        node_id=node.id,
        runtime="codex",
        status=status,
        summary=summary,
        artifacts=[_artifact_from_tool_string(item) for item in result.artifacts],
        data={
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "tool_artifacts": [artifact.__dict__ for artifact in result.tool_artifacts],
        },
        error=None if result.ok else NodeError(code="codex_tool_failed", message=summary, retryable=False),
    )


def _codex_node_summary(result: ToolExecutionResult) -> str:
    if result.ok:
        return result.stdout or result.summary or "Codex runtime finished."
    return result.summary or result.stderr or result.stdout or "Codex runtime failed."


def _artifact_from_tool_string(value: str) -> NodeArtifact:
    text = str(value)
    kind, _, ref = text.partition(":")
    if not ref:
        kind = "artifact"
        ref = text
    return NodeArtifact(ref=ref, kind=kind or "artifact", name=ref)


def _artifacts_from_payload(payload: dict[str, Any]) -> list[NodeArtifact]:
    raw = payload.get("artifacts")
    if not isinstance(raw, list):
        return []
    artifacts: list[NodeArtifact] = []
    for item in raw:
        if isinstance(item, str):
            artifacts.append(_artifact_from_tool_string(item))
        elif isinstance(item, dict):
            ref = str(item.get("ref") or item.get("id") or item.get("artifact_id") or "").strip()
            if ref:
                artifacts.append(
                    NodeArtifact(
                        ref=ref,
                        kind=str(item.get("kind") or item.get("type") or "artifact"),
                        name=_optional_text(item.get("name") or item.get("filename") or item.get("title")),
                        description=str(item.get("description") or item.get("summary") or ""),
                        path=_optional_text(item.get("path")),
                        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                    )
                )
    return artifacts


def _tool_artifact_dict(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "turn_id": artifact.turn_id,
        "tool_call_id": artifact.tool_call_id,
        "path": artifact.path,
        "mime_type": artifact.mime_type,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "source_tool": artifact.source_tool,
        "metadata": artifact.metadata,
    }


def _truncate(value: str, *, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _active_repo(context: NodeExecutionContext) -> str | None:
    value = context.runtime_hints.get("active_repo")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _blocked(node: PlanNode, code: str, message: str) -> NodeResult:
    return NodeResult(node_id=node.id, runtime=node.runtime, status="blocked", summary=message, error=NodeError(code=code, message=message))


def _failed(node: PlanNode, code: str, message: str, *, retryable: bool = False) -> NodeResult:
    return NodeResult(
        node_id=node.id,
        runtime=node.runtime,
        status="failed",
        summary=message,
        error=NodeError(code=code, message=message, retryable=retryable),
    )
