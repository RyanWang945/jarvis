from __future__ import annotations

import re
from typing import cast
from uuid import uuid4

from app.agent.common import clean_optional
from app.agent.state import ActionKind, PendingAction, RiskLevel
from app.workers import WorkOrder

HIGH_RISK_PATTERNS = [
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b.*\b-Recurse\b",
    r"\bdel\s+/s\b",
    r"\brmdir\s+/s\b",
    r"\bdocker\s+system\s+prune\b",
    r"\bkubectl\s+apply\b",
    r"\bvercel\b.*\b--prod\b",
    r"\bgit\s+config\s+--global\b",
]

RECOVERY_APPROVAL_PATTERNS = [
    *HIGH_RISK_PATTERNS,
    r">\s*[^&]",
    r">>",
    r"\bSet-Content\b",
    r"\bAdd-Content\b",
    r"\bOut-File\b",
    r"\bNew-Item\b",
    r"\bMove-Item\b",
    r"\bCopy-Item\b",
    r"\bgit\s+commit\b",
]


def classify_risk(command: str | None) -> RiskLevel:
    if not command:
        return "low"
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return "high"
    return "low"


def work_order_risk(base_risk: RiskLevel, *, command: str | None, verification_cmd: str | None) -> RiskLevel:
    risk = highest_risk(base_risk, classify_risk(command))
    return highest_risk(risk, classify_risk(verification_cmd))


def highest_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    order: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if order[left] >= order[right] else right


def pending_action_from_order(order: WorkOrder) -> PendingAction:
    command = approval_command_summary(order)
    return {
        "action_id": str(uuid4()),
        "capability_name": order.capability_name,
        "kind": cast(ActionKind, order.worker_type),
        "skill": order.worker_type,
        "provider": order.provider,
        "action": order.action,
        "args": order.args,
        "command": command,
        "workdir": order.workdir,
        "risk_level": order.risk_level,
        "reason": order.reason,
        "status": "waiting_approval",
        "order_id": order.order_id,
    }


def approval_command_summary(order: WorkOrder) -> str | None:
    commands: list[str] = []
    command = clean_optional(order.args.get("command"))
    if order.worker_type == "coder":
        command = clean_optional(order.args.get("instruction"))
    if command:
        commands.append(command)
    verification_cmd = clean_optional(order.verification_cmd)
    if verification_cmd:
        commands.append(f"verification: {verification_cmd}")
    return "\n".join(commands) if commands else None


def requires_recovery_approval(order: WorkOrder) -> bool:
    if order.risk_level in {"high", "critical"}:
        return True
    if order.worker_type == "coder":
        return True
    command = approval_command_summary(order)
    if not command:
        return False
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in RECOVERY_APPROVAL_PATTERNS)
