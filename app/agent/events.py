from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

AgentEventType = Literal[
    "user_message",
    "schedule",
    "approval",
    "system_resume",
    "task_cancel",
    "task_status_query",
    "worker_complete",
    "worker_failed",
]
AgentEventSource = Literal["api", "cli", "scheduler", "feishu", "system"]


class AgentEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: AgentEventType = "user_message"
    source: AgentEventSource = "api"
    thread_id: str | None = None
    user_id: str | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)


def build_user_event(
    *,
    instruction: str,
    command: str | None = None,
    verification_cmd: str | None = None,
    workdir: str | None = None,
    resource_key: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> AgentEvent:
    payload: dict[str, Any] = {
        "instruction": instruction,
        "command": command,
        "verification_cmd": verification_cmd,
        "workdir": workdir,
        "resource_key": resource_key,
    }
    return AgentEvent(thread_id=thread_id, user_id=user_id, payload=payload)
