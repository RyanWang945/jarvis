"""Claude Agent SDK React runtime for DAG node execution.

Replaces the hand-rolled ReAct loop with Claude Agent SDK's managed agent
loop, while delegating to DeepSeek (or any Anthropic-compatible endpoint)
for LLM inference.  Custom Jarvis tools are injected via the MCP bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import Settings, get_settings
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.observability import add_event, content_capture_enabled, set_attributes, trace_preview
from app.prompting import PromptRegistry
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult
from app.task_runtime.planner import PlanNode
from app.task_runtime.runtime_context import RuntimeContext
from app.tools.runtime import list_tool_definitions

logger = logging.getLogger(__name__)

# Internal error codes
_CODE_MISSING_API_KEY = "react_missing_api_key"
_CODE_RUNTIME_ERROR = "react_runtime_error"
_CODE_NO_RESPONSE = "react_no_response"
_CODE_TIMEOUT = "react_timeout"
_CODE_MAX_TURNS_NO_FINAL_OUTPUT = "react_max_turns_no_final_output"
_REACT_TIMEOUT_SECONDS = 900  # 15 minutes
_FINALIZE_TIMEOUT_SECONDS = 180  # 3 minutes
_FINALIZE_MAX_TURNS = 1
_CLAUDE_NATIVE_MUTATION_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
_CLAUDE_ALWAYS_DISALLOWED_TOOLS = ["Bash"]
_REACT_WRITE_TOOLS = {"write_file"}
_FINALIZE_DISALLOWED_TOOLS = [
    "Bash",
    "WebFetch",
    "WebSearch",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LS",
]

_CLAUDE_REACT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "findings", "sources", "data", "artifacts"],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "failed", "blocked"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {},
        },
        "sources": {
            "type": "array",
            "items": {},
        },
        "data": {"type": "object", "additionalProperties": True},
        "artifacts": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
}


class ClaudeReactNodeExecuteRuntime:
    """Claude Agent SDK based tool-using runtime for one DAG plan node."""

    def __init__(
        self,
        *,
        model_resolver=None,
        max_turns: int = 6,
        tool_timeout_seconds: int = 60,
        settings: Settings | None = None,
    ) -> None:
        self._model_resolver = model_resolver or (
            lambda context: ModelRouter().resolve(LLMNode.AGENT_STEP, None)
        )
        self._max_turns = max(2, int(max_turns))
        self._tool_timeout_seconds = max(1, int(tool_timeout_seconds))
        self._settings = settings or get_settings()

    def run(self, context) -> NodeResult:
        """Execute one plan node via Claude Agent SDK."""
        # Import NodeExecutionContext here to avoid circular dependency
        from app.task_runtime.node_execute_runtime import NodeExecutionContext
        if not isinstance(context, NodeExecutionContext):
            context = NodeExecutionContext(
                user_objective=getattr(context, "user_objective", ""),
                node=getattr(context, "node", PlanNode(id="unknown", runtime="react", objective="unknown")),
                resolved_inputs=getattr(context, "resolved_inputs", []),
                legacy_hints=getattr(context, "legacy_hints", {}),
                instructions=getattr(context, "instructions", []),
            )
        return _execute(
            context,
            self._model_resolver,
            self._settings,
            max_turns=self._max_turns,
            tool_timeout_seconds=self._tool_timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute(
    context,
    model_resolver,
    settings: Settings,
    *,
    max_turns: int = 6,
    tool_timeout_seconds: int = 60,
) -> NodeResult:
    from app.task_runtime.node_execute_runtime import NodeExecutionContext

    started = time.perf_counter()
    resolved = model_resolver(context)
    if not resolved.profile.api_key:
        logger.info(
            "claude react node skipped node_id=%s reason=missing_api_key profile=%s",
            context.node.id,
            getattr(resolved.profile, "id", None),
        )
        return _blocked(context.node, _CODE_MISSING_API_KEY, "Claude react runtime LLM API key is not configured.")

    if not is_claude_agent_sdk_available():
        return _blocked(
            context.node,
            _CODE_MISSING_API_KEY,
            "claude-agent-sdk is not installed. Run: pip install claude-agent-sdk",
        )

    # Build the system prompt (reuses existing react_node_execute template).
    system_prompt = _build_system_prompt(context)
    prompt_text = _build_user_prompt(context)

    # Resolve model and endpoint.
    model_name = "deepseek-v4-pro"
    endpoint = _resolve_claude_endpoint(settings, resolved)

    logger.info(
        "claude react node start node_id=%s model=%s",
        context.node.id,
        model_name,
    )
    set_attributes(
        **{
            "jarvis.runtime_backend": "claude_agent_sdk",
            "jarvis.model": model_name,
            "jarvis.max_turns": max_turns,
            "jarvis.tool_timeout_seconds": tool_timeout_seconds,
            "jarvis.endpoint": endpoint,
        }
    )

    try:
        coro = _run_agent(
            context=context,
            prompt=prompt_text,
            system_prompt=system_prompt,
            model=model_name,
            endpoint=endpoint,
            api_key=resolved.profile.api_key,
            max_turns=max_turns,
            tool_timeout_seconds=tool_timeout_seconds,
        )
        agent_result = asyncio.run(asyncio.wait_for(coro, timeout=_REACT_TIMEOUT_SECONDS))
        if _needs_finalize_fork(agent_result):
            session_id = str(agent_result.get("session_id") or "").strip()
            if session_id:
                add_event(
                    "node.finalize_fork.started",
                    **{
                        "jarvis.primary_agent_session_id": session_id,
                        "jarvis.max_turns": _FINALIZE_MAX_TURNS,
                    },
                )
                finalize_coro = _run_finalize_agent(
                    context=context,
                    primary_result=agent_result,
                    session_id=session_id,
                    model=model_name,
                    endpoint=endpoint,
                    api_key=resolved.profile.api_key,
                )
                finalize_result = asyncio.run(
                    asyncio.wait_for(finalize_coro, timeout=_FINALIZE_TIMEOUT_SECONDS)
                )
                agent_result = _merge_finalize_result(agent_result, finalize_result)
            else:
                logger.warning(
                    "claude react finalize fork skipped node_id=%s reason=missing_session_id",
                    context.node.id,
                )
                add_event(
                    "node.finalize_fork.skipped",
                    **{"jarvis.reason": "missing_session_id"},
                )
                agent_result = _mark_max_turns_no_final_output(agent_result)
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "claude react node timeout node_id=%s elapsed_ms=%s",
            context.node.id,
            elapsed_ms,
        )
        return _failed(
            context.node,
            _CODE_TIMEOUT,
            f"React runtime exceeded {_REACT_TIMEOUT_SECONDS}s time limit.",
            retryable=True,
        )
    except Exception as exc:
        logger.exception(
            "claude react node failed node_id=%s elapsed_ms=%s",
            context.node.id,
            int((time.perf_counter() - started) * 1000),
        )
        return _failed(context.node, _CODE_RUNTIME_ERROR, str(exc), retryable=True)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "claude react node completed node_id=%s status=%s summary_len=%s tool_count=%s elapsed_ms=%s",
        context.node.id,
        "completed" if agent_result["ok"] else "failed",
        len(agent_result["summary"]),
        agent_result["tool_count"],
        elapsed_ms,
    )
    set_attributes(
        **{
            "jarvis.status": "completed" if agent_result["ok"] else "failed",
            "jarvis.tool_count": agent_result["tool_count"],
            "jarvis.final_text_len": agent_result.get("final_text_len", 0),
            "jarvis.max_turns_reached": agent_result.get("max_turns_reached", False),
            "jarvis.finalize_fork_used": agent_result.get("finalize_fork_used", False),
            "jarvis.elapsed_ms": elapsed_ms,
        }
    )
    return _build_node_result(context, agent_result)


# ---------------------------------------------------------------------------
# Async agent execution
# ---------------------------------------------------------------------------


async def _run_agent(
    *,
    context,
    prompt: str,
    system_prompt: str,
    model: str,
    endpoint: str,
    api_key: str,
    max_turns: int = 6,
    tool_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run the Claude Agent SDK query and collect results.

    Uses Claude SDK tools for file inspection/writes and injects Jarvis MCP
    tools for non-native actions such as knowledge lookup or delivery.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query
    from app.task_runtime.claude_tool_adapter import build_claude_mcp_server

    env = {
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": api_key,
    }
    mcp_server, _jarvis_tool_names = build_claude_mcp_server(
        _claude_react_tool_definitions(context),
        tool_timeout_seconds=tool_timeout_seconds,
    )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        disallowed_tools=_claude_disallowed_tools(context),
        mcp_servers={"jarvis": mcp_server},
        output_format={"type": "json_schema", "schema": _CLAUDE_REACT_OUTPUT_SCHEMA},
        max_turns=max_turns,
        env=env,
        cwd=_claude_cwd(context),
    )

    summary = ""
    tool_calls: list[dict[str, Any]] = []
    tool_start_times: dict[str, float] = {}  # tool_call_id -> perf_counter
    usage_records: list[dict[str, Any]] = []
    final_text = ""
    ok = False
    step_index = 0
    max_turns_reached = False
    session_id = ""

    try:
        async for msg in query(prompt=prompt, options=options):
            msg_type = type(msg).__name__
            session_id = _message_session_id(msg) or session_id

            if msg_type == "AssistantMessage":
                if hasattr(msg, "content") and isinstance(msg.content, list):
                    for block in msg.content:
                        block_type = type(block).__name__
                        if block_type == "ToolUseBlock":
                            tool_id = getattr(block, "id", "")
                            tool_name = getattr(block, "name", "")
                            tool_args = dict(getattr(block, "input", {}) or {})
                            tc = {
                                "id": tool_id,
                                "tool_name": tool_name,
                                "args": tool_args,
                                "status": "pending",
                            }
                            tool_calls.append(tc)
                            tool_start_times[tool_id] = time.perf_counter()
                            add_event(
                                "node.tool.started",
                                **{
                                    "jarvis.tool_call_id": tool_id,
                                    "jarvis.tool_name": tool_name,
                                    "jarvis.tool_index": len(tool_calls) - 1,
                                    "jarvis.tool_args_preview": trace_preview(_trace_tool_args(tool_args), limit=240),
                                },
                            )
                            logger.info(
                                "claude react tool start tool=%s tool_call_id=%s args=%s tool_observation_count=%s",
                                tool_name,
                                tool_id,
                                tool_args,
                                len(tool_calls) - 1,
                            )
                        elif block_type == "TextBlock":
                            final_text = getattr(block, "text", "")
                        elif block_type == "ThinkingBlock":
                            step_index += 1
                            logger.info(
                                "claude react step start step=%s tool_observation_count=%s",
                                step_index,
                                len(tool_calls),
                            )

                # Some SDK versions attach usage to AssistantMessage
                msg_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                if isinstance(msg_usage, dict) and msg_usage:
                    usage_records.append(_normalize_usage(msg_usage, step_index))

            elif msg_type == "UserMessage":
                if hasattr(msg, "content") and isinstance(msg.content, list):
                    for block in msg.content:
                        if type(block).__name__ == "ToolResultBlock":
                            tool_id = getattr(block, "tool_use_id", "")
                            for tc in tool_calls:
                                if tc.get("id") == tool_id:
                                    tc["status"] = "completed"
                                    _apply_tool_result(tc, block)
                                    started = tool_start_times.pop(tool_id, 0)
                                    elapsed_ms = int((time.perf_counter() - started) * 1000) if started else 0
                                    logger.info(
                                        "claude react tool finished tool=%s tool_call_id=%s status=%s elapsed_ms=%s summary=%s",
                                        tc.get("tool_name"),
                                        tool_id,
                                        tc.get("status", "?"),
                                        elapsed_ms,
                                        _truncate(str(tc.get("summary", "") or ""), limit=300),
                                    )
                                    event_attributes = {
                                        "jarvis.tool_call_id": tool_id,
                                        "jarvis.tool_name": tc.get("tool_name"),
                                        "jarvis.tool_status": tc.get("status", "?"),
                                        "jarvis.elapsed_ms": elapsed_ms,
                                        "jarvis.summary_len": len(str(tc.get("summary", "") or "")),
                                        "jarvis.artifact_count": len(tc.get("artifacts") or []),
                                        "jarvis.tool_artifact_count": len(tc.get("tool_artifacts") or []),
                                    }
                                    if content_capture_enabled():
                                        event_attributes["jarvis.summary_preview"] = trace_preview(
                                            tc.get("summary", ""), limit=240
                                        )
                                    add_event("node.tool.completed", **event_attributes)
                                    break

            elif msg_type == "ResultMessage":
                ok = not bool(getattr(msg, "is_error", False)) and getattr(msg, "status", "") != "error"
                result_text = _result_message_text(msg)
                if result_text:
                    final_text = result_text
                # Extract usage from the result message
                result_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                if isinstance(result_usage, dict) and result_usage:
                    usage_records.append(_normalize_usage(result_usage, -1))
                if usage_records:
                    total_input = sum(r.get("input_tokens", r.get("prompt_tokens", 0)) for r in usage_records if isinstance(r, dict))
                    total_output = sum(r.get("output_tokens", r.get("completion_tokens", 0)) for r in usage_records if isinstance(r, dict))
                    logger.info(
                        "claude react result received status=%s tool_count=%s usage_records=%s total_input=%s total_output=%s",
                        getattr(msg, "status", "?"),
                        len(tool_calls),
                        len(usage_records),
                        total_input,
                        total_output,
                    )
                    set_attributes(
                        **{
                            "jarvis.tool_count": len(tool_calls),
                            "jarvis.usage.record_count": len(usage_records),
                            "jarvis.usage.input_tokens": total_input,
                            "jarvis.usage.output_tokens": total_output,
                        }
                    )
                    add_event(
                        "claude_react.result",
                        **{
                            "jarvis.sdk_status": getattr(msg, "status", "?"),
                            "jarvis.tool_count": len(tool_calls),
                            "jarvis.usage.record_count": len(usage_records),
                            "jarvis.usage.input_tokens": total_input,
                            "jarvis.usage.output_tokens": total_output,
                        },
                    )
                else:
                    logger.info(
                        "claude react result received status=%s tool_count=%s (no usage data available)",
                        getattr(msg, "status", "?"),
                        len(tool_calls),
                    )

    except Exception as exc:
        exc_msg = str(exc)
        if "max_turns" in exc_msg.lower() or "maximum number of turns" in exc_msg.lower():
            # max_turns reached but we may already have useful results.
            max_turns_reached = True
            if tool_calls or final_text:
                logger.warning(
                    "claude react node max_turns reached with data tool_count=%s final_text_len=%s",
                    len(tool_calls),
                    len(final_text),
                )
                ok = bool(final_text)
                summary = summary or final_text.strip()
                treated_as_completed = bool(final_text)
            else:
                logger.warning("claude react node max_turns reached with no data")
                ok = False
                summary = "Research incomplete: reached step limit before gathering sufficient information."
                treated_as_completed = False
            add_event(
                "node.max_turns_reached",
                **{
                    "jarvis.max_turns": max_turns,
                    "jarvis.tool_count": len(tool_calls),
                    "jarvis.final_text_len": len(final_text),
                    "jarvis.treated_as_completed": treated_as_completed,
                },
            )
        else:
            logger.exception("claude react node sdk query exception tool_count=%s", len(tool_calls))
            ok = False
            summary = f"Claude Agent SDK query failed: {exc}"

    if not summary and final_text:
        summary = final_text.strip()
    if not summary:
        summary = "Claude react runtime completed."

    return {
        "ok": ok,
        "summary": summary,
        "tool_calls": tool_calls,
        "tool_count": len(tool_calls),
        "final_text": final_text,
        "final_text_len": len(final_text),
        "max_turns_reached": max_turns_reached,
        "session_id": session_id,
        "usage_records": usage_records,
    }


async def _run_finalize_agent(
    *,
    context,
    primary_result: dict[str, Any],
    session_id: str,
    model: str,
    endpoint: str,
    api_key: str,
) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    env = {
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": api_key,
    }
    options = ClaudeAgentOptions(
        system_prompt=_finalize_system_prompt(context),
        model=model,
        permission_mode="dontAsk",
        tools=[],
        allowed_tools=[],
        disallowed_tools=list(_FINALIZE_DISALLOWED_TOOLS),
        mcp_servers={},
        strict_mcp_config=True,
        output_format={"type": "json_schema", "schema": _CLAUDE_REACT_OUTPUT_SCHEMA},
        max_turns=_FINALIZE_MAX_TURNS,
        resume=session_id,
        fork_session=True,
        env=env,
        cwd=_claude_cwd(context),
    )
    prompt = _build_finalize_prompt(context, primary_result)
    final_text = ""
    summary = ""
    usage_records: list[dict[str, Any]] = []
    finalize_session_id = ""
    tool_calls: list[dict[str, Any]] = []
    ok = False

    try:
        async for msg in query(prompt=prompt, options=options):
            msg_type = type(msg).__name__
            finalize_session_id = _message_session_id(msg) or finalize_session_id
            if msg_type == "AssistantMessage":
                msg_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                if isinstance(msg_usage, dict) and msg_usage:
                    usage_records.append(_normalize_usage(msg_usage, _FINALIZE_MAX_TURNS))
                if hasattr(msg, "content") and isinstance(msg.content, list):
                    for block in msg.content:
                        block_type = type(block).__name__
                        if block_type == "TextBlock":
                            final_text = getattr(block, "text", "") or final_text
                        elif block_type == "ToolUseBlock":
                            tool_calls.append(
                                {
                                    "id": getattr(block, "id", ""),
                                    "tool_name": getattr(block, "name", ""),
                                    "args": dict(getattr(block, "input", {}) or {}),
                                    "status": "unexpected",
                                }
                            )
            elif msg_type == "ResultMessage":
                ok = not bool(getattr(msg, "is_error", False)) and getattr(msg, "status", "") != "error"
                result_text = _result_message_text(msg)
                if result_text:
                    final_text = result_text
                result_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                if isinstance(result_usage, dict) and result_usage:
                    usage_records.append(_normalize_usage(result_usage, -2))
    except Exception as exc:
        logger.warning("claude react finalize fork failed session_id=%s error=%s", session_id, exc, exc_info=True)
        add_event(
            "node.finalize_fork.failed",
            **{
                "jarvis.primary_agent_session_id": session_id,
                "jarvis.reason": str(exc),
            },
        )
        return {
            "ok": False,
            "summary": f"Finalize fork failed: {exc}",
            "final_text": "",
            "final_text_len": 0,
            "usage_records": usage_records,
            "session_id": finalize_session_id,
            "tool_calls": tool_calls,
        }

    if final_text:
        try:
            parsed = json.loads(final_text)
            if isinstance(parsed, dict):
                summary = str(parsed.get("summary") or "").strip()
                status = parsed.get("status")
                if status == "completed":
                    ok = True
                elif status in {"failed", "blocked"}:
                    ok = False
        except (json.JSONDecodeError, TypeError):
            summary = final_text.strip()

    add_event(
        "node.finalize_fork.completed",
        **{
            "jarvis.primary_agent_session_id": session_id,
            "jarvis.finalize_agent_session_id": finalize_session_id,
            "jarvis.final_text_len": len(final_text),
            "jarvis.tool_count": len(tool_calls),
            "jarvis.ok": ok,
        },
    )
    logger.info(
        "claude react finalize fork completed primary_session_id=%s finalize_session_id=%s ok=%s final_text_len=%s tool_count=%s",
        session_id,
        finalize_session_id,
        ok,
        len(final_text),
        len(tool_calls),
    )
    return {
        "ok": ok and bool(final_text) and not tool_calls,
        "summary": summary or final_text.strip(),
        "final_text": final_text,
        "final_text_len": len(final_text),
        "usage_records": usage_records,
        "session_id": finalize_session_id,
        "tool_calls": tool_calls,
    }


def _needs_finalize_fork(agent_result: dict[str, Any]) -> bool:
    return (
        bool(agent_result.get("max_turns_reached"))
        and int(agent_result.get("final_text_len") or 0) <= 0
        and int(agent_result.get("tool_count") or 0) > 0
    )


def _merge_finalize_result(
    primary_result: dict[str, Any],
    finalize_result: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(primary_result)
    finalize_text = str(finalize_result.get("final_text") or "")
    finalize_used_tools = bool(finalize_result.get("tool_calls"))
    if finalize_text and not finalize_used_tools:
        merged["ok"] = bool(finalize_result.get("ok"))
        merged["summary"] = str(finalize_result.get("summary") or "").strip()
        merged["final_text"] = finalize_text
        merged["final_text_len"] = len(merged["final_text"])
        merged["recovered_from"] = "max_turns_finalize_fork"
    else:
        merged = _mark_max_turns_no_final_output(merged)
        merged["finalize_error"] = str(finalize_result.get("summary") or "Finalize fork did not produce output.")

    merged["finalize_fork_used"] = True
    merged["primary_agent_session_id"] = primary_result.get("session_id")
    merged["finalize_agent_session_id"] = finalize_result.get("session_id")
    merged["finalize_tool_calls"] = finalize_result.get("tool_calls", [])
    merged["usage_records"] = [
        *(primary_result.get("usage_records") or []),
        *(finalize_result.get("usage_records") or []),
    ]
    return merged


def _mark_max_turns_no_final_output(agent_result: dict[str, Any]) -> dict[str, Any]:
    marked = dict(agent_result)
    marked["ok"] = False
    marked["summary"] = (
        "Claude react runtime reached the step limit after collecting tool results, "
        "but did not produce the required final structured output."
    )
    marked["error_code"] = _CODE_MAX_TURNS_NO_FINAL_OUTPUT
    marked["retryable"] = True
    return marked


def _message_session_id(msg: Any) -> str:
    value = getattr(msg, "session_id", None)
    if value is None:
        return ""
    return str(value).strip()


def _result_message_text(msg: Any) -> str:
    structured = getattr(msg, "structured_output", None)
    if structured:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except TypeError:
            return str(structured)
    result = getattr(msg, "result", None)
    if isinstance(result, str) and result.strip():
        return result.strip()
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)
    return ""


def _build_finalize_prompt(context, primary_result: dict[str, Any]) -> str:
    payload = {
        "instruction": (
            "Finalize the current node using only the existing resumed conversation and tool results. "
            "Do not call any tools. Return the required JSON schema now."
        ),
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "tool_count": primary_result.get("tool_count", 0),
        "max_turns_reached": primary_result.get("max_turns_reached", False),
    }
    return json.dumps(payload, ensure_ascii=False)


def _finalize_system_prompt(context) -> str:
    temporal_line = _build_temporal_context_line(context)
    parts = [
        "You are Jarvis ClaudeReactNodeExecuteRuntime finalizer.",
        "You are resuming a prior tool-using session that reached its step limit before producing final output.",
        "Use only the conversation and tool observations already present in the resumed session.",
        "Do not call tools, search the web, read files, or request more information.",
        "Return JSON matching the configured schema: status, summary, findings, sources, data, artifacts.",
        "If the existing evidence is insufficient, return status 'blocked' with a concrete summary of what is missing.",
    ]
    if temporal_line:
        parts.append(temporal_line)
    return "\n".join(parts)


def _claude_react_tool_definitions(context) -> list[Any]:
    blocked = set(_REACT_WRITE_TOOLS)
    return [
        tool
        for tool in list_tool_definitions(exposed_to_llm=True)
        if tool.name not in blocked
    ]


def _claude_disallowed_tools(context) -> list[str]:
    tools = list(_CLAUDE_ALWAYS_DISALLOWED_TOOLS)
    if getattr(context.node, "mode", "read") != "write":
        tools.extend(_CLAUDE_NATIVE_MUTATION_TOOLS)
    return tools


def _claude_cwd(context) -> str:
    runtime_context = getattr(context, "runtime_context", None) or RuntimeContext.from_hints(context.legacy_hints)
    workspace = runtime_context.workspace
    if workspace.node_workspace is not None:
        return str(workspace.node_workspace)
    if workspace.session_root is not None:
        return str(workspace.session_root)
    return "."


def _apply_tool_result(tc: dict[str, Any], block: Any) -> None:
    """Extract summary from a tool result block and attach to the tool call record."""
    try:
        content = getattr(block, "content", None)
        if content is None:
            return
        # Content can be a string or list of dicts
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                tc["summary"] = content[:500]
                return
        elif isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                text = first.get("text", "")
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    tc["summary"] = str(text)[:500]
                    return
            else:
                return
        else:
            return

        if isinstance(payload, dict):
            tc["ok"] = payload.get("ok", True)
            tc["status"] = payload.get("status", "completed")
            tc["summary"] = str(payload.get("summary", "") or "")[:500]
            artifacts = payload.get("artifacts")
            if isinstance(artifacts, list):
                tc["artifacts"] = [item for item in artifacts if isinstance(item, (str, dict))]
            tool_artifacts = payload.get("tool_artifacts")
            if isinstance(tool_artifacts, list):
                tc["tool_artifacts"] = [item for item in tool_artifacts if isinstance(item, dict)]
    except Exception:
        pass


def _trace_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    locator_keys = {"url", "path", "file_path", "repo", "repo_id", "owner", "name"}
    content_keys = {"query", "prompt", "input"}
    allowed_keys = locator_keys | (content_keys if content_capture_enabled() else set())
    result: dict[str, Any] = {}
    for key, value in args.items():
        if key not in allowed_keys:
            continue
        if _looks_secret_key(key):
            continue
        result[key] = trace_preview(value, limit=180)
    if not result and args:
        result["keys"] = sorted(str(key) for key in args.keys())
    return result


def _looks_secret_key(key: str) -> bool:
    text = str(key).lower()
    return any(part in text for part in ("token", "secret", "password", "api_key", "auth"))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_system_prompt(context) -> str:
    """Build the system prompt for Claude Agent SDK.

    Adapts the existing react_node_execute prompt template, removing
    the JSON-output-at-the-end instruction since Claude Agent SDK
    handles its own output formatting.
    """
    # Load the existing system prompt template content
    try:
        bundle = PromptRegistry().load("react_node_execute")
        # Render with dummy payload to get the messages
        messages = bundle.render(
            {"input_json": json.dumps({"user_objective": "", "node": {}, "resolved_inputs": [], "temporal_context": {}, "runtime_context": {}, "instructions": []}, ensure_ascii=False)}
        )
    except Exception:
        logger.warning("claude react runtime failed to load react_node_execute prompt, using fallback", exc_info=True)
        return _fallback_system_prompt(context)

    # Extract the system message content
    system_text = ""
    from app.llm.client import LLMMessage

    for msg in messages:
        if isinstance(msg, LLMMessage) and getattr(msg, "role", "") == "system":
            system_text = str(msg.content or "")
            break

    if not system_text:
        return _fallback_system_prompt(context)

    # Build temporal context line so the Claude Code CLI subprocess
    # (claude.exe) knows the actual current date/time instead of falling
    # back to its baked-in knowledge cutoff.
    temporal_line = _build_temporal_context_line(context)

    # Adapt the prompt for Claude Agent SDK environment.
    # Appended instruction guides the agent toward a final structured answer.
    parts = [system_text.strip()]
    if temporal_line:
        parts.append(temporal_line)
    parts.append(_FINAL_RESPONSE_GUIDANCE)
    adapted = "\n\n".join(parts)
    return adapted


def _build_user_prompt(context) -> str:
    """Build the user prompt as a JSON payload string (same format as existing runtime)."""
    from app.task_runtime.node_execute_runtime import _temporal_context

    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [
            item.model_dump(mode="json", exclude_none=True)
            for item in (context.resolved_inputs or [])
        ],
        "temporal_context": _temporal_context(
            getattr(context, "runtime_context", None)
            or RuntimeContext.from_hints(context.legacy_hints)
        ),
        "runtime_context": getattr(context, "legacy_hints", {}),
        "instructions": getattr(context, "instructions", []),
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_temporal_context_line(context) -> str:
    """Return a single-line temporal context snippet for the system prompt.

    This ensures the Claude Code CLI subprocess (claude.exe) sees the actual
    current date and time rather than relying on its baked-in knowledge cutoff.
    """
    from app.task_runtime.node_execute_runtime import _temporal_context

    temporal = _temporal_context(
        getattr(context, "runtime_context", None)
        or RuntimeContext.from_hints(context.legacy_hints)
    )
    if not temporal:
        return ""
    date = temporal.get("current_date", "")
    time_ = temporal.get("current_time", "")
    tz = temporal.get("timezone", "")
    if not date:
        return ""
    parts = [f"Current date: {date}"]
    if time_:
        parts.append(f"Current time: {time_}")
    if tz:
        parts.append(f"Timezone: {tz}")
    return " | ".join(parts)


_FINAL_RESPONSE_GUIDANCE = """
Execute only this node. The final answer is a machine-readable node result, not a user-facing reply.
Respect node.mode: read nodes gather evidence only; write nodes may create requested artifacts with Claude native file tools.
For written files, include artifacts with paths relative to runtime_context.session_workspace_dir when available.
Return JSON matching the configured schema with status, summary, findings, sources, data, and artifacts.
If the task cannot be completed, set status to failed or blocked and explain the reason in summary.
""".strip()


def _fallback_system_prompt(context=None) -> str:
    parts = [
        "You are Jarvis ClaudeReactNodeExecuteRuntime. "
        "Execute one non-repository plan node using available tools. "
        "Do not perform code edits, shell commands, or repository workflows "
        "(code and shell work belongs to coder runtime nodes).",
    ]
    if context is not None:
        temporal_line = _build_temporal_context_line(context)
        if temporal_line:
            parts.append(temporal_line)
    parts.append(_FINAL_RESPONSE_GUIDANCE)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


def _resolve_claude_endpoint(settings: Settings, resolved_profile) -> str:
    """Determine the Anthropic-compatible endpoint for the resolved model."""
    # If the model provider has a known Anthropic-compatible endpoint, use it.
    provider = getattr(resolved_profile, "provider", "") or getattr(settings, "llm_provider", "deepseek")
    provider = str(provider).strip().lower()

    # Map Jarvis providers to Anthropic-compatible endpoints.
    provider_endpoints: dict[str, str] = {
        "deepseek": "https://api.deepseek.com/anthropic",
        "google": "https://generativelanguage.googleapis.com/v1beta/anthropic",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/anthropic",
    }

    if provider in provider_endpoints:
        return provider_endpoints[provider]

    # Fallback: construct from the profile's base_url if available.
    resolved_base = getattr(resolved_profile, "base_url", "") or getattr(resolved_profile.profile, "base_url", "")
    if resolved_base:
        return str(resolved_base).rstrip("/") + "/anthropic"

    # Ultimate fallback to deepseek.
    return "https://api.deepseek.com/anthropic"


def is_claude_agent_sdk_available() -> bool:
    """Check whether claude-agent-sdk is installed."""
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Result construction (mirrors existing _react_result_from_response)
# ---------------------------------------------------------------------------


def _build_node_result(context, agent_result: dict[str, Any]) -> NodeResult:
    """Build a NodeResult from the Claude Agent SDK agent result."""
    tool_calls = agent_result.get("tool_calls", [])
    summary = agent_result.get("summary", "")
    final_text = agent_result.get("final_text", "")
    ok = bool(agent_result.get("ok"))

    # Parse final_text as JSON if possible (mirrors existing react behavior)
    data: dict[str, Any] = {}
    if final_text:
        try:
            data = json.loads(final_text)
        except (json.JSONDecodeError, TypeError):
            data = {"raw_output": final_text}
    if not isinstance(data, dict):
        data = {"raw_output": data}

    structured_summary = data.get("summary")
    if isinstance(structured_summary, str) and structured_summary.strip():
        summary = structured_summary.strip()
    structured_status = data.get("status")
    node_status = "completed" if ok else "failed"
    if structured_status == "completed":
        ok = True
        node_status = "completed"
    elif structured_status in {"failed", "blocked"}:
        ok = False
        node_status = structured_status
    else:
        node_status = "completed" if ok else "failed"

    findings = data.get("findings")
    sources = data.get("sources")
    data.setdefault("findings", findings if isinstance(findings, list) else [])
    data.setdefault("sources", sources if isinstance(sources, list) else [])
    data["runtime_backend"] = "claude_agent_sdk"
    data["tool_count"] = agent_result.get("tool_count", len(tool_calls))
    data["final_text_len"] = agent_result.get("final_text_len", len(final_text))
    data["max_turns_reached"] = bool(agent_result.get("max_turns_reached", False))
    for key in (
        "session_id",
        "primary_agent_session_id",
        "finalize_agent_session_id",
        "recovered_from",
        "finalize_fork_used",
        "finalize_error",
    ):
        if agent_result.get(key) is not None:
            data[key] = agent_result.get(key)
    if agent_result.get("finalize_tool_calls"):
        data["finalize_tool_calls"] = agent_result.get("finalize_tool_calls")

    return NodeResult(
        node_id=context.node.id,
        runtime="react",
        status=node_status,
        summary=summary or "Claude react runtime completed.",
        tool_calls=tool_calls,
        tool_artifacts=_tool_artifacts_from_tool_calls(tool_calls),
        usage_records=agent_result.get("usage_records", []),
        data=data,
        artifacts=_artifacts_from_data(data),
        error=None
        if ok
        else NodeError(
            code=str(agent_result.get("error_code") or _CODE_RUNTIME_ERROR),
            message=summary,
            retryable=bool(agent_result.get("retryable", True)),
        ),
    )


def _tool_artifacts_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for call in tool_calls:
        call_artifacts = call.get("tool_artifacts")
        if isinstance(call_artifacts, list):
            artifacts.extend(item for item in call_artifacts if isinstance(item, dict))
    return artifacts


def _artifacts_from_data(data: dict[str, Any]) -> list[NodeArtifact]:
    raw = data.get("artifacts")
    if not isinstance(raw, list):
        return []
    result: list[NodeArtifact] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(NodeArtifact(**item))
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Common result helpers
# ---------------------------------------------------------------------------


def _normalize_usage(usage: dict[str, Any], step_index: int) -> dict[str, Any]:
    """Normalize SDK usage dict to Jarvis usage record format."""
    return {
        "source": "claude_react",
        "stage": f"claude_react_step_{step_index}" if step_index >= 0 else "claude_react",
        "provider": "deepseek",
        "model": usage.get("model", ""),
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "_raw": usage,
    }


def _truncate(text: str, *, limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _blocked(node: PlanNode, code: str, message: str) -> NodeResult:
    return NodeResult(
        node_id=node.id,
        runtime="react",
        status="blocked",
        error=NodeError(code=code, message=message, retryable=False),
    )


def _failed(node: PlanNode, code: str, message: str, *, retryable: bool = False) -> NodeResult:
    return NodeResult(
        node_id=node.id,
        runtime="react",
        status="failed",
        error=NodeError(code=code, message=message, retryable=retryable),
    )
