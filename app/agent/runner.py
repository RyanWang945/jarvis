import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.agent.events import AgentEvent
from app.agent.graph import build_agent_graph
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


class ThreadManager:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path = data_dir / "langgraph_checkpoints.sqlite"
        self._conn = sqlite3.connect(str(self._checkpoint_path), check_same_thread=False)
        self._checkpointer = SqliteSaver(self._conn)
        self._checkpointer.setup()
        self._graph = build_agent_graph(checkpointer=self._checkpointer)
        self._business_db = get_business_db(data_dir / "business.db")

    @property
    def db(self) -> BusinessDB:
        return self._business_db

    def run_event(self, event: AgentEvent) -> AgentRunResult:
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

        result: AgentState = self._graph.invoke(state, config=config)
        parsed = self._parse_result(result)

        # Persist business state
        self._persist_run_state(result, parsed, instruction)

        return parsed

    def resume(self, thread_id: str, resume_value: Any) -> AgentRunResult:
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

        return parsed

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

        # Persist work orders and their latest lifecycle status.
        active_order_ids = set(result.get("active_workers", {}).values())
        completed_order_ids = set(result.get("worker_results", {}).keys())
        for order_dict in result.get("work_orders", {}).values():
            order = WorkOrder(**order_dict)
            self._business_db.work_orders.save(order)
            if order.order_id in active_order_ids:
                self._business_db.work_orders.mark_dispatched(order.order_id)
            if order.order_id in completed_order_ids:
                self._business_db.work_orders.mark_completed(order.order_id)

        # Persist worker_results -> work_results table
        worker_results = result.get("worker_results", {})
        for order_id, wr_dict in worker_results.items():
            wr = WorkResult(**wr_dict)
            self._business_db.work_results.save(wr)
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

    def _parse_result(self, result: AgentState) -> AgentRunResult:
        thread_id = result.get("thread_id", "")
        status = result.get("status", "blocked")
        summary = result.get("final_summary")
        pending_approval_id = result.get("pending_approval_id")

        # Handle interrupt: graph returns __interrupt__ when paused
        if "__interrupt__" in result:
            interrupts = result["__interrupt__"]
            interrupt_info = interrupts[0] if interrupts else None
            if interrupt_info and isinstance(interrupt_info.value, dict):
                if interrupt_info.value.get("type") == "approval_required":
                    status = "waiting_approval"
                    summary = "Waiting for approval"
                elif interrupt_info.value.get("type") == "wait_workers":
                    status = "monitoring"
                    summary = "Waiting for workers"

        return AgentRunResult(
            thread_id=thread_id,
            status=status,
            summary=summary,
            tasks=[dict(task) for task in result.get("task_list", [])],
            pending_approval_id=pending_approval_id,
        )
