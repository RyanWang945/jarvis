"""Convert Jarvis ToolDefinition objects to Claude Agent SDK MCP tools.

This module provides the bridge between Jarvis' OpenAI-format tool definitions
and Claude Agent SDK's MCP-based tool system.  It wraps Jarvis tool handlers
so they can be called transparently by the SDK agent loop.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.definitions import ToolDefinition
from app.tools.runtime import check_tool_policy, get_tool_definition

logger = logging.getLogger(__name__)

# Tool names that are only for Coder runtime – blocked from react nodes.
_CODER_ONLY_TOOLS = frozenset(
    {
        "delegate_to_claude_code",
        "delegate_to_codex",
        "shell_inspect",
        "shell_run_command",
    }
)

# Tool names that need special handling (Skill loading feedback loop).
_SKILL_TOOL_NAMES = frozenset({"Skill"})

# Maximum chars for tool result text fed back to the LLM.
_MAX_OUTPUT_CHARS = 4_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def adapt_jarvis_tool_to_sdk_tool(
    tool_def: ToolDefinition,
    *,
    tool_timeout_seconds: int = 60,
):
    """Convert a single Jarvis ToolDefinition to an SDK MCP tool.

    Returns an SdkMcpTool instance suitable for passing to
    :func:`claude_agent_sdk.create_sdk_mcp_server`.
    """
    from claude_agent_sdk import tool as sdk_tool

    handler = tool_def.handler
    tool_name = tool_def.name
    timeout = tool_timeout_seconds

    # Build the JSON Schema input_schema for the SDK tool.
    # Jarvis stores OpenAI-format {type, properties, required} in args_schema.
    input_schema = dict(tool_def.args_schema)

    @sdk_tool(
        name=tool_name,
        description=tool_def.description,
        input_schema=input_schema,
    )
    async def _wrapper(args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        # Policy check (same as existing react runtime)
        rejection = check_tool_policy(tool_def, args, [])
        if rejection is not None:
            logger.info(
                "claude react tool rejected tool=%s reason=%s",
                tool_name,
                rejection,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": False,
                                "status": "rejected",
                                "error": rejection,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            }

        request = ToolExecutionRequest(
            tool_name=tool_name,
            workdir=None,
            args=args,
            timeout_seconds=timeout,
        )

        try:
            result: ToolExecutionResult = handler(request)
        except Exception as exc:
            logger.exception(
                "claude react tool handler raised tool=%s elapsed_ms=%s",
                tool_name,
                int((time.perf_counter() - started) * 1000),
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": False,
                                "status": "failed",
                                "error": f"Tool handler error: {exc}",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            }

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        status = "completed" if result.ok else "failed"
        logger.info(
            "claude react tool finished tool=%s status=%s exit_code=%s elapsed_ms=%s summary=%s",
            tool_name,
            status,
            result.exit_code,
            elapsed_ms,
            _truncate(result.summary, limit=300),
        )

        text = _tool_observation_stdout(tool_name, result.stdout) or ""
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ok": result.ok,
                            "status": status,
                            "tool_name": tool_name,
                            "summary": result.summary,
                            "stdout": text,
                            "stderr": _truncate(result.stderr),
                            "artifacts": list(result.artifacts),
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        }

    return _wrapper


def build_claude_mcp_server(
    tool_definitions: list[ToolDefinition],
    *,
    server_name: str = "jarvis",
    tool_timeout_seconds: int = 60,
) -> tuple[Any, list[str]]:
    """Build an MCP server config and tool-name list for the SDK.

    Returns
    -------
    (mcp_server_config, sdk_tool_names)
        mcp_server_config can be passed to ClaudeAgentOptions(mcp_servers={...}).
        sdk_tool_names is the list of tool names the SDK expects (for
        ``allowed_tools`` / ``disallowed_tools``).
    """
    from claude_agent_sdk import create_sdk_mcp_server

    # Filter: skip coder-only tools (same boundary as existing react runtime).
    # Keep exposed_to_llm tools (or all tools in dev mode).
    filtered: list[Any] = []
    sdk_tool_names: list[str] = []

    for td in tool_definitions:
        if td.name in _CODER_ONLY_TOOLS:
            continue
        sdk_tool = adapt_jarvis_tool_to_sdk_tool(td, tool_timeout_seconds=tool_timeout_seconds)
        filtered.append(sdk_tool)
        # SDK expects MCP tool names as mcp__<server>__<tool>
        sdk_tool_names.append(f"mcp__{server_name}__{td.name}")

    server = create_sdk_mcp_server(
        name=server_name,
        version="0.1.0",
        tools=filtered,
    )
    return server, sdk_tool_names


def sdk_tool_name_for(jarvis_tool_name: str, *, server_name: str = "jarvis") -> str:
    """Map a Jarvis tool name to its SDK-visible MCP tool name."""
    return f"mcp__{server_name}__{jarvis_tool_name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, *, limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _tool_observation_stdout(tool_name: str, stdout: str) -> str:
    """Reduce Skill tool stdout to just the loaded content."""
    if tool_name not in _SKILL_TOOL_NAMES:
        return stdout
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return stdout
    if not isinstance(payload, dict):
        return stdout
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return stdout
