from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from app.config import Settings
from app.tools.codex import run_codex_coder_tool
from app.tools.codex_app_server import respond_to_codex_approval
from app.tools.common import ToolExecutionRequest
from app.tools.coder import run_coder_tool

CoderAccessMode = Literal["read", "write"]
ApprovalLevel = Literal["allow", "ask", "strong_ask", "deny"]


@dataclass(frozen=True)
class CoderPolicy:
    access_mode: CoderAccessMode
    allow_commit: bool = False
    allow_push: bool = False


@dataclass(frozen=True)
class CoderAction:
    kind: Literal[
        "read_file",
        "search",
        "git_status",
        "git_diff",
        "git_log",
        "edit_file",
        "commit",
        "push",
        "secret_read",
        "dangerous_command",
        "outside_workspace_write",
        "unknown_external_action",
    ]
    command: str | None = None
    path: str | None = None
    description: str = ""
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    decision: ApprovalLevel
    reason: str = ""
    approval_id: str | None = None


@dataclass(frozen=True)
class CoderApprovalRequest:
    approval_id: str
    action_kind: str
    reason: str = ""
    command: str | None = None
    path: str | None = None
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoderRunRequest:
    repo_id: str
    workdir: Path
    instruction: str
    policy: CoderPolicy
    timeout_seconds: int = 1800
    run_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoderRunResult:
    ok: bool
    exit_code: int | None = None
    summary: str = ""
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    approval_requests: list[CoderApprovalRequest] = field(default_factory=list)
    raw_events_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoderApprovalContinuationResult:
    status: Literal["completed", "approval_requested", "failed", "timeout", "missing", "unsupported"]
    final_text: str = ""
    raw_events: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None
    approval_requests: list[CoderApprovalRequest] = field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class CoderProvider(Protocol):
    name: str

    def run(
        self,
        request: CoderRunRequest,
        *,
        decide_action: Callable[[CoderAction], ApprovalDecision],
    ) -> CoderRunResult: ...

    def resume_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        timeout_seconds: int,
        trusted_command_prefixes: list[str] | None = None,
    ) -> CoderApprovalContinuationResult: ...


class CodexCoderProvider:
    name = "codex"

    def __init__(self, *, runner=run_codex_coder_tool) -> None:
        self._runner = runner

    def run(
        self,
        request: CoderRunRequest,
        *,
        decide_action: Callable[[CoderAction], ApprovalDecision],
    ) -> CoderRunResult:
        del decide_action
        tool_result = self._runner(
            ToolExecutionRequest(
                tool_name="codex_coder_provider",
                workdir=None,
                args={
                    "instruction": request.instruction,
                    "repo_id": request.repo_id,
                    "allow_commit": request.policy.allow_commit,
                    "allow_push": request.policy.allow_push,
                    "_read_only": request.policy.access_mode == "read",
                },
                timeout_seconds=request.timeout_seconds,
            )
        )
        approval_requests = _approval_requests_from_artifacts(tool_result.artifacts)
        return CoderRunResult(
            ok=tool_result.ok,
            exit_code=tool_result.exit_code,
            summary=tool_result.summary,
            stdout=tool_result.stdout,
            stderr=tool_result.stderr,
            artifacts=list(tool_result.artifacts),
            approval_requests=approval_requests,
            metadata={"tool_artifacts": [artifact.__dict__ for artifact in tool_result.tool_artifacts]},
        )

    def resume_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        timeout_seconds: int,
        trusted_command_prefixes: list[str] | None = None,
    ) -> CoderApprovalContinuationResult:
        result = respond_to_codex_approval(
            approval_id,
            approved=approved,
            timeout_seconds=timeout_seconds,
            trusted_command_prefixes=trusted_command_prefixes,
        )
        return CoderApprovalContinuationResult(
            status=result.status,
            final_text=result.final_text,
            raw_events=result.raw_events,
            raw_stderr=result.raw_stderr,
            exit_code=result.exit_code,
            approval_requests=[
                _approval_request_from_payload(item, index=index)
                for index, item in enumerate(result.approval_requests, start=1)
                if isinstance(item, dict)
            ],
            error=result.error,
            metadata={"provider": self.name, "tool_artifacts": [artifact.__dict__ for artifact in result.tool_artifacts]},
        )


class ClaudeCodeCoderProvider:
    name = "claude_code"

    def run(
        self,
        request: CoderRunRequest,
        *,
        decide_action: Callable[[CoderAction], ApprovalDecision],
    ) -> CoderRunResult:
        del decide_action
        tool_result = run_coder_tool(
            ToolExecutionRequest(
                tool_name="claude_code_coder_provider",
                workdir=str(request.workdir),
                args={
                    "instruction": request.instruction,
                    "repo_id": request.repo_id,
                    "allow_commit": request.policy.allow_commit,
                    "allow_push": request.policy.allow_push,
                    "_read_only": request.policy.access_mode == "read",
                },
                timeout_seconds=request.timeout_seconds,
            )
        )
        return CoderRunResult(
            ok=tool_result.ok,
            exit_code=tool_result.exit_code,
            summary=tool_result.summary,
            stdout=tool_result.stdout,
            stderr=tool_result.stderr,
            artifacts=list(tool_result.artifacts),
            metadata={"provider": self.name, "tool_artifacts": [artifact.__dict__ for artifact in tool_result.tool_artifacts]},
        )

    def resume_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        timeout_seconds: int,
        trusted_command_prefixes: list[str] | None = None,
    ) -> CoderApprovalContinuationResult:
        del approval_id, approved, timeout_seconds, trusted_command_prefixes
        return CoderApprovalContinuationResult(
            status="unsupported",
            error="Claude Code coder provider approval resume is not implemented yet.",
            metadata={"provider": self.name, "reason": "claude_agent_sdk_resume_missing"},
        )


def build_coder_provider(settings: Settings) -> CoderProvider:
    provider = (settings.coder_runtime_provider or "codex").strip().lower()
    if provider == "codex":
        return CodexCoderProvider()
    if provider == "claude_code":
        return ClaudeCodeCoderProvider()
    raise ValueError(f"Unsupported coder runtime provider: {provider}")


def resume_coder_approval(
    approval_id: str,
    *,
    approved: bool,
    timeout_seconds: int,
    provider: str = "codex",
    trusted_command_prefixes: list[str] | None = None,
) -> CoderApprovalContinuationResult:
    provider_name = str(provider or "codex").strip().lower()
    if provider_name == "codex":
        coder_provider: CoderProvider = CodexCoderProvider()
    elif provider_name == "claude_code":
        coder_provider = ClaudeCodeCoderProvider()
    else:
        return CoderApprovalContinuationResult(
            status="unsupported",
            error=f"Unsupported coder runtime provider for approval resume: {provider_name}",
            metadata={"provider": provider_name},
        )
    return coder_provider.resume_approval(
        approval_id,
        approved=approved,
        timeout_seconds=timeout_seconds,
        trusted_command_prefixes=trusted_command_prefixes,
    )


def _approval_requests_from_artifacts(artifacts: list[str]) -> list[CoderApprovalRequest]:
    requests: list[CoderApprovalRequest] = []
    for artifact in artifacts:
        text = str(artifact)
        if not text.startswith("codex_approval_requests:"):
            continue
        path = Path(text.removeprefix("codex_approval_requests:"))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_requests = payload if isinstance(payload, list) else [payload]
        for index, raw in enumerate(raw_requests, start=1):
            if isinstance(raw, dict):
                requests.append(_approval_request_from_payload(raw, index=index))
    return requests


def _approval_request_from_payload(payload: dict[str, Any], *, index: int) -> CoderApprovalRequest:
    command = _optional_text(payload.get("command"))
    path = _optional_text(payload.get("path"))
    return CoderApprovalRequest(
        approval_id=_optional_text(payload.get("id") or payload.get("approval_id") or payload.get("request_id")) or f"approval_{index}",
        action_kind=_action_kind_from_command(command),
        command=command,
        path=path,
        reason=_optional_text(payload.get("reason") or payload.get("description") or payload.get("message")) or "",
        raw_provider_payload=dict(payload),
    )


def _action_kind_from_command(command: str | None) -> str:
    text = (command or "").strip().lower()
    if not text:
        return "unknown_external_action"
    if text.startswith("git status"):
        return "git_status"
    if text.startswith("git diff"):
        return "git_diff"
    if text.startswith("git log"):
        return "git_log"
    if text.startswith("git commit"):
        return "commit"
    if text.startswith("git push"):
        return "push"
    return "unknown_external_action"


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
