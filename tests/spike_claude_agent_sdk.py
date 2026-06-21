"""
Spike test: Claude Agent SDK integration for React runtime replacement.

Verifies:
  1. Custom tool injection via @tool() decorator + MCP server
  2. External model endpoint configuration (DeepSeek via ANTHROPIC_BASE_URL)
  3. System prompt enforcement
  4. max_turns limit (ReAct loop boundary)
  5. output_format for structured JSON
  6. Message stream inspection (what events are yielded)

Requirements:
  - claude-agent-sdk >= 0.2.106 installed
  - JARVIS_DEEPSEEK_API_KEY set or passed via env
  - DeepSeek Anthropic-compatible endpoint: https://api.deepseek.com/anthropic

Run:
  python tests/spike_claude_agent_sdk.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Configuration – adjust or override via environment
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("JARVIS_DEEPSEEK_API_KEY", "")
DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_TURNS = 3  # Simulate typical react node step budget


# ---------------------------------------------------------------------------
# 1. Define custom tools (simulating real Jarvis tools)
# ---------------------------------------------------------------------------


def _join_lines(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


# Tool 1: Current time / temporal context
async def _handler_get_current_time(args: dict[str, Any]) -> dict[str, Any]:
    """Return the current time in the specified timezone."""
    tz_name = str(args.get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz=timezone.utc)
    # Simulated – real impl would use zoneinfo
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "current_date": now.strftime("%Y-%m-%d"),
                        "current_time": now.strftime("%H:%M:%S"),
                        "timezone": tz_name,
                        "day_of_week": now.strftime("%A"),
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    }


# Tool 2: Simple calculator
async def _handler_calculator(args: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a simple arithmetic expression."""
    expression = str(args.get("expression", ""))
    try:
        # Safe eval – only arithmetic
        result = eval(expression, {"__builtins__": {}}, {})
        text = f"Result: {result}"
    except Exception as exc:
        text = f"Error: {exc}"
    return {"content": [{"type": "text", "text": text}]}


# Tool 3: Greeting tracker (for system prompt verification)
async def _handler_greeting_info(args: dict[str, Any]) -> dict[str, Any]:
    """Return greeting information."""
    name = str(args.get("name", "anonymous"))
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "greeting": f"Hello, {name}!",
                        "note": "This tool was called from a custom MCP server.",
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    }


# ---------------------------------------------------------------------------
# 2. Build MCP Server from tools (using claude_agent_sdk.tool decorator)
# ---------------------------------------------------------------------------


def build_test_mcp_server():
    """Build an MCP server config with custom Jarvis tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    # Define tools using the SDK's @tool decorator
    @tool(
        name="get_current_time",
        description="Get the current date and time. Use this when you need temporal context.",
        input_schema={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Timezone name, e.g. Asia/Shanghai",
                }
            },
        },
    )
    async def get_current_time(args):
        return await _handler_get_current_time(args)

    @tool(
        name="calculator",
        description="Evaluate a simple arithmetic expression. Input: an expression string.",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression to evaluate",
                }
            },
            "required": ["expression"],
        },
    )
    async def calculator(args):
        return await _handler_calculator(args)

    @tool(
        name="greeting_info",
        description="Get greeting information for a given name.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the person to greet",
                }
            },
            "required": ["name"],
        },
    )
    async def greeting_info(args):
        return await _handler_greeting_info(args)

    server = create_sdk_mcp_server(
        name="jarvis_test_tools",
        version="0.1.0",
        tools=[get_current_time, calculator, greeting_info],
    )
    return server


# ---------------------------------------------------------------------------
# 3. Test Runner
# ---------------------------------------------------------------------------


@dataclass
class SpikeResult:
    test_name: str
    ok: bool
    details: str = ""
    messages: list[Any] = None
    error: str | None = None

    def __post_init__(self):
        if self.messages is None:
            self.messages = []


async def _make_tool_permission_allow():
    """Return a can_use_tool callback that allows ALL tools (Jarvis does its own policy)."""
    from claude_agent_sdk import PermissionResultAllow

    async def _allow_all(
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow:
        return PermissionResultAllow()

    return _allow_all


async def _run_query(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    output_format: dict | None = None,
    env_extra: dict | None = None,
    mcp_server=None,
    mcp_tool_names: list[str] | None = None,
) -> SpikeResult:
    """Run a single query against Claude Agent SDK and collect messages."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    env = {
        "ANTHROPIC_BASE_URL": DEEPSEEK_ANTHROPIC_URL,
        "ANTHROPIC_AUTH_TOKEN": DEEPSEEK_API_KEY,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model or DEEPSEEK_MODEL,
    }
    if env_extra:
        env.update(env_extra)

    options_kwargs: dict = {
        "env": env,
        # bypassPermissions auto-approves all tool calls (Jarvis has its own policy layer)
        "permission_mode": "bypassPermissions",
    }
    if system_prompt is not None:
        options_kwargs["system_prompt"] = system_prompt
    if model is not None:
        options_kwargs["model"] = model
    if max_turns is not None:
        options_kwargs["max_turns"] = max_turns
    if output_format is not None:
        options_kwargs["output_format"] = output_format
    if mcp_server is not None:
        options_kwargs["mcp_servers"] = {"jarvis_test": mcp_server}
    if mcp_tool_names:
        options_kwargs["allowed_tools"] = mcp_tool_names

    options = ClaudeAgentOptions(**options_kwargs)

    messages: list[Any] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            messages.append(msg)
        return SpikeResult(test_name="query", ok=True, messages=messages)
    except Exception as exc:
        traceback.print_exc()
        return SpikeResult(
            test_name="query",
            ok=False,
            messages=messages,
            error=f"{type(exc).__name__}: {exc}",
        )


def _describe_messages(messages: list[Any]) -> str:
    """Summarize message types and key content."""
    lines = [f"Total messages received: {len(messages)}"]
    for i, msg in enumerate(messages):
        type_name = type(msg).__name__
        line = f"  [{i}] {type_name}"
        # Extract key fields
        if hasattr(msg, "role"):
            line += f"  role={msg.role}"
        if hasattr(msg, "content"):
            content_preview = str(msg.content)[:200]
            line += f"  content={content_preview}"
        if hasattr(msg, "status"):
            line += f"  status={msg.status}"
        if hasattr(msg, "tool_use"):
            tu = msg.tool_use
            if hasattr(tu, "name"):
                line += f"  tool={tu.name}"
            elif isinstance(tu, dict):
                line += f"  tool={tu.get('name', '?')}"
        lines.append(line)
    return "\n".join(lines)


def _extract_final_text(messages: list[Any]) -> str:
    """Extract the final text answer from messages."""
    for msg in reversed(messages):
        type_name = type(msg).__name__
        # AssistantMessage with TextBlock content
        if type_name == "AssistantMessage" and hasattr(msg, "content"):
            if isinstance(msg.content, list):
                for block in msg.content:
                    block_type = type(block).__name__
                    if block_type == "TextBlock" and hasattr(block, "text"):
                        return str(block.text)[:500]
            elif isinstance(msg.content, str):
                return msg.content
        # ResultMessage might have status
        if type_name == "ResultMessage":
            if hasattr(msg, "status") and msg.status:
                return f"Result: status={msg.status}"
            if hasattr(msg, "result"):
                return str(msg.result)[:500]
    return "(no final text found)"


# ---------------------------------------------------------------------------
# 4. Individual Tests
# ---------------------------------------------------------------------------


async def test_1_basic_echo():
    """Verify basic connectivity: no tools, simple prompt."""
    print("\n" + "=" * 60)
    print("TEST 1: Basic connectivity (echo)")
    print("=" * 60)
    result = await _run_query(
        prompt="Reply with exactly: 'OK basic test passed' and nothing else.",
        system_prompt="You are a test assistant. Be concise.",
        max_turns=1,
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


async def test_2_deepseek_provider():
    """Verify DeepSeek model routing works via ANTHROPIC_BASE_URL."""
    print("\n" + "=" * 60)
    print("TEST 2: DeepSeek model routing")
    print("=" * 60)
    result = await _run_query(
        prompt="What is 2024 + 2? Reply with just the number.",
        system_prompt="You are a test assistant.",
        model=DEEPSEEK_MODEL,
        max_turns=1,
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


async def test_3_system_prompt():
    """Verify system prompt is effective."""
    print("\n" + "=" * 60)
    print("TEST 3: System prompt enforcement")
    print("=" * 60)
    result = await _run_query(
        prompt="What is your job?",
        system_prompt=(
            "You are JarvisTestRuntime. Your ONLY job is to reply with: "
            "'system_prompt_verified: JarvisTestRuntime reporting'. "
            "Do not say anything else. Do not use tools if offered."
        ),
        max_turns=1,
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


async def test_4_custom_tools():
    """Verify custom tools can be injected and called."""
    print("\n" + "=" * 60)
    print("TEST 4: Custom tool injection")
    print("=" * 60)
    mcp = build_test_mcp_server()
    result = await _run_query(
        prompt="Call the greeting_info tool with name='World', then report what it returned.",
        system_prompt="You are a test assistant. Use tools when they help.",
        max_turns=3,
        mcp_server=mcp,
        mcp_tool_names=[
            "mcp__jarvis_test__greeting_info",
            "mcp__jarvis_test__get_current_time",
            "mcp__jarvis_test__calculator",
        ],
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


async def test_5_tool_loop():
    """Verify multi-turn tool use works (the core ReAct loop)."""
    print("\n" + "=" * 60)
    print("TEST 5: Multi-turn ReAct loop")
    print("=" * 60)
    mcp = build_test_mcp_server()
    result = await _run_query(
        prompt=(
            "1. Call get_current_time to get the time.\n"
            "2. Call calculator with expression '42 * 2' to compute something.\n"
            "3. Report both results back to me."
        ),
        system_prompt=(
            "You are a research assistant. Follow instructions step by step. "
            "Call tools when needed. Report results concisely."
        ),
        max_turns=5,
        mcp_server=mcp,
        mcp_tool_names=[
            "mcp__jarvis_test__greeting_info",
            "mcp__jarvis_test__get_current_time",
            "mcp__jarvis_test__calculator",
        ],
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


async def test_6_structured_output():
    """Verify structured JSON output via system prompt."""
    print("\n" + "=" * 60)
    print("TEST 6: Structured JSON output (system prompt enforced)")
    print("=" * 60)
    mcp = build_test_mcp_server()
    result = await _run_query(
        prompt="Get the current time and compute 100 + 23, then report both.",
        system_prompt=(
            "You are a research assistant. Use tools to get the current time "
            "and compute. At the end, output ONLY valid JSON with this schema: "
            '{"summary": "string", "current_date": "string", "current_time": "string", "computation": number}. '
            "Do NOT include markdown fences or any other text."
        ),
        max_turns=5,
        mcp_server=mcp,
        mcp_tool_names=[
            "mcp__jarvis_test__greeting_info",
            "mcp__jarvis_test__get_current_time",
            "mcp__jarvis_test__calculator",
        ],
    )
    print(f"  OK: {result.ok}")
    print(f"  Error: {result.error}")
    print(_describe_messages(result.messages))
    print(f"  Final text: {_extract_final_text(result.messages)}")
    return result


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------


async def main():
    print("=" * 60)
    print("Claude Agent SDK – Spike Test Suite")
    print(f"  Endpoint: {DEEPSEEK_ANTHROPIC_URL}")
    print(f"  Model:    {DEEPSEEK_MODEL}")
    print(f"  MaxTurns: {MAX_TURNS}")
    print("=" * 60)

    results: dict[str, SpikeResult] = {}

    # Run tests sequentially (avoid rate limiting)
    for name, coro in [
        ("basic_echo", test_1_basic_echo),
        ("deepseek_provider", test_2_deepseek_provider),
        ("system_prompt", test_3_system_prompt),
        ("custom_tools", test_4_custom_tools),
        ("tool_loop", test_5_tool_loop),
        ("structured_output", test_6_structured_output),
    ]:
        try:
            results[name] = await coro()
        except Exception as exc:
            results[name] = SpikeResult(test_name=name, ok=False, error=str(exc))
            print(f"\n  !!! {name} UNHANDLED: {exc}")

    # Summary
    print("\n\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    passed = 0
    total = 0
    for name, r in results.items():
        if r is None:
            status = "CRASH"
            error_msg = " – returned None"
        else:
            total += 1
            status = "PASS" if r.ok else "FAIL"
            if r.ok:
                passed += 1
            error_msg = f" – {r.error[:80]}" if (r.error and not r.ok) else ""
        print(f"  [{status}] {name}{error_msg}")
    if total > 0:
        print(f"\n  {passed}/{total} tests passed")
        if passed < total:
            sys.exit(1)
    else:
        print(f"\n  All tests crashed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
