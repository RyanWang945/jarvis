from __future__ import annotations

from dataclasses import dataclass

from app.scheduler.service import CreateReminderResponse, ReminderJob
from app.tools.common import ToolExecutionRequest
from app.tools.scheduled_task import run_scheduled_task


@dataclass
class _FakeSchedulerService:
    captured_time_text: str | None = None

    def create_reminder(self, request):
        self.captured_time_text = request.time_text
        return CreateReminderResponse(
            job=ReminderJob(
                id=42,
                title=request.title,
                prompt=request.prompt,
                timezone=request.timezone or "Asia/Shanghai",
                next_run_at=None,
                lifecycle_status="active",
            ),
            message="已设置提醒：喝水提醒，时间：10分钟后。",
            ok=True,
        )


def test_scheduled_task_create_accepts_source_time_text(monkeypatch) -> None:
    service = _FakeSchedulerService()
    monkeypatch.setattr("app.tools.scheduled_task.get_scheduler_service", lambda: service)

    result = run_scheduled_task(
        ToolExecutionRequest(
            tool_name="scheduled_task",
            workdir=None,
            args={
                "action": "create",
                "conversation_id": 1,
                "title": "喝水提醒",
                "prompt": "该喝水了！",
                "source_time_text": "10分钟后",
                "timezone": "Asia/Shanghai",
                "platform": "feishu",
                "external_chat_id": "oc_test",
            },
        )
    )

    assert result.ok is True
    assert service.captured_time_text == "10分钟后"
    assert "已设置提醒" in result.stdout


def test_scheduled_task_create_accepts_run_at_when_time_text_missing(monkeypatch) -> None:
    service = _FakeSchedulerService()
    monkeypatch.setattr("app.tools.scheduled_task.get_scheduler_service", lambda: service)

    result = run_scheduled_task(
        ToolExecutionRequest(
            tool_name="scheduled_task",
            workdir=None,
            args={
                "action": "create",
                "conversation_id": 1,
                "title": "喝水提醒",
                "prompt": "该喝水了！",
                "run_at": "2026-05-13T18:54:05+08:00",
                "timezone": "Asia/Shanghai",
                "platform": "feishu",
                "external_chat_id": "oc_test",
            },
        )
    )

    assert result.ok is True
    assert service.captured_time_text == "2026-05-13T18:54:05+08:00"
