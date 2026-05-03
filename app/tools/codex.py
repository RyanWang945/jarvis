from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from uuid import uuid4

from app.config import get_settings
from app.repositories import RepositoryRef, RepositoryRegistryError, get_repository_registry
from app.tools.coder_common import (
    build_coder_instruction,
    check_coder_permissions,
    collect_git_state,
    postflight_artifacts,
    prepare_workspace,
)
from app.tools.codex_app_server import CodexAppServerRunResult, run_codex_app_server_turn
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_codex_coder_tool(request: ToolExecutionRequest) -> ToolExecutionResult:
    raw_instruction = str(request.args.get("instruction") or "").strip()
    if bool(request.args.get("allow_push")) and not bool(request.args.get("allow_commit")):
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            summary="allow_push=true requires allow_commit=true.",
        )

    instruction = build_coder_instruction(raw_instruction, request.args)
    if not instruction:
        return ToolExecutionResult(ok=False, exit_code=None, summary="Coder instruction is required.")

    try:
        repo, repository_warnings = _resolve_repository(request)
    except RepositoryRegistryError as exc:
        return ToolExecutionResult(ok=False, exit_code=None, summary=str(exc))

    workdir = repo.canonical_root_path

    provider_command = _resolve_codex_command()
    if provider_command is None:
        return ToolExecutionResult(ok=False, exit_code=None, summary="codex CLI was not found on PATH.")

    settings = get_settings()
    run_id = uuid4().hex
    run_dir = _coder_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    preflight_notes = [*repository_warnings, *prepare_workspace(workdir)]
    preflight = collect_git_state(workdir, ignored_paths=(run_dir,))
    app_server_result = _run_codex_app_server(
        provider_command=provider_command,
        workdir=workdir,
        run_dir=run_dir,
        instruction=instruction,
        timeout_seconds=settings.coder_timeout_seconds,
        trusted_command_prefixes=_trusted_command_prefixes(request.args),
    )
    raw_events = app_server_result.raw_events
    raw_stderr = app_server_result.raw_stderr
    exit_code = app_server_result.exit_code

    if app_server_result.status in {"failed", "timeout"}:
        events_path = run_dir / "codex-events.jsonl"
        _write_text(events_path, raw_events)
        stderr_path: Path | None = None
        if raw_stderr:
            stderr_path = run_dir / "codex-stderr.log"
            _write_text(stderr_path, raw_stderr)
        postflight = collect_git_state(workdir, ignored_paths=(run_dir,))
        audit_path = run_dir / "jarvis-audit.log"
        _write_text(
            audit_path,
            _compose_audit_report(
                preflight=preflight,
                postflight=postflight,
                permission_check=None,
                preflight_notes=preflight_notes,
                parse_errors=[app_server_result.error] if app_server_result.error else None,
            ),
        )
        artifacts = postflight_artifacts(postflight)
        artifacts.append(f"codex_events:{events_path}")
        artifacts.append(f"codex_run:{run_dir}")
        artifacts.append(f"jarvis_audit:{audit_path}")
        if stderr_path is not None:
            artifacts.append(f"codex_stderr:{stderr_path}")
        summary = (
            app_server_result.final_text
            or app_server_result.error
            or raw_stderr
            or "Codex app-server failed."
        )
        return ToolExecutionResult(
            ok=False,
            exit_code=exit_code,
            stdout=_compose_user_stdout(summary),
            stderr=raw_stderr,
            artifacts=artifacts,
            summary=summary,
        )

    events_path = run_dir / "codex-events.jsonl"
    _write_text(events_path, raw_events)
    stderr_path: Path | None = None
    if raw_stderr:
        stderr_path = run_dir / "codex-stderr.log"
        _write_text(stderr_path, raw_stderr)

    parsed = _parse_codex_jsonl(raw_events)
    if app_server_result.final_text:
        parsed["final_text"] = app_server_result.final_text
    if app_server_result.approval_requests:
        parsed["approval_requests"] = app_server_result.approval_requests
    postflight = collect_git_state(workdir, ignored_paths=(run_dir,))
    permission_check = check_coder_permissions(
        preflight,
        postflight,
        allow_commit=bool(request.args.get("allow_commit")),
        allow_push=bool(request.args.get("allow_push")),
    )
    approval_requests = parsed["approval_requests"]
    approval_path: Path | None = None
    if approval_requests:
        approval_path = run_dir / "codex-approval-requests.json"
        _write_text(approval_path, json.dumps(approval_requests, ensure_ascii=False, indent=2))
    final_summary = parsed["final_text"] or _default_codex_summary(exit_code or 0, parsed["parse_errors"])
    if approval_requests:
        final_summary = _compose_approval_summary(approval_requests)
    stdout = _compose_user_stdout(final_summary)
    audit_path = run_dir / "jarvis-audit.log"
    _write_text(
        audit_path,
        _compose_audit_report(
            preflight=preflight,
            postflight=postflight,
            permission_check=permission_check,
            approval_requests=approval_requests,
            preflight_notes=preflight_notes,
            parse_errors=parsed["parse_errors"],
        ),
    )
    artifacts = postflight_artifacts(postflight)
    artifacts.append(f"codex_events:{events_path}")
    artifacts.append(f"codex_run:{run_dir}")
    artifacts.append(f"jarvis_audit:{audit_path}")
    if stderr_path is not None:
        artifacts.append(f"codex_stderr:{stderr_path}")
    if approval_path is not None:
        artifacts.append(f"codex_approval_requests:{approval_path}")
    for violation in permission_check["violations"]:
        artifacts.append(f"permission_violation:{violation}")

    ok = app_server_result.status == "completed" and bool(permission_check["ok"]) and not approval_requests
    summary = f"codex app-server {app_server_result.status}."
    if approval_requests:
        summary = _compose_approval_summary(approval_requests)
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


def _run_codex_app_server(
    *,
    provider_command: list[str],
    workdir: Path,
    run_dir: Path,
    instruction: str,
    timeout_seconds: int,
    trusted_command_prefixes: list[str] | None = None,
) -> CodexAppServerRunResult:
    return run_codex_app_server_turn(
        provider_command=provider_command,
        workdir=workdir,
        run_dir=run_dir,
        instruction=instruction,
        timeout_seconds=timeout_seconds,
        trusted_command_prefixes=trusted_command_prefixes,
    )


def _trusted_command_prefixes(args: dict[str, object]) -> list[str]:
    raw = args.get("_trusted_codex_approval_prefixes")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


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


def _resolve_repository(request: ToolExecutionRequest) -> tuple[RepositoryRef, list[str]]:
    registry = get_repository_registry()
    repo_id = str(request.args.get("repo_id") or "").strip()
    raw_workdir = str(request.args.get("workdir") or request.workdir or "").strip()
    warnings: list[str] = []

    if repo_id:
        repo = registry.resolve_repo(repo_id)
        if raw_workdir:
            matched = registry.find_by_workdir(raw_workdir)
            if matched is None or matched.repo_id != repo.repo_id:
                raise RepositoryRegistryError("repo_id and workdir do not refer to the same registered repository.")
            warnings.append("workdir is deprecated; use repo_id instead.")
        return repo, warnings

    if raw_workdir:
        repo = registry.find_by_workdir(raw_workdir)
        if repo is None:
            raise RepositoryRegistryError("Repository is not registered or not authorized. Ask the user to register this repository first.")
        warnings.append("workdir is deprecated; use repo_id instead.")
        return repo, warnings

    raise RepositoryRegistryError("Coder repo_id or registered workdir is required.")


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
    approval_requests: list[dict[str, object]] = []
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
        approval_request = _extract_approval_request(event)
        if approval_request is not None:
            approval_requests.append(approval_request)
    return {
        "final_text": final_text,
        "parse_errors": parse_errors,
        "approval_requests": approval_requests,
    }


def _extract_approval_request(event: object) -> dict[str, object] | None:
    if not isinstance(event, dict):
        return None
    nested_item = event.get("item")
    if isinstance(nested_item, dict):
        nested = _extract_approval_request(nested_item)
        if nested is not None:
            return nested
    approval_value = event.get("approval")
    if isinstance(approval_value, dict):
        return _approval_payload_from_event(approval_value, fallback_event=event)

    event_type = str(event.get("type") or event.get("event") or event.get("name") or "").lower()
    if "approval" not in event_type:
        return None
    return _approval_payload_from_event(event, fallback_event=event)


def _approval_payload_from_event(event: dict[str, object], *, fallback_event: dict[str, object]) -> dict[str, object]:
    event_type = str(fallback_event.get("type") or fallback_event.get("event") or fallback_event.get("name") or "approval")
    command = _find_nested_string(event, ("command", "cmd", "shell_command", "exec_command"))
    reason = _find_nested_string(event, ("reason", "justification", "description", "message", "summary"))
    patch = _find_nested_string(event, ("patch", "diff"))
    approval_id = _find_nested_string(event, ("id", "approval_id", "request_id", "call_id"))
    if not approval_id:
        approval_id = _find_nested_string(fallback_event, ("id", "approval_id", "request_id", "call_id"))
    payload: dict[str, object] = {
        "type": event_type,
        "id": approval_id or "",
        "reason": reason or "",
    }
    if command:
        payload["command"] = command
    if patch:
        payload["patch_preview"] = patch[:2000]
    return payload


def _find_nested_string(value: object, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, list):
                joined = " ".join(str(part) for part in candidate if part is not None).strip()
                if joined:
                    return joined
        for nested_value in value.values():
            found = _find_nested_string(nested_value, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_nested_string(item, keys)
            if found:
                return found
    return None


def _compose_approval_summary(approval_requests: object) -> str:
    requests = approval_requests if isinstance(approval_requests, list) else []
    if not requests:
        return "Codex requested approval."
    first = requests[0] if isinstance(requests[0], dict) else {}
    command = str(first.get("command") or "").strip()
    reason = str(first.get("reason") or "").strip()
    approval_id = str(first.get("id") or "").strip()
    kind = str(first.get("type") or "approval").strip()
    lines = [f"Codex requested approval ({kind})."]
    if approval_id:
        lines.append(f"Approval ID: {approval_id}")
    if command:
        lines.append(f"Command: {command}")
    if reason:
        lines.append(f"Reason: {reason}")
    if len(requests) > 1:
        lines.append(f"Additional approval requests: {len(requests) - 1}")
    lines.append("Approve this request, reject it, or ask Jarvis to continue with a safer alternative.")
    return "\n".join(lines)


def _extract_event_text(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    nested_item = event.get("item")
    if isinstance(nested_item, dict):
        text = _extract_event_text(nested_item)
        if text:
            return text
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


def _compose_user_stdout(final_summary: str) -> str:
    return final_summary.strip()


def _compose_audit_report(
    *,
    preflight: dict[str, object],
    postflight: dict[str, object],
    permission_check: dict[str, object] | None,
    approval_requests: object | None = None,
    preflight_notes: list[str],
    parse_errors: list[str] | None = None,
) -> str:
    lines: list[str] = []
    if parse_errors:
        lines.extend(["[JARVIS_CODEX_PARSE]", *[f"- {error}" for error in parse_errors[:5]], ""])
    if preflight_notes:
        lines.extend(["[JARVIS_PREFLIGHT_NOTES]", *[f"- {note}" for note in preflight_notes], ""])
    lines.extend(
        [
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
    if approval_requests:
        lines.extend(["", "[JARVIS_CODEX_APPROVAL_REQUESTS]", json.dumps(approval_requests, ensure_ascii=False)])
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
