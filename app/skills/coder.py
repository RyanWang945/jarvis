from __future__ import annotations

import subprocess
from shutil import which
from pathlib import Path

from app.config import get_settings
from app.skills.base import SkillRequest, SkillResult


class CoderSkill:
    name = "coder"

    def run(self, request: SkillRequest) -> SkillResult:
        raw_instruction = str(request.args.get("instruction") or "").strip()
        instruction = _build_coder_instruction(raw_instruction, request)
        if not instruction:
            return SkillResult(ok=False, exit_code=None, summary="Coder instruction is required.")
        if not request.workdir:
            return SkillResult(ok=False, exit_code=None, summary="Coder workdir is required.")

        workdir = Path(request.workdir).resolve()
        if not workdir.exists() or not workdir.is_dir():
            return SkillResult(ok=False, exit_code=None, summary=f"Coder workdir does not exist: {workdir}")

        settings = get_settings()
        provider = "claude"
        provider_command = _resolve_cli_command()
        if provider_command is None:
            return SkillResult(ok=False, exit_code=None, summary="claude CLI was not found on PATH.")

        command = provider_command + [
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            "Read,Write,Edit,MultiEdit,Bash(git:*),Bash(type:*),Bash(dir),Bash(pwd)",
        ]

        try:
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                input=instruction,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.coder_timeout_seconds,
            )
        except FileNotFoundError:
            return SkillResult(ok=False, exit_code=None, summary="claude CLI was not found on PATH.")
        except subprocess.TimeoutExpired as exc:
            return SkillResult(
                ok=False,
                exit_code=None,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                summary=f"{provider} CLI timed out.",
            )

        return SkillResult(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            summary=f"{provider} CLI exited with code {completed.returncode}.",
        )


def _build_coder_instruction(instruction: str, request: SkillRequest) -> str:
    if not instruction:
        return ""
    rules = [
        "You are running as a Jarvis coder worker for a local repository.",
        "Operate only inside the working directory provided by the process cwd.",
        "Prefer direct file edits over explaining what should be changed.",
        "Before committing or pushing, inspect git status and the relevant diff.",
        "If the task explicitly asks to commit, create a focused commit with an appropriate message.",
        "If the task explicitly asks to push, push the current branch to origin after committing.",
        "Do not push unless the task explicitly asks for it.",
        "Do not modify unrelated files.",
        "End with a concise summary of files changed, commit hash if created, and push result if pushed.",
    ]
    verification_cmd = request.args.get("verification_cmd")
    if verification_cmd:
        rules.append(f"Run this verification command before finishing: {verification_cmd}")
    return "\n".join(
        [
            "Jarvis coder worker instructions:",
            *[f"- {rule}" for rule in rules],
            "",
            "User task:",
            instruction,
        ]
    )


def _resolve_cli_command() -> list[str] | None:
    executable = which("claude")
    if executable is None:
        return None
    path = Path(executable)
    if path.suffix.lower() == ".ps1":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "&",
            str(path),
        ]
    return [str(path)]
