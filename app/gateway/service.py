from __future__ import annotations

from functools import lru_cache

from app.api.schemas import MessageCreateRequest, SenderInput
from app.gateway.events import GatewayResult, InboundEvent


class GatewayService:
    def __init__(
        self,
        *,
        conversation_store=None,
    ) -> None:
        self._conversation_store = conversation_store

    @property
    def conversation_store(self):
        if self._conversation_store is None:
            from app.api.agent import get_conversation_store

            self._conversation_store = get_conversation_store()
        return self._conversation_store

    def handle_inbound_event(self, event: InboundEvent) -> GatewayResult:
        ingest = self.conversation_store.ingest_message(_message_request(event))
        return GatewayResult(
            status=ingest.status,
            conversation_id=ingest.conversation_id,
            message_id=ingest.message_id,
            turn_id=ingest.turn_id,
            should_run_agent=bool(ingest.should_respond),
            immediate_reply=ingest.reset_message,
            delivery_kind="text",
        )


@lru_cache
def get_gateway_service() -> GatewayService:
    return GatewayService()


def _message_request(event: InboundEvent, *, metadata: dict | None = None) -> MessageCreateRequest:
    return MessageCreateRequest(
        platform=event.platform,  # type: ignore[arg-type]
        external_chat_id=event.external_chat_id,
        chat_type=event.chat_type,  # type: ignore[arg-type]
        sender=SenderInput(platform_user_id=event.sender_id, display_name=event.sender_name),
        content=event.text,
        external_message_id=event.external_message_id,
        reply_to_external_message_id=event.reply_to_external_message_id,
        mentions=event.mentions,
        raw_payload=event.raw_payload,
        metadata=metadata if metadata is not None else event.metadata,
    )
