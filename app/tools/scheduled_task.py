from __future__ import annotations

from app.config import get_settings
from app.scheduler.service import CreateReminderRequest, get_scheduler_service
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_scheduled_task(request: ToolExecutionRequest) -> ToolExecutionResult:
    action = str(request.args.get("action") or "").strip().lower()
    service = get_scheduler_service()
    if action == "list":
        conversation_id = _required_int(request.args.get("conversation_id"), "conversation_id")
        if isinstance(conversation_id, ToolExecutionResult):
            return conversation_id
        jobs = service.list_reminders(conversation_id)
        if not jobs:
            return ToolExecutionResult(ok=True, exit_code=0, stdout="No active reminders.", summary="No active reminders.")
        lines = [
            f"#{job.id} {job.title} {job.next_run_at.isoformat() if job.next_run_at else '-'}"
            for job in jobs
        ]
        return ToolExecutionResult(ok=True, exit_code=0, stdout="\n".join(lines), summary=f"Found {len(jobs)} reminders.")

    if action == "remove":
        conversation_id = _required_int(request.args.get("conversation_id"), "conversation_id")
        if isinstance(conversation_id, ToolExecutionResult):
            return conversation_id
        job_id = _required_int(request.args.get("job_id"), "job_id")
        if isinstance(job_id, ToolExecutionResult):
            return job_id
        ok = service.cancel_reminder(conversation_id=conversation_id, job_id=job_id)
        return ToolExecutionResult(
            ok=ok,
            exit_code=0 if ok else None,
            stdout="Reminder cancelled." if ok else "",
            summary="Reminder cancelled." if ok else "Reminder not found.",
        )

    if action == "create":
        conversation_id = _required_int(request.args.get("conversation_id"), "conversation_id")
        if isinstance(conversation_id, ToolExecutionResult):
            return conversation_id
        title = str(request.args.get("title") or "提醒").strip()
        prompt = str(request.args.get("prompt") or title or "提醒").strip()
        time_text = str(request.args.get("time_text") or "").strip()
        platform = str(request.args.get("platform") or "feishu").strip()
        external_chat_id = str(request.args.get("external_chat_id") or "").strip()
        if not time_text:
            return ToolExecutionResult(ok=False, exit_code=None, stderr="time_text is required", summary="Missing time_text.")
        if not external_chat_id:
            return ToolExecutionResult(
                ok=False,
                exit_code=None,
                stderr="external_chat_id is required",
                summary="Missing external_chat_id.",
            )
        job, reply = service.create_reminder(
            CreateReminderRequest(
                conversation_id=conversation_id,
                created_by_user_id=_optional_int(request.args.get("created_by_user_id")),
                platform=platform,
                external_chat_id=external_chat_id,
                title=title,
                prompt=prompt,
                time_text=time_text,
                timezone=str(request.args.get("timezone") or get_settings().default_timezone),
            )
        )
        return ToolExecutionResult(
            ok=job is not None,
            exit_code=0 if job is not None else None,
            stdout=reply,
            summary=reply,
        )

    return ToolExecutionResult(ok=False, exit_code=None, stderr=f"unsupported action: {action}", summary="Unsupported action.")


def _required_int(value: object, name: str) -> int | ToolExecutionResult:
    try:
        return int(value)
    except (TypeError, ValueError):
        return ToolExecutionResult(ok=False, exit_code=None, stderr=f"{name} is required", summary=f"Missing {name}.")


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
