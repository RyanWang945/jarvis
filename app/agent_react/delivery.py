from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent_react.artifacts import ChannelAttachment
from app.persistence.models import DeliveryRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    artifact_id: str
    status: str
    external_message_id: str | None = None
    upload_key: str | None = None
    error_message: str | None = None


class DeliveryStore(Protocol):
    def find_sent_delivery(
        self,
        *,
        channel: str,
        external_chat_id: str,
        artifact_id: str,
        purposes: tuple[str, ...],
    ) -> DeliveryRecord | None: ...

    def create_delivery_record(
        self,
        *,
        delivery_id: str,
        artifact_id: str,
        conversation_id: int | None,
        turn_id: int | None,
        channel: str,
        external_chat_id: str,
        purpose: str,
        status: str,
    ) -> DeliveryRecord: ...

    def mark_delivery_uploaded(self, delivery_id: str, *, upload_key: str) -> None: ...

    def mark_delivery_sent(
        self,
        delivery_id: str,
        *,
        upload_key: str | None = None,
        external_message_id: str | None = None,
    ) -> None: ...

    def mark_delivery_failed(self, delivery_id: str, *, error_message: str) -> None: ...


class AttachmentDeliveryHandler(Protocol):
    channel: str

    def upload_attachment(self, attachment: ChannelAttachment) -> str | None: ...

    def send_attachment(self, external_chat_id: str, attachment: ChannelAttachment, upload_key: str | None) -> str | None: ...

    def send_failure_notice(self, external_chat_id: str, attachment: ChannelAttachment, error_message: str) -> None: ...


class DeliveryManager:
    def __init__(self, store: DeliveryStore | None, handler: AttachmentDeliveryHandler) -> None:
        self._store = store
        self._handler = handler

    def deliver_attachments(
        self,
        *,
        external_chat_id: str,
        attachments: tuple[ChannelAttachment, ...],
        conversation_id: int | None = None,
        turn_id: int | None = None,
        purpose: str = "auto",
    ) -> tuple[DeliveryResult, ...]:
        results: list[DeliveryResult] = []
        for attachment in attachments:
            results.append(
                self.deliver_attachment(
                    external_chat_id=external_chat_id,
                    attachment=attachment,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    purpose=purpose,
                )
            )
        return tuple(results)

    def deliver_attachment(
        self,
        *,
        external_chat_id: str,
        attachment: ChannelAttachment,
        conversation_id: int | None = None,
        turn_id: int | None = None,
        purpose: str = "auto",
    ) -> DeliveryResult:
        store = self._store
        channel = self._handler.channel
        if store is not None and purpose != "redeliver":
            try:
                existing = store.find_sent_delivery(
                    channel=channel,
                    external_chat_id=external_chat_id,
                    artifact_id=attachment.artifact_id,
                    purposes=("auto", "explicit") if purpose == "explicit" else (purpose,),
                )
            except Exception:
                logger.exception(
                    "delivery idempotency check failed channel=%s chat=%s artifact_id=%s; continuing without persistent state",
                    channel,
                    external_chat_id,
                    attachment.artifact_id,
                )
                store = None
                existing = None
            if existing is not None:
                logger.info(
                    "delivery skipped channel=%s chat=%s artifact_id=%s purpose=%s reason=already_sent",
                    channel,
                    external_chat_id,
                    attachment.artifact_id,
                    purpose,
                )
                return DeliveryResult(
                    artifact_id=attachment.artifact_id,
                    status="already_sent",
                    upload_key=existing.upload_key,
                    external_message_id=existing.external_message_id,
                )

        delivery_id = _new_delivery_id()
        if store is not None:
            try:
                store.create_delivery_record(
                    delivery_id=delivery_id,
                    artifact_id=attachment.artifact_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    channel=channel,
                    external_chat_id=external_chat_id,
                    purpose=purpose,
                    status="pending",
                )
            except Exception:
                logger.exception(
                    "delivery record create failed channel=%s chat=%s artifact_id=%s; continuing without persistent state",
                    channel,
                    external_chat_id,
                    attachment.artifact_id,
                )
                store = None

        try:
            upload_key = self._handler.upload_attachment(attachment)
            if store is not None and upload_key:
                try:
                    store.mark_delivery_uploaded(delivery_id, upload_key=upload_key)
                except Exception:
                    logger.exception("delivery mark uploaded failed delivery_id=%s", delivery_id)
            external_message_id = self._handler.send_attachment(external_chat_id, attachment, upload_key)
            if store is not None:
                try:
                    store.mark_delivery_sent(
                        delivery_id,
                        upload_key=upload_key,
                        external_message_id=external_message_id,
                    )
                except Exception:
                    logger.exception("delivery mark sent failed delivery_id=%s", delivery_id)
            return DeliveryResult(
                artifact_id=attachment.artifact_id,
                status="sent",
                upload_key=upload_key,
                external_message_id=external_message_id,
            )
        except Exception as exc:
            error_message = str(exc)
            logger.exception(
                "delivery failed channel=%s chat=%s artifact_id=%s path=%s",
                channel,
                external_chat_id,
                attachment.artifact_id,
                attachment.path,
            )
            if store is not None:
                try:
                    store.mark_delivery_failed(delivery_id, error_message=error_message)
                except Exception:
                    logger.exception("delivery mark failed failed delivery_id=%s", delivery_id)
            try:
                self._handler.send_failure_notice(external_chat_id, attachment, error_message)
            except Exception:
                logger.exception("delivery failure notice failed channel=%s chat=%s", channel, external_chat_id)
            return DeliveryResult(
                artifact_id=attachment.artifact_id,
                status="failed",
                error_message=error_message,
            )


def _new_delivery_id() -> str:
    return f"delivery_{uuid.uuid4().hex}"


_HANDLERS: dict[str, AttachmentDeliveryHandler] = {}


def register_delivery_handler(handler: AttachmentDeliveryHandler) -> None:
    _HANDLERS[handler.channel] = handler


def get_delivery_handler(channel: str) -> AttachmentDeliveryHandler | None:
    return _HANDLERS.get(channel)


def get_delivery_manager(store: DeliveryStore | None, channel: str) -> DeliveryManager | None:
    handler = get_delivery_handler(channel)
    if handler is None:
        return None
    return DeliveryManager(store, handler)
