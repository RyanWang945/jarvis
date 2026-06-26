from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which

from app.config import get_settings
from app.tools.coder_common import build_coder_instruction as build_common_coder_instruction
from app.tools.coder_common import check_coder_permissions
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_coder_tool(request: ToolExecutionRequest) -> ToolExecutionResult:
    raw_instruction = str(request.args.get("instruction") or "").strip()
    instruction = _build_coder_instruction(raw_instruction, request)
    if not instruction:
        return ToolExecutionResult(ok=False, exit_code=None, summary="Coder instruction is required.")
    if not request.workdir:
        return ToolExecutionResult(ok=False, exit_code=None, summary="Coder workdir is required.")

    workdir = Path(request.workdir).resolve()
    if not workdir.exists() or not workdir.is_dir():
        return ToolExecutionResult(ok=False, exit_code=None, summary=f"Coder workdir does not exist: {workdir}")

    settings = get_settings()
    provider = "claude"
    provider_command = _resolve_cli_command()
    if provider_command is None:
        return ToolExecutionResult(ok=False, exit_code=None, summary="claude CLI was not found on PATH.")

    run_dir = _runtime_run_dir(request.args)
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
    preflight_notes = _prepare_workspace(workdir)
    preflight = _collect_postflight(workdir)
    permission_mode = str(request.args.get("_permission_mode") or "delegate").strip() or "delegate"
    command = provider_command + [
        "--print",
        "--permission-mode",
        permission_mode,
        "--allowedTools",
        "Read,Write,Edit,MultiEdit,Bash(git:*),Bash(type:*),Bash(dir),Bash(pwd)",
    ]

    try:
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = str(workdir)
        completed = subprocess.run(
            command,
            cwd=str(workdir),
            input=instruction,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=settings.coder_timeout_seconds,
        )
    except FileNotFoundError:
        return ToolExecutionResult(ok=False, exit_code=None, summary="claude CLI was not found on PATH.")
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_timeout_output(exc.stdout)
        stderr = _coerce_timeout_output(exc.stderr)
        artifacts: list[str] = []
        if run_dir is not None:
            _write_provider_run_files(
                run_dir,
                stdout=stdout,
                stderr=stderr,
                audit="claude CLI timed out.",
                artifacts=artifacts,
            )
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            summary=f"{provider} CLI timed out.",
        )

    postflight = _collect_postflight(workdir)
    permission_check = check_coder_permissions(
        preflight,
        postflight,
        allow_commit=bool(request.args.get("allow_commit")),
        allow_push=bool(request.args.get("allow_push")),
    )
    stdout = completed.stdout
    if preflight_notes:
        stdout = f"{stdout}\n\n[JARVIS_PREFLIGHT]\n" + "\n".join(preflight_notes)
    stdout = f"{stdout}\n\n[JARVIS_POSTFLIGHT]\n{json.dumps(postflight, ensure_ascii=False, indent=2)}"
    artifacts = _postflight_artifacts(postflight)
    for violation in permission_check["violations"]:
        artifacts.append(f"permission_violation:{violation}")
    if run_dir is not None:
        _write_provider_run_files(
            run_dir,
            stdout=completed.stdout,
            stderr=completed.stderr,
            audit=_compose_audit_report(
                preflight=preflight,
                postflight=postflight,
                permission_check=permission_check,
                preflight_notes=preflight_notes,
            ),
            artifacts=artifacts,
        )
    ok = completed.returncode == 0 and bool(permission_check["ok"])
    summary = f"{provider} CLI exited with code {completed.returncode}."
    if not permission_check["ok"]:
        summary = summary + " Permission check failed: " + "; ".join(str(v) for v in permission_check["violations"])

    return ToolExecutionResult(
        ok=ok,
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=completed.stderr,
        artifacts=artifacts,
        summary=summary,
    )


def _build_coder_instruction(instruction: str, request: ToolExecutionRequest) -> str:
    return build_common_coder_instruction(instruction, request.args)


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
    porcelain_status = _run_git(workdir, "status", "--porcelain")
    diff_stat = _run_git(workdir, "diff", "--stat")
    branch = _run_git(workdir, "branch", "--show-current")
    commit_hash = _run_git(workdir, "rev-parse", "--short", "HEAD")
    head = _run_git(workdir, "rev-parse", "HEAD")
    commit_subject = _run_git(workdir, "log", "-1", "--pretty=%s")
    remote = _run_git(workdir, "remote", "get-url", "origin")
    upstream = _run_git(workdir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream_head = (
        _run_git(workdir, "rev-parse", "@{u}")
        if upstream["exit_code"] == 0
        else {"exit_code": None, "stdout": "", "stderr": ""}
    )
    status_stdout = str(status["stdout"]).strip()
    branch_name = str(branch["stdout"]).strip()
    return {
        "git_available": status["exit_code"] == 0,
        "status_exit_code": status["exit_code"],
        "status": status_stdout,
        "branch": branch_name,
        "head": str(head["stdout"]).strip(),
        "commit": str(commit_hash["stdout"]).strip(),
        "commit_subject": str(commit_subject["stdout"]).strip(),
        "origin": str(remote["stdout"]).strip(),
        "upstream_name": str(upstream["stdout"]).strip(),
        "upstream_head": str(upstream_head["stdout"]).strip(),
        "upstream_available": upstream["exit_code"] == 0 and bool(str(upstream["stdout"]).strip()),
        "working_tree_clean": _is_working_tree_clean(status_stdout),
        "synced_with_upstream": _is_synced_with_upstream(status_stdout),
        "files_modified": _modified_files_from_status(str(porcelain_status["stdout"])),
        "diff_stat": str(diff_stat["stdout"]).strip(),
        "diff_stat_stderr": str(diff_stat["stderr"]).strip(),
        "status_stderr": str(status["stderr"]).strip(),
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
    for path in postflight.get("files_modified") or []:
        artifacts.append(f"git_file:{path}")
    return artifacts


def _runtime_run_dir(args: dict[str, object]) -> Path | None:
    raw = str(args.get("_runtime_run_dir") or "").strip()
    return Path(raw).resolve() if raw else None


def _write_provider_run_files(
    run_dir: Path,
    *,
    stdout: str,
    stderr: str,
    audit: str,
    artifacts: list[str],
) -> None:
    stdout_path = run_dir / "claude-stdout.log"
    _write_text(stdout_path, stdout)
    artifacts.append(f"claude_stdout:{stdout_path}")
    if stderr:
        stderr_path = run_dir / "claude-stderr.log"
        _write_text(stderr_path, stderr)
        artifacts.append(f"claude_stderr:{stderr_path}")
    audit_path = run_dir / "jarvis-audit.log"
    _write_text(audit_path, audit)
    artifacts.append(f"jarvis_audit:{audit_path}")


def _compose_audit_report(
    *,
    preflight: dict[str, object],
    postflight: dict[str, object],
    permission_check: dict[str, object],
    preflight_notes: list[str],
) -> str:
    lines: list[str] = []
    if preflight_notes:
        lines.extend(["[JARVIS_PREFLIGHT_NOTES]", *[f"- {note}" for note in preflight_notes], ""])
    lines.extend(
        [
            "[JARVIS_PREFLIGHT]",
            _state_line(preflight),
            "",
            "[JARVIS_POSTFLIGHT]",
            _state_line(postflight),
            "",
            "[JARVIS_PERMISSION_CHECK]",
            (
                f"commit_allowed={str(permission_check['commit_allowed']).lower()} "
                f"commit_changed={str(permission_check['commit_changed']).lower()} "
                f"result={permission_check['commit_check']}"
            ),
            (
                f"push_allowed={str(permission_check['push_allowed']).lower()} "
                f"upstream_changed={str(permission_check['upstream_changed']).lower()} "
                f"result={permission_check['upstream_check']}"
            ),
        ]
    )
    warnings = [str(item) for item in permission_check.get("warnings", [])]
    violations = [str(item) for item in permission_check.get("violations", [])]
    if warnings:
        lines.extend(["", "[JARVIS_PERMISSION_WARNINGS]", *[f"- {warning}" for warning in warnings]])
    if violations:
        lines.extend(["", "[JARVIS_PERMISSION_VIOLATIONS]", *[f"- {violation}" for violation in violations]])
    return "\n".join(lines).strip()


def _state_line(state: dict[str, object]) -> str:
    status = "clean" if state.get("working_tree_clean") else "dirty"
    upstream = state.get("upstream_name") or "none"
    files_modified = state.get("files_modified") or []
    return (
        f"branch={state.get('branch') or 'unknown'} "
        f"head={state.get('commit') or 'unknown'} "
        f"status={status} "
        f"upstream={upstream} "
        f"files_modified={len(files_modified)}"
    )


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _coerce_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _modified_files_from_status(status_stdout: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in status_stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path = _status_path(line)
        if path and path not in seen:
            files.append(path)
            seen.add(path)
    return files


def _status_path(line: str) -> str | None:
    value = line[3:].strip()
    if not value:
        return None
    if " -> " in value:
        value = value.rsplit(" -> ", 1)[1]
    return value.strip('"') or None


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
