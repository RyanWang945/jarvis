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
                description=(
                    "Run a simple local shell command after Jarvis risk checks. "
                    "Do not use this for multi-step code editing, git commit, or git push workflows; "
                    "use delegate_to_claude_code for those."
                ),
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
                description=(
                    "Delegate a repository development workflow to the local Claude Code CLI. "
                    "Use this for code/file edits, README updates, tests, "
                    "git status/diff review, git commit, and git push when the user explicitly asks "
                    "to commit or push. The worker runs in the provided workdir and should complete "
                    "the workflow end-to-end instead of asking the user to type shell commands."
                ),
                args_schema={
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": (
                                "Detailed development task for the coder worker, including whether "
                                "to commit and/or push. Include file constraints and commit message "
                                "requirements if provided by the user."
                            ),
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the target repository working directory.",
                        },
                        "verification_cmd": {
                            "type": "string",
                            "description": "Optional command the coder worker should run before finishing.",
                        },
                    },
                    "required": ["instruction", "workdir"],
                },
                skill="claude_code",
                action="run",
                risk_level="high",
                exposed_to_llm=True,
            ),
            ToolSpec(
                name="web_search",
                description="Search the web for current public information using Tavily.",
                args_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
                        "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
                    },
                    "required": ["query"],
                },
                skill="web_search",
                action="search",
                risk_level="low",
                exposed_to_llm=True,
            ),
        ]
    )
