from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.config import get_settings
from app.tools.definitions import ToolDefinition, builtin_tool_definitions
from app.tools.common import ToolExecutionRequest, ToolExecutionResult

_CODE_REQUEST_MARKERS = (
    "code",
    "coding",
    "bug",
    "fix",
    "repair",
    "change",
    "modify",
    "refactor",
    "repo",
    "repository",
    "commit",
    "push",
    "test",
    ".py",
    ".ts",
    ".js",
    ".md",
)

_TOOLS = {tool.name: tool for tool in builtin_tool_definitions()}
_SHELL_SEPARATOR_PATTERN = re.compile(r"(\&\&)|(\|\|)|(;)|(\|)")
_INSPECT_ALLOW_PREFIXES = (
    "pwd",
    "Get-Location",
    "ls",
    "dir",
    "Get-ChildItem",
    "type ",
    "cat ",
    "Get-Content ",
    "rg ",
    "git status",
    "git diff",
    "git branch",
    "git log",
    "python --version",
    "uv --version",
    "pytest --version",
)
_COMMAND_DENY_PREFIXES = (
    "rm ",
    "del ",
    "Remove-Item ",
    "mv ",
    "move ",
    "Move-Item ",
    "cp ",
    "copy ",
    "Copy-Item ",
    "git commit",
    "git push",
    "git reset",
    "git checkout",
    "git clean",
    "pip install",
    "uv add",
    "npm install",
)
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^\s\"']*")
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])/[^\s\"']*")


def list_tool_definitions(*, exposed_to_llm: bool | None = None) -> list[ToolDefinition]:
    tools = list(_TOOLS.values())
    if exposed_to_llm is not None:
        tools = [tool for tool in tools if tool.exposed_to_llm is exposed_to_llm]
    return tools


def get_tool_definition(name: str) -> ToolDefinition:
    try:
        return _TOOLS[name]
    except KeyError as exc:
        raise ValueError(f"unknown tool: {name}") from exc


def build_llm_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.args_schema,
            },
        }
        for tool in list_tool_definitions(exposed_to_llm=True)
    ]


def check_tool_policy(tool: ToolDefinition, tool_args: dict[str, Any], messages: list[BaseMessage]) -> str | None:
    command = str(tool_args.get("command") or "").strip()
    if tool.name == "shell_inspect":
        return _check_shell_inspect(command)
    if tool.name == "shell_run_command":
        return _check_shell_command(command)
    if tool.execution_mode != "proposal":
        return None

    workdir = str(tool_args.get("workdir") or "").strip()
    instruction = str(tool_args.get("instruction") or "").strip()
    latest_user = _latest_user_message(messages)

    if not instruction:
        return "Rejected: high-privilege delegation requires a non-empty instruction."
    if tool.requires_workdir and not workdir:
        return "Rejected: high-privilege delegation requires an explicit workdir."
    if tool.can_modify_files and not _looks_like_code_request(latest_user):
        return (
            "Rejected: high-privilege delegation is reserved for explicit repository or code tasks. "
            "Use regular tools or answer directly unless the user clearly asked for code changes."
        )

    allow_push = bool(tool_args.get("allow_push"))
    allow_commit = bool(tool_args.get("allow_commit"))
    if allow_push and not allow_commit:
        return "Rejected: allow_push=true requires allow_commit=true."
    if workdir and tool.requires_workdir and not Path(workdir).exists():
        return f"Rejected: workdir does not exist: {workdir}"
    return None


def execute_tool(tool: ToolDefinition, tool_args: dict[str, Any], *, timeout_seconds: int = 30) -> ToolExecutionResult:
    workdir_value = tool_args.get("workdir")
    workdir: str | None
    if workdir_value:
        workdir = str(workdir_value)
    elif tool.name in {"shell_inspect", "shell_run_command"}:
        workdir = str(get_settings().workspace_root)
    else:
        workdir = None
    request = ToolExecutionRequest(
        tool_name=tool.name,
        workdir=workdir,
        args=tool_args,
        timeout_seconds=timeout_seconds,
    )
    return tool.handler(request)


def _latest_user_message(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _looks_like_code_request(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _CODE_REQUEST_MARKERS):
        return True
    return bool(re.search(r"\b[a-z0-9_\-\\/]+\.(py|ts|js|tsx|jsx|md|json|yaml|yml)\b", lowered))


def _check_shell_inspect(command: str) -> str | None:
    if not command:
        return "Rejected: shell_inspect requires a non-empty command."
    if _SHELL_SEPARATOR_PATTERN.search(command):
        return "Rejected: shell_inspect only allows a single read-only command."
    normalized = command.strip()
    if not any(normalized.startswith(prefix) for prefix in _INSPECT_ALLOW_PREFIXES):
        return "Rejected: shell_inspect only allows read-only inspection commands."
    path_rejection = _check_workspace_path_constraints(command)
    if path_rejection is not None:
        return path_rejection
    return None


def _check_shell_command(command: str) -> str | None:
    if not command:
        return "Rejected: shell_run_command requires a non-empty command."
    if _SHELL_SEPARATOR_PATTERN.search(command):
        return "Rejected: shell_run_command only allows one command at a time."
    normalized = command.strip()
    if any(normalized.startswith(prefix) for prefix in _COMMAND_DENY_PREFIXES):
        return "Rejected: this command is too risky for shell_run_command; use delegate_to_claude_code or ask explicitly."
    path_rejection = _check_workspace_path_constraints(command)
    if path_rejection is not None:
        return path_rejection
    return None


def _check_workspace_path_constraints(command: str) -> str | None:
    workspace_root = get_settings().workspace_root.resolve()
    for raw_path in _extract_absolute_paths(command):
        candidate = Path(raw_path).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError:
            return (
                "Rejected: shell tools may only inspect paths inside the Jarvis workspace. "
                f"Outside path detected: {raw_path}"
            )
    return None


def _extract_absolute_paths(command: str) -> list[str]:
    paths = _WINDOWS_ABSOLUTE_PATH_PATTERN.findall(command)
    paths.extend(_POSIX_ABSOLUTE_PATH_PATTERN.findall(command))
    return paths
