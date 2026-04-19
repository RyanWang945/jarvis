from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.workers.base import WorkOrder, WorkResult


class TaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, task: dict[str, Any], run_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO tasks (
                task_id, run_id, title, description, status, resource_key,
                dod, verification_cmd, tool_name, worker_type, order_id,
                retry_count, max_retries, result_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status=excluded.status,
                order_id=excluded.order_id,
                retry_count=excluded.retry_count,
                result_summary=excluded.result_summary,
                updated_at=datetime('now')
            """,
            (
                task["id"],
                run_id,
                task.get("title"),
                task.get("description"),
                task["status"],
                task.get("resource_key"),
                task.get("dod"),
                task.get("verification_cmd"),
                task.get("tool_name"),
                task.get("worker_type"),
                task.get("order_id"),
                task.get("retry_count", 0),
                task.get("max_retries", 0),
                task.get("result_summary"),
            ),
        )
        self._conn.commit()

    def get_by_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


class WorkOrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, order: WorkOrder) -> None:
        self._conn.execute(
            """
            INSERT INTO work_orders (
                order_id, task_id, ca_thread_id, worker_type, action,
                args, workdir, risk_level, reason, verification_cmd, timeout_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                worker_type=excluded.worker_type,
                action=excluded.action,
                args=excluded.args,
                workdir=excluded.workdir,
                reason=excluded.reason,
                verification_cmd=excluded.verification_cmd,
                timeout_seconds=excluded.timeout_seconds
            """,
            (
                order.order_id,
                order.task_id,
                order.ca_thread_id,
                order.worker_type,
                order.action,
                json.dumps(order.args),
                order.workdir,
                order.risk_level,
                order.reason,
                order.verification_cmd,
                order.timeout_seconds,
            ),
        )
        self._conn.commit()

    def mark_dispatched(self, order_id: str) -> None:
        self._conn.execute(
            "UPDATE work_orders SET status = 'dispatched', dispatched_at = datetime('now') WHERE order_id = ?",
            (order_id,),
        )
        self._conn.commit()

    def mark_completed(self, order_id: str) -> None:
        self._conn.execute(
            "UPDATE work_orders SET status = 'completed', completed_at = datetime('now') WHERE order_id = ?",
            (order_id,),
        )
        self._conn.commit()

    def get_by_order(self, order_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM work_orders WHERE order_id = ?",
            (order_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM work_orders WHERE ca_thread_id = ? ORDER BY created_at",
            (thread_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def list_incomplete(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        if thread_id:
            cursor = self._conn.execute(
                """
                SELECT * FROM work_orders
                WHERE ca_thread_id = ?
                  AND status NOT IN ('completed')
                ORDER BY created_at
                """,
                (thread_id,),
            )
        else:
            cursor = self._conn.execute(
                """
                SELECT * FROM work_orders
                WHERE status NOT IN ('completed')
                ORDER BY created_at
                """
            )
        return [dict(row) for row in cursor.fetchall()]


class WorkResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, result: WorkResult) -> None:
        self._conn.execute(
            """
            INSERT INTO work_results (
                order_id, task_id, ca_thread_id, worker_type,
                ok, exit_code, stdout, stderr, artifacts, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                ok=excluded.ok,
                exit_code=excluded.exit_code,
                stdout=excluded.stdout,
                stderr=excluded.stderr,
                artifacts=excluded.artifacts,
                summary=excluded.summary
            """,
            (
                result.order_id,
                result.task_id,
                result.ca_thread_id,
                result.worker_type,
                1 if result.ok else 0,
                result.exit_code,
                result.stdout,
                result.stderr,
                json.dumps(result.artifacts),
                result.summary,
            ),
        )
        self._conn.commit()

    def get_by_order(self, order_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM work_results WHERE order_id = ?",
            (order_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM work_results WHERE ca_thread_id = ? ORDER BY created_at",
            (thread_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


class ApprovalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, approval: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO approvals (
                approval_id, thread_id, task_id, order_id, action_kind,
                command, risk_level, reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason
            """,
            (
                approval["approval_id"],
                approval["thread_id"],
                approval["task_id"],
                approval.get("order_id"),
                approval.get("action_kind"),
                approval.get("command"),
                approval.get("risk_level"),
                approval.get("reason"),
                approval.get("status", "waiting"),
            ),
        )
        self._conn.commit()

    def update_status(self, approval_id: str, status: str, approved_by: str | None = None) -> None:
        self._conn.execute(
            """
            UPDATE approvals
            SET status = ?, approved_by = ?, approved_at = datetime('now')
            WHERE approval_id = ?
            """,
            (status, approved_by, approval_id),
        )
        self._conn.commit()

    def get_pending_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM approvals WHERE thread_id = ? AND status = 'waiting' ORDER BY created_at",
            (thread_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM approvals WHERE thread_id = ? ORDER BY created_at",
            (thread_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


class AuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def log(self, thread_id: str, node: str, action: str, *, task_id: str | None = None, order_id: str | None = None, detail: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_logs (thread_id, task_id, order_id, node, action, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, task_id, order_id, node, action, detail),
        )
        self._conn.commit()

    def get_by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM audit_logs WHERE thread_id = ? ORDER BY created_at",
            (thread_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


class RunRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, run: dict[str, Any]) -> None:
        existing = self.get_by_thread(run["thread_id"])
        if existing:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    instruction = COALESCE(?, instruction),
                    summary = ?,
                    updated_at = datetime('now')
                WHERE thread_id = ?
                """,
                (
                    run["status"],
                    run.get("instruction"),
                    run.get("summary"),
                    run["thread_id"],
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, thread_id, status, instruction, summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["thread_id"],
                    run["status"],
                    run.get("instruction"),
                    run.get("summary"),
                ),
            )
        self._conn.commit()

    def get_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM runs WHERE thread_id = ?",
            (thread_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_unfinished(self) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM runs WHERE status NOT IN ('completed', 'blocked', 'failed') ORDER BY updated_at DESC"
        )
        return [dict(row) for row in cursor.fetchall()]


@dataclass(frozen=True)
class BusinessDB:
    conn: sqlite3.Connection
    runs: RunRepository
    tasks: TaskRepository
    work_orders: WorkOrderRepository
    work_results: WorkResultRepository
    approvals: ApprovalRepository
    audits: AuditRepository


def get_business_db(db_path: Any) -> BusinessDB:
    from app.persistence.db import init_business_db

    conn = init_business_db(db_path)
    return BusinessDB(
        conn=conn,
        runs=RunRepository(conn),
        tasks=TaskRepository(conn),
        work_orders=WorkOrderRepository(conn),
        work_results=WorkResultRepository(conn),
        approvals=ApprovalRepository(conn),
        audits=AuditRepository(conn),
    )
