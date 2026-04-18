from app.tools.specs import ToolSpec


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc

    def list(self, *, exposed_to_llm: bool | None = None) -> list[ToolSpec]:
        tools = list(self._tools.values())
        if exposed_to_llm is None:
            return tools
        return [tool for tool in tools if tool.exposed_to_llm is exposed_to_llm]


def get_default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="echo",
                description="Echo the task instruction without external side effects.",
                args_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                skill="echo",
                action="echo",
                risk_level="low",
                exposed_to_llm=True,
            ),
            ToolSpec(
                name="run_shell_command",
                description="Run a local shell command after Jarvis risk checks.",
                args_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "workdir": {"type": "string"},
                    },
                    "required": ["command"],
                },
                skill="shell",
                action="run",
                risk_level="medium",
                exposed_to_llm=True,
            ),
            ToolSpec(
                name="run_tests",
                description="Run a configured project test command.",
                args_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": ["uv run pytest", "pytest"],
                        },
                        "workdir": {"type": "string"},
                    },
                    "required": ["command"],
                },
                skill="shell",
                action="run",
                risk_level="low",
                exposed_to_llm=True,
            ),
            ToolSpec(
                name="delegate_to_claude_code",
                description="Delegate a code-editing task to the local Claude Code CLI.",
                args_schema={
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string"},
                        "workdir": {"type": "string"},
                        "verification_cmd": {"type": "string"},
                    },
                    "required": ["instruction", "workdir"],
                },
                skill="claude_code",
                action="run",
                risk_level="high",
                exposed_to_llm=True,
            ),
        ]
    )
