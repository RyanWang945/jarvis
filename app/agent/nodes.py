from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from langgraph.types import interrupt

from app.agent.state import AgentState, PendingAction, RiskLevel, Task
from app.config import get_settings
from app.llm.jarvis import get_jarvis_llm
from app.tools import get_default_tool_registry
from app.tools.specs import ToolCallPlan
from app.workers import WorkOrder, WorkResult, get_worker_client

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

CompletionDecision = Literal["success", "retry", "replan", "failed", "blocked", "needs_assessment"]


@dataclass(frozen=True)
class CompletionAssessment:
    decision: CompletionDecision
    summary: str


def ingest_event(state: AgentState) -> dict[str, Any]:
    instruction = _payload(state).get("instruction") or ""
    return {
        "status": "created",
        "messages": [{"role": "user", "content": str(instruction)}],
    }


def contextualize(state: AgentState) -> dict[str, Any]:
    payload = _payload(state)
    return {
        "status": "contextualizing",
        "resource_key": payload.get("resource_key") or payload.get("workdir"),
        "context_summary": str(payload.get("instruction") or ""),
    }


def strategize(state: AgentState) -> dict[str, Any]:
    try:
        planned_calls = _planned_tool_calls(state)
    except Exception as exc:
        return {
            "status": "failed",
            "last_error": f"Strategize failed: {exc}",
            "next_node": "blocked",
        }

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    registry = get_default_tool_registry()
    previous_tasks = [task.copy() for task in state.get("task_list", [])]
    tasks: list[Task] = []
    dispatch_queue: list[dict[str, Any]] = []
    work_orders: dict[str, dict[str, Any]] = dict(state.get("work_orders", {}))

    for item in planned_calls:
        tool_name = item.tool_name or "echo"
        try:
            tool = registry.get(tool_name)
        except ValueError:
            tool_name = "echo"
            tool = registry.get(tool_name)

        task_id = str(uuid4())
        order_id = str(uuid4())
        tool_args = item.tool_args if isinstance(item.tool_args, dict) else {}
        if payload.get("workdir") and "workdir" not in tool_args:
            tool_args["workdir"] = payload["workdir"]

        command = _clean_optional(tool_args.get("command"))
        if tool.skill == "claude_code":
            command = _clean_optional(tool_args.get("instruction"))
        risk_level = _highest_risk(tool.risk_level, _classify_risk(command))
        worker_type = _tool_to_worker_type(tool_name)
        workdir = _clean_optional(tool_args.get("workdir"))
        verification_cmd = (
            _clean_optional(item.verification_cmd)
            or _clean_optional(tool_args.get("verification_cmd"))
            or _clean_optional(payload.get("verification_cmd"))
        )

        task: Task = {
            "id": task_id,
            "title": item.title or _title_from_tool_call(tool_name, tool_args, instruction),
            "description": item.description or instruction or tool.description,
            "status": "pending",
            "resource_key": state.get("resource_key"),
            "dod": item.dod or f"{tool.name} completed successfully.",
            "verification_cmd": verification_cmd,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "worker_type": worker_type,
            "order_id": order_id,
            "retry_count": 0,
            "max_retries": int(item.max_retries or 0),
            "result_summary": None,
        }
        order = WorkOrder(
            order_id=order_id,
            task_id=task_id,
            ca_thread_id=state["thread_id"],
            worker_type=worker_type,
            action=tool.action,
            args=tool_args,
            workdir=workdir,
            risk_level=risk_level,
            reason=task["description"],
            verification_cmd=verification_cmd,
            timeout_seconds=30,
        )
        tasks.append(task)
        order_dump = order.model_dump()
        dispatch_queue.append(order_dump)
        work_orders[order.order_id] = order_dump

    if not tasks:
        return {"status": "failed", "last_error": "Strategize produced no work orders.", "next_node": "blocked"}

    return {
        "status": "strategizing",
        "task_list": previous_tasks + tasks,
        "dispatch_queue": dispatch_queue,
        "work_orders": work_orders,
        "approved_order_ids": state.get("approved_order_ids", []),
        "active_workers": {},
        "worker_results": {},
        "next_node": "dispatch",
    }


def route_after_strategize(state: AgentState) -> str:
    return state.get("next_node") or "dispatch"


def dispatch(state: AgentState) -> dict[str, Any]:
    if not state.get("dispatch_queue"):
        return {"status": "failed", "last_error": "No work orders to dispatch.", "next_node": "blocked"}

    client = get_worker_client()
    active_workers = dict(state.get("active_workers", {}))
    work_orders = dict(state.get("work_orders", {}))
    approved_order_ids = set(state.get("approved_order_ids", []))
    task_list = [task.copy() for task in state["task_list"]]

    for order_dict in state.get("dispatch_queue", []):
        order = WorkOrder(**order_dict)
        work_orders[order.order_id] = order.model_dump()
        if order.risk_level in {"high", "critical"} and order.order_id not in approved_order_ids:
            for task in task_list:
                if task["id"] == order.task_id:
                    task["status"] = "waiting"
            return {
                "status": "waiting_approval",
                "task_list": task_list,
                "current_task_id": order.task_id,
                "pending_action": _pending_action_from_order(order),
                "pending_approval_id": str(uuid4()),
                "next_node": "wait_approval",
            }

        client.dispatch(order)
        active_workers[order.task_id] = order.order_id
        for task in task_list:
            if task["id"] == order.task_id:
                task["status"] = "running"

    return {
        "status": "dispatching",
        "task_list": task_list,
        "dispatch_queue": [],
        "work_orders": work_orders,
        "approved_order_ids": list(approved_order_ids),
        "active_workers": active_workers,
        "next_node": "monitor",
    }


def route_after_dispatch(state: AgentState) -> str:
    return state.get("next_node") or "monitor"


def monitor(state: AgentState) -> dict[str, Any]:
    worker_results = dict(state.get("worker_results", {}))
    active_workers = dict(state.get("active_workers", {}))
    client = get_worker_client()

    for task_id, order_id in list(active_workers.items()):
        if order_id not in worker_results:
            result = client.poll(order_id)
            if result is not None:
                worker_results[order_id] = result.model_dump()
        if order_id in worker_results:
            active_workers.pop(task_id, None)

    if active_workers:
        worker_event = interrupt(
            {
                "type": "wait_workers",
                "active_workers": dict(active_workers),
            }
        )
        if isinstance(worker_event, dict) and worker_event.get("event_type") in {
            "worker_complete",
            "worker_failed",
        }:
            payload = worker_event.get("payload", {})
            order_id = payload.get("order_id")
            if order_id:
                normalized = _normalize_worker_event_payload(
                    payload,
                    order_id=order_id,
                    active_workers=active_workers,
                    work_orders=state.get("work_orders", {}),
                    failed=worker_event.get("event_type") == "worker_failed",
                )
                worker_results[order_id] = normalized
                for tid, oid in list(active_workers.items()):
                    if oid == order_id:
                        active_workers.pop(tid, None)

    return {
        "status": "monitoring",
        "active_workers": active_workers,
        "worker_results": worker_results,
        "next_node": "aggregate" if not active_workers else "monitor",
    }


def route_after_monitor(state: AgentState) -> str:
    return state.get("next_node") or "blocked"


def aggregate(state: AgentState) -> dict[str, Any]:
    worker_results = state.get("worker_results", {})
    work_orders = dict(state.get("work_orders", {}))
    tasks: list[Task] = []
    failed = False
    needs_replan = False
    dispatch_queue: list[dict[str, Any]] = []
    for task in state["task_list"]:
        updated = task.copy()
        order_id = updated.get("order_id")
        result = WorkResult(**worker_results[order_id]) if order_id and order_id in worker_results else None
        assessment = _assess_task_completion(updated, result)
        if assessment.decision == "success":
            updated["status"] = "success"
            updated["result_summary"] = assessment.summary
        elif assessment.decision == "retry":
            retry = _retry_task(
                updated,
                work_orders,
                ca_thread_id=state["thread_id"],
                failure_summary=assessment.summary,
            )
            updated = retry["task"]
            dispatch_queue.append(retry["order"])
        elif assessment.decision == "replan":
            updated["status"] = "cancelled"
            updated["result_summary"] = f"Replanning: {assessment.summary}"
            needs_replan = True
        else:
            updated["status"] = "blocked" if assessment.decision == "blocked" else "failed"
            updated["result_summary"] = assessment.summary
            failed = True
        tasks.append(updated)

    if dispatch_queue:
        return {
            "status": "dispatching",
            "task_list": tasks,
            "dispatch_queue": dispatch_queue,
            "work_orders": work_orders,
            "active_workers": {},
            "next_node": "dispatch",
        }

    if needs_replan and not failed:
        return {
            "status": "strategizing",
            "task_list": tasks,
            "work_orders": work_orders,
            "active_workers": {},
            "worker_results": {},
            "last_error": "Replanning after completion assessment.",
            "next_node": "strategize",
        }

    return {
        "status": "failed" if failed else "running",
        "task_list": tasks,
        "next_node": "blocked" if failed else "summarize",
    }


def route_after_aggregate(state: AgentState) -> str:
    return state.get("next_node") or "blocked"


def wait_approval(state: AgentState) -> dict[str, Any]:
    approval = interrupt(
        {
            "type": "approval_required",
            "pending_approval_id": state.get("pending_approval_id"),
            "pending_action": state.get("pending_action"),
        }
    )

    if isinstance(approval, dict) and approval.get("approved"):
        tasks = _update_current_task(
            state, status="approved", result_summary="Approved by user."
        )
        pending = state.get("pending_action")
        if pending:
            order_id = pending.get("order_id") or str(uuid4())
            work_orders = dict(state.get("work_orders", {}))
            order_dump = work_orders.get(order_id)
            if order_dump is None:
                order_dump = WorkOrder(
                    order_id=order_id,
                    task_id=state.get("current_task_id", ""),
                    ca_thread_id=state["thread_id"],
                    worker_type=pending["kind"],
                    action=pending["action"],
                    args=pending["args"],
                    workdir=pending["workdir"],
                    risk_level=pending["risk_level"],
                    reason=pending["reason"],
                ).model_dump()
                work_orders[order_id] = order_dump
            approved_order_ids = set(state.get("approved_order_ids", []))
            approved_order_ids.add(order_id)
            updated_tasks: list[Task] = []
            for task in tasks:
                t = task.copy()
                if t["id"] == state.get("current_task_id"):
                    t["order_id"] = order_id
                updated_tasks.append(t)
            return {
                "task_list": updated_tasks,
                "dispatch_queue": [order_dump],
                "work_orders": work_orders,
                "approved_order_ids": list(approved_order_ids),
                "pending_action": None,
                "pending_approval_id": None,
                "next_node": "dispatch",
            }
        return {
            "task_list": tasks,
            "next_node": "summarize",
        }
    else:
        tasks = _update_current_task(
            state, status="blocked", result_summary="Rejected by user."
        )
        return {
            "task_list": tasks,
            "next_node": "blocked",
            "pending_action": None,
            "pending_approval_id": None,
        }


def route_after_wait_approval(state: AgentState) -> str:
    return state.get("next_node") or "blocked"


def summarize(state: AgentState) -> dict[str, Any]:
    tasks = state["task_list"]
    successful = sum(1 for task in tasks if task["status"] == "success")
    failed = sum(1 for task in tasks if task["status"] in {"failed", "blocked"})
    summary = f"Completed {successful} task(s)"
    if failed:
        summary += f"; {failed} task(s) failed or blocked"
    if tasks and tasks[-1].get("result_summary"):
        summary += f". Last result: {tasks[-1]['result_summary']}"
    return {"status": "completed" if failed == 0 else "failed", "final_summary": summary}


def blocked(state: AgentState) -> dict[str, Any]:
    tasks = state["task_list"]
    if state.get("current_task_id"):
        tasks = _update_current_task(
            state,
            status="blocked",
            result_summary=state.get("last_error") or state.get("final_summary") or "Task blocked.",
        )
    return {
        "status": "blocked",
        "task_list": tasks,
        "final_summary": state.get("final_summary") or state.get("last_error") or "Task blocked.",
    }


def _payload(state: AgentState) -> dict[str, Any]:
    payload = state["event"].get("payload", {})
    return payload if isinstance(payload, dict) else {}


def _update_current_task(state: AgentState, *, status: str, result_summary: str | None) -> list[Task]:
    current_task_id = state.get("current_task_id")
    tasks: list[Task] = []
    for task in state["task_list"]:
        updated = task.copy()
        if updated["id"] == current_task_id:
            updated["status"] = status  # type: ignore[typeddict-item]
            updated["result_summary"] = result_summary
        tasks.append(updated)
    return tasks


def _can_retry(task: Task) -> bool:
    return int(task.get("retry_count") or 0) < int(task.get("max_retries") or 0)


def _assess_task_completion(task: Task, result: WorkResult | None) -> CompletionAssessment:
    rule_assessment = _assess_task_completion_by_rules(task, result)
    if rule_assessment.decision != "needs_assessment":
        return rule_assessment
    return _assess_task_completion_semantically(task, result)


def _assess_task_completion_by_rules(
    task: Task,
    result: WorkResult | None,
) -> CompletionAssessment:
    if result is None:
        summary = "Worker result missing."
        if _can_retry(task):
            return CompletionAssessment("retry", summary)
        return CompletionAssessment("blocked", summary)

    if not result.ok:
        summary = result.summary or result.stderr or "Worker failed."
        if _can_retry(task):
            return CompletionAssessment("retry", summary)
        return CompletionAssessment("failed", summary)

    if _is_objective_success(task):
        return CompletionAssessment("success", result.summary)

    return CompletionAssessment("needs_assessment", result.summary)


def _assess_task_completion_semantically(
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
            task=_task_assessment_payload(task),
            result=result.model_dump(),
            can_retry=_can_retry(task),
        )
    except Exception as exc:
        return CompletionAssessment("success", f"{result.summary} Completion assessment unavailable: {exc}")

    decision = assessment["decision"]
    if decision == "retry" and not _can_retry(task):
        decision = "failed"
    return CompletionAssessment(decision, assessment["summary"])


def _is_objective_success(task: Task) -> bool:
    if task.get("verification_cmd"):
        return True
    tool_name = task.get("tool_name")
    worker_type = task.get("worker_type")
    dod = (task.get("dod") or "").lower()
    if tool_name in {"run_shell_command", "run_tests", "echo"}:
        return True
    if worker_type in {"shell", "echo"} and any(
        marker in dod for marker in ("completed", "success", "passed", "exited")
    ):
        return True
    return False


def _task_assessment_payload(task: Task) -> dict[str, Any]:
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


def _retry_task(
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
            worker_type=updated.get("worker_type") or "echo",
            action="echo",
            args=updated.get("tool_args", {}),
            risk_level="low",
            reason=updated.get("description") or "Retry task",
            verification_cmd=updated.get("verification_cmd"),
        )
        order_dump = order.model_dump()

    work_orders[order_id] = order_dump
    return {"task": updated, "order": order_dump}


def _classify_risk(command: str | None) -> RiskLevel:
    if not command:
        return "low"
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return "high"
    return "low"


def _highest_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    order: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if order[left] >= order[right] else right


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _title_from_instruction(instruction: str, command: str | None) -> str:
    if command:
        return f"Run command: {command[:80]}"
    return instruction[:80] or "Agent task"


def _title_from_tool_call(tool_name: str, tool_args: dict[str, Any], instruction: str) -> str:
    command = _clean_optional(tool_args.get("command"))
    if command:
        return f"Run command: {command[:80]}"
    if tool_name == "delegate_to_claude_code":
        delegated = _clean_optional(tool_args.get("instruction"))
        return f"Delegate code task: {delegated[:80]}" if delegated else "Delegate code task"
    return instruction[:80] or f"Use tool: {tool_name}"


def _planner_instruction(payload: dict[str, Any], instruction: str) -> str:
    context: list[str] = [instruction]
    command = _clean_optional(payload.get("command"))
    verification_cmd = _clean_optional(payload.get("verification_cmd"))
    workdir = _clean_optional(payload.get("workdir"))
    if command:
        context.append(f"Explicit command requested by caller: {command}")
    if verification_cmd:
        context.append(f"Verification command requested by caller: {verification_cmd}")
    if workdir:
        context.append(f"Preferred working directory: {workdir}")
    return "\n\n".join(context)


def _replan_context(state: AgentState) -> str | None:
    lines: list[str] = []
    worker_results = state.get("worker_results", {})
    for task in state.get("task_list", []):
        result_summary = _clean_optional(task.get("result_summary"))
        if not result_summary:
            continue
        if task.get("status") not in {"cancelled", "failed", "blocked"}:
            continue
        lines.append(
            "\n".join(
                [
                    f"- Previous task: {task.get('title') or task.get('description') or task['id']}",
                    f"  status: {task.get('status')}",
                    f"  worker: {task.get('worker_type')}",
                    f"  DoD: {task.get('dod')}",
                    f"  outcome: {result_summary}",
                ]
            )
        )
        order_id = task.get("order_id")
        if order_id and order_id in worker_results:
            result = WorkResult(**worker_results[order_id])
            if result.stderr:
                lines.append(f"  stderr: {result.stderr[:1000]}")
            if result.stdout:
                lines.append(f"  stdout: {result.stdout[:1000]}")
    if not lines:
        return None
    return "Replanning context from previous attempts:\n" + "\n".join(lines)


def _planned_tool_calls(state: AgentState) -> list[ToolCallPlan]:
    settings = get_settings()
    if settings.planner_type != "llm":
        return _rule_based_tool_calls(state)

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    replan_context = _replan_context(state)
    if replan_context:
        instruction = f"{instruction}\n\n{replan_context}"
    return get_jarvis_llm().plan_tasks(
        instruction=_planner_instruction(payload, instruction),
        tools=get_default_tool_registry().list(exposed_to_llm=True),
    )


def _rule_based_tool_calls(state: AgentState) -> list[ToolCallPlan]:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "Run local agent task.")
    command = _clean_optional(payload.get("command"))
    if command:
        return [
            ToolCallPlan(
                tool_name="run_shell_command",
                tool_args={"command": command},
                title=_title_from_instruction(instruction, command),
                description=instruction,
                dod=str(payload.get("dod") or "Task execution completed successfully."),
                verification_cmd=_clean_optional(payload.get("verification_cmd")),
                max_retries=int(payload.get("max_retries") or 0),
            )
        ]
    return [
        ToolCallPlan(
            tool_name="echo",
            tool_args={"text": instruction},
            title=instruction[:80] or "Agent task",
            description=instruction,
            dod=str(payload.get("dod") or "Task execution completed successfully."),
            verification_cmd=_clean_optional(payload.get("verification_cmd")),
            max_retries=int(payload.get("max_retries") or 0),
        )
    ]


def _tool_to_worker_type(tool_name: str) -> str:
    if tool_name in {"run_shell_command", "run_tests"}:
        return "shell"
    if tool_name == "delegate_to_claude_code":
        return "coder"
    if tool_name == "web_search":
        return "web_search"
    return "echo"


def _pending_action_from_order(order: WorkOrder) -> PendingAction:
    command = _clean_optional(order.args.get("command"))
    if order.worker_type == "coder":
        command = _clean_optional(order.args.get("instruction"))
    return {
        "action_id": str(uuid4()),
        "kind": order.worker_type,
        "skill": order.worker_type,
        "action": order.action,
        "args": order.args,
        "command": command,
        "workdir": order.workdir,
        "risk_level": order.risk_level,
        "reason": order.reason,
        "status": "waiting_approval",
        "order_id": order.order_id,
    }


def _normalize_worker_event_payload(
    payload: dict[str, Any],
    *,
    order_id: str,
    active_workers: dict[str, str],
    work_orders: dict[str, dict[str, Any]],
    failed: bool,
) -> dict[str, Any]:
    task_id = payload.get("task_id")
    if not task_id:
        task_id = next(
            (tid for tid, active_order_id in active_workers.items() if active_order_id == order_id),
            "",
        )
    order_dict = work_orders.get(order_id, {})
    worker_type = payload.get("worker_type") or order_dict.get("worker_type") or "echo"
    result = WorkResult(
        order_id=order_id,
        task_id=task_id,
        ca_thread_id=payload.get("ca_thread_id") or order_dict.get("ca_thread_id") or "",
        worker_type=worker_type,
        ok=bool(payload.get("ok", not failed)),
        exit_code=payload.get("exit_code"),
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
        artifacts=payload.get("artifacts", []),
        summary=payload.get("summary", "Worker failed." if failed else ""),
    )
    return result.model_dump()
