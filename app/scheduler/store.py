from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ScheduledJobRecord:
    id: int
    conversation_id: int
    created_by_user_id: int | None
    name: str
    prompt: str
    schedule_kind: str
    schedule_expr: str
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    lifecycle_status: str
    run_count: int
    delivery_mode: str
    delivery_target: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ScheduledJobRunRecord:
    id: int
    job_id: int
    status: str
    scheduled_for: datetime
    started_at: datetime | None
    finished_at: datetime | None
    output_summary: str | None
    error_message: str | None
    metadata: dict[str, Any]


class MySQLSchedulerStore:
    def __init__(self, engine: Engine | None = None, *, ensure_schema: bool = True) -> None:
        self._engine = engine or _create_engine()
        if ensure_schema:
            self.ensure_schema()

    @property
    def engine(self) -> Engine:
        return self._engine

    def ensure_schema(self) -> None:
        with self._engine.begin() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(sa.text(statement))
            conn.execute(
                sa.text(
                    "INSERT IGNORE INTO scheduler_locks (lock_name, owner, expires_at) "
                    "VALUES ('scheduler', '', '1970-01-01 00:00:00')"
                )
            )

    def create_job(
        self,
        *,
        conversation_id: int,
        created_by_user_id: int | None,
        name: str,
        prompt: str,
        schedule_expr: str,
        timezone: str,
        next_run_at: datetime,
        delivery_target: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> ScheduledJobRecord:
        now = utcnow()
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT INTO scheduled_jobs "
                    "(conversation_id, created_by_user_id, name, prompt, schedule_kind, schedule_expr, timezone, "
                    "next_run_at, lifecycle_status, run_count, delivery_mode, delivery_target_json, metadata_json, created_at, updated_at) "
                    "VALUES (:conversation_id, :created_by_user_id, :name, :prompt, 'at', :schedule_expr, :timezone, "
                    ":next_run_at, 'active', 0, 'origin', :delivery_target, :metadata, :now, :now)"
                ),
                {
                    "conversation_id": conversation_id,
                    "created_by_user_id": created_by_user_id,
                    "name": name,
                    "prompt": prompt,
                    "schedule_expr": schedule_expr,
                    "timezone": timezone,
                    "next_run_at": next_run_at.replace(tzinfo=None),
                    "delivery_target": json.dumps(delivery_target, ensure_ascii=False),
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                    "now": now,
                },
            )
            return self.get_job(int(result.lastrowid), conn=conn)

    def list_jobs(self, conversation_id: int, *, include_inactive: bool = False) -> list[ScheduledJobRecord]:
        where = "conversation_id = :conversation_id"
        if not include_inactive:
            where += " AND lifecycle_status IN ('active', 'paused')"
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(f"SELECT * FROM scheduled_jobs WHERE {where} ORDER BY next_run_at ASC, id ASC"),
                {"conversation_id": conversation_id},
            ).mappings().all()
            return [self._job_from_row(row) for row in rows]

    def get_job(self, job_id: int, *, conn: sa.Connection | None = None) -> ScheduledJobRecord:
        owns_conn = conn is None
        if owns_conn:
            conn = self._engine.connect()
        try:
            row = conn.execute(
                sa.text("SELECT * FROM scheduled_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().one()
            return self._job_from_row(row)
        finally:
            if owns_conn and conn is not None:
                conn.close()

    def cancel_job(self, *, conversation_id: int, job_id: int) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE scheduled_jobs SET lifecycle_status = 'cancelled', updated_at = :now "
                    "WHERE id = :id AND conversation_id = :conversation_id AND lifecycle_status IN ('active', 'paused')"
                ),
                {"id": job_id, "conversation_id": conversation_id, "now": utcnow()},
            )
            return result.rowcount > 0

    def acquire_scheduler_lock(self, *, owner: str, ttl_seconds: int = 60) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE scheduler_locks SET owner = :owner, expires_at = :expires_at "
                    "WHERE lock_name = 'scheduler' AND expires_at < :now"
                ),
                {
                    "owner": owner,
                    "expires_at": utcnow() + timedelta(seconds=ttl_seconds),
                    "now": utcnow(),
                },
            )
            return result.rowcount == 1

    def release_scheduler_lock(self, *, owner: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE scheduler_locks SET owner = '', expires_at = :now "
                    "WHERE lock_name = 'scheduler' AND owner = :owner"
                ),
                {"owner": owner, "now": utcnow()},
            )

    def claim_due_runs(self, *, limit: int = 10) -> list[tuple[ScheduledJobRecord, ScheduledJobRunRecord]]:
        now = utcnow()
        claimed: list[tuple[ScheduledJobRecord, ScheduledJobRunRecord]] = []
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM scheduled_jobs "
                    "WHERE lifecycle_status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= :now "
                    "ORDER BY next_run_at ASC, id ASC LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            ).mappings().all()
            for row in rows:
                job = self._job_from_row(row)
                scheduled_for = job.next_run_at
                if scheduled_for is None:
                    continue
                insert = conn.execute(
                    sa.text(
                        "INSERT IGNORE INTO scheduled_job_runs "
                        "(job_id, status, scheduled_for, metadata_json) "
                        "VALUES (:job_id, 'queued', :scheduled_for, '{}')"
                    ),
                    {"job_id": job.id, "scheduled_for": scheduled_for},
                )
                run_row = conn.execute(
                    sa.text(
                        "SELECT * FROM scheduled_job_runs WHERE job_id = :job_id AND scheduled_for = :scheduled_for"
                    ),
                    {"job_id": job.id, "scheduled_for": scheduled_for},
                ).mappings().one()
                run = self._run_from_row(run_row)
                if insert.rowcount == 1 or run.status == "queued":
                    claimed.append((job, run))
        return claimed

    def mark_run_running(self, run_id: int) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "UPDATE scheduled_job_runs SET status = 'running', started_at = :now "
                    "WHERE id = :id AND status = 'queued'"
                ),
                {"id": run_id, "now": utcnow()},
            )
            return result.rowcount == 1

    def create_pending_delivery(
        self,
        *,
        source_id: int,
        platform: str,
        external_chat_id: str,
        delivery_key: str,
    ) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT IGNORE INTO message_deliveries "
                    "(source_type, source_id, platform, external_chat_id, delivery_key, status, created_at) "
                    "VALUES ('scheduled_job_run', :source_id, :platform, :external_chat_id, :delivery_key, 'pending', :now)"
                ),
                {
                    "source_id": source_id,
                    "platform": platform,
                    "external_chat_id": external_chat_id,
                    "delivery_key": delivery_key,
                    "now": utcnow(),
                },
            )
            return result.rowcount == 1

    def mark_delivery_sent(self, *, delivery_key: str, external_message_id: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE message_deliveries SET status = 'sent', external_message_id = :external_message_id, "
                    "sent_at = :now WHERE delivery_key = :delivery_key"
                ),
                {"delivery_key": delivery_key, "external_message_id": external_message_id, "now": utcnow()},
            )

    def mark_delivery_failed(self, *, delivery_key: str, error_message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE message_deliveries SET status = 'failed', error_message = :error_message "
                    "WHERE delivery_key = :delivery_key"
                ),
                {"delivery_key": delivery_key, "error_message": error_message[:2000]},
            )

    def complete_run_and_job(self, *, run_id: int, job_id: int, output_summary: str) -> None:
        now = utcnow()
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE scheduled_job_runs SET status = 'completed', finished_at = :now, output_summary = :summary "
                    "WHERE id = :run_id"
                ),
                {"run_id": run_id, "now": now, "summary": output_summary[:1000]},
            )
            conn.execute(
                sa.text(
                    "UPDATE scheduled_jobs SET lifecycle_status = 'completed', last_run_at = :now, "
                    "run_count = run_count + 1, next_run_at = NULL, updated_at = :now WHERE id = :job_id"
                ),
                {"job_id": job_id, "now": now},
            )

    def fail_run(self, *, run_id: int, error_message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE scheduled_job_runs SET status = 'failed', finished_at = :now, error_message = :error "
                    "WHERE id = :run_id"
                ),
                {"run_id": run_id, "now": utcnow(), "error": error_message[:2000]},
            )

    @staticmethod
    def _job_from_row(row: sa.RowMapping) -> ScheduledJobRecord:
        return ScheduledJobRecord(
            id=int(row["id"]),
            conversation_id=int(row["conversation_id"]),
            created_by_user_id=int(row["created_by_user_id"]) if row["created_by_user_id"] is not None else None,
            name=str(row["name"] or ""),
            prompt=str(row["prompt"] or ""),
            schedule_kind=str(row["schedule_kind"] or ""),
            schedule_expr=str(row["schedule_expr"] or ""),
            timezone=str(row["timezone"] or ""),
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            lifecycle_status=str(row["lifecycle_status"] or ""),
            run_count=int(row["run_count"] or 0),
            delivery_mode=str(row["delivery_mode"] or ""),
            delivery_target=json.loads(row["delivery_target_json"] or "{}"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run_from_row(row: sa.RowMapping) -> ScheduledJobRunRecord:
        return ScheduledJobRunRecord(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            status=str(row["status"] or ""),
            scheduled_for=row["scheduled_for"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            output_summary=row["output_summary"],
            error_message=row["error_message"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )


def _create_engine() -> Engine:
    settings = get_settings()
    url = (
        f"mysql+pymysql://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
        f"?charset=utf8mb4"
    )
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
        conversation_id BIGINT UNSIGNED NOT NULL,
        created_by_user_id BIGINT UNSIGNED NULL,
        name VARCHAR(255) NOT NULL,
        prompt TEXT NOT NULL,
        schedule_kind VARCHAR(32) NOT NULL,
        schedule_expr VARCHAR(255) NOT NULL,
        timezone VARCHAR(64) NOT NULL,
        next_run_at DATETIME(6) NULL,
        last_run_at DATETIME(6) NULL,
        lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
        run_count INT UNSIGNED NOT NULL DEFAULT 0,
        delivery_mode VARCHAR(32) NOT NULL DEFAULT 'origin',
        delivery_target_json JSON,
        metadata_json JSON,
        created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
        KEY idx_scheduled_jobs_due (lifecycle_status, next_run_at),
        KEY idx_scheduled_jobs_conversation (conversation_id, lifecycle_status, next_run_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_job_runs (
        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
        job_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'queued',
        scheduled_for DATETIME(6) NOT NULL,
        started_at DATETIME(6) NULL,
        finished_at DATETIME(6) NULL,
        output_summary TEXT,
        error_message TEXT,
        metadata_json JSON,
        created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        UNIQUE KEY uk_scheduled_job_runs_job_time (job_id, scheduled_for),
        KEY idx_scheduled_job_runs_status (status, scheduled_for),
        CONSTRAINT fk_scheduled_job_runs_job FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS message_deliveries (
        id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
        source_type VARCHAR(64) NOT NULL,
        source_id BIGINT UNSIGNED NOT NULL,
        platform VARCHAR(32) NOT NULL,
        external_chat_id VARCHAR(128) NOT NULL,
        delivery_key VARCHAR(255) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        external_message_id VARCHAR(128) NULL,
        error_message TEXT,
        created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        sent_at DATETIME(6) NULL,
        UNIQUE KEY uk_message_deliveries_key (delivery_key),
        KEY idx_message_deliveries_source (source_type, source_id),
        KEY idx_message_deliveries_status (status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_locks (
        lock_name VARCHAR(64) PRIMARY KEY,
        owner VARCHAR(128) NOT NULL,
        expires_at DATETIME(6) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
]
