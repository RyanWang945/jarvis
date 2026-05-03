from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from uuid import uuid4

from app.config import get_settings
from app.tools.coder_common import (
    build_coder_instruction,
    check_coder_permissions,
    collect_git_state,
    postflight_artifacts,
    prepare_workspace,
)
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_codex_coder_tool(request: ToolExecutionRequest) -> ToolExecutionResult:
    raw_instruction = str(request.args.get("instruction") or "").strip()
    instruction = build_coder_instruction(raw_instruction, request.args)
    if not instruction:
        return ToolExecutionResult(ok=False, exit_code=None, summary="Coder instruction is required.")
    if not request.workdir:
        return ToolExecutionResult(ok=False, exit_code=None, summary="Coder workdir is required.")

    workdir = Path(request.workdir).resolve()
    if not workdir.exists() or not workdir.is_dir():
        return ToolExecutionResult(ok=False, exit_code=None, summary=f"Coder workdir does not exist: {workdir}")

    provider_command = _resolve_codex_command()
    if provider_command is None:
        return ToolExecutionResult(ok=False, exit_code=None, summary="codex CLI was not found on PATH.")

    settings = get_settings()
    run_id = uuid4().hex
    run_dir = _coder_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    preflight_notes = prepare_workspace(workdir)
    preflight = collect_git_state(workdir)
    command = provider_command + [
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(workdir),
        "-",
    ]

    try:
        completed = _run_codex_process(
            command,
            workdir=workdir,
            instruction=instruction,
            timeout_seconds=settings.coder_timeout_seconds,
        )
        raw_events = completed.stdout or ""
        raw_stderr = completed.stderr or ""
        exit_code = completed.returncode
    except FileNotFoundError:
        return ToolExecutionResult(ok=False, exit_code=None, summary="codex CLI was not found on PATH.")
    except subprocess.TimeoutExpired as exc:
        raw_events = _coerce_timeout_output(exc.stdout)
        raw_stderr = _coerce_timeout_output(exc.stderr)
        _write_text(run_dir / "codex-events.jsonl", raw_events)
        if raw_stderr:
            _write_text(run_dir / "codex-stderr.log", raw_stderr)
        postflight = collect_git_state(workdir)
        artifacts = postflight_artifacts(postflight)
        artifacts.append(f"codex_events:{run_dir / 'codex-events.jsonl'}")
        if raw_stderr:
            artifacts.append(f"codex_stderr:{run_dir / 'codex-stderr.log'}")
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout=_compose_clean_stdout(
                "Codex CLI timed out.",
                preflight=preflight,
                postflight=postflight,
                permission_check=None,
                preflight_notes=preflight_notes,
            ),
            stderr=raw_stderr,
            artifacts=artifacts,
            summary="codex CLI timed out.",
        )

    events_path = run_dir / "codex-events.jsonl"
    _write_text(events_path, raw_events)
    stderr_path: Path | None = None
    if raw_stderr:
        stderr_path = run_dir / "codex-stderr.log"
        _write_text(stderr_path, raw_stderr)

    parsed = _parse_codex_jsonl(raw_events)
    postflight = collect_git_state(workdir)
    permission_check = check_coder_permissions(
        preflight,
        postflight,
        allow_commit=bool(request.args.get("allow_commit")),
        allow_push=bool(request.args.get("allow_push")),
    )
    final_summary = parsed["final_text"] or _default_codex_summary(exit_code, parsed["parse_errors"])
    stdout = _compose_clean_stdout(
        final_summary,
        preflight=preflight,
        postflight=postflight,
        permission_check=permission_check,
        preflight_notes=preflight_notes,
        parse_errors=parsed["parse_errors"],
    )
    artifacts = postflight_artifacts(postflight)
    artifacts.append(f"codex_events:{events_path}")
    artifacts.append(f"codex_run:{run_dir}")
    if stderr_path is not None:
        artifacts.append(f"codex_stderr:{stderr_path}")
    for violation in permission_check["violations"]:
        artifacts.append(f"permission_violation:{violation}")

    ok = exit_code == 0 and bool(permission_check["ok"])
    summary = f"codex CLI exited with code {exit_code}."
    if not permission_check["ok"]:
        summary = summary + " Permission check failed: " + "; ".join(str(v) for v in permission_check["violations"])

    return ToolExecutionResult(
        ok=ok,
        exit_code=exit_code,
        stdout=stdout,
        stderr=raw_stderr,
        artifacts=artifacts,
        summary=summary,
    )


def _run_codex_process(
    command: list[str],
    *,
    workdir: Path,
    instruction: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = str(workdir)
    return subprocess.run(
        command,
        cwd=str(workdir),
        input=instruction,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_seconds,
    )


def _resolve_codex_command() -> list[str] | None:
    executable = which("codex")
    if executable is None:
        return None
    path = Path(executable)
    if path.suffix.lower() == ".ps1":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]
    return [str(path)]


def _coder_run_dir(run_id: str) -> Path:
    settings = get_settings()
    data_dir = settings.data_dir
    if not data_dir.is_absolute():
        data_dir = settings.workspace_root / data_dir
    return data_dir / "coder_runs" / run_id


def _parse_codex_jsonl(raw_events: str) -> dict[str, object]:
    final_text = ""
    parse_errors: list[str] = []
    for line_number, raw_line in enumerate(raw_events.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc.msg}")
            continue
        candidate = _extract_event_text(event)
        if candidate:
            final_text = candidate
    return {"final_text": final_text, "parse_errors": parse_errors}


def _extract_event_text(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    event_type = str(event.get("type") or event.get("event") or "").lower()
    role = str(event.get("role") or "").lower()
    if event_type in {"agent_message", "assistant_message", "final_answer", "task_complete", "message"} or role == "assistant":
        for key in ("message", "content", "text", "answer", "summary"):
            text = _content_to_text(event.get(key))
            if text:
                return text
    if "final" in event_type or "complete" in event_type:
        for key in ("content", "text", "message", "summary"):
            text = _content_to_text(event.get(key))
            if text:
                return text
    return ""


def _content_to_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_content_to_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            text = _content_to_text(value.get(key))
            if text:
                return text
    return ""


def _default_codex_summary(exit_code: int, parse_errors: list[str]) -> str:
    if exit_code == 0:
        if parse_errors:
            return "Codex completed, but Jarvis could not parse a final message. See codex_events artifact."
        return "Codex completed without a parseable final message. See codex_events artifact."
    return "Codex CLI failed. See codex_events and codex_stderr artifacts."


def _compose_clean_stdout(
    final_summary: str,
    *,
    preflight: dict[str, object],
    postflight: dict[str, object],
    permission_check: dict[str, object] | None,
    preflight_notes: list[str],
    parse_errors: list[str] | None = None,
) -> str:
    lines = [final_summary.strip()]
    if parse_errors:
        lines.extend(["", "[JARVIS_CODEX_PARSE]", *[f"- {error}" for error in parse_errors[:5]]])
    if preflight_notes:
        lines.extend(["", "[JARVIS_PREFLIGHT_NOTES]", *[f"- {note}" for note in preflight_notes]])
    lines.extend(
        [
            "",
            "[JARVIS_PREFLIGHT]",
            _state_line(preflight),
            "",
            "[JARVIS_POSTFLIGHT]",
            _state_line(postflight),
        ]
    )
    if permission_check is not None:
        lines.extend(
            [
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
        f"head={state.get('short_head') or state.get('commit') or 'unknown'} "
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
