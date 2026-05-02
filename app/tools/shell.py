from __future__ import annotations

import subprocess
from pathlib import Path

from app.tools.common import ToolExecutionRequest, ToolExecutionResult

MAX_OUTPUT_CHARS = 12_000


def run_shell_inspect(request: ToolExecutionRequest) -> ToolExecutionResult:
    return _run_subprocess(request)


def run_shell_command(request: ToolExecutionRequest) -> ToolExecutionResult:
    return _run_subprocess(request)


def _run_subprocess(request: ToolExecutionRequest) -> ToolExecutionResult:
    command = str(request.args.get("command", "")).strip()
    if not command:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="missing command", summary="Missing shell command.")

    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(request.workdir).resolve()) if request.workdir else None,
            capture_output=True,
            shell=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=request.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _truncate(exc.stdout or "")
        stderr = _truncate(exc.stderr or "")
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            summary=f"Command timed out after {request.timeout_seconds}s.",
        )
    except OSError as exc:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=str(exc),
            summary=f"Command failed to start: {exc}",
        )

    stdout = _truncate(completed.stdout or "")
    stderr = _truncate(completed.stderr or "")
    return ToolExecutionResult(
        ok=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        summary=f"Command exited with code {completed.returncode}.",
    )


def _truncate(value: str) -> str:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS] + "\n...[truncated]"

