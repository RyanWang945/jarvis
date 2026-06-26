from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

ApprovalSource = Literal["codex_provider", "runtime_git"]
ApprovalContinuationStatus = Literal["completed", "approval_requested", "failed", "timeout", "missing", "unsupported", "rejected"]


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    action_kind: str
    reason: str = ""
    command: str | None = None
    path: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalContinuationResult:
    status: ApprovalContinuationStatus
    final_text: str = ""
    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def approval_request_from_mapping(value: Any, *, fallback_id: str | None = None) -> ApprovalRequest | None:
    if not isinstance(value, dict):
        return None
    approval_id = _text(value.get("approval_id") or value.get("id") or value.get("request_id") or fallback_id)
    if not approval_id:
        return None
    raw_payload = value.get("payload")
    payload = dict(raw_payload) if isinstance(raw_payload, dict) else dict(value)
    command = _optional_text(value.get("command"))
    return ApprovalRequest(
        approval_id=approval_id,
        action_kind=_action_kind(value, command),
        command=command,
        path=_optional_text(value.get("path")),
        reason=_text(value.get("reason") or value.get("description") or value.get("message")),
        payload=payload,
    )


def approval_request_to_dict(value: ApprovalRequest) -> dict[str, Any]:
    data: dict[str, Any] = {
        "approval_id": value.approval_id,
        "action_kind": value.action_kind,
        "reason": value.reason,
        "payload": dict(value.payload),
    }
    if value.command:
        data["command"] = value.command
    if value.path:
        data["path"] = value.path
    return data


def approval_request_dicts(values: Iterable[Any]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        request = value if isinstance(value, ApprovalRequest) else approval_request_from_mapping(value)
        if request is None or request.approval_id in seen:
            continue
        seen.add(request.approval_id)
        requests.append(approval_request_to_dict(request))
    return requests


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _action_kind(value: dict[str, Any], command: str | None) -> str:
    explicit = _text(value.get("action_kind") or value.get("kind"))
    if explicit:
        return explicit
    inferred = _action_kind_from_command(command)
    if inferred:
        return inferred
    fallback = _text(value.get("type"))
    return fallback or "unknown_external_action"


def _action_kind_from_command(command: str | None) -> str:
    text = (command or "").strip().lower()
    if not text:
        return ""
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
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip() if value is not None else ""
