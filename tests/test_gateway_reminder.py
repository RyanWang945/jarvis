from dataclasses import dataclass

from app.api.agent import InMemoryConversationStore
from app.gateway.events import InboundEvent
from app.gateway.service import GatewayService


@dataclass(frozen=True)
class _FakeJob:
    id: int = 42


class _FakeScheduler:
    def __init__(self) -> None:
        self.created = []
        self.cancelled = []

    def create_reminder(self, request):
        self.created.append(request)
        return _FakeJob(), "已设置提醒：提醒起床，时间：2026-05-04T10:00:00+08:00。"

    def list_reminders(self, conversation_id: int):
        return []

    def cancel_reminder(self, *, conversation_id: int, job_id: int) -> bool:
        self.cancelled.append((conversation_id, job_id))
        return True


def test_gateway_reminder_text_falls_through_to_normal_turn() -> None:
    store = InMemoryConversationStore()
    scheduler = _FakeScheduler()
    gateway = GatewayService(conversation_store=store)

    result = gateway.handle_inbound_event(
        InboundEvent(
            platform="cli",
            external_chat_id="chat-1",
            external_message_id="msg-1",
            chat_type="cli",
            sender_id="u1",
            sender_name="User",
            text="10点提醒我起床",
        )
    )

    assert result.status == "queued"
    assert result.should_run_agent is True
    assert result.turn_id is not None
    assert result.reminder_job_id is None
    assert scheduler.created == []


def test_gateway_non_reminder_falls_back_to_normal_turn() -> None:
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)

    result = gateway.handle_inbound_event(
        InboundEvent(
            platform="cli",
            external_chat_id="chat-2",
            external_message_id="msg-2",
            chat_type="cli",
            sender_id="u1",
            sender_name=None,
            text="hello",
        )
    )

    assert result.should_run_agent is True
    assert result.turn_id is not None
