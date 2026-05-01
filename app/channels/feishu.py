from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import lark_oapi.ws.client as lark_ws_client
from lark_oapi import EventDispatcherHandler, ws
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from app.agent_react import ChannelMessage, TurnResult
from app.api.agent import get_agent_runtime, get_conversation_store
from app.api.schemas import MessageCreateRequest, SenderInput
from app.channels.feishu_renderer import FeishuDelivery, FeishuRenderer
from app.config import get_settings

logger = logging.getLogger(__name__)


class FeishuChannel:
    """Feishu WebSocket channel for inbound messages and outbound replies."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        bot_name: str = "Jarvis",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._bot_name = bot_name
        self._client: ws.Client | None = None
        self._event_handler: EventDispatcherHandler | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu_")
        self._running = False
        self._lock = threading.Lock()

        self._http = httpx.Client(timeout=30.0)
        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._renderer = FeishuRenderer(title=bot_name)

    def start(self) -> None:
        with self._lock:
            if self._running:
                logger.warning("feishu channel already running")
                return
            self._running = True

        logger.info("feishu channel starting app_id=%s", self._app_id)
        try:
            event_handler = _install_event_diagnostics(
                EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .build()
            )
            self._event_handler = event_handler
            self._stopping = False
            self._ws_thread = threading.Thread(
                target=self._run_ws_in_thread,
                name="feishu-ws",
                daemon=True,
            )
            self._ws_thread.start()
            logger.info("feishu channel started")
        except Exception:
            logger.exception("failed to start feishu channel")
            with self._lock:
                self._running = False
            raise

    def _run_ws_in_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        lark_ws_client.loop = loop
        self._ws_loop = loop
        try:
            client = ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=self._event_handler,
            )
            self._client = client
            client.start()
        except RuntimeError as exc:
            if self._stopping and "Event loop stopped before Future completed" in str(exc):
                logger.info("feishu ws thread stopped")
            else:
                logger.exception("feishu ws thread exited with error")
        except Exception:
            logger.exception("feishu ws thread exited with error")
        finally:
            self._client = None
            self._ws_loop = None
            loop.close()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False

        logger.info("feishu channel stopping")
        self._stopping = True
        client = self._client
        loop = self._ws_loop
        if client is not None and loop is not None and loop.is_running():
            try:
                disconnect = getattr(client, "_disconnect", None)
                if disconnect is not None:
                    future = asyncio.run_coroutine_threadsafe(disconnect(), loop)
                    future.result(timeout=5)
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                logger.exception("error stopping feishu ws client")
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5)
            self._ws_thread = None
        self._executor.shutdown(wait=False)
        self._http.close()
        logger.info("feishu channel stopped")

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            event = data.event
            if event is None or event.sender is None or event.message is None:
                logger.warning("feishu message event missing sender or message data=%s", data)
                return
            sender = event.sender.sender_id.open_id
            chat_id = event.message.chat_id
            chat_type = event.message.chat_type
            msg_type = event.message.message_type
            content_raw = event.message.content
            message_id = getattr(event.message, "message_id", None)
            parent_id = getattr(event.message, "parent_id", None)
            root_id = getattr(event.message, "root_id", None)

            logger.info(
                "feishu message received sender=%s chat=%s chat_type=%s type=%s message_id=%s",
                sender,
                chat_id,
                chat_type,
                msg_type,
                message_id,
            )

            if msg_type != "text":
                logger.debug("skipping non-text message type=%s", msg_type)
                return

            content = json.loads(content_raw)
            raw_text = content.get("text", "").strip()
            mentions = _extract_mentions(event.message, content, self._bot_name)
            text = _strip_bot_mention(raw_text, mentions, self._bot_name)
            logger.info(
                "feishu text parsed chat=%s chat_type=%s text_len=%s text_preview=%s",
                chat_id,
                chat_type,
                len(raw_text),
                _safe_preview(raw_text),
            )

            if chat_type == "group" and not text:
                logger.debug("message was only an @mention, ignoring")
                return

            ingest = get_conversation_store().ingest_message(
                MessageCreateRequest(
                    platform="feishu",
                    external_chat_id=chat_id,
                    chat_type=_conversation_chat_type(chat_type),
                    sender=SenderInput(platform_user_id=sender),
                    content=text or raw_text,
                    external_message_id=message_id,
                    reply_to_external_message_id=parent_id or root_id,
                    mentions=mentions,
                    raw_payload={
                        "chat_type": chat_type,
                        "message_type": msg_type,
                        "content": content,
                        "message_id": message_id,
                        "parent_id": parent_id,
                        "root_id": root_id,
                    },
                    metadata={
                        "reply_to_bot": _reply_to_bot(event.message),
                        "feishu_chat_type": chat_type,
                    },
                )
            )
            if not ingest.should_respond:
                logger.info(
                    "feishu message stored without response chat=%s conversation_id=%s message_id=%s",
                    chat_id,
                    ingest.conversation_id,
                    ingest.message_id,
                )
                return

            self._executor.submit(
                self._handle_agent_run,
                sender,
                chat_id,
                chat_type,
                text or raw_text,
                ingest.conversation_id,
                ingest.turn_id,
            )
        except Exception:
            logger.exception("error handling feishu message")

    def _handle_agent_run(
        self,
        sender_open_id: str,
        chat_id: str,
        chat_type: str,
        text: str,
        conversation_id: int,
        turn_id: int | None,
    ) -> None:
        try:
            logger.info(
                "feishu agent run starting chat=%s conversation_id=%s turn_id=%s chat_type=%s sender=%s text_preview=%s",
                chat_id,
                conversation_id,
                turn_id,
                chat_type,
                sender_open_id,
                _safe_preview(text),
            )
            if turn_id is None:
                raise ValueError("Feishu triggered message did not create a turn.")
            result = get_agent_runtime().run_turn(turn_id)
        except Exception:
            logger.exception("agent run failed for feishu message")
            if turn_id is not None:
                get_conversation_store().complete_turn(
                    turn_id,
                    status="failed",
                    error_message="agent run failed",
                )
            self._send_text_message(
                chat_id,
                "Sorry, something went wrong. Please try again later.",
            )
            return

        logger.info(
            "feishu agent run finished chat=%s status=%s summary_len=%s",
            chat_id,
            result.status,
            len(result.reply),
        )
        message = self._format_result(result)
        self._send_channel_message(chat_id, message)

    @staticmethod
    def _format_result(result: TurnResult) -> ChannelMessage:
        return result.message

    def _ensure_token(self) -> str:
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token

        resp = self._http.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu token error: {payload}")

        self._tenant_access_token = payload["tenant_access_token"]
        self._token_expires_at = now + payload.get("expire", 7200)
        return self._tenant_access_token

    def _send_channel_message(self, receive_id: str, message: ChannelMessage) -> None:
        delivery = self._renderer.render(message)
        try:
            self._send_delivery(receive_id, delivery)
        except Exception:
            logger.exception(
                "failed to send feishu delivery receive_id=%s msg_type=%s, retrying text fallback",
                receive_id,
                delivery.msg_type,
            )
            fallback = self._renderer.render_text_fallback(message.content)
            try:
                self._send_delivery(receive_id, fallback)
            except Exception:
                logger.exception("failed to send feishu fallback text to %s", receive_id)

    def _send_text_message(self, receive_id: str, text: str) -> None:
        self._send_delivery(receive_id, self._renderer.render_text_fallback(text))

    def _send_delivery(self, receive_id: str, delivery: FeishuDelivery) -> None:
        token = self._ensure_token()
        resp = self._http.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": receive_id,
                "msg_type": delivery.msg_type,
                "content": delivery.content,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu send message failed: {payload}")
        logger.info(
            "feishu message sent receive_id=%s msg_type=%s",
            receive_id,
            delivery.msg_type,
        )

    def send_message(self, receive_id: str, text: str) -> bool:
        try:
            self._send_text_message(receive_id, text)
            return True
        except Exception:
            return False


def build_feishu_channel() -> FeishuChannel | None:
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.info("feishu credentials not configured, skipping channel")
        return None
    return FeishuChannel(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        bot_name=settings.feishu_bot_name or "Jarvis",
    )


def _conversation_chat_type(feishu_chat_type: str) -> str:
    if feishu_chat_type == "p2p":
        return "dm"
    if feishu_chat_type == "group":
        return "group"
    return "group"


def _extract_mentions(message: Any, content: dict[str, Any], bot_name: str) -> list[str]:
    mentions: list[str] = []
    raw_mentions = getattr(message, "mentions", None) or content.get("mentions") or []
    for item in raw_mentions:
        for value in _mention_values(item):
            if value and value not in mentions:
                mentions.append(value)

    text = str(content.get("text", ""))
    if f"@{bot_name}".lower() in text.lower() and "jarvis" not in {item.lower() for item in mentions}:
        mentions.append("jarvis")
    return mentions


def _mention_values(item: Any) -> list[str]:
    values: list[str] = []
    if isinstance(item, dict):
        for key in ("name", "key", "id", "open_id", "user_id", "tenant_key"):
            value = item.get(key)
            if value:
                values.append(str(value))
        return values

    for attr in ("name", "key", "id", "open_id", "user_id", "tenant_key"):
        value = getattr(item, attr, None)
        if value:
            values.append(str(value))
    return values


def _strip_bot_mention(text: str, mentions: list[str], bot_name: str) -> str:
    cleaned = text
    cleaned = re.sub(r"@_user_\d+", "", cleaned)
    cleaned = re.sub(rf"@{re.escape(bot_name)}\b", "", cleaned, flags=re.IGNORECASE)
    if any(value.lower() == "jarvis" for value in mentions):
        cleaned = re.sub(r"@Jarvis\b", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _reply_to_bot(message: Any) -> bool:
    parent_id = getattr(message, "parent_id", None)
    root_id = getattr(message, "root_id", None)
    if not parent_id and not root_id:
        return False
    return False


def _install_event_diagnostics(handler: EventDispatcherHandler) -> EventDispatcherHandler:
    original = handler.do_without_validation

    def traced(payload: bytes) -> Any:
        event_key = _event_key_from_payload(payload)
        processors = getattr(handler, "_processorMap", {})
        callback_processors = getattr(handler, "_callback_processor_map", {})
        logger.info(
            "feishu raw event received event_key=%s payload_bytes=%s payload_preview=%s",
            event_key,
            len(payload),
            _safe_preview(payload.decode("utf-8", errors="replace"), limit=500),
        )
        if event_key not in processors and event_key not in callback_processors:
            logger.info(
                "feishu raw event ignored event_key=%s registered_processors=%s",
                event_key,
                sorted(processors.keys()),
            )
            return None
        try:
            return original(payload)
        except Exception:
            logger.exception(
                "feishu raw event dispatch failed event_key=%s registered_processors=%s",
                event_key,
                sorted(processors.keys()),
            )
            raise

    handler.do_without_validation = traced  # type: ignore[method-assign]
    return handler


def _event_key_from_payload(payload: bytes) -> str:
    try:
        body = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "unparseable"
    if not isinstance(body, dict):
        return "unknown"
    schema = body.get("schema")
    header = body.get("header")
    if isinstance(schema, str) and isinstance(header, dict):
        event_type = header.get("event_type")
        return f"p2.{event_type}" if event_type else f"p2.{schema}"
    event = body.get("event")
    if isinstance(event, dict) and isinstance(event.get("type"), str):
        return f"p1.{event['type']}"
    return "unknown"


def _safe_preview(value: str, *, limit: int = 120) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "...[truncated]"
