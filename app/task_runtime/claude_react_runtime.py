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
from dataclasses import replace
from typing import Any

from app.config import Settings, get_settings
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult
from app.task_runtime.planner import PlanNode
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

# Internal error codes
_CODE_MISSING_API_KEY = "react_missing_api_key"
_CODE_RUNTIME_ERROR = "react_runtime_error"
_CODE_NO_RESPONSE = "react_no_response"
_CODE_TIMEOUT = "react_timeout"
_REACT_TIMEOUT_SECONDS = 900  # 15 minutes


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
        return _execute(context, self._model_resolver, self._settings)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute(
    context,
    model_resolver,
    settings: Settings,
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

    try:
        coro = _run_agent(
            prompt=prompt_text,
            system_prompt=system_prompt,
            model=model_name,
            endpoint=endpoint,
            api_key=resolved.profile.api_key,
        )
        agent_result = asyncio.run(asyncio.wait_for(coro, timeout=_REACT_TIMEOUT_SECONDS))
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
    return _build_node_result(context, agent_result)


# ---------------------------------------------------------------------------
# Async agent execution
# ---------------------------------------------------------------------------


async def _run_agent(
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    endpoint: str,
    api_key: str,
) -> dict[str, Any]:
    """Run the Claude Agent SDK query and collect results.

    Uses the SDK's built-in tools (WebSearch, WebFetch, Read, Glob, Grep, etc.)
    — no custom MCP tools are injected.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query

    env = {
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": api_key,
    }

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        env=env,
        cwd=".",
    )

    summary = ""
    tool_calls: list[dict[str, Any]] = []
    tool_start_times: dict[str, float] = {}  # tool_call_id -> perf_counter
    usage_records: list[dict[str, Any]] = []
    final_text = ""
    ok = False
    step_index = 0

    try:
        async for msg in query(prompt=prompt, options=options):
            msg_type = type(msg).__name__

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
                                    break

            elif msg_type == "ResultMessage":
                ok = getattr(msg, "status", "") != "error"
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
            if tool_calls or final_text:
                logger.warning(
                    "claude react node max_turns reached with data tool_count=%s final_text_len=%s – treating as completed",
                    len(tool_calls),
                    len(final_text),
                )
                ok = True
                summary = summary or final_text.strip()
            else:
                logger.warning("claude react node max_turns reached with no data")
                ok = False
                summary = "Research incomplete: reached step limit before gathering sufficient information."
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
        "usage_records": usage_records,
    }


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
    except Exception:
        pass


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
        return _fallback_system_prompt()

    # Extract the system message content
    system_text = ""
    from app.llm.client import LLMMessage

    for msg in messages:
        if isinstance(msg, LLMMessage) and getattr(msg, "role", "") == "system":
            system_text = str(msg.content or "")
            break

    if not system_text:
        return _fallback_system_prompt()

    # Adapt the prompt for Claude Agent SDK environment.
    # Appended instruction guides the agent toward a final structured answer.
    adapted = (
        system_text.strip()
        + "\n\n"
        + _FINAL_RESPONSE_GUIDANCE
    )
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


_FINAL_RESPONSE_GUIDANCE = """
Use whatever tools you need to complete the task (WebSearch, WebFetch, etc.).
When you have gathered enough information, provide your final response.
Your final response should be well-structured and concise, including:
- A summary of what was found or done
- Any relevant findings, data, or sources
Do NOT produce a final user reply — downstream nodes will use your output.
If you are approaching the step limit, summarize what you have gathered so far
rather than making additional tool calls.
""".strip()


def _fallback_system_prompt() -> str:
    return (
        "You are Jarvis ClaudeReactNodeExecuteRuntime. "
        "Execute one non-repository plan node using available tools. "
        "Do not perform code edits, shell commands, or repository workflows "
        "(code and shell work belongs to coder runtime nodes). "
        + _FINAL_RESPONSE_GUIDANCE
    )


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

    findings = data.get("findings")
    sources = data.get("sources")
    data.setdefault("findings", findings if isinstance(findings, list) else [])
    data.setdefault("sources", sources if isinstance(sources, list) else [])
    data["runtime_backend"] = "claude_agent_sdk"

    return NodeResult(
        node_id=context.node.id,
        runtime="react",
        status="completed" if ok else "failed",
        summary=summary or "Claude react runtime completed.",
        tool_calls=tool_calls,
        tool_artifacts=_tool_artifacts_from_tool_calls(tool_calls),
        usage_records=agent_result.get("usage_records", []),
        data=data,
        artifacts=_artifacts_from_data(data),
        error=None
        if ok
        else NodeError(
            code=_CODE_RUNTIME_ERROR,
            message=summary,
            retryable=True,
        ),
    )


def _tool_artifacts_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for call in tool_calls:
        call_artifacts = call.get("artifacts")
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
