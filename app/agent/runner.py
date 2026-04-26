import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.agent.events import AgentEvent
from app.agent.graph import build_agent_graph
from app.agent.interrupts import parse_interrupt_result
from app.agent.reports import write_run_report
from app.agent.state import AgentState, initial_state
from app.persistence import get_business_db, BusinessDB
from app.workers import WorkOrder, WorkResult


@dataclass(frozen=True)
class AgentRunResult:
    thread_id: str
    status: str
    summary: str | None
    tasks: list[dict[str, Any]]
    pending_approval_id: str | None
    diagnostics: dict[str, Any] | None = None


class ThreadManager:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self._checkpoint_path = data_dir / "langgraph_checkpoints.sqlite"
        self._conn = sqlite3.connect(str(self._checkpoint_path), check_same_thread=False)
        self._checkpointer = SqliteSaver(self._conn)
        self._checkpointer.setup()
        self._graph = build_agent_graph(checkpointer=self._checkpointer)
        self._business_db = get_business_db(data_dir / "business.db")
        self._lock = RLock()

    @property
    def db(self) -> BusinessDB:
        return self._business_db

    def run_event(self, event: AgentEvent) -> AgentRunResult:
        with self._lock:
            thread_id = event.thread_id or str(uuid4())
            state = initial_state(event, thread_id)
            config = {"configurable": {"thread_id": thread_id}}

            # Record run start
            instruction = event.payload.get("instruction") if isinstance(event.payload, dict) else None
            self._business_db.runs.save({
                "run_id": str(uuid4()),
                "thread_id": thread_id,
                "status": "created",
                "instruction": instruction,
            })
            resource_key = _resource_key_from_event(event)
            if resource_key and not self._business_db.resource_locks.acquire(
                resource_key=resource_key,
                owner_thread_id=thread_id,
            ):
                owner = self._business_db.resource_locks.get(resource_key)
                summary = (
                    f"Resource is locked by thread {owner['owner_thread_id']}."
                    if owner
                    else "Resource is locked by another thread."
                )
                self._business_db.runs.save({
                    "run_id": str(uuid4()),
                    "thread_id": thread_id,
                    "status": "blocked",
                    "instruction": instruction,
                    "summary": summary,
                })
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="resource_lock",
                    action="resource_lock_conflict",
                    detail=f"resource_key={resource_key}",
                )
                return AgentRunResult(
                    thread_id=thread_id,
                    status="blocked",
                    summary=summary,
                    tasks=[],
                    pending_approval_id=None,
                )
            if resource_key:
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="resource_lock",
                    action="resource_lock_acquired",
                    detail=f"resource_key={resource_key}",
                )

            try:
                result: AgentState = self._graph.invoke(state, config=config)
            except Exception:
                if resource_key:
                    self._business_db.resource_locks.release_by_thread(thread_id)
                    self._business_db.audits.log(
                        thread_id=thread_id,
                        node="resource_lock",
                        action="resource_lock_released_after_error",
                        detail=f"resource_key={resource_key}",
                    )
                raise
            parsed = self._parse_result(result)

            # Persist business state
            self._persist_run_state(result, parsed, instruction)

            return self._with_diagnostics(parsed)

    def resume(self, thread_id: str, resume_value: Any) -> AgentRunResult:
        with self._lock:
            config = {"configurable": {"thread_id": thread_id}}
            result: AgentState = self._graph.invoke(
                Command(resume=resume_value),
                config=config,
            )
            parsed = self._parse_result(result)

            self._persist_approval_decision(thread_id, resume_value)
            # Persist business state after resume. Approval decisions are recorded first
            # so completed runs do not leave stale waiting approvals in the business DB.
            self._persist_run_state(result, parsed, None)

            return self._with_diagnostics(parsed)

    def inspect_run(self, thread_id: str) -> dict[str, Any] | None:
        run = self._business_db.runs.get_by_thread(thread_id)
        if not run:
            return None
        return {
            "run": run,
            "tasks": self._business_db.tasks.get_by_run(run["run_id"]),
            "work_orders": self._business_db.work_orders.get_by_thread(thread_id),
            "work_results": self._business_db.work_results.get_by_thread(thread_id),
            "approvals": self._business_db.approvals.get_by_thread(thread_id),
            "audit_logs": self._business_db.audits.get_by_thread(thread_id),
            "resource_locks": self._business_db.resource_locks.get_by_thread(thread_id),
        }

    def export_run_report(self, thread_id: str) -> dict[str, str]:
        inspection = self.inspect_run(thread_id)
        if not inspection:
            raise ValueError(f"Run not found: {thread_id}")
        paths = write_run_report(self._data_dir, inspection)
        self._business_db.audits.log(
            thread_id=thread_id,
            node="runner",
            action="report_exported",
            detail=json.dumps(paths),
        )
        return paths

    def recover_unfinished(self) -> dict[str, Any]:
        recovered: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for run in self._business_db.runs.list_unfinished():
            thread_id = run["thread_id"]
            if run["status"] == "waiting_approval":
                skipped.append(
                    {
                        "thread_id": thread_id,
                        "reason": "waiting_approval_requires_user_decision",
                    }
                )
                continue

            recoverable_results = self._recoverable_worker_results(thread_id)
            if not recoverable_results:
                skipped.append(
                    {
                        "thread_id": thread_id,
                        "reason": "no_completed_worker_result_to_replay",
                    }
                )
                continue

            for worker_result in recoverable_results:
                event_type = "worker_complete" if worker_result["ok"] else "worker_failed"
                try:
                    result = self.resume(
                        thread_id,
                        {"event_type": event_type, "payload": worker_result, "recovered": True},
                    )
                except Exception as exc:  # pragma: no cover - defensive audit path
                    failed.append(
                        {
                            "thread_id": thread_id,
                            "order_id": worker_result["order_id"],
                            "error": str(exc),
                        }
                    )
                    self._business_db.audits.log(
                        thread_id=thread_id,
                        node="recovery",
                        action="worker_result_replay_failed",
                        task_id=worker_result.get("task_id"),
                        order_id=worker_result["order_id"],
                        detail=str(exc),
                    )
                    continue
                recovered.append(
                    {
                        "thread_id": thread_id,
                        "order_id": worker_result["order_id"],
                        "status": result.status,
                    }
                )
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="recovery",
                    action="worker_result_replayed",
                    task_id=worker_result.get("task_id"),
                    order_id=worker_result["order_id"],
                    detail=f"event_type={event_type}",
                )

        return {"recovered": recovered, "skipped": skipped, "failed": failed}

    def _recoverable_worker_results(self, thread_id: str) -> list[dict[str, Any]]:
        recoverable: list[dict[str, Any]] = []
        for order in self._business_db.work_orders.list_incomplete(thread_id):
            if order["status"] != "dispatched":
                continue
            result = self._business_db.work_results.get_by_order(order["order_id"])
            if result:
                recoverable.append(_worker_result_payload(result))
        return recoverable

    def _persist_run_state(self, result: AgentState, parsed: AgentRunResult, instruction: str | None) -> None:
        thread_id = parsed.thread_id
        run_id = str(uuid4())

        # Upsert run
        existing = self._business_db.runs.get_by_thread(thread_id)
        if existing:
            run_id = existing["run_id"]
        self._business_db.runs.save({
            "run_id": run_id,
            "thread_id": thread_id,
            "status": parsed.status,
            "instruction": instruction,
            "summary": parsed.summary,
        })

        # Persist tasks
        for task in result.get("task_list", []):
            self._business_db.tasks.save(dict(task), run_id)

        intent = result.get("intent")
        if intent:
            self._business_db.audits.log(
                thread_id=thread_id,
                node="classify_intent",
                action="intent_classified",
                detail=json.dumps(intent, ensure_ascii=False),
            )
        candidate_tools = result.get("candidate_tools")
        if candidate_tools:
            self._business_db.audits.log(
                thread_id=thread_id,
                node="strategize",
                action="candidate_tools_selected",
                detail=json.dumps(candidate_tools, ensure_ascii=False),
            )
        planner_raw_output = result.get("planner_raw_output")
        if planner_raw_output:
            self._business_db.audits.log(
                thread_id=thread_id,
                node="strategize",
                action="planner_raw_output",
                detail=json.dumps(planner_raw_output, ensure_ascii=False, default=str),
            )
        work_plan = result.get("work_plan")
        if work_plan:
            self._business_db.audits.log(
                thread_id=thread_id,
                node="strategize",
                action="work_plan_snapshot",
                detail=json.dumps(work_plan, ensure_ascii=False, default=str),
            )

        # Persist work orders and their latest lifecycle status.
        active_order_ids = set(result.get("active_workers", {}).values())
        completed_order_ids = set(result.get("worker_results", {}).keys())
        for order_dict in result.get("work_orders", {}).values():
            order = WorkOrder(**order_dict)
            self._business_db.work_orders.save(order)
            if order.order_id in active_order_ids:
                self._business_db.work_orders.mark_dispatched(order.order_id)
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="dispatch",
                    action="worker_dispatched",
                    task_id=order.task_id,
                    order_id=order.order_id,
                    detail=f"worker_type={order.worker_type}",
                )
            if order.order_id in completed_order_ids:
                self._business_db.work_orders.mark_completed(order.order_id)
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="monitor",
                    action="worker_completed",
                    task_id=order.task_id,
                    order_id=order.order_id,
                    detail=f"worker_type={order.worker_type}",
                )

        # Persist worker_results -> work_results table
        worker_results = result.get("worker_results", {})
        for order_id, wr_dict in worker_results.items():
            wr = WorkResult(**wr_dict)
            self._business_db.work_results.save(wr)
            self._business_db.audits.log(
                thread_id=thread_id,
                node="skill",
                action="skill_call_recorded",
                task_id=wr.task_id,
                order_id=order_id,
                detail=f"worker_type={wr.worker_type} ok={wr.ok}",
            )
            self._business_db.audits.log(
                thread_id=thread_id,
                node="runner",
                action="worker_result_persisted",
                task_id=wr.task_id,
                order_id=order_id,
                detail=f"ok={wr.ok}",
            )

        # Persist approvals
        pending_action = result.get("pending_action")
        pending_approval_id = result.get("pending_approval_id")
        if pending_action and pending_approval_id:
            self._business_db.approvals.create({
                "approval_id": pending_approval_id,
                "thread_id": thread_id,
                "task_id": result.get("current_task_id", ""),
                "order_id": pending_action.get("order_id"),
                "action_kind": pending_action.get("kind"),
                "command": pending_action.get("command"),
                "risk_level": pending_action.get("risk_level"),
                "reason": pending_action.get("reason"),
                "status": "waiting",
            })
            self._business_db.audits.log(
                thread_id=thread_id,
                node="runner",
                action="approval_created",
                task_id=result.get("current_task_id", ""),
                order_id=pending_action.get("order_id"),
                detail=f"risk={pending_action.get('risk_level')}",
            )

        # Audit log for final state
        self._business_db.audits.log(
            thread_id=thread_id,
            node="runner",
            action="persist_state",
            detail=f"status={parsed.status}",
        )

        if parsed.status in {"completed", "blocked", "failed"}:
            self.export_run_report(thread_id)
            released = self._business_db.resource_locks.release_by_thread(thread_id)
            if released:
                self._business_db.audits.log(
                    thread_id=thread_id,
                    node="resource_lock",
                    action="resource_lock_released",
                    detail=f"released={released}",
                )

    def _persist_approval_decision(self, thread_id: str, resume_value: Any) -> None:
        if not isinstance(resume_value, dict) or "approved" not in resume_value:
            return

        status = "approved" if resume_value.get("approved") else "rejected"
        approval_id = resume_value.get("approval_id")
        pending_approvals = self._business_db.approvals.get_pending_by_thread(thread_id)
        for approval in pending_approvals:
            if approval_id and approval["approval_id"] != approval_id:
                continue
            self._business_db.approvals.update_status(approval["approval_id"], status)
            self._business_db.audits.log(
                thread_id=thread_id,
                node="runner",
                action=f"approval_{status}",
                task_id=approval.get("task_id"),
                order_id=approval.get("order_id"),
                detail=f"approval_id={approval['approval_id']}",
            )

    def _with_diagnostics(self, result: AgentRunResult) -> AgentRunResult:
        if result.status not in {"blocked", "failed"}:
            return result
        diagnostics = self._latest_worker_diagnostics(result.thread_id)
        if not diagnostics:
            return result
        return replace(result, diagnostics=diagnostics)

    def _latest_worker_diagnostics(self, thread_id: str) -> dict[str, Any] | None:
        rows = self._business_db.work_results.get_by_thread(thread_id)
        if not rows:
            return None
        row = rows[-1]
        return {
            "order_id": row.get("order_id"),
            "task_id": row.get("task_id"),
            "worker_type": row.get("worker_type"),
            "ok": bool(row.get("ok")),
            "exit_code": row.get("exit_code"),
            "summary": row.get("summary"),
            "stdout_tail": _tail(row.get("stdout") or ""),
            "stderr_tail": _tail(row.get("stderr") or ""),
            "artifacts": _parse_artifacts(row.get("artifacts")),
        }

    def _parse_result(self, result: AgentState) -> AgentRunResult:
        thread_id = result.get("thread_id", "")
        status = result.get("status", "blocked")
        summary = result.get("final_summary")
        pending_approval_id = result.get("pending_approval_id")

        parsed_interrupt = parse_interrupt_result(result)
        if parsed_interrupt:
            status = parsed_interrupt["status"]
            summary = parsed_interrupt["summary"]
            pending_approval_id = parsed_interrupt.get("pending_approval_id", pending_approval_id)

        return AgentRunResult(
            thread_id=thread_id,
            status=status,
            summary=summary,
            tasks=[dict(task) for task in result.get("task_list", [])],
            pending_approval_id=pending_approval_id,
        )


def _worker_result_payload(row: dict[str, Any]) -> dict[str, Any]:
    artifacts_value = _parse_artifacts(row.get("artifacts"))
    return {
        "order_id": row["order_id"],
        "task_id": row["task_id"],
        "ca_thread_id": row["ca_thread_id"],
        "worker_type": row["worker_type"],
        "ok": bool(row["ok"]),
        "exit_code": row.get("exit_code"),
        "stdout": row.get("stdout") or "",
        "stderr": row.get("stderr") or "",
        "artifacts": artifacts_value,
        "summary": row.get("summary") or "",
    }


def _resource_key_from_event(event: AgentEvent) -> str | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    explicit_key = payload.get("resource_key")
    if explicit_key is not None:
        value = str(explicit_key).strip()
        return value or None

    workdir = payload.get("workdir")
    if workdir is None:
        return None
    value = str(workdir).strip()
    if not value:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return value


def _parse_artifacts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _tail(value: str, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]
