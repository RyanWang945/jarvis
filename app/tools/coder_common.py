from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

from app.prompting import PromptRegistry


def build_coder_instruction(instruction: str, request_args: dict[str, Any]) -> str:
    if not instruction:
        return ""
    read_only = bool(request_args.get("_read_only"))
    return PromptRegistry().load("coder_worker").render_text(
        {
            "instruction": instruction,
            "read_only": read_only,
            "allow_commit": bool(request_args.get("allow_commit")) and not read_only,
            "allow_push": bool(request_args.get("allow_push")) and not read_only,
            "source_branch": str(request_args.get("source_branch") or "").strip(),
            "target_branch": str(request_args.get("target_branch") or "").strip(),
            "node_branch": str(request_args.get("node_branch") or "").strip(),
            "verification_cmd": str(request_args.get("verification_cmd") or "").strip(),
        }
    )


def prepare_workspace(workdir: Path) -> list[str]:
    notes: list[str] = []
    git_dir = workdir / ".git"
    if git_dir.is_dir():
        lock = git_dir / "index.lock"
        if lock.exists() and is_zero_byte(lock):
            unlink_path(lock)
            notes.append("Removed stale .git/index.lock.")
    if os.name == "nt":
        reserved_names = {"nul", "con", "prn", "aux"}
        for child in workdir.iterdir():
            if child.name.lower() in reserved_names and is_zero_byte(child):
                unlink_path(child)
                notes.append(f"Removed stale Windows reserved-name file: {child.name}.")
    return notes


def collect_git_state(workdir: Path, *, ignored_paths: tuple[Path, ...] = ()) -> dict[str, object]:
    ignored_prefixes = ignored_relative_prefixes(workdir, ignored_paths)
    status = run_git(workdir, "status", "--short", "--branch", "--untracked-files=all")
    porcelain_status = run_git(workdir, "status", "--porcelain", "--untracked-files=all")
    diff_stat = run_git(workdir, "diff", "--stat")
    branch = run_git(workdir, "branch", "--show-current")
    short_head = run_git(workdir, "rev-parse", "--short", "HEAD")
    head = run_git(workdir, "rev-parse", "HEAD")
    commit_subject = run_git(workdir, "log", "-1", "--pretty=%s")
    remote = run_git(workdir, "remote", "get-url", "origin")
    upstream = run_git(workdir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream_head = (
        run_git(workdir, "rev-parse", "@{u}")
        if upstream["exit_code"] == 0
        else {"exit_code": None, "stdout": "", "stderr": ""}
    )

    status_stdout = filter_status_stdout(str(status["stdout"]), ignored_prefixes).strip()
    porcelain_stdout = filter_status_stdout(str(porcelain_status["stdout"]), ignored_prefixes)
    branch_name = str(branch["stdout"]).strip()
    upstream_name = str(upstream["stdout"]).strip()
    return {
        "git_available": status["exit_code"] == 0,
        "status_exit_code": status["exit_code"],
        "status": status_stdout,
        "status_short_branch": status_stdout,
        "status_porcelain": porcelain_stdout.strip(),
        "branch": branch_name,
        "head": str(head["stdout"]).strip(),
        "short_head": str(short_head["stdout"]).strip(),
        "commit": str(short_head["stdout"]).strip(),
        "commit_subject": str(commit_subject["stdout"]).strip(),
        "origin": str(remote["stdout"]).strip(),
        "upstream_name": upstream_name,
        "upstream_head": str(upstream_head["stdout"]).strip(),
        "upstream_available": upstream["exit_code"] == 0 and bool(upstream_name),
        "working_tree_clean": is_working_tree_clean(status_stdout),
        "synced_with_upstream": is_synced_with_upstream(status_stdout),
        "files_modified": modified_files_from_status(porcelain_stdout),
        "diff_stat": str(diff_stat["stdout"]).strip(),
        "diff_stat_stderr": str(diff_stat["stderr"]).strip(),
        "status_stderr": str(status["stderr"]).strip(),
    }


def run_git(workdir: Path, *args: str) -> dict[str, object]:
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


def postflight_artifacts(
    postflight: dict[str, object],
    *,
    files_modified: Iterable[str] | None = None,
) -> list[str]:
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
    artifact_files = files_modified if files_modified is not None else postflight.get("files_modified") or []
    for path in artifact_files:
        artifacts.append(f"git_file:{path}")
    return artifacts


def check_coder_permissions(
    preflight: dict[str, object],
    postflight: dict[str, object],
    *,
    allow_commit: bool,
    allow_push: bool,
) -> dict[str, object]:
    violations: list[str] = []
    warnings: list[str] = []
    pre_head = str(preflight.get("head") or "")
    post_head = str(postflight.get("head") or "")
    commit_changed = bool(pre_head and post_head and pre_head != post_head)
    if not allow_commit and commit_changed:
        violations.append("allow_commit=false but repository HEAD changed.")

    pre_upstream = str(preflight.get("upstream_head") or "")
    post_upstream = str(postflight.get("upstream_head") or "")
    upstream_changed = bool(pre_upstream and post_upstream and pre_upstream != post_upstream)
    upstream_check = "ok"
    if not pre_upstream or not post_upstream:
        upstream_check = "unknown"
        warnings.append("Unable to prove upstream head did not change because upstream was unavailable.")
    elif not allow_push and upstream_changed:
        upstream_check = "failed"
        violations.append("allow_push=false but upstream head changed.")

    pre_branch = str(preflight.get("branch") or "")
    post_branch = str(postflight.get("branch") or "")
    branch_changed = bool(pre_branch and post_branch and pre_branch != post_branch)
    if branch_changed:
        warnings.append(f"Branch changed from {pre_branch} to {post_branch}.")

    git_available = bool(preflight.get("git_available")) and bool(postflight.get("git_available"))
    if not git_available:
        violations.append("Unable to verify git state before and after Codex execution.")

    return {
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "commit_allowed": allow_commit,
        "commit_changed": commit_changed,
        "commit_check": "ok" if allow_commit or not commit_changed else "failed",
        "push_allowed": allow_push,
        "upstream_changed": upstream_changed,
        "upstream_check": upstream_check,
        "branch_changed": branch_changed,
    }


def modified_files_from_status(status_stdout: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in status_stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path = status_path(line)
        if path and path not in seen:
            files.append(path)
            seen.add(path)
    return files


def status_path(line: str) -> str | None:
    value = line[3:].strip()
    if not value:
        return None
    if " -> " in value:
        value = value.rsplit(" -> ", 1)[1]
    return value.strip('"') or None


def is_working_tree_clean(status_stdout: str) -> bool:
    lines = [line for line in status_stdout.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("## ") for line in lines)


def is_synced_with_upstream(status_stdout: str) -> bool:
    first_line = next((line for line in status_stdout.splitlines() if line.startswith("## ")), "")
    if "..." not in first_line:
        return False
    return "[" not in first_line


def ignored_relative_prefixes(workdir: Path, ignored_paths: tuple[Path, ...]) -> tuple[str, ...]:
    prefixes: list[str] = []
    root = workdir.resolve()
    for ignored_path in ignored_paths:
        try:
            relative = ignored_path.resolve().relative_to(root).as_posix().strip("/")
        except ValueError:
            continue
        if relative:
            prefixes.append(relative)
            prefixes.append(relative + "/")
    return tuple(dict.fromkeys(prefixes))


def filter_status_stdout(status_stdout: str, ignored_prefixes: tuple[str, ...]) -> str:
    if not ignored_prefixes:
        return status_stdout
    lines: list[str] = []
    for line in status_stdout.splitlines():
        if line.startswith("## "):
            lines.append(line)
            continue
        path = status_path(line)
        if path is not None and is_ignored_relative_path(path, ignored_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines)


def is_ignored_relative_path(path: str, ignored_prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    for prefix in ignored_prefixes:
        if prefix.endswith("/"):
            if normalized.startswith(prefix):
                return True
        elif normalized == prefix:
            return True
    return False


def is_zero_byte(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def unlink_path(path: Path) -> None:
    try:
        path.unlink()
        return
    except OSError:
        if os.name != "nt":
            raise
    extended = "\\\\?\\" + str(path)
    os.remove(extended)
