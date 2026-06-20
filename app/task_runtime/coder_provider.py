from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from app.config import Settings
from app.runtime_usage import usage_record_from_token_usage
from app.task_runtime.approval_types import ApprovalRequest, approval_request_from_mapping
from app.tools.codex import run_codex_coder_tool
from app.tools.codex_app_server import respond_to_codex_approval
from app.tools.common import ToolExecutionRequest
from app.tools.coder import run_coder_tool

@dataclass(frozen=True)
class CoderRunRequest:
    repo_id: str
    workdir: Path
    instruction: str
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
    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    raw_events_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoderApprovalContinuationResult:
    status: Literal["completed", "approval_requested", "failed", "timeout", "missing", "unsupported"]
    final_text: str = ""
    raw_events: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None
    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class CoderProvider(Protocol):
    name: str

    def run(self, request: CoderRunRequest) -> CoderRunResult: ...

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

    def run(self, request: CoderRunRequest) -> CoderRunResult:
        tool_result = self._runner(
            ToolExecutionRequest(
                tool_name="codex_coder_provider",
                workdir=None,
                args={
                    "instruction": request.instruction,
                    "repo_id": request.repo_id,
                    "_runtime_workdir": str(request.workdir),
                    "_runtime_run_dir": str(request.run_dir) if request.run_dir is not None else "",
                    "source_branch": str(request.metadata.get("source_branch") or ""),
                    "target_branch": str(request.metadata.get("target_branch") or ""),
                    "node_branch": str(request.metadata.get("node_branch") or ""),
                    "allow_commit": True,
                    "allow_push": False,
                    "_read_only": False,
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
            metadata={
                **dict(tool_result.metadata),
                "tool_artifacts": [artifact.__dict__ for artifact in tool_result.tool_artifacts],
            },
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
            approval_requests=_approval_requests_from_payloads(result.approval_requests),
            error=result.error,
            metadata={
                "provider": self.name,
                "tool_artifacts": [artifact.__dict__ for artifact in result.tool_artifacts],
                **_codex_usage_metadata(result.usage),
            },
        )


class ClaudeCodeCoderProvider:
    name = "claude_code"

    def run(self, request: CoderRunRequest) -> CoderRunResult:
        tool_result = run_coder_tool(
            ToolExecutionRequest(
                tool_name="claude_code_coder_provider",
                workdir=str(request.workdir),
                args={
                    "instruction": request.instruction,
                    "repo_id": request.repo_id,
                    "_runtime_run_dir": str(request.run_dir) if request.run_dir is not None else "",
                    "source_branch": str(request.metadata.get("source_branch") or ""),
                    "target_branch": str(request.metadata.get("target_branch") or ""),
                    "node_branch": str(request.metadata.get("node_branch") or ""),
                    "allow_commit": True,
                    "allow_push": False,
                    "_read_only": False,
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


def _codex_usage_metadata(usage) -> dict[str, Any]:
    record = usage_record_from_token_usage(
        usage,
        source="codex_app_server",
        provider="codex",
        model="codex",
        stage="coder",
    )
    return {"usage_records": [record]} if record is not None else {}


def _approval_requests_from_artifacts(artifacts: list[str]) -> list[ApprovalRequest]:
    requests: list[ApprovalRequest] = []
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
        requests.extend(_approval_requests_from_payloads(raw_requests))
    return requests


def _approval_requests_from_payloads(values: list[Any]) -> list[ApprovalRequest]:
    requests: list[ApprovalRequest] = []
    for index, value in enumerate(values, start=1):
        request = approval_request_from_mapping(value, fallback_id=f"approval_{index}")
        if request is not None:
            requests.append(request)
    return requests
