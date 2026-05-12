from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


APPROVAL_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}


@dataclass(frozen=True)
class CodexAppServerRunResult:
    status: Literal["completed", "approval_requested", "failed", "timeout"]
    raw_events: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None
    final_text: str = ""
    approval_requests: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class CodexApprovalContinuationResult:
    status: Literal["completed", "approval_requested", "failed", "timeout", "missing"]
    raw_events: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None
    final_text: str = ""
    approval_requests: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class CodexAppServerSession:
    def __init__(
        self,
        *,
        provider_command: list[str],
        workdir: Path,
        run_dir: Path,
        trusted_command_prefixes: list[str] | None = None,
    ) -> None:
        self._provider_command = provider_command
        self._workdir = workdir
        self._run_dir = run_dir
        self._trusted_command_prefixes = tuple(
            prefix for prefix in (_normalize_command_prefix(value) for value in trusted_command_prefixes or []) if prefix
        )
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._events: list[str] = []
        self._events_path = self._run_dir / "codex-events.jsonl"
        self._stderr_path = self._run_dir / "codex-stderr.log"
        self._lock = threading.RLock()
        self._client_request_id = 0
        self._thread_id = ""
        self._turn_id = ""
        self._pending_approval_id = ""
        self._pending_server_request_id: int | str | None = None
        self._pending_approval_payload: dict[str, Any] | None = None
        self._closed = False

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def start_turn(self, *, instruction: str, timeout_seconds: int) -> CodexAppServerRunResult:
        with self._lock:
            self._start_process()
            initialize_id = self._next_client_request_id()
            thread_start_id = self._next_client_request_id()
            self._send(
                {
                    "method": "initialize",
                    "id": initialize_id,
                    "params": {
                        "clientInfo": {
                            "name": "jarvis",
                            "title": "Jarvis",
                            "version": "0.1.0",
                        }
                    },
                }
            )
            self._send({"method": "initialized", "params": {}})
            self._send(
                {
                    "method": "thread/start",
                    "id": thread_start_id,
                    "params": {
                        "cwd": str(self._workdir),
                        "sandbox": "workspace-write",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sessionStartSource": "startup",
                    },
                }
            )
            return self._drain_until_waiting_or_done(
                timeout_seconds=timeout_seconds,
                thread_start_id=thread_start_id,
                instruction=instruction,
            )

    def respond_approval(self, *, approved: bool, timeout_seconds: int) -> CodexApprovalContinuationResult:
        with self._lock:
            if self._closed or self._proc is None or self._pending_server_request_id is None:
                return CodexApprovalContinuationResult(status="missing", error="Codex approval session is no longer active.")
            decision = _approval_decision(self._pending_approval_payload or {}, approved=approved)
            self._send({"id": self._pending_server_request_id, "result": {"decision": decision}})
            self._pending_server_request_id = None
            self._pending_approval_payload = None
            run_result = self._drain_until_waiting_or_done(
                timeout_seconds=timeout_seconds,
                auto_approve_routine_approvals=approved,
            )
            if run_result.status == "approval_requested":
                return CodexApprovalContinuationResult(
                    status="approval_requested",
                    raw_events=run_result.raw_events,
                    raw_stderr=run_result.raw_stderr,
                    exit_code=run_result.exit_code,
                    final_text=run_result.final_text,
                    approval_requests=run_result.approval_requests,
                )
            return CodexApprovalContinuationResult(
                status=run_result.status if run_result.status in {"completed", "failed", "timeout"} else "failed",
                raw_events=run_result.raw_events,
                raw_stderr=run_result.raw_stderr,
                exit_code=run_result.exit_code,
                final_text=run_result.final_text,
                error=run_result.error,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.terminate()
            except OSError:
                return

    def _start_process(self) -> None:
        if self._proc is not None:
            return
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._events_path.write_text("", encoding="utf-8")
        self._stderr_path.write_text("", encoding="utf-8")
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = str(self._workdir)
        self._proc = subprocess.Popen(
            [*self._provider_command, "app-server"],
            cwd=str(self._workdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        if self._proc.stdout is not None:
            threading.Thread(target=self._read_stdout, args=(self._proc.stdout,), daemon=True).start()
        if self._proc.stderr is not None:
            threading.Thread(target=self._read_stderr, args=(self._proc.stderr,), daemon=True).start()

    def _read_stdout(self, stream: Any) -> None:
        try:
            for line in stream:
                self._stdout_queue.put(line)
        finally:
            self._stdout_queue.put(None)

    def _read_stderr(self, stream: Any) -> None:
        for line in stream:
            text = line.rstrip()
            self._stderr_lines.append(text)
            self._append_line(self._stderr_path, text)

    def _next_client_request_id(self) -> int:
        value = self._client_request_id
        self._client_request_id += 1
        return value

    def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Codex app-server process is not running.")
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _drain_until_waiting_or_done(
        self,
        *,
        timeout_seconds: int,
        thread_start_id: int | None = None,
        instruction: str | None = None,
        auto_approve_routine_approvals: bool = False,
    ) -> CodexAppServerRunResult:
        deadline = time.monotonic() + timeout_seconds
        final_text = ""
        last_agent_text = ""
        turn_started = instruction is None
        turn_start_id: int | None = None

        while time.monotonic() < deadline:
            remaining = max(0.1, min(1.0, deadline - time.monotonic()))
            try:
                raw_line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    return self._failed_result(final_text, "Codex app-server exited before turn completion.")
                continue
            if raw_line is None:
                return self._failed_result(final_text, "Codex app-server stdout closed.")

            raw_line = raw_line.rstrip("\n")
            self._events.append(raw_line)
            self._append_line(self._events_path, raw_line)
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            if thread_start_id is not None and event.get("id") == thread_start_id and isinstance(event.get("result"), dict):
                self._thread_id = _thread_id_from_thread_start(event)
                if self._thread_id and instruction is not None and not turn_started:
                    turn_start_id = self._next_client_request_id()
                    self._send(
                        {
                            "method": "turn/start",
                            "id": turn_start_id,
                            "params": {
                                "threadId": self._thread_id,
                                "input": [{"type": "text", "text": instruction}],
                                "cwd": str(self._workdir),
                                "sandbox": "workspace-write",
                                "approvalPolicy": "on-request",
                                "approvalsReviewer": "user",
                            },
                        }
                    )
                    turn_started = True
                continue
            if thread_start_id is not None and event.get("id") == thread_start_id and "error" in event:
                return self._failed_result(final_text, _error_text(event) or "Codex thread/start failed.")

            if turn_start_id is not None and event.get("id") == turn_start_id and isinstance(event.get("result"), dict):
                self._turn_id = _turn_id_from_turn_start(event)
                continue
            if turn_start_id is not None and event.get("id") == turn_start_id and "error" in event:
                return self._failed_result(final_text, _error_text(event) or "Codex turn/start failed.")

            method = str(event.get("method") or "")
            agent_text = _agent_message_text_from_app_server_event(event, final_only=False)
            if agent_text:
                last_agent_text = agent_text
            text = _final_text_from_app_server_event(event)
            if text:
                final_text = text
            if method in APPROVAL_REQUEST_METHODS:
                approval = self._build_approval_payload(event)
                if self._matches_trusted_command_prefix(approval) or (
                    auto_approve_routine_approvals and _is_routine_repo_git_approval(approval)
                ):
                    self._send({"id": event.get("id"), "result": {"decision": _approval_decision(approval, approved=True)}})
                    continue
                self._pending_approval_id = str(approval["id"])
                self._pending_server_request_id = event.get("id")
                self._pending_approval_payload = approval
                _register_pending_session(self._pending_approval_id, self)
                return CodexAppServerRunResult(
                    status="approval_requested",
                    raw_events=self.raw_events(),
                    raw_stderr=self.raw_stderr(),
                    exit_code=None,
                    final_text=final_text,
                    approval_requests=[approval],
                )
            if method == "turn/completed":
                completion_status = _turn_completion_status(event)
                if completion_status and completion_status != "completed":
                    error = _turn_completion_error(event) or f"Codex turn completed with status {completion_status}."
                    self.close()
                    return CodexAppServerRunResult(
                        status="failed",
                        raw_events=self.raw_events(),
                        raw_stderr=self.raw_stderr(),
                        exit_code=None,
                        final_text=final_text or last_agent_text,
                        error=error,
                    )
                self.close()
                return CodexAppServerRunResult(
                    status="completed",
                    raw_events=self.raw_events(),
                    raw_stderr=self.raw_stderr(),
                    exit_code=0,
                    final_text=final_text,
                )

        self.close()
        return CodexAppServerRunResult(
            status="timeout",
            raw_events=self.raw_events(),
            raw_stderr=self.raw_stderr(),
            exit_code=None,
            final_text=final_text,
            error="Codex app-server timed out.",
        )

    def _failed_result(self, final_text: str, error: str) -> CodexAppServerRunResult:
        exit_code = self._proc.poll() if self._proc is not None else None
        self.close()
        return CodexAppServerRunResult(
            status="failed",
            raw_events=self.raw_events(),
            raw_stderr=self.raw_stderr(),
            exit_code=exit_code,
            final_text=final_text,
            error=error,
        )

    def _build_approval_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        approval_id = "codex_" + uuid4().hex
        payload: dict[str, Any] = {
            "type": str(event.get("method") or "approval"),
            "id": approval_id,
            "server_request_id": event.get("id"),
            "thread_id": str(params.get("threadId") or self._thread_id),
            "codex_turn_id": str(params.get("turnId") or self._turn_id),
            "item_id": str(params.get("itemId") or ""),
            "command": str(params.get("command") or ""),
            "cwd": str(params.get("cwd") or self._workdir),
            "reason": str(params.get("reason") or ""),
        }
        available = params.get("availableDecisions")
        if isinstance(available, list):
            payload["available_decisions"] = available
        return payload

    def raw_events(self) -> str:
        return "\n".join(self._events)

    def raw_stderr(self) -> str:
        return "\n".join(self._stderr_lines)

    def _matches_trusted_command_prefix(self, approval: dict[str, Any]) -> bool:
        return _matches_trusted_command_prefix(approval, self._trusted_command_prefixes)

    def _append_line(self, path: Path, line: str) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            return


_SESSIONS: dict[str, CodexAppServerSession] = {}
_SESSIONS_LOCK = threading.RLock()


def run_codex_app_server_turn(
    *,
    provider_command: list[str],
    workdir: Path,
    run_dir: Path,
    instruction: str,
    timeout_seconds: int,
    trusted_command_prefixes: list[str] | None = None,
) -> CodexAppServerRunResult:
    session = CodexAppServerSession(
        provider_command=provider_command,
        workdir=workdir,
        run_dir=run_dir,
        trusted_command_prefixes=trusted_command_prefixes,
    )
    try:
        return session.start_turn(instruction=instruction, timeout_seconds=timeout_seconds)
    except FileNotFoundError:
        session.close()
        return CodexAppServerRunResult(status="failed", error="codex CLI was not found on PATH.")
    except Exception as exc:
        session.close()
        return CodexAppServerRunResult(status="failed", error=f"Codex app-server failed: {exc}")


def respond_to_codex_approval(
    approval_id: str,
    *,
    approved: bool,
    timeout_seconds: int,
    trusted_command_prefixes: list[str] | None = None,
) -> CodexApprovalContinuationResult:
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(approval_id)
    if session is None:
        return CodexApprovalContinuationResult(status="missing", error="Codex approval session is no longer active.")
    if trusted_command_prefixes:
        session._trusted_command_prefixes = tuple(
            dict.fromkeys(
                [
                    *session._trusted_command_prefixes,
                    *[
                        prefix
                        for prefix in (_normalize_command_prefix(value) for value in trusted_command_prefixes)
                        if prefix
                    ],
                ]
            )
        )
    try:
        result = session.respond_approval(approved=approved, timeout_seconds=timeout_seconds)
        _persist_continuation_result(session, approval_id, result)
    finally:
        _forget_pending_session(approval_id)
    return result


def _persist_continuation_result(
    session: CodexAppServerSession,
    approval_id: str,
    result: CodexApprovalContinuationResult,
) -> None:
    safe_approval_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", approval_id).strip("._") or "approval"
    path = session.run_dir / f"codex-continuation-{safe_approval_id}.jsonl"
    try:
        path.write_text(result.raw_events or "", encoding="utf-8")
    except OSError:
        return


def _register_pending_session(approval_id: str, session: CodexAppServerSession) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS[approval_id] = session


def _forget_pending_session(approval_id: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(approval_id, None)


def _thread_id_from_thread_start(event: dict[str, Any]) -> str:
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return ""
    return str(thread.get("id") or "")


def _turn_id_from_turn_start(event: dict[str, Any]) -> str:
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    turn = result.get("turn")
    if not isinstance(turn, dict):
        return ""
    return str(turn.get("id") or "")


def _error_text(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    return ""


def _final_text_from_app_server_event(event: dict[str, Any]) -> str:
    return _agent_message_text_from_app_server_event(event, final_only=True)


def _agent_message_text_from_app_server_event(event: dict[str, Any], *, final_only: bool) -> str:
    method = str(event.get("method") or "")
    params = event.get("params")
    if not isinstance(params, dict):
        return ""
    item = params.get("item")
    if isinstance(item, dict) and item.get("type") == "agentMessage":
        phase = str(item.get("phase") or "")
        text = str(item.get("text") or "").strip()
        if text and (not final_only or phase in {"final_answer", "final"}):
            return text
    if method == "item/agentMessage/delta":
        return ""
    return ""


def _turn_completion_status(event: dict[str, Any]) -> str:
    if str(event.get("method") or "") != "turn/completed":
        return ""
    params = event.get("params")
    if not isinstance(params, dict):
        return ""
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return ""
    return str(turn.get("status") or "").strip().lower()


def _turn_completion_error(event: dict[str, Any]) -> str:
    params = event.get("params")
    if not isinstance(params, dict):
        return ""
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return ""
    error = turn.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message.strip()
    if isinstance(error, str):
        return error.strip()
    return ""


def _approval_decision(approval: dict[str, Any], *, approved: bool) -> str | dict[str, Any]:
    available = approval.get("available_decisions")
    if not isinstance(available, list):
        return "accept" if approved else "decline"

    if approved:
        if "accept" in available:
            return "accept"
        return "accept"

    if "cancel" in available:
        return "cancel"
    if "decline" in available:
        return "decline"
    return "decline"


def _is_routine_repo_git_approval(approval: dict[str, Any]) -> bool:
    command = str(approval.get("command") or "").strip()
    if not command:
        return False
    inner = _inner_shell_command(command)
    if not inner or any(token in inner for token in ("\n", "\r", "&&", "||", ";", "|", ">", "<", "$(", "`")):
        return False

    words = _shell_words(inner)
    if not words:
        return False
    if not _is_git_executable(words[0]):
        return False

    index = 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index].lower()
        index += 1
        if option in {"-c", "--git-dir", "--work-tree"} and index < len(words):
            index += 1
    if index >= len(words):
        return False

    subcommand = words[index].lower()
    lower_words = {word.lower() for word in words[index + 1 :]}
    if subcommand in {"status", "diff", "rev-parse", "log", "branch", "remote", "show", "ls-files"}:
        return True
    if subcommand == "add":
        return True
    if subcommand == "commit":
        return "--amend" not in lower_words
    if subcommand == "restore":
        return "--staged" in lower_words and "--worktree" not in lower_words and "--source" not in lower_words
    return False


def approval_command_prefix(command: str) -> str:
    normalized = _normalize_command_prefix(command)
    words = _shell_words(normalized)
    if len(words) >= 2 and _is_git_executable(words[0]):
        subcommand = words[1].lower()
        if subcommand == "push":
            return ""
        if subcommand == "restore" and any(word.lower() == "--staged" for word in words[2:]):
            return "git restore --staged"
        if subcommand in {"add", "commit", "status", "diff", "log", "branch", "remote", "rev-parse"}:
            return f"git {subcommand}"
    return normalized


def _matches_trusted_command_prefix(approval: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    if not prefixes:
        return False
    command = _normalize_command_prefix(str(approval.get("command") or ""))
    words = _shell_words(command)
    if len(words) >= 2 and _is_git_executable(words[0]) and words[1].lower() == "push":
        return False
    return bool(command) and any(command.startswith(prefix) for prefix in prefixes)


def _normalize_command_prefix(command: str) -> str:
    inner = _inner_shell_command(command)
    if not inner or any(token in inner for token in ("\n", "\r", "&&", "||", ";", "|", ">", "<", "$(", "`")):
        return ""
    words = _shell_words(inner)
    if not words:
        return ""
    if _is_git_executable(words[0]):
        words[0] = "git"
    return " ".join(words).lower()


def _is_git_executable(word: str) -> bool:
    executable = word.replace("\\", "/").lower().rstrip(".exe")
    return executable == "git" or executable.endswith("/git")


def _inner_shell_command(command: str) -> str:
    match = re.search(r"""(?is)\s-command\s+(['"])(?P<inner>.*)\1\s*$""", command.strip())
    if match:
        return match.group("inner").strip()
    return command.strip()


def _shell_words(command: str) -> list[str]:
    return [
        word.strip("\"'")
        for word in re.findall(r'''"[^"]*"|'[^']*'|\S+''', command)
        if word.strip("\"'")
    ]
