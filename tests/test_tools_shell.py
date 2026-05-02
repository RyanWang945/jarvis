from app.tools.runtime import check_tool_policy, execute_tool, get_tool_definition


def test_shell_inspect_allows_read_only_git_status() -> None:
    tool = get_tool_definition("shell_inspect")

    rejection = check_tool_policy(tool, {"command": "git status", "workdir": "."}, messages=[])

    assert rejection is None


def test_shell_inspect_rejects_write_command() -> None:
    tool = get_tool_definition("shell_inspect")

    rejection = check_tool_policy(tool, {"command": "git commit -m test", "workdir": "."}, messages=[])

    assert rejection == "Rejected: shell_inspect only allows read-only inspection commands."


def test_shell_inspect_rejects_multi_command() -> None:
    tool = get_tool_definition("shell_inspect")

    rejection = check_tool_policy(tool, {"command": "git status && pwd", "workdir": "."}, messages=[])

    assert rejection == "Rejected: shell_inspect only allows a single read-only command."


def test_shell_run_command_rejects_dangerous_command() -> None:
    tool = get_tool_definition("shell_run_command")

    rejection = check_tool_policy(tool, {"command": "git push origin main", "workdir": "."}, messages=[])

    assert rejection == "Rejected: this command is too risky for shell_run_command; use delegate_to_claude_code or ask explicitly."


def test_shell_run_command_rejects_multi_command() -> None:
    tool = get_tool_definition("shell_run_command")

    rejection = check_tool_policy(tool, {"command": "pytest && git status", "workdir": "."}, messages=[])

    assert rejection == "Rejected: shell_run_command only allows one command at a time."


def test_shell_run_command_executes_single_command() -> None:
    tool = get_tool_definition("shell_run_command")

    result = execute_tool(
        tool,
        {
            "command": "python -c \"print('shell-ok')\"",
            "workdir": ".",
        },
        timeout_seconds=10,
    )

    assert result.ok is True
    assert "shell-ok" in result.stdout


def test_shell_inspect_rejects_absolute_path_outside_workspace() -> None:
    tool = get_tool_definition("shell_inspect")

    rejection = check_tool_policy(tool, {"command": r"dir C:\Users\Administrator"}, messages=[])

    assert rejection is not None
    assert "inside the Jarvis workspace" in rejection
