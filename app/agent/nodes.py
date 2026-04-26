from __future__ import annotations

import re
from typing import Any, cast
from uuid import uuid4

from langgraph.types import Command, interrupt

from app.agent.common import clean_optional as _clean_optional
from app.agent.common import goto as _goto
from app.agent.common import payload as _payload
from app.agent.common import task_status_context as _task_status_context
from app.agent.common import update_current_task as _update_current_task
from app.agent.risk import pending_action_from_order as _pending_action_from_order
from app.agent.risk import requires_recovery_approval
from app.agent.risk import work_order_risk as _work_order_risk
from app.agent.state import AgentState, IntentDecision, PlanStep, Task, WorkPlan
from app.agent.synthesis import synthesize_final_answer as _synthesize_final_answer
from app.agent.verification import CompletionAssessment as CompletionAssessment
from app.agent.verification import assess_task_completion as _assess_task_completion
from app.agent.verification import can_retry as _can_retry
from app.agent.verification import retry_task as _retry_task
from app.config import get_settings
from app.llm.jarvis import get_jarvis_llm
from app.tools import WorkerCapability, get_default_capability_registry
from app.tools.specs import IntentKind, PlannerDecision, ToolCallPlan
from app.workers import WorkOrder, WorkResult, get_worker_client

def ingest_event(state: AgentState) -> dict[str, Any]:
    instruction = _payload(state).get("instruction") or ""
    return {
        "status": "created",
        "messages": [{"role": "user", "content": str(instruction)}],
    }


def contextualize(state: AgentState) -> dict[str, Any]:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    summary_parts = [instruction] if instruction else []
    previous_summary = state.get("context_summary")
    if previous_summary and previous_summary != instruction:
        summary_parts.append(f"Previous context: {previous_summary}")
    if state.get("task_list"):
        summary_parts.append(_task_status_context(state))
    if state.get("last_error"):
        summary_parts.append(f"Last error: {state['last_error']}")
    return {
        "status": "contextualizing",
        "resource_key": payload.get("resource_key") or payload.get("workdir"),
        "context_summary": "\n\n".join(summary_parts),
    }


def classify_intent(state: AgentState) -> dict[str, Any]:
    decision = _classify_intent(state)
    work_plan = _build_work_plan(state, decision)
    return {
        "status": "planning",
        "intent": decision,
        "observation_intent": decision,
        "work_plan": work_plan,
        "allowed_tools": decision["allowed_tools"],
        "plan_steps": decision["plan_steps"],
    }


def strategize(state: AgentState) -> Command:
    try:
        planner_decision = _planned_decision(state)
    except Exception as exc:
        return _goto(
            "blocked",
            {
                "status": "failed",
                "last_error": f"Strategize failed: {exc}",
            },
        )
    if planner_decision.needs_clarification or planner_decision.confidence < 0.7:
        clarification = (
            planner_decision.clarification_question
            or "I need more information before I can plan this request."
        )
        return _goto(
            "clarify",
            {
                "status": "waiting_clarification",
                "pending_clarification": clarification,
                "last_error": clarification,
                "planner_raw_output": planner_decision.raw_output,
            },
        )
    planned_calls = planner_decision.tool_calls

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    capability_registry = get_default_capability_registry()
    candidate_tools = [capability.name for capability in _candidate_capabilities_for_state(state)]
    previous_tasks = [task.copy() for task in state.get("task_list", [])]
    tasks: list[Task] = []
    dispatch_queue: list[dict[str, Any]] = []
    work_orders: dict[str, dict[str, Any]] = dict(state.get("work_orders", {}))

    for item in planned_calls:
        requested_tool_name = item.tool_name or "answer.echo"
        try:
            capability = capability_registry.get(requested_tool_name)
        except ValueError:
            return _goto(
                "blocked",
                {
                    "status": "failed",
                    "last_error": f"Planner selected unknown capability '{requested_tool_name}'.",
                },
            )
        tool_name = capability.name
        tool_args = dict(item.tool_args) if isinstance(item.tool_args, dict) else {}
        if payload.get("workdir") and "workdir" not in tool_args:
            tool_args["workdir"] = payload["workdir"]
        if capability.name == "shell.command" and payload.get("command") and "command" not in tool_args:
            tool_args["command"] = payload["command"]
        if capability.name == "shell.test" and "command" not in tool_args:
            payload_command = _clean_optional(payload.get("command"))
            tool_args["command"] = (
                payload_command
                if payload_command and _is_allowed_test_command(payload_command)
                else "uv run pytest"
            )
        eligibility_error = _capability_eligibility_error(capability, state, tool_args)
        if eligibility_error:
            return _goto(
                "blocked",
                {
                    "status": "failed",
                    "last_error": (
                        f"Planner selected ineligible capability '{requested_tool_name}' "
                        f"(resolved to '{tool_name}'): {eligibility_error}"
                    ),
                },
            )
        tool = capability.to_tool_spec()

        task_id = str(uuid4())
        order_id = str(uuid4())

        command = _clean_optional(tool_args.get("command"))
        if tool.worker_type == "coder":
            command = _clean_optional(tool_args.get("instruction"))
        worker_type = tool.worker_type
        workdir = _clean_optional(tool_args.get("workdir"))
        verification_cmd = (
            _clean_optional(item.verification_cmd)
            or _clean_optional(tool_args.get("verification_cmd"))
            or _clean_optional(payload.get("verification_cmd"))
        )
        risk_level = _work_order_risk(tool.risk_level, command=command, verification_cmd=verification_cmd)

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
        if item.plan_step_id:
            task["plan_step_id"] = item.plan_step_id  # type: ignore[typeddict-unknown-key]
        order = WorkOrder(
            order_id=order_id,
            task_id=task_id,
            ca_thread_id=state["thread_id"],
            capability_name=capability.name,
            worker_type=worker_type,
            provider=tool.skill,
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
        if item.plan_step_id:
            work_plan = _mark_plan_step_running(
                state.get("work_plan"),
                step_id=item.plan_step_id,
                order_id=order_id,
                capability_name=capability.name,
            )
        else:
            work_plan = state.get("work_plan")
        dispatch_queue.append(order_dump)
        work_orders[order.order_id] = order_dump

    if not tasks:
        return _goto(
            "blocked",
            {
                "status": "failed",
                "last_error": "Strategize produced no work orders.",
                "planner_raw_output": planner_decision.raw_output,
            },
        )

    return _goto(
        "risk_gate",
        {
            "status": "strategizing",
            "task_list": previous_tasks + tasks,
            "dispatch_queue": dispatch_queue,
            "work_orders": work_orders,
            "work_plan": work_plan if "work_plan" in locals() else state.get("work_plan"),
            "candidate_tools": candidate_tools,
            "planner_raw_output": planner_decision.raw_output,
            "approved_order_ids": state.get("approved_order_ids", []),
            "active_workers": {},
            "worker_results": {},
        },
    )


def clarify(state: AgentState) -> Command:
    question = state.get("pending_clarification") or "I need more information before continuing."
    answer = interrupt(
        {
            "type": "clarification_required",
            "question": question,
        }
    )
    if isinstance(answer, dict):
        clarification = _clean_optional(answer.get("clarification") or answer.get("answer"))
        if clarification:
            event = dict(state.get("event", {}))
            payload = dict(_payload(state))
            instruction = str(payload.get("instruction") or "")
            payload["instruction"] = f"{instruction}\n\nUser clarification: {clarification}".strip()
            event["payload"] = payload
            return _goto(
                "contextualize",
                {
                    "event": event,
                    "status": "contextualizing",
                    "pending_clarification": None,
                    "last_error": None,
                    "messages": [{"role": "user", "content": clarification}],
                },
            )
    return _goto(
        "blocked",
        {
            "status": "blocked",
            "final_summary": question,
            "last_error": question,
            "pending_clarification": None,
        },
    )


def risk_gate(state: AgentState) -> Command:
    if not state.get("dispatch_queue"):
        return _goto("blocked", {"status": "failed", "last_error": "No work orders to dispatch."})

    active_workers = dict(state.get("active_workers", {}))
    work_orders = dict(state.get("work_orders", {}))
    approved_order_ids = set(state.get("approved_order_ids", []))
    task_list = [task.copy() for task in state["task_list"]]

    for order_dict in state.get("dispatch_queue", []):
        order = WorkOrder(**order_dict)
        work_orders[order.order_id] = order.model_dump()
        needs_approval = order.risk_level in {"high", "critical"} or (
            state.get("recovered_resume") and requires_recovery_approval(order)
        )
        if needs_approval and order.order_id not in approved_order_ids:
            for task in task_list:
                if task["id"] == order.task_id:
                    task["status"] = "waiting"
            return _goto(
                "wait_approval",
                {
                    "status": "waiting_approval",
                    "task_list": task_list,
                    "work_orders": work_orders,
                    "active_workers": active_workers,
                    "current_task_id": order.task_id,
                    "pending_action": _pending_action_from_order(order),
                    "pending_approval_id": str(uuid4()),
                },
            )

    return _goto(
        "dispatch",
        {
            "status": "dispatching",
            "task_list": task_list,
            "work_orders": work_orders,
            "active_workers": active_workers,
            "recovered_resume": False,
        },
    )


def dispatch(state: AgentState) -> Command:
    if not state.get("dispatch_queue"):
        return _goto("blocked", {"status": "failed", "last_error": "No work orders to dispatch."})

    client = get_worker_client()
    active_workers = dict(state.get("active_workers", {}))
    work_orders = dict(state.get("work_orders", {}))
    approved_order_ids = set(state.get("approved_order_ids", []))
    task_list = [task.copy() for task in state["task_list"]]

    for order_dict in state.get("dispatch_queue", []):
        order = WorkOrder(**order_dict)
        work_orders[order.order_id] = order.model_dump()

        client.dispatch(order)
        active_workers[order.task_id] = order.order_id
        for task in task_list:
            if task["id"] == order.task_id:
                task["status"] = "running"

    return _goto(
        "monitor",
        {
            "status": "dispatching",
            "task_list": task_list,
            "dispatch_queue": [],
            "work_orders": work_orders,
            "approved_order_ids": list(approved_order_ids),
            "active_workers": active_workers,
        },
    )


def monitor(state: AgentState) -> Command:
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
        recovered_resume = _apply_worker_resume_events(
            worker_event,
            active_workers=active_workers,
            worker_results=worker_results,
            work_orders=state.get("work_orders", {}),
        )
    else:
        recovered_resume = bool(state.get("recovered_resume"))

    return _goto(
        "aggregate" if not active_workers else "monitor",
        {
            "status": "monitoring",
            "active_workers": active_workers,
            "worker_results": worker_results,
            "recovered_resume": recovered_resume,
        },
    )


def aggregate(state: AgentState) -> Command:
    worker_results = state.get("worker_results", {})
    work_orders = dict(state.get("work_orders", {}))
    tasks: list[Task] = []
    failed = False
    dispatch_queue: list[dict[str, Any]] = []
    for task in state["task_list"]:
        updated = task.copy()
        if updated.get("status") in {"success", "failed", "blocked", "cancelled", "verifying"}:
            if updated.get("status") in {"failed", "blocked"}:
                failed = True
            tasks.append(updated)
            continue

        order_id = updated.get("order_id")
        result = WorkResult(**worker_results[order_id]) if order_id and order_id in worker_results else None
        if result is None:
            summary = "Worker result missing."
            if _can_retry(updated):
                retry = _retry_task(
                    updated,
                    work_orders,
                    ca_thread_id=state["thread_id"],
                    failure_summary=summary,
                )
                updated = retry["task"]
                dispatch_queue.append(retry["order"])
            else:
                updated["status"] = "blocked"
                updated["result_summary"] = summary
                failed = True
        elif not result.ok:
            summary = result.summary or result.stderr or "Worker failed."
            if _can_retry(updated):
                retry = _retry_task(
                    updated,
                    work_orders,
                    ca_thread_id=state["thread_id"],
                    failure_summary=summary,
                )
                updated = retry["task"]
                dispatch_queue.append(retry["order"])
            else:
                updated["status"] = "failed"
                updated["result_summary"] = summary
                failed = True
        else:
            updated["status"] = "verifying"
            updated["result_summary"] = result.summary
        tasks.append(updated)

    if dispatch_queue:
        return _goto(
            "risk_gate",
            {
                "status": "dispatching",
                "task_list": tasks,
                "dispatch_queue": dispatch_queue,
                "work_orders": work_orders,
                "work_plan": state.get("work_plan"),
                "active_workers": {},
            },
        )

    return _goto(
        "blocked" if failed else "verify",
        {
            "status": "failed" if failed else "verifying",
            "task_list": tasks,
            "work_orders": work_orders,
            "work_plan": state.get("work_plan"),
        },
    )


def verify(state: AgentState) -> Command:
    worker_results = state.get("worker_results", {})
    work_orders = dict(state.get("work_orders", {}))
    work_plan = state.get("work_plan")
    tasks: list[Task] = []
    failed = False
    needs_replan = False
    dispatch_queue: list[dict[str, Any]] = []
    for task in state["task_list"]:
        updated = task.copy()
        if updated.get("status") in {"success", "failed", "blocked", "cancelled"}:
            if updated.get("status") in {"failed", "blocked"}:
                failed = True
            tasks.append(updated)
            continue
        if updated.get("status") != "verifying":
            tasks.append(updated)
            continue

        order_id = updated.get("order_id")
        result = WorkResult(**worker_results[order_id]) if order_id and order_id in worker_results else None
        assessment = _assess_task_completion(updated, result)
        if assessment.decision == "success":
            updated["status"] = "success"
            updated["result_summary"] = assessment.summary
            work_plan = _mark_plan_step_finished(
                work_plan,
                step_id=updated.get("plan_step_id"),  # type: ignore[typeddict-item]
                status="success",
                result_summary=assessment.summary,
            )
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
            work_plan = _mark_plan_step_finished(
                work_plan,
                step_id=updated.get("plan_step_id"),  # type: ignore[typeddict-item]
                status=updated["status"],
                result_summary=assessment.summary,
            )
            failed = True
        tasks.append(updated)

    if dispatch_queue:
        return _goto(
            "risk_gate",
            {
                "status": "dispatching",
                "task_list": tasks,
                "dispatch_queue": dispatch_queue,
                "work_orders": work_orders,
                "work_plan": work_plan,
                "active_workers": {},
            },
        )

    if work_plan and _next_pending_plan_step(work_plan) and not failed:
        return _goto(
            "strategize",
            {
                "status": "strategizing",
                "task_list": tasks,
                "work_orders": work_orders,
                "work_plan": work_plan,
                "active_workers": {},
            },
        )

    if work_plan and not _next_pending_plan_step(work_plan) and not failed:
        work_plan = cast(WorkPlan, dict(work_plan))
        work_plan["status"] = "completed"

    if needs_replan and not failed:
        return _goto(
            "strategize",
            {
                "status": "strategizing",
                "task_list": tasks,
                "work_orders": work_orders,
                "work_plan": work_plan,
                "active_workers": {},
                "worker_results": {},
                "last_error": "Replanning after completion assessment.",
            },
        )

    return _goto(
        "blocked" if failed else "summarize",
        {
            "status": "failed" if failed else "running",
            "task_list": tasks,
            "work_plan": work_plan,
        },
    )


def wait_approval(state: AgentState) -> Command:
    approval = interrupt(
        {
            "type": "approval_required",
            "pending_approval_id": state.get("pending_approval_id"),
            "pending_action": state.get("pending_action"),
        }
    )

    if isinstance(approval, dict) and approval.get("approved"):
        tasks = _update_current_task(
            state, status="waiting", result_summary="Approved by user."
        )
        pending = state.get("pending_action")
        if pending:
            order_id = pending.get("order_id") or str(uuid4())
            work_orders = dict(state.get("work_orders", {}))
            order_dump = work_orders.get(order_id)
            if order_dump is None:
                order_dump = WorkOrder(
                    order_id=order_id,
                    task_id=state.get("current_task_id") or "",
                    ca_thread_id=state["thread_id"],
                    capability_name=pending.get("capability_name"),
                    worker_type=pending["kind"],
                    provider=pending.get("provider") or pending["skill"],
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
            dispatch_queue = list(state.get("dispatch_queue", []))
            if not any(item.get("order_id") == order_id for item in dispatch_queue):
                dispatch_queue.append(order_dump)
            return _goto(
                "risk_gate",
                {
                    "task_list": updated_tasks,
                    "dispatch_queue": dispatch_queue,
                    "work_orders": work_orders,
                    "approved_order_ids": list(approved_order_ids),
                    "pending_action": None,
                    "pending_approval_id": None,
                },
            )
        return _goto("summarize", {"task_list": tasks})
    else:
        tasks = _update_current_task(
            state, status="blocked", result_summary="Rejected by user."
        )
        return _goto(
            "blocked",
            {
                "task_list": tasks,
                "pending_action": None,
                "pending_approval_id": None,
            },
        )


def summarize(state: AgentState) -> dict[str, Any]:
    final_answer = _synthesize_final_answer(state)
    if final_answer:
        return {"status": "completed", "final_summary": final_answer}

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


def _title_from_instruction(instruction: str, command: str | None) -> str:
    if command:
        return f"Run command: {command[:80]}"
    return instruction[:80] or "Agent task"


def _title_from_tool_call(tool_name: str, tool_args: dict[str, Any], instruction: str) -> str:
    command = _clean_optional(tool_args.get("command"))
    if command:
        return f"Run command: {command[:80]}"
    if tool_name in {"coder.claude_code", "delegate_to_claude_code"}:
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
    return _planned_decision(state).tool_calls


def _planned_decision(state: AgentState) -> PlannerDecision:
    work_plan = state.get("work_plan")
    if work_plan:
        step = _next_pending_plan_step(work_plan)
        if step is None:
            return PlannerDecision()
        if step["capability_name"] == "__planner__":
            payload = _payload(state)
            tools = [
                capability.to_tool_spec()
                for capability in _candidate_capabilities_for_state(state, instruction_override=step["instruction"])
            ]
            if not tools:
                raise ValueError(f"No exposed tools are eligible for WorkPlan step {step['id']}.")
            decision = get_jarvis_llm().plan_decision(
                instruction=_planner_instruction(payload, step["instruction"]),
                tools=tools,
            )
            decision.tool_calls = _normalize_plan_step_tool_calls(decision.tool_calls, step_id=step["id"])
            return decision
        return PlannerDecision(tool_calls=[_tool_call_from_plan_step(state, step)])

    settings = get_settings()
    if settings.planner_type != "llm":
        return PlannerDecision(tool_calls=_rule_based_tool_calls(state))

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    replan_context = _replan_context(state)
    if replan_context:
        instruction = f"{instruction}\n\n{replan_context}"
    tools = [capability.to_tool_spec() for capability in _candidate_capabilities_for_state(state)]
    if not tools:
        raise ValueError("No exposed tools are eligible for the current request.")
    return get_jarvis_llm().plan_decision(
        instruction=_planner_instruction(payload, instruction),
        tools=tools,
    )


def _candidate_capabilities_for_state(
    state: AgentState,
    *,
    instruction_override: str | None = None,
) -> list[WorkerCapability]:
    payload = _payload(state)
    instruction = instruction_override if instruction_override is not None else str(payload.get("instruction") or "")
    command = _clean_optional(payload.get("command"))
    workdir = _clean_optional(payload.get("workdir"))
    capabilities = get_default_capability_registry().list(exposed_to_llm=True)
    by_name = {capability.name: capability for capability in capabilities}
    selected: set[str] = set()

    def add(name: str) -> None:
        if name in by_name:
            selected.add(name)

    add("answer.echo")
    if _looks_like_search_request(instruction):
        add("search.tavily")
    if command:
        add("shell.command")
        if _is_allowed_test_command(command):
            add("shell.test")
    if _looks_like_test_request(instruction):
        add("shell.test")
    if workdir and (
        _looks_like_code_write(instruction)
        or _looks_like_code_review(instruction)
        or _requires_multiple_work_orders(instruction)
    ):
        add("coder.claude_code")

    return [capability for capability in capabilities if capability.name in selected]


def _normalize_plan_step_tool_calls(tool_calls: list[ToolCallPlan], *, step_id: str) -> list[ToolCallPlan]:
    if not tool_calls:
        return []
    first = tool_calls[0].model_copy()
    first.plan_step_id = step_id
    return [first]


def _capability_eligibility_error(
    capability: WorkerCapability,
    state: AgentState,
    tool_args: dict[str, Any],
) -> str | None:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    caller_command = _clean_optional(payload.get("command"))
    requested_command = _clean_optional(tool_args.get("command"))
    workdir = _clean_optional(tool_args.get("workdir")) or _clean_optional(payload.get("workdir"))

    if capability.requires_workdir and not workdir:
        return "capability requires a workdir."
    if capability.can_modify_files and not workdir:
        return "file-modifying capability requires a workdir."
    if capability.name == "shell.command":
        if not caller_command:
            return "shell.command requires an explicit caller command."
        if requested_command and requested_command != caller_command:
            return "shell.command must execute the exact explicit caller command."
    if capability.name == "answer.echo" and _requires_external_capability(instruction, workdir=workdir):
        return "answer.echo cannot satisfy a request that requires an external capability."
    if capability.name == "shell.test":
        if requested_command and not _is_allowed_test_command(requested_command):
            return "shell.test can only run configured low-risk test commands."
        if not caller_command and not _looks_like_test_request(instruction):
            return "shell.test requires a test request or explicit caller command."
    if capability.requires_explicit_user_command and capability.name not in {"shell.test"}:
        if not caller_command:
            return f"{capability.name} requires an explicit caller command."
    return None


def _rule_based_tool_calls(state: AgentState) -> list[ToolCallPlan]:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "Run local agent task.")
    command = _clean_optional(payload.get("command"))
    intent: IntentDecision | dict[str, Any] = state.get("intent") or {}
    if intent.get("kind") == "code_write":
        workdir = _clean_optional(payload.get("workdir"))
        tool_name = _default_tool_name_for_intent("code_write") or "delegate_to_claude_code"
        return [
            ToolCallPlan(
                tool_name=tool_name,
                tool_args={"instruction": instruction, "workdir": workdir},
                title=_title_from_instruction(instruction, None),
                description=instruction,
                dod=str(payload.get("dod") or "Code change completed successfully."),
                verification_cmd=_clean_optional(payload.get("verification_cmd")),
                max_retries=int(payload.get("max_retries") or 0),
            )
        ]
    if intent.get("kind") == "search_summary":
        tool_name = _default_tool_name_for_intent("search_summary") or "tavily_search"
        return [
            ToolCallPlan(
                tool_name=tool_name,
                tool_args={"query": instruction, "max_results": 5, "include_answer": True},
                title=_title_from_instruction(instruction, None),
                description=instruction,
                dod=str(payload.get("dod") or "Search summary returned with sources."),
                max_retries=int(payload.get("max_retries") or 0),
            )
        ]
    if command:
        tool_name = _default_tool_name_for_intent("explicit_shell") or "run_shell_command"
        return [
            ToolCallPlan(
                tool_name=tool_name,
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
            tool_name=_default_tool_name_for_intent("simple_chat") or "echo",
            tool_args={"text": instruction},
            title=instruction[:80] or "Agent task",
            description=instruction,
            dod=str(payload.get("dod") or "Task execution completed successfully."),
            verification_cmd=_clean_optional(payload.get("verification_cmd")),
            max_retries=int(payload.get("max_retries") or 0),
        )
    ]


def _classify_intent(state: AgentState) -> IntentDecision:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    command = _clean_optional(payload.get("command"))
    workdir = _clean_optional(payload.get("workdir"))

    if command:
        return _intent_decision(
            "explicit_shell",
            confidence=1.0,
            reason="User supplied an explicit shell command.",
            allowed_tools=_allowed_tools_for_intent("explicit_shell"),
            requires_workdir=False,
        )

    if workdir and _looks_like_code_write(instruction):
        return _intent_decision(
            "code_write",
            confidence=0.95,
            reason="Instruction asks to create or modify code/files inside a repository.",
            allowed_tools=_allowed_tools_for_intent("code_write"),
            requires_workdir=True,
        )

    if _looks_like_search_summary(instruction):
        return _intent_decision(
            "search_summary",
            confidence=0.9,
            reason="Instruction asks to search or summarize external information.",
            allowed_tools=_allowed_tools_for_intent("search_summary"),
            requires_workdir=False,
        )

    return _intent_decision(
        "simple_chat",
        confidence=0.75,
        reason="No repository edit, explicit shell command, or search request detected.",
        allowed_tools=_allowed_tools_for_intent("simple_chat"),
        requires_workdir=False,
    )


def _build_work_plan(state: AgentState, decision: IntentDecision) -> WorkPlan | None:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    steps = _extract_numbered_steps(instruction)
    if len(steps) < 2 or not _requires_multiple_work_orders(instruction):
        return None

    capability_name = "__planner__" if get_settings().planner_type == "llm" else (
        _default_tool_name_for_intent(decision["kind"])
        or (decision["allowed_tools"][0] if decision["allowed_tools"] else "answer.echo")
    )
    plan_steps: list[PlanStep] = []
    for index, step_text in enumerate(steps, start=1):
        plan_steps.append(
            {
                "id": f"step-{index}",
                "title": step_text[:120],
                "instruction": _plan_step_instruction(
                    goal=instruction,
                    step_text=step_text,
                    index=index,
                    total=len(steps),
                ),
                "capability_name": capability_name,
                "status": "pending",
                "order_id": None,
                "result_summary": None,
            }
        )
    return {
        "id": str(uuid4()),
        "goal": instruction,
        "status": "planned",
        "requires_multiple_work_orders": True,
        "steps": plan_steps,
    }


def _extract_numbered_steps(instruction: str) -> list[str]:
    steps: list[str] = []
    for line in instruction.splitlines():
        match = re.match(r"^\s*\d+[\.\)、)]\s*(.+?)\s*$", line)
        if match:
            steps.append(match.group(1).strip())
    return steps


def _requires_multiple_work_orders(instruction: str) -> bool:
    text = instruction.lower()
    markers = [
        "多个 work order",
        "多 work order",
        "work orders",
        "分多个",
        "分多步",
        "拆成多个",
        "先",
        "再",
        "最后",
    ]
    return any(marker in text for marker in markers)


def _plan_step_instruction(*, goal: str, step_text: str, index: int, total: int) -> str:
    return "\n".join(
        [
            f"This is Jarvis WorkPlan step {index} of {total}.",
            "Execute only this step. Do not perform later steps unless explicitly included in this step.",
            "",
            "Overall user goal and constraints:",
            goal,
            "",
            "Current step:",
            step_text,
        ]
    )


def _next_pending_plan_step(work_plan: WorkPlan | None) -> PlanStep | None:
    if not work_plan:
        return None
    for step in work_plan.get("steps", []):
        if step.get("status") == "pending":
            return step
    return None


def _tool_call_from_plan_step(state: AgentState, step: PlanStep) -> ToolCallPlan:
    payload = _payload(state)
    workdir = _clean_optional(payload.get("workdir"))
    tool_args: dict[str, Any]
    if step["capability_name"] == "coder.claude_code":
        tool_args = {"instruction": step["instruction"], "workdir": workdir}
    elif step["capability_name"] == "search.tavily":
        tool_args = {"query": step["instruction"], "max_results": 5, "include_answer": True}
    elif step["capability_name"] in {"shell.command", "shell.test"}:
        tool_args = {"command": _clean_optional(payload.get("command")) or step["instruction"]}
    else:
        tool_args = {"text": step["instruction"]}
    return ToolCallPlan(
        tool_name=step["capability_name"],
        tool_args=tool_args,
        title=step["title"],
        description=step["instruction"],
        dod=f"WorkPlan step {step['id']} completed successfully.",
        verification_cmd=_clean_optional(payload.get("verification_cmd")),
        max_retries=int(payload.get("max_retries") or 0),
        plan_step_id=step["id"],
    )


def _mark_plan_step_running(
    work_plan: WorkPlan | None,
    *,
    step_id: str,
    order_id: str,
    capability_name: str | None = None,
) -> WorkPlan | None:
    if not work_plan:
        return None
    updated = dict(work_plan)
    steps: list[PlanStep] = []
    for step in work_plan["steps"]:
        item = dict(step)
        if item["id"] == step_id:
            item["status"] = "running"
            item["order_id"] = order_id
            if capability_name:
                item["capability_name"] = capability_name
        steps.append(item)  # type: ignore[arg-type]
    updated["steps"] = steps
    updated["status"] = "running"
    return updated  # type: ignore[return-value]


def _mark_plan_step_finished(
    work_plan: WorkPlan | None,
    *,
    step_id: object,
    status: str,
    result_summary: str,
) -> WorkPlan | None:
    if not work_plan or not isinstance(step_id, str):
        return work_plan
    updated = dict(work_plan)
    steps: list[PlanStep] = []
    for step in work_plan["steps"]:
        item = dict(step)
        if item["id"] == step_id:
            item["status"] = status
            item["result_summary"] = result_summary
        steps.append(item)  # type: ignore[arg-type]
    updated["steps"] = steps
    if status in {"failed", "blocked"}:
        updated["status"] = "blocked"
    return updated  # type: ignore[return-value]


def _intent_decision(
    kind: str,
    *,
    confidence: float,
    reason: str,
    allowed_tools: list[str],
    requires_workdir: bool,
) -> IntentDecision:
    return {
        "kind": kind,  # type: ignore[typeddict-item]
        "confidence": confidence,
        "confidence_source": "rule",
        "reason": reason,
        "allowed_tools": allowed_tools,
        "requires_workdir": requires_workdir,
        "plan_steps": [
            {
                "kind": kind,
                "allowed_tools": allowed_tools,
                "reason": reason,
            }
        ],
    }


def _allowed_tools_for_intent(intent_kind: IntentKind) -> list[str]:
    return get_default_capability_registry().names_for_intent(intent_kind)


def _default_tool_name_for_intent(intent_kind: IntentKind) -> str | None:
    return get_default_capability_registry().default_name_for_intent(intent_kind)


def _looks_like_code_write(instruction: str) -> bool:
    text = instruction.lower()
    action_terms = [
        "写",
        "加",
        "增加",
        "新增",
        "修改",
        "实现",
        "修复",
        "生成",
        "创建",
        "add",
        "create",
        "modify",
        "implement",
        "fix",
    ]
    object_terms = [
        "脚本",
        "代码",
        "文件",
        "功能",
        "feature",
        "commit",
        "push",
        "script",
        "file",
        "code",
    ]
    return any(term in text for term in action_terms) and any(term in text for term in object_terms)


def _looks_like_search_summary(instruction: str) -> bool:
    return _looks_like_search_request(instruction) and not _looks_like_code_context(instruction)


def _looks_like_search_request(instruction: str) -> bool:
    text = instruction.lower()
    search_terms = [
        "搜索",
        "查一下",
        "查找",
        "调研",
        "带来源",
        "引用",
        "search",
        "research",
        "sources",
        "citations",
        "latest",
    ]
    return any(term in text for term in search_terms)


def _requires_external_capability(instruction: str, *, workdir: str | None) -> bool:
    if _looks_like_search_request(instruction) or _looks_like_test_request(instruction):
        return True
    return bool(
        workdir
        and (
            _looks_like_code_write(instruction)
            or _looks_like_code_review(instruction)
            or _requires_multiple_work_orders(instruction)
        )
    )


def _looks_like_code_context(instruction: str) -> bool:
    text = instruction.lower()
    code_context_terms = ["test 失败", "报错", "bug", "代码", "workdir"]
    return any(term in text for term in code_context_terms)


def _looks_like_code_review(instruction: str) -> bool:
    text = instruction.lower()
    review_terms = [
        "审查",
        "检查",
        "review",
        "diff",
        "git status",
        "设计债务",
        "仓库",
        "repo",
        "repository",
    ]
    return any(term in text for term in review_terms)


def _looks_like_test_request(instruction: str) -> bool:
    text = instruction.lower()
    test_terms = [
        "跑测试",
        "运行测试",
        "执行测试",
        "pytest",
        "run tests",
        "run test",
        "test suite",
        "测试",
    ]
    return any(term in text for term in test_terms)


def _is_allowed_test_command(command: str) -> bool:
    return command.strip() in {"uv run pytest", "pytest"}


def _apply_worker_resume_events(
    resume_value: Any,
    *,
    active_workers: dict[str, str],
    worker_results: dict[str, dict[str, Any]],
    work_orders: dict[str, dict[str, Any]],
) -> bool:
    recovered = False
    for worker_event in _worker_resume_events(resume_value):
        recovered = recovered or bool(worker_event.get("recovered"))
        payload = worker_event.get("payload", {})
        order_id = payload.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            continue
        normalized = _normalize_worker_event_payload(
            payload,
            order_id=order_id,
            active_workers=active_workers,
            work_orders=work_orders,
            failed=worker_event.get("event_type") == "worker_failed",
        )
        worker_results[order_id] = normalized
        for task_id, active_order_id in list(active_workers.items()):
            if active_order_id == order_id:
                active_workers.pop(task_id, None)
    return recovered


def _worker_resume_events(resume_value: Any) -> list[dict[str, Any]]:
    if not isinstance(resume_value, dict):
        return []
    raw_events = resume_value.get("events") or resume_value.get("worker_events")
    if isinstance(raw_events, list):
        return [
            event
            for event in raw_events
            if isinstance(event, dict)
            and event.get("event_type") in {"worker_complete", "worker_failed"}
            and isinstance(event.get("payload"), dict)
        ]
    if resume_value.get("event_type") in {"worker_complete", "worker_failed"} and isinstance(
        resume_value.get("payload"),
        dict,
    ):
        return [resume_value]
    return []


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
