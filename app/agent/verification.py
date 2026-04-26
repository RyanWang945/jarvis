from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from app.agent.state import Task
from app.config import get_settings
from app.llm.jarvis import get_jarvis_llm
from app.workers import WorkOrder, WorkResult

CompletionDecision = Literal["success", "retry", "replan", "failed", "blocked", "needs_assessment"]


@dataclass(frozen=True)
class CompletionAssessment:
    decision: CompletionDecision
    summary: str


def can_retry(task: Task) -> bool:
    return int(task.get("retry_count") or 0) < int(task.get("max_retries") or 0)


def assess_task_completion(task: Task, result: WorkResult | None) -> CompletionAssessment:
    rule_assessment = assess_task_completion_by_rules(task, result)
    if rule_assessment.decision != "needs_assessment":
        return rule_assessment
    return assess_task_completion_semantically(task, result)


def assess_task_completion_by_rules(
    task: Task,
    result: WorkResult | None,
) -> CompletionAssessment:
    if result is None:
        summary = "Worker result missing."
        if can_retry(task):
            return CompletionAssessment("retry", summary)
        return CompletionAssessment("blocked", summary)

    if not result.ok:
        summary = result.summary or result.stderr or "Worker failed."
        if can_retry(task):
            return CompletionAssessment("retry", summary)
        return CompletionAssessment("failed", summary)

    if is_objective_success(task):
        return CompletionAssessment("success", result.summary)

    return CompletionAssessment("needs_assessment", result.summary)


def assess_task_completion_semantically(
    task: Task,
    result: WorkResult | None,
) -> CompletionAssessment:
    if result is None:
        return CompletionAssessment("blocked", "Worker result missing.")
    settings = get_settings()
    if settings.planner_type != "llm":
        return CompletionAssessment("success", result.summary)

    try:
        assessment = get_jarvis_llm().assess_completion(
            task=task_assessment_payload(task),
            result=result.model_dump(),
            can_retry=can_retry(task),
        )
    except Exception as exc:
        return CompletionAssessment("success", f"{result.summary} Completion assessment unavailable: {exc}")

    decision = cast(CompletionDecision, assessment["decision"])
    if decision == "retry" and not can_retry(task):
        decision = "failed"
    return CompletionAssessment(decision, assessment["summary"])


def is_objective_success(task: Task) -> bool:
    if task.get("verification_cmd"):
        return True
    tool_name = task.get("tool_name")
    worker_type = task.get("worker_type")
    dod = (task.get("dod") or "").lower()
    if tool_name in {"shell.command", "shell.test", "answer.echo", "run_shell_command", "run_tests", "echo"}:
        return True
    if worker_type in {"shell", "echo"} and any(
        marker in dod for marker in ("completed", "success", "passed", "exited")
    ):
        return True
    if worker_type and worker_type != "coder":
        return True
    return False


def task_assessment_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task["description"],
        "dod": task.get("dod"),
        "tool_name": task.get("tool_name"),
        "worker_type": task.get("worker_type"),
        "retry_count": task.get("retry_count"),
        "max_retries": task.get("max_retries"),
    }


def retry_task(
    task: Task,
    work_orders: dict[str, dict[str, Any]],
    *,
    ca_thread_id: str,
    failure_summary: str | None = None,
) -> dict[str, Any]:
    previous_order_id = task.get("order_id")
    previous_order = work_orders.get(previous_order_id or "")
    retry_count = int(task.get("retry_count") or 0) + 1
    order_id = str(uuid4())

    updated = task.copy()
    updated["status"] = "pending"
    updated["retry_count"] = retry_count
    updated["order_id"] = order_id
    if failure_summary:
        updated["result_summary"] = f"Retry {retry_count}: {failure_summary}"
    else:
        updated["result_summary"] = f"Retry {retry_count}: worker result missing."

    if previous_order:
        order_dump = dict(previous_order)
        order_dump["order_id"] = order_id
        order_dump["task_id"] = updated["id"]
    else:
        order = WorkOrder(
            order_id=order_id,
            task_id=updated["id"],
            ca_thread_id=ca_thread_id,
            capability_name=updated.get("tool_name"),
            worker_type=updated.get("worker_type") or "echo",
            provider=updated.get("worker_type") or "echo",
            action="echo",
            args=updated.get("tool_args", {}),
            risk_level="low",
            reason=updated.get("description") or "Retry task",
            verification_cmd=updated.get("verification_cmd"),
        )
        order_dump = order.model_dump()

    work_orders[order_id] = order_dump
    return {"task": updated, "order": order_dump}
