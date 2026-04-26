from __future__ import annotations

from typing import Any, Literal, Mapping, NotRequired, TypedDict


class ApprovalInterrupt(TypedDict):
    type: Literal["approval_required"]
    pending_approval_id: str | None
    pending_action: dict[str, Any] | None


class WaitWorkersInterrupt(TypedDict):
    type: Literal["wait_workers"]
    active_workers: dict[str, str]


class ClarificationInterrupt(TypedDict):
    type: Literal["clarification_required"]
    question: str


class ParsedInterrupt(TypedDict):
    type: Literal["approval_required", "wait_workers", "clarification_required"]
    value: ApprovalInterrupt | WaitWorkersInterrupt | ClarificationInterrupt
    status: Literal["waiting_approval", "monitoring", "waiting_clarification"]
    summary: str
    pending_approval_id: NotRequired[str | None]


def parse_interrupt_result(result: Mapping[str, Any]) -> ParsedInterrupt | None:
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None

    interrupt_info = interrupts[0]
    value = getattr(interrupt_info, "value", None)
    if not isinstance(value, dict):
        return None

    interrupt_type = value.get("type")
    if interrupt_type == "approval_required":
        parsed: ParsedInterrupt = {
            "type": "approval_required",
            "value": {
                "type": "approval_required",
                "pending_approval_id": _optional_str(value.get("pending_approval_id")),
                "pending_action": value.get("pending_action")
                if isinstance(value.get("pending_action"), dict)
                else None,
            },
            "status": "waiting_approval",
            "summary": "Waiting for approval",
            "pending_approval_id": _optional_str(value.get("pending_approval_id")),
        }
        return parsed

    if interrupt_type == "wait_workers":
        return {
            "type": "wait_workers",
            "value": {
                "type": "wait_workers",
                "active_workers": _string_dict(value.get("active_workers")),
            },
            "status": "monitoring",
            "summary": "Waiting for workers",
        }

    if interrupt_type == "clarification_required":
        question = str(value.get("question") or "Waiting for clarification")
        return {
            "type": "clarification_required",
            "value": {
                "type": "clarification_required",
                "question": question,
            },
            "status": "waiting_clarification",
            "summary": question,
        }

    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
