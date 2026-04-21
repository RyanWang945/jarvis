from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from langgraph.types import interrupt

from app.agent.state import AgentState, IntentDecision, PendingAction, RiskLevel, Task
from app.config import get_settings
from app.llm.jarvis import get_jarvis_llm
from app.tools import get_default_capability_registry, get_default_tool_registry
from app.tools.specs import IntentKind, ToolCallPlan
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


def classify_intent(state: AgentState) -> dict[str, Any]:
    decision = _classify_intent(state)
    return {
        "status": "planning",
        "intent": decision,
        "allowed_tools": decision["allowed_tools"],
        "plan_steps": decision["plan_steps"],
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
    capability_registry = get_default_capability_registry()
    allowed_tool_set = set(state.get("allowed_tools", []))
    previous_tasks = [task.copy() for task in state.get("task_list", [])]
    tasks: list[Task] = []
    dispatch_queue: list[dict[str, Any]] = []
    work_orders: dict[str, dict[str, Any]] = dict(state.get("work_orders", {}))

    for item in planned_calls:
        requested_tool_name = item.tool_name or "answer.echo"
        try:
            capability = capability_registry.get(requested_tool_name)
        except ValueError:
            return {
                "status": "failed",
                "last_error": f"Planner selected unknown capability '{requested_tool_name}'.",
                "next_node": "blocked",
            }
        tool_name = capability.name
        if allowed_tool_set and tool_name not in allowed_tool_set:
            return {
                "status": "failed",
                "last_error": (
                    f"Planner selected disallowed capability '{requested_tool_name}' "
                    f"(resolved to '{tool_name}'). Allowed capabilities for this intent: "
                    f"{sorted(allowed_tool_set)}."
                ),
                "next_node": "blocked",
            }
        tool = capability.to_tool_spec()

        task_id = str(uuid4())
        order_id = str(uuid4())
        tool_args = item.tool_args if isinstance(item.tool_args, dict) else {}
        if payload.get("workdir") and "workdir" not in tool_args:
            tool_args["workdir"] = payload["workdir"]

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
                "work_orders": work_orders,
                "active_workers": active_workers,
                "current_task_id": order.task_id,
                "pending_action": _pending_action_from_order(order),
                "pending_approval_id": str(uuid4()),
                "next_node": "wait_approval",
            }

    for order_dict in state.get("dispatch_queue", []):
        order = WorkOrder(**order_dict)

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
            return {
                "task_list": updated_tasks,
                "dispatch_queue": dispatch_queue,
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
    if tool_name in {"shell.command", "shell.test", "answer.echo", "run_shell_command", "run_tests", "echo"}:
        return True
    if worker_type in {"shell", "echo"} and any(
        marker in dod for marker in ("completed", "success", "passed", "exited")
    ):
        return True
    if worker_type and worker_type != "coder":
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


def _synthesize_final_answer(state: AgentState) -> str | None:
    settings = get_settings()
    if settings.planner_type != "llm":
        return None

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    if not instruction:
        return None

    worker_results = _final_answer_worker_results(state)
    if not worker_results:
        return None

    try:
        answer = get_jarvis_llm().synthesize_final_answer(
            instruction=instruction,
            tasks=_final_answer_tasks(state),
            worker_results=worker_results,
        )
    except Exception:
        return _fallback_final_answer(instruction=instruction, worker_results=worker_results)
    return answer or None


def _final_answer_tasks(state: AgentState) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for task in state.get("task_list", []):
        tasks.append(
            {
                "title": task.get("title"),
                "description": task.get("description"),
                "status": task.get("status"),
                "dod": task.get("dod"),
                "tool_name": task.get("tool_name"),
                "worker_type": task.get("worker_type"),
                "tool_args": task.get("tool_args"),
                "result_summary": task.get("result_summary"),
                "order_id": task.get("order_id"),
            }
        )
    return tasks


def _final_answer_worker_results(state: AgentState) -> list[dict[str, Any]]:
    worker_results = state.get("worker_results", {})
    results: list[dict[str, Any]] = []
    for task in state.get("task_list", []):
        order_id = task.get("order_id")
        if not order_id or order_id not in worker_results:
            continue
        result = WorkResult(**worker_results[order_id])
        results.append(
            {
                "order_id": result.order_id,
                "task_id": result.task_id,
                "worker_type": result.worker_type,
                "ok": result.ok,
                "summary": result.summary,
                "stdout": _compact_stdout_for_final_answer(result.stdout),
                "stderr": _truncate_for_final_answer(result.stderr, limit=2000),
                "artifacts": result.artifacts,
            }
        )
    return results


def _compact_stdout_for_final_answer(stdout: str) -> str:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return _truncate_for_final_answer(stdout)
    if not isinstance(parsed, dict):
        return _truncate_for_final_answer(stdout)

    results = parsed.get("results")
    if not isinstance(results, list):
        return _truncate_for_final_answer(stdout)

    compact: dict[str, Any] = {
        "query": parsed.get("query"),
        "answer": parsed.get("answer"),
        "results": [],
    }
    for item in results[:5]:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") or item.get("content") or ""
        compact["results"].append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": _truncate_for_final_answer(str(snippet), limit=700),
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def _fallback_final_answer(*, instruction: str, worker_results: list[dict[str, Any]]) -> str | None:
    for result in worker_results:
        answer = _fallback_search_answer(instruction=instruction, stdout=str(result.get("stdout") or ""))
        if answer:
            return answer
    return None


def _fallback_search_answer(*, instruction: str, stdout: str) -> str | None:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return _fallback_text_answer(instruction=instruction, stdout=stdout)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return None

    lines = [f"根据搜索结果，{instruction}："]
    answer = parsed.get("answer")
    if answer:
        lines.extend(["", str(answer).strip()])
    else:
        summary = _summary_from_search_items(parsed["results"])
        if summary:
            lines.extend(["", "摘要：", summary])

    lines.extend(["", "来源："])
    for index, item in enumerate(parsed["results"][:5], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {_truncate_for_final_answer(snippet, limit=280)}")
    return "\n".join(lines).strip()


def _fallback_text_answer(*, instruction: str, stdout: str) -> str | None:
    items = _parse_text_search_items(stdout)
    urls = [item["url"] for item in items if item.get("url")]
    if not urls:
        return None

    lines = [f"根据搜索结果，{instruction}："]
    summary = _summary_from_search_items(items)
    if summary:
        lines.extend(["", "摘要：", summary])

    lines.extend(["", "来源："])
    for index, item in enumerate(items[:5], start=1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or ""
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines).strip()


def _summary_from_search_items(items: list[Any]) -> str | None:
    snippets: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        if not snippet:
            continue
        snippets.append(_truncate_for_final_answer(_clean_search_snippet(snippet), limit=260))
        if len(snippets) >= 3:
            break
    if not snippets:
        return None
    return "\n".join(f"- {snippet}" for snippet in snippets)


def _clean_search_snippet(snippet: str) -> str:
    cleaned = re.sub(r"\s+", " ", snippet).strip()
    return cleaned


def _parse_text_search_items(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    has_url = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        title_match = re.match(r"^\d+\.\s+(.+)$", line)
        if title_match:
            if current:
                items.append(current)
            current = {"title": title_match.group(1).strip(), "url": "", "snippet": ""}
            has_url = False
            continue
        if stripped.startswith(("http://", "https://")):
            if current is None:
                current = {"title": "", "url": "", "snippet": ""}
            current["url"] = stripped
            has_url = True
            continue
        if current is not None and (stripped.startswith("- ") or has_url):
            if current is None:
                current = {"title": "", "url": "", "snippet": ""}
            snippet = stripped[2:].strip() if stripped.startswith("- ") else stripped
            current["snippet"] = f"{current.get('snippet', '')} {snippet}".strip()

    if current:
        items.append(current)

    if items:
        return items
    return [{"title": "", "url": url, "snippet": ""} for url in _extract_urls_from_text(text)]


def _extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)>\"]+", text):
        url = match.group(0).rstrip(".,;]")
        if url not in urls:
            urls.append(url)
    return urls
def _truncate_for_final_answer(value: str, *, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


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


def _classify_risk(command: str | None) -> RiskLevel:
    if not command:
        return "low"
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return "high"
    return "low"


def _work_order_risk(base_risk: RiskLevel, *, command: str | None, verification_cmd: str | None) -> RiskLevel:
    risk = _highest_risk(base_risk, _classify_risk(command))
    return _highest_risk(risk, _classify_risk(verification_cmd))


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
    settings = get_settings()
    if settings.planner_type != "llm":
        return _rule_based_tool_calls(state)

    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    replan_context = _replan_context(state)
    if replan_context:
        instruction = f"{instruction}\n\n{replan_context}"
    allowed_tools = state.get("allowed_tools", [])
    intent_kinds = [state["intent"]["kind"]] if state.get("intent") else None
    capability_registry = get_default_capability_registry()
    capabilities = capability_registry.list(
        exposed_to_llm=True,
        intent_kinds=intent_kinds,
    )
    if allowed_tools:
        tools = [
            capability.to_tool_spec()
            for capability in capabilities
            if capability.name in set(allowed_tools)
        ]
    else:
        tools = [capability.to_tool_spec() for capability in capabilities]
    if not tools:
        raise ValueError(
            f"No exposed tools are eligible for intent {intent_kinds or ['unknown']} "
            f"and allowed tools {allowed_tools or ['<none>']}."
        )
    return get_jarvis_llm().plan_tasks(
        instruction=_planner_instruction(payload, instruction),
        tools=tools,
    )


def _rule_based_tool_calls(state: AgentState) -> list[ToolCallPlan]:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "Run local agent task.")
    command = _clean_optional(payload.get("command"))
    intent = state.get("intent") or {}
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
        "feature",
        "commit",
        "push",
        "script",
        "file",
        "code",
    ]
    return any(term in text for term in action_terms) and any(term in text for term in object_terms)


def _looks_like_search_summary(instruction: str) -> bool:
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
    code_context_terms = ["test 失败", "报错", "bug", "代码", "workdir"]
    return any(term in text for term in search_terms) and not any(term in text for term in code_context_terms)


def _pending_action_from_order(order: WorkOrder) -> PendingAction:
    command = _approval_command_summary(order)
    return {
        "action_id": str(uuid4()),
        "capability_name": order.capability_name,
        "kind": order.worker_type,
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


def _approval_command_summary(order: WorkOrder) -> str | None:
    commands: list[str] = []
    command = _clean_optional(order.args.get("command"))
    if order.worker_type == "coder":
        command = _clean_optional(order.args.get("instruction"))
    if command:
        commands.append(command)
    verification_cmd = _clean_optional(order.verification_cmd)
    if verification_cmd:
        commands.append(f"verification: {verification_cmd}")
    return "\n".join(commands) if commands else None


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
