from __future__ import annotations

import json
import os
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

        preflight_notes = _prepare_workspace(workdir)
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

        postflight = _collect_postflight(workdir)
        stdout = completed.stdout
        if preflight_notes:
            stdout = f"{stdout}\n\n[JARVIS_PREFLIGHT]\n" + "\n".join(preflight_notes)
        stdout = f"{stdout}\n\n[JARVIS_POSTFLIGHT]\n{json.dumps(postflight, ensure_ascii=False, indent=2)}"
        artifacts = _postflight_artifacts(postflight)

        return SkillResult(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=completed.stderr,
            artifacts=artifacts,
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


def _prepare_workspace(workdir: Path) -> list[str]:
    notes: list[str] = []
    git_dir = workdir / ".git"
    if git_dir.is_dir():
        lock = git_dir / "index.lock"
        if lock.exists() and _is_zero_byte(lock):
            _unlink_path(lock)
            notes.append("Removed stale .git/index.lock.")
    if os.name == "nt":
        reserved_names = {"nul", "con", "prn", "aux"}
        for child in workdir.iterdir():
            if child.name.lower() in reserved_names and _is_zero_byte(child):
                _unlink_path(child)
                notes.append(f"Removed stale Windows reserved-name file: {child.name}.")
    return notes


def _collect_postflight(workdir: Path) -> dict[str, object]:
    status = _run_git(workdir, "status", "--short", "--branch")
    branch = _run_git(workdir, "branch", "--show-current")
    commit_hash = _run_git(workdir, "rev-parse", "--short", "HEAD")
    commit_subject = _run_git(workdir, "log", "-1", "--pretty=%s")
    remote = _run_git(workdir, "remote", "get-url", "origin")
    status_stdout = status["stdout"].strip()
    branch_name = branch["stdout"].strip()
    return {
        "git_available": status["exit_code"] is not None,
        "status_exit_code": status["exit_code"],
        "status": status_stdout,
        "branch": branch_name,
        "commit": commit_hash["stdout"].strip(),
        "commit_subject": commit_subject["stdout"].strip(),
        "origin": remote["stdout"].strip(),
        "working_tree_clean": _is_working_tree_clean(status_stdout),
        "synced_with_upstream": _is_synced_with_upstream(status_stdout),
        "status_stderr": status["stderr"].strip(),
    }


def _run_git(workdir: Path, *args: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={workdir}", *args],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": None, "stdout": "", "stderr": str(exc)}
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _postflight_artifacts(postflight: dict[str, object]) -> list[str]:
    artifacts: list[str] = []
    if postflight.get("commit"):
        artifacts.append(f"git_commit:{postflight['commit']}")
    if postflight.get("branch"):
        artifacts.append(f"git_branch:{postflight['branch']}")
    if postflight.get("working_tree_clean"):
        artifacts.append("git_worktree:clean")
    else:
        artifacts.append("git_worktree:dirty")
    if postflight.get("synced_with_upstream"):
        artifacts.append("git_upstream:synced")
    return artifacts


def _is_working_tree_clean(status_stdout: str) -> bool:
    lines = [line for line in status_stdout.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("## ") for line in lines)


def _is_synced_with_upstream(status_stdout: str) -> bool:
    first_line = next((line for line in status_stdout.splitlines() if line.startswith("## ")), "")
    if "..." not in first_line:
        return False
    return "[" not in first_line


def _is_zero_byte(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def _unlink_path(path: Path) -> None:
    try:
        path.unlink()
        return
    except OSError:
        if os.name != "nt":
            raise
    extended = "\\\\?\\" + str(path)
    os.remove(extended)
