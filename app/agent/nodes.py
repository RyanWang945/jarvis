from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from app.agent.state import AgentState, PendingAction, RiskLevel, Task
from app.config import get_settings
from app.llm.deepseek import DeepSeekClient
from app.tools import get_default_tool_registry
from app.tools.specs import ToolCallPlan
from app.workers import WorkOrder, WorkResult, get_inline_worker_client

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
    tasks: list[Task] = []
    dispatch_queue: list[dict[str, Any]] = []

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
        dispatch_queue.append(order.model_dump())

    if not tasks:
        return {"status": "failed", "last_error": "Strategize produced no work orders.", "next_node": "blocked"}

    return {
        "status": "strategizing",
        "task_list": tasks,
        "dispatch_queue": dispatch_queue,
        "active_workers": {},
        "worker_results": {},
        "next_node": "dispatch",
    }


def route_after_strategize(state: AgentState) -> str:
    return state.get("next_node") or "dispatch"


def dispatch(state: AgentState) -> dict[str, Any]:
    if not state.get("dispatch_queue"):
        return {"status": "failed", "last_error": "No work orders to dispatch.", "next_node": "blocked"}

    client = get_inline_worker_client()
    active_workers = dict(state.get("active_workers", {}))
    worker_results = dict(state.get("worker_results", {}))
    task_list = [task.copy() for task in state["task_list"]]

    for order_dict in state.get("dispatch_queue", []):
        order = WorkOrder(**order_dict)
        if order.risk_level in {"high", "critical"}:
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
        result = client.poll(order.order_id)
        if result is not None:
            worker_results[order.order_id] = result.model_dump()
        active_workers[order.task_id] = order.order_id
        for task in task_list:
            if task["id"] == order.task_id:
                task["status"] = "running"

    return {
        "status": "dispatching",
        "task_list": task_list,
        "dispatch_queue": [],
        "active_workers": active_workers,
        "worker_results": worker_results,
        "next_node": "monitor",
    }


def route_after_dispatch(state: AgentState) -> str:
    return state.get("next_node") or "monitor"


def monitor(state: AgentState) -> dict[str, Any]:
    worker_results = dict(state.get("worker_results", {}))
    active_workers = dict(state.get("active_workers", {}))
    client = get_inline_worker_client()

    for task_id, order_id in list(active_workers.items()):
        if order_id not in worker_results:
            result = client.poll(order_id)
            if result is not None:
                worker_results[order_id] = result.model_dump()
        if order_id in worker_results:
            active_workers.pop(task_id, None)

    return {
        "status": "monitoring",
        "active_workers": active_workers,
        "worker_results": worker_results,
        "next_node": "aggregate" if not active_workers else "blocked",
    }


def route_after_monitor(state: AgentState) -> str:
    return state.get("next_node") or "blocked"


def aggregate(state: AgentState) -> dict[str, Any]:
    worker_results = state.get("worker_results", {})
    tasks: list[Task] = []
    failed = False
    for task in state["task_list"]:
        updated = task.copy()
        order_id = updated.get("order_id")
        result = WorkResult(**worker_results[order_id]) if order_id and order_id in worker_results else None
        if result is None:
            updated["status"] = "blocked"
            updated["result_summary"] = "Worker result missing."
            failed = True
        elif result.ok:
            updated["status"] = "success"
            updated["result_summary"] = result.summary
        else:
            updated["status"] = "failed"
            updated["result_summary"] = result.summary or result.stderr or "Worker failed."
            failed = True
        tasks.append(updated)

    return {
        "status": "failed" if failed else "running",
        "task_list": tasks,
        "next_node": "blocked" if failed else "summarize",
    }


def route_after_aggregate(state: AgentState) -> str:
    return state.get("next_node") or "blocked"


def wait_approval(state: AgentState) -> dict[str, Any]:
    tasks = _update_current_task(
        state,
        status="waiting",
        result_summary=f"Waiting for approval: {state.get('pending_approval_id')}",
    )
    return {
        "task_list": tasks,
        "final_summary": "Task is waiting for local approval before executing a high-risk action.",
    }


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


def _planned_tool_calls(state: AgentState) -> list[ToolCallPlan]:
    settings = get_settings()
    if settings.planner_type != "llm":
        return _rule_based_tool_calls(state)
    if not settings.deepseek_api_key:
        raise ValueError("JARVIS_DEEPSEEK_API_KEY is required when JARVIS_PLANNER_TYPE=llm.")

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    client = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_timeout_seconds,
    )
    return client.plan_tasks(
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
    }
