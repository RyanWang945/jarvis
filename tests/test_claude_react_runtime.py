"""Tests for ClaudeReactNodeExecuteRuntime and claude_tool_adapter."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.task_runtime.node_result import NodeResult
from app.task_runtime.planner import PlanNode


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _plan_node(**kwargs) -> PlanNode:
    defaults = {
        "id": kwargs.pop("id", "node-1"),
        "runtime": kwargs.pop("runtime", "react"),
        "objective": kwargs.pop("objective", "Test objective"),
    }
    defaults.update(kwargs)
    return PlanNode(**defaults)


def _context(**kwargs):
    from app.task_runtime.node_execute_runtime import NodeExecutionContext

    return NodeExecutionContext(
        user_objective=kwargs.get("user_objective", "test objective"),
        node=kwargs.get("node", _plan_node()),
        resolved_inputs=kwargs.get("resolved_inputs", []),
        legacy_hints=kwargs.get("legacy_hints", {}),
        instructions=kwargs.get("instructions", []),
    )


# ---------------------------------------------------------------------------
#  Tool adapter tests
# ---------------------------------------------------------------------------


class TestToolAdapter:
    """Tests for claude_tool_adapter module."""

    def test_imports(self):
        from app.task_runtime import claude_tool_adapter as module

        assert hasattr(module, "adapt_jarvis_tool_to_sdk_tool")
        assert hasattr(module, "build_claude_mcp_server")
        assert hasattr(module, "sdk_tool_name_for")

    def test_sdk_tool_name_convention(self):
        from app.task_runtime.claude_tool_adapter import sdk_tool_name_for

        assert sdk_tool_name_for("tavily_search") == "mcp__jarvis__tavily_search"
        assert sdk_tool_name_for("read_file") == "mcp__jarvis__read_file"

    def test_coder_only_tools_excluded(self):
        """Verify coder-only tools are filtered out when building MCP server."""
        from app.tools.definitions import ToolDefinition
        from unittest.mock import MagicMock

        def _noop_handler(req):
            from app.tools.common import ToolExecutionResult
            return ToolExecutionResult(ok=True, exit_code=0)

        tool_defs = [
            ToolDefinition(
                name="tavily_search",
                description="Search web",
                args_schema={"type": "object", "properties": {}},
                handler=_noop_handler,
            ),
            ToolDefinition(
                name="shell_inspect",
                description="Run shell command",
                args_schema={"type": "object", "properties": {}},
                handler=_noop_handler,
            ),
            ToolDefinition(
                name="delegate_to_claude_code",
                description="Delegate to Claude",
                args_schema={"type": "object", "properties": {}},
                handler=_noop_handler,
            ),
        ]

        mock_sdk = MagicMock()
        mock_sdk.create_sdk_mcp_server.return_value = MagicMock()
        mock_sdk.tool = lambda **kw: (lambda fn: fn)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}):
            from app.task_runtime.claude_tool_adapter import build_claude_mcp_server

            _server, tool_names = build_claude_mcp_server(tool_defs)

        assert "mcp__jarvis__tavily_search" in tool_names
        assert "mcp__jarvis__shell_inspect" not in tool_names
        assert "mcp__jarvis__delegate_to_claude_code" not in tool_names


# ---------------------------------------------------------------------------
#  Runtime protocol compliance tests
# ---------------------------------------------------------------------------


class TestClaudeReactRuntimeSkip:
    """Tests for runtime skip conditions."""

    def test_skip_when_sdk_not_available(self):
        """Runtime should return blocked when SDK is not installed."""
        with patch("app.task_runtime.claude_react_runtime.is_claude_agent_sdk_available", return_value=False):
            from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

            runtime = ClaudeReactNodeExecuteRuntime()
            result = runtime.run(_context())

        assert result.status == "blocked"
        assert result.runtime == "react"
        assert result.error is not None

    def test_skip_when_no_api_key(self):
        """Runtime should return blocked when no API key is configured."""
        with patch("app.task_runtime.claude_react_runtime.is_claude_agent_sdk_available", return_value=True):
            from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

            runtime = ClaudeReactNodeExecuteRuntime(
                model_resolver=lambda ctx: MagicMock(
                    profile=MagicMock(api_key=None, model=None),
                )
            )
            result = runtime.run(_context())

        assert result.status == "blocked"
        assert result.runtime == "react"
        assert result.error is not None
        assert "api" in result.error.message.lower() or "key" in result.error.message.lower()


class TestClaudeReactRuntimeProtocol:
    """Verify the runtime satisfies the NodeExecuteRuntime protocol."""

    def test_has_run_method(self):
        from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

        runtime = ClaudeReactNodeExecuteRuntime()
        assert callable(runtime.run)

    def test_run_accepts_context(self):
        from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime
        from app.task_runtime.node_execute_runtime import NodeExecutionContext

        runtime = ClaudeReactNodeExecuteRuntime()
        ctx = _context()
        assert isinstance(ctx, NodeExecutionContext)

    def test_returns_node_result_type(self):
        """Run returns a NodeResult with runtime='react'."""
        with patch("app.task_runtime.claude_react_runtime.is_claude_agent_sdk_available", return_value=False):
            from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

            runtime = ClaudeReactNodeExecuteRuntime()
            result = runtime.run(_context())

        assert isinstance(result, NodeResult)
        assert result.node_id == "node-1"
        assert result.runtime == "react"


# ---------------------------------------------------------------------------
#  Runtime execution tests (mocked SDK)
# ---------------------------------------------------------------------------


class TestClaudeReactRuntimeExecution:
    """End-to-end execution tests with mocked Claude Agent SDK."""

    @pytest.fixture(autouse=True)
    def _mock_sdk(self):
        """Mock the entire SDK layer for unit testing."""
        import sys as _sys

        async def _fake_query(**kwargs):
            """Simulate a simple SDK message stream."""
            text_block = type("TextBlock", (), {"text": "Mocked response: task completed successfully."})()
            msg = type("AssistantMessage", (), {"content": [text_block]})()
            yield msg
            result = type("ResultMessage", (), {"status": "completed"})()
            yield result

        mock_sdk = MagicMock()
        mock_sdk.ClaudeAgentOptions = MagicMock(return_value=MagicMock())
        mock_sdk.query = _fake_query
        mock_sdk.create_sdk_mcp_server = MagicMock(return_value=MagicMock())
        mock_sdk.tool = lambda **kw: (lambda fn: fn)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}):
            yield

    def test_execution_with_sdk(self):
        """Full execution path with mocked SDK: runtime name is 'react'."""
        from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime, is_claude_agent_sdk_available

        assert is_claude_agent_sdk_available(), "SDK mock should make this True"

        runtime = ClaudeReactNodeExecuteRuntime(
            model_resolver=lambda ctx: MagicMock(
                profile=MagicMock(api_key="test-key", model="deepseek-v4-flash"),
            ),
            max_turns=3,
        )
        result = runtime.run(_context())

        assert result.status == "completed"
        assert result.runtime == "react"
        assert result.data.get("runtime_backend") == "claude_agent_sdk"
        assert "Mocked response" in result.summary

    def test_execution_handles_error(self):
        """Runtime should handle SDK query errors gracefully."""
        import sys as _sys

        async def _failing_query(**kwargs):
            raise RuntimeError("Simulated SDK failure")

        mock_sdk = MagicMock()
        mock_sdk.ClaudeAgentOptions = MagicMock(return_value=MagicMock())
        mock_sdk.query = _failing_query
        mock_sdk.create_sdk_mcp_server = MagicMock(return_value=MagicMock())
        mock_sdk.tool = lambda **kw: (lambda fn: fn)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}):
            from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

            runtime = ClaudeReactNodeExecuteRuntime(
                model_resolver=lambda ctx: MagicMock(
                    profile=MagicMock(api_key="test-key", model="deepseek-v4-flash"),
                ),
            )
            result = runtime.run(_context())

        assert result.status == "failed"
        assert result.runtime == "react"
        assert result.error is not None


# ---------------------------------------------------------------------------
#  Prompt construction tests
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    """Tests for system prompt and user prompt construction."""

    def test_fallback_system_prompt(self):
        from app.task_runtime.claude_react_runtime import _fallback_system_prompt

        prompt = _fallback_system_prompt()
        assert "Jarvis" in prompt
        assert "ClaudeReactNodeExecuteRuntime" in prompt
        assert "coder" in prompt.lower()

    def test_user_prompt_structure(self):
        from app.task_runtime.claude_react_runtime import _build_user_prompt

        prompt = _build_user_prompt(_context())
        data = json.loads(prompt)
        assert "user_objective" in data
        assert "node" in data
        assert "resolved_inputs" in data
        assert "temporal_context" in data
        assert "instructions" in data

    def test_endpoint_resolution_deepseek(self):
        from app.task_runtime.claude_react_runtime import _resolve_claude_endpoint

        settings = MagicMock()
        settings.llm_provider = "deepseek"

        profile = MagicMock(provider="deepseek")
        endpoint = _resolve_claude_endpoint(settings, profile)
        assert "deepseek" in endpoint
        assert "anthropic" in endpoint


# ---------------------------------------------------------------------------
#  Config-driven mutual exclusivity tests
# ---------------------------------------------------------------------------


class TestMutualExclusivity:
    """react backend is mutually exclusive: builtin OR claude_agent_sdk, never both."""

    def test_react_uses_builtin_by_default(self):
        """Default config: react slot is builtin."""
        with patch("app.task_runtime.agent_runtime.get_settings") as mock_settings:
            mock_settings.return_value.react_runtime_backend = "builtin"
            from app.task_runtime.agent_runtime import _build_default_runtimes

            runtimes = _build_default_runtimes()
            from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime
            from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

            assert isinstance(runtimes["react"], ReactNodeExecuteRuntime)

    def test_react_uses_claude_agent_sdk_when_configured(self):
        """When config=claude_agent_sdk and SDK installed: react slot is ClaudeAgent."""
        with patch("app.task_runtime.agent_runtime.get_settings") as mock_settings:
            mock_settings.return_value.react_runtime_backend = "claude_agent_sdk"
            with patch("app.task_runtime.agent_runtime.is_claude_agent_sdk_available", return_value=True):
                from app.task_runtime.agent_runtime import _build_default_runtimes

                runtimes = _build_default_runtimes()
                from app.task_runtime.claude_react_runtime import ClaudeReactNodeExecuteRuntime

                assert isinstance(runtimes["react"], ClaudeReactNodeExecuteRuntime)

    def test_react_falls_back_to_builtin_when_sdk_missing(self):
        """When config=claude_agent_sdk but SDK not installed: fallback to builtin."""
        with patch("app.task_runtime.agent_runtime.get_settings") as mock_settings:
            mock_settings.return_value.react_runtime_backend = "claude_agent_sdk"
            with patch("app.task_runtime.agent_runtime.is_claude_agent_sdk_available", return_value=False):
                from app.task_runtime.agent_runtime import _build_default_runtimes

                runtimes = _build_default_runtimes()
                from app.task_runtime.node_execute_runtime import ReactNodeExecuteRuntime

                assert isinstance(runtimes["react"], ReactNodeExecuteRuntime)

    def test_only_one_react_key_exists(self):
        """The dictionary should have exactly one 'react' key, never 'claude_react'."""
        for backend in ("builtin", "claude_agent_sdk"):
            with patch("app.task_runtime.agent_runtime.get_settings") as mock_settings:
                mock_settings.return_value.react_runtime_backend = backend
                with patch("app.task_runtime.agent_runtime.is_claude_agent_sdk_available", return_value=True):
                    from app.task_runtime.agent_runtime import _build_default_runtimes

                    runtimes = _build_default_runtimes()
                    assert "react" in runtimes
                    assert "claude_react" not in runtimes
                    assert len([k for k in runtimes if "react" in k]) == 1

    def test_available_runtimes_always_standard_names(self):
        """available_runtimes is always ['llm', 'react', 'coder']."""
        from app.task_runtime.agent_runtime import _default_available_runtimes

        names = _default_available_runtimes()
        assert names == ["llm", "react", "coder"]
        assert "claude_react" not in names


# ---------------------------------------------------------------------------
#  Planner type tests
# ---------------------------------------------------------------------------


class TestPlannerTypes:
    """Tests for NodeRuntime type in planner."""

    def test_react_in_runtimes(self):
        from app.task_runtime.planner import _RUNTIMES

        assert "react" in _RUNTIMES

    def test_claude_react_not_in_runtimes(self):
        """claude_react is NOT a separate runtime type — it's a backend of 'react'."""
        from app.task_runtime.planner import _RUNTIMES

        assert "claude_react" not in _RUNTIMES

    def test_react_node_validation(self):
        from app.task_runtime.planner import PlanNode

        node = PlanNode(
            id="test-node",
            runtime="react",
            objective="Test react node",
        )
        assert node.runtime == "react"
