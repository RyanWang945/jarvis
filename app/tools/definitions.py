from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.tools.coder import run_coder_tool
from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.shell import run_shell_command, run_shell_inspect

RiskLevel = Literal["low", "medium", "high", "critical"]
ExecutionMode = Literal["direct", "proposal"]
ToolHandler = Callable[[ToolExecutionRequest], ToolExecutionResult]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    args_schema: dict[str, Any]
    handler: ToolHandler
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = True
    execution_mode: ExecutionMode = "direct"
    requires_explicit_user_command: bool = False
    can_modify_files: bool = False
    requires_workdir: bool = False


def builtin_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell_inspect",
            description=(
                "Inspect the local environment and repository using read-only shell commands. "
                "Use this for listing files, reading file content, searching text, and checking git or tool status."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
            },
            handler=run_shell_inspect,
        ),
        ToolDefinition(
            name="shell_run_command",
            description=(
                "Run one explicit local shell command after Jarvis safety checks. "
                "Use this for targeted commands such as tests, lint, build, or diagnostic commands. "
                "Do not use this for multi-step repository workflows, code editing, git commit, or git push; "
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
            handler=run_shell_command,
            risk_level="medium",
            requires_explicit_user_command=True,
        ),
        ToolDefinition(
            name="delegate_to_claude_code",
            description=(
                "High-privilege delegation tool for repository development workflows. "
                "Use this only for substantial code tasks such as multi-file edits, refactors, "
                "bug fixes, code review follow-up, test execution, and git workflows inside a repository. "
                "Do not use this for simple shell commands, factual questions, or lightweight search. "
                "Before calling it, gather enough context to issue one complete task contract."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Detailed development task for the coder worker, including file constraints, "
                            "verification expectations, and whether commit or push is permitted."
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
                    "allow_commit": {
                        "type": "boolean",
                        "description": "Whether the coder worker may create a git commit.",
                        "default": False,
                    },
                    "allow_push": {
                        "type": "boolean",
                        "description": "Whether the coder worker may push to origin. Requires allow_commit=true.",
                        "default": False,
                    },
                },
                "required": ["instruction", "workdir"],
            },
            handler=run_coder_tool,
            risk_level="high",
            execution_mode="proposal",
            can_modify_files=True,
            requires_workdir=True,
        ),
    ]
