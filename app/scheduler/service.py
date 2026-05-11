from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol
from uuid import uuid4

from app.config import get_settings
from app.scheduler.parser import parse_one_shot_time
from app.scheduler.store import MySQLSchedulerStore, ScheduledJobRecord

logger = logging.getLogger(__name__)


class ReminderDelivery(Protocol):
    def send_reminder(self, *, platform: str, external_chat_id: str, text: str) -> str | None: ...


@dataclass(frozen=True)
class CreateReminderRequest:
    conversation_id: int
    created_by_user_id: int | None
    platform: str
    external_chat_id: str
    title: str
    prompt: str
    time_text: str
    timezone: str | None = None
    source_message_id: int | None = None

@dataclass(frozen=True)
class CreateReminderResponse:
    job: ReminderJob | None
    message: str | None
    ok: bool

@dataclass(frozen=True)
class ReminderJob:
    id: int
    title: str
    prompt: str
    timezone: str
    next_run_at: datetime | None
    lifecycle_status: str


class SchedulerService:
    def __init__(self, store: MySQLSchedulerStore | None = None) -> None:
        self._store = store or MySQLSchedulerStore()

    def create_reminder(self, request: CreateReminderRequest) -> CreateReminderResponse:
        timezone = request.timezone or get_settings().default_timezone
        parsed = parse_one_shot_time(request.time_text, timezone=timezone)
        if not parsed.ok or parsed.next_run_at is None or parsed.schedule_expr is None:
            return CreateReminderResponse(None, f"时间不够明确：{parsed.reason or request.time_text}", False)
        if parsed.confidence < 0.8:
            return None, "时间不够明确，请补充具体日期或时间。"

        job = self._store.create_job(
            conversation_id=request.conversation_id,
            created_by_user_id=request.created_by_user_id,
            name=request.title[:255] or "提醒",
            prompt=request.prompt or request.title or "提醒",
            schedule_expr=parsed.schedule_expr,
            timezone=parsed.timezone or timezone,
            next_run_at=parsed.next_run_at,
            delivery_target={
                "platform": request.platform,
                "external_chat_id": request.external_chat_id,
            },
            metadata={
                "source": "scheduled_task.tool",
                "time_text": request.time_text,
                "source_message_id": request.source_message_id,
                "parser_reason": parsed.reason,
                "parser_confidence": parsed.confidence,
            },
        )
        return CreateReminderResponse(_job(job), _format_created_reply(job))

    def list_reminders(self, conversation_id: int) -> list[ReminderJob]:
        return [_job(record) for record in self._store.list_jobs(conversation_id)]

    def cancel_reminder(self, *, conversation_id: int, job_id: int) -> bool:
        return self._store.cancel_job(conversation_id=conversation_id, job_id=job_id)

    def tick(self, delivery: ReminderDelivery, *, owner: str | None = None, limit: int = 10) -> int:
        owner = owner or f"jarvis-{uuid4().hex[:8]}"
        if not self._store.acquire_scheduler_lock(owner=owner, ttl_seconds=60):
            return 0
        completed = 0
        try:
            for job, run in self._store.claim_due_runs(limit=limit):
                if not self._store.mark_run_running(run.id):
                    continue
                target = job.delivery_target
                platform = str(target.get("platform") or "")
                external_chat_id = str(target.get("external_chat_id") or "")
                delivery_key = f"scheduled_job_run:{run.id}:origin"
                if not platform or not external_chat_id:
                    self._store.fail_run(run_id=run.id, error_message="missing delivery target")
                    continue
                if not self._store.create_pending_delivery(
                    source_id=run.id,
                    platform=platform,
                    external_chat_id=external_chat_id,
                    delivery_key=delivery_key,
                ):
                    logger.info("skipping duplicate delivery delivery_key=%s", delivery_key)
                    continue
                text = _reminder_text(job)
                try:
                    external_message_id = delivery.send_reminder(
                        platform=platform,
                        external_chat_id=external_chat_id,
                        text=text,
                    )
                except Exception as exc:
                    logger.exception("reminder delivery failed job_id=%s run_id=%s", job.id, run.id)
                    self._store.mark_delivery_failed(delivery_key=delivery_key, error_message=str(exc))
                    self._store.fail_run(run_id=run.id, error_message=str(exc))
                    continue
                self._store.mark_delivery_sent(
                    delivery_key=delivery_key,
                    external_message_id=external_message_id,
                )
                self._store.complete_run_and_job(run_id=run.id, job_id=job.id, output_summary=text)
                completed += 1
            return completed
        finally:
            self._store.release_scheduler_lock(owner=owner)


@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService()


def _job(record: ScheduledJobRecord) -> ReminderJob:
    return ReminderJob(
        id=record.id,
        title=record.name,
        prompt=record.prompt,
        timezone=record.timezone,
        next_run_at=_as_utc(record.next_run_at),
        lifecycle_status=record.lifecycle_status,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_created_reply(job: ScheduledJobRecord) -> str:
    local_time = job.schedule_expr
    return f"已设置提醒：{job.name}，时间：{local_time}。"


def _reminder_text(job: ScheduledJobRecord) -> str:
    prompt = job.prompt.strip() or job.name.strip() or "提醒"
    if prompt.startswith("提醒"):
        return prompt
    return f"提醒：{prompt}"
