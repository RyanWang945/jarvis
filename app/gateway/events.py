from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InboundEvent:
    platform: str
    external_chat_id: str
    external_message_id: str | None
    chat_type: str
    sender_id: str
    sender_name: str | None
    text: str
    mentions: list[str] = field(default_factory=list)
    reply_to_external_message_id: str | None = None
    raw_payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayResult:
    status: str
    conversation_id: int | None = None
    message_id: int | None = None
    turn_id: int | None = None
    reminder_job_id: int | None = None
    should_run_agent: bool = False
    immediate_reply: str | None = None
    delivery_kind: str = "none"
