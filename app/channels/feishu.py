from __future__ import annotations

import asyncio
import base64
import http
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import httpx
import lark_oapi.ws.client as lark_ws_client
from lark_oapi import EventDispatcherHandler, ws
from lark_oapi.core.const import UTF_8
from lark_oapi.core.json import JSON
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws.client import _get_by_key
from lark_oapi.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.enum import MessageType
from lark_oapi.ws.model import Response

from app.agent_react import ChannelMessage, TurnResult
from app.api.agent import get_agent_runtime, get_conversation_store
from app.api.schemas import MessageCreateRequest, SenderInput
from app.channels.feishu_renderer import FeishuDelivery, FeishuRenderer
from app.config import get_settings
from app.tools.codex_app_server import approval_command_prefix, respond_to_codex_approval

logger = logging.getLogger(__name__)


class _JarvisFeishuWsClient(ws.Client):
    def __init__(self, *args: Any, card_payload_handler=None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._jarvis_card_payload_handler = card_payload_handler

    async def _handle_data_frame(self, frame) -> None:
        hs = frame.headers
        message_type = MessageType(_get_by_key(hs, HEADER_TYPE))
        if message_type != MessageType.CARD:
            await super()._handle_data_frame(frame)
            return

        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        payload = frame.payload
        if int(sum_) > 1:
            payload = self._combine(msg_id, int(sum_), int(seq), payload)
            if payload is None:
                return

        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            if self._jarvis_card_payload_handler is None:
                return
            result = self._jarvis_card_payload_handler(payload)
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as exc:
            logger.exception(
                "feishu ws card payload handling failed message_id=%s trace_id=%s error=%s",
                msg_id,
                trace_id,
                exc,
            )
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())


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

        self._http = httpx.Client(timeout=30.0, trust_env=False)
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
                .register_p2_card_action_trigger(self._on_card_action)
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
            client = _JarvisFeishuWsClient(
                self._app_id,
                self._app_secret,
                event_handler=self._event_handler,
                card_payload_handler=self._on_ws_card_payload,
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
                if getattr(ingest, "reset_message", None):
                    self._send_text_message(chat_id, ingest.reset_message)
                logger.info(
                    "feishu message stored without response chat=%s conversation_id=%s message_id=%s status=%s",
                    chat_id,
                    ingest.conversation_id,
                    ingest.message_id,
                    ingest.status,
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

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            value = getattr(action, "value", None) or {}
            if not isinstance(value, dict) or value.get("source") != "jarvis_codex_approval":
                return _card_action_toast("info", "已收到操作。")

            decision = str(value.get("decision") or "")
            if decision not in {"approve", "reject"}:
                return _card_action_toast("error", "未知审批操作。")

            conversation_id = _coerce_int(value.get("conversation_id"))
            turn_id = _coerce_int(value.get("turn_id"))
            approval_id = str(value.get("approval_id") or "")
            command = str(value.get("command") or "")
            reason = str(value.get("reason") or "")
            language = str(value.get("language") or _codex_approval_language(conversation_id or 0))
            context = getattr(event, "context", None)
            message_id = getattr(context, "open_message_id", None)
            chat_id = getattr(context, "open_chat_id", None) or str(value.get("chat_id") or "")
            operator = getattr(event, "operator", None)
            sender_open_id = getattr(operator, "open_id", None) or str(
                value.get("sender_open_id") or "feishu_approval"
            )
            logger.info(
                "feishu codex approval action received decision=%s conversation_id=%s "
                "turn_id=%s approval_id=%s chat_id=%s message_id=%s",
                decision,
                conversation_id,
                turn_id,
                approval_id,
                chat_id,
                message_id,
            )
            if conversation_id is None or turn_id is None:
                return _card_action_toast("error", "审批上下文缺失。")

            terminal_statuses = {"approved", "rejected", "completed", "failed", "timeout", "missing"}
            approval_status = _codex_approval_status(conversation_id, approval_id)
            if approval_status in terminal_statuses:
                delivery = self._renderer.render_approval_decision_card(
                    decision=approval_status or "completed",
                    command=command,
                    reason=reason,
                    language=language,
                )
                if message_id:
                    try:
                        self._update_card_message(message_id, delivery)
                    except Exception:
                        logger.exception("failed to refresh processed approval card message_id=%s", message_id)
                return _card_action_response("info", "该审批已处理。", delivery=delivery)

            _record_codex_approval_decision(
                conversation_id=conversation_id,
                approval_id=approval_id,
                decision="approved" if decision == "approve" else "rejected",
                decided_by=sender_open_id,
            )
            if decision == "approve":
                _remember_codex_approval_prefix(conversation_id, command)
            delivery = self._renderer.render_approval_decision_card(
                decision=decision,
                command=command,
                reason=reason,
                language=language,
            )
            if message_id:
                try:
                    self._update_card_message(message_id, delivery)
                except Exception:
                    logger.exception("failed to update approval card message_id=%s", message_id)

            content = "已同意 Codex 审批请求。" if decision == "approve" else "已拒绝 Codex 审批请求。"
            logger.info(
                "feishu codex approval decision decision=%s turn_id=%s approval_id=%s command=%s",
                decision,
                turn_id,
                approval_id,
                _safe_preview(command),
            )
            self._executor.submit(
                self._complete_codex_approval,
                chat_id,
                conversation_id,
                turn_id,
                approval_id,
                decision == "approve",
                message_id,
            )
            return _card_action_response("success", content, delivery=delivery)
        except Exception:
            logger.exception("error handling feishu card action")
            return _card_action_toast("error", "审批操作处理失败。")

    def _on_ws_card_payload(self, payload: bytes) -> Any:
        text = payload.decode(UTF_8, errors="replace")
        logger.info("feishu ws card payload received payload_preview=%s", _safe_preview(text, limit=500))
        body = json.loads(text)
        if not isinstance(body, dict):
            return _card_action_toast("error", "卡片回调格式异常。")

        if body.get("schema") and body.get("header") and self._event_handler is not None:
            return self._event_handler.do_without_validation(payload)

        action = body.get("action") if isinstance(body.get("action"), dict) else {}
        event = SimpleNamespace(
            action=SimpleNamespace(value=action.get("value") if isinstance(action, dict) else {}),
            context=SimpleNamespace(
                open_message_id=body.get("open_message_id"),
                open_chat_id=body.get("open_chat_id"),
            ),
            operator=SimpleNamespace(
                open_id=body.get("open_id"),
                user_id=body.get("user_id"),
                union_id=body.get("union_id"),
            ),
        )
        return self._on_card_action(SimpleNamespace(event=event))

    def _complete_codex_approval(
        self,
        chat_id: str,
        conversation_id: int,
        turn_id: int,
        approval_id: str,
        approved: bool,
        message_id: str | None = None,
    ) -> None:
        try:
            result = respond_to_codex_approval(
                approval_id,
                approved=approved,
                timeout_seconds=get_settings().coder_timeout_seconds,
                trusted_command_prefixes=_codex_approval_prefixes(conversation_id),
            )
            logger.info(
                "codex approval continuation finished approval_id=%s status=%s final_len=%s error=%s",
                approval_id,
                result.status,
                len(result.final_text or ""),
                _safe_preview(result.error or ""),
            )
            if result.status == "completed":
                _record_codex_approval_decision(
                    conversation_id=conversation_id,
                    approval_id=approval_id,
                    decision="completed",
                    decided_by="codex_app_server",
                )
                reply = result.final_text.strip() or "Codex completed after the approval decision."
                self._send_channel_message(chat_id, ChannelMessage(content=reply, content_type="markdown"))
                return

            if result.status == "approval_requested":
                approval = result.approval_requests[0] if result.approval_requests else {}
                next_approval_id = str(approval.get("id") or approval_id)
                next_command = str(approval.get("command") or "")
                next_reason = str(approval.get("reason") or "")
                logger.info(
                    "codex approval continuation requested next approval previous_approval_id=%s next_approval_id=%s command=%s reason=%s",
                    approval_id,
                    next_approval_id,
                    _safe_preview(next_command),
                    _safe_preview(next_reason),
                )
                _record_codex_approval(
                    conversation_id=conversation_id,
                    approval_id=next_approval_id,
                    turn_id=turn_id,
                    chat_id=chat_id,
                    command=next_command,
                    reason=next_reason,
                    language=_codex_approval_language(conversation_id),
                )
                delivery = self._renderer.render_approval_card(
                    approval_id=next_approval_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    chat_id=chat_id,
                    command=next_command,
                    reason=next_reason,
                    language=_codex_approval_language(conversation_id),
                )
                self._send_delivery(chat_id, delivery)
                return

            _record_codex_approval_decision(
                conversation_id=conversation_id,
                approval_id=approval_id,
                decision=result.status,
                decided_by="codex_app_server",
            )
            if result.status == "missing":
                message = "Codex 审批会话已失效，通常是 Jarvis 重启或审批卡过期导致。请重新发起任务。"
            else:
                message = result.error or f"Codex approval continuation ended with status: {result.status}"
            self._send_text_message(chat_id, message)
        except Exception:
            logger.exception("failed to complete codex approval approval_id=%s", approval_id)
            self._send_text_message(chat_id, "Codex approval continuation failed.")

    def _handle_agent_run(
        self,
        sender_open_id: str,
        chat_id: str,
        chat_type: str,
        text: str,
        conversation_id: int,
        turn_id: int | None,
    ) -> None:
        thinking_message_id: str | None = None
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
            thinking_message_id = self._send_thinking_card(chat_id, text)
            result = get_agent_runtime().run_turn(turn_id)
        except Exception:
            logger.exception("agent run failed for feishu message")
            if turn_id is not None:
                get_conversation_store().complete_turn(
                    turn_id,
                    status="failed",
                    error_message="agent run failed",
                )
            if thinking_message_id:
                self._update_card_message(
                    thinking_message_id,
                    self._renderer.render_error_card("Sorry, something went wrong. Please try again later."),
                )
            else:
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
        approval = _extract_codex_approval_from_reply(result.reply)
        if approval is not None:
            approval_id = approval.get("approval_id", "") or f"turn_{turn_id}"
            approval["approval_id"] = approval_id
            _record_codex_approval(
                conversation_id=conversation_id,
                approval_id=approval_id,
                turn_id=turn_id,
                chat_id=chat_id,
                command=approval.get("command", ""),
                reason=approval.get("reason", ""),
                language=_detect_approval_language(text),
            )
            delivery = self._renderer.render_approval_card(
                approval_id=approval_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                chat_id=chat_id,
                command=approval.get("command", ""),
                reason=approval.get("reason", ""),
                language=_detect_approval_language(text),
            )
            if thinking_message_id:
                self._update_card_message(thinking_message_id, delivery)
            else:
                self._send_delivery(chat_id, delivery)
            return

        message = self._format_result(result)
        if thinking_message_id:
            self._update_channel_message(thinking_message_id, message)
        else:
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

    def _update_channel_message(self, message_id: str, message: ChannelMessage) -> None:
        delivery = self._renderer.render(message)
        if delivery.msg_type != "interactive":
            raise RuntimeError("Only interactive card messages can be updated.")
        self._update_card_message(message_id, delivery)

    def _send_thinking_card(self, receive_id: str, prompt: str) -> str | None:
        try:
            payload = self._send_delivery(receive_id, self._renderer.render_thinking_card(prompt))
        except Exception:
            logger.exception("failed to send thinking card to %s", receive_id)
            return None
        return _extract_message_id(payload)

    def _send_text_message(self, receive_id: str, text: str) -> None:
        self._send_delivery(receive_id, self._renderer.render_text_fallback(text))

    def _send_delivery(self, receive_id: str, delivery: FeishuDelivery) -> dict[str, Any]:
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
        return payload

    def _update_card_message(self, message_id: str, delivery: FeishuDelivery) -> None:
        if delivery.msg_type != "interactive":
            raise RuntimeError("Only interactive card messages can be updated.")
        token = self._ensure_token()
        resp = self._http.patch(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": delivery.content},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu update message failed: {payload}")
        logger.info("feishu message updated message_id=%s", message_id)

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


def _extract_codex_approval_from_reply(reply: str) -> dict[str, str] | None:
    text = str(reply or "").strip()
    if not text.startswith("Codex requested approval"):
        return None
    approval_id = ""
    command = ""
    reason = ""
    for line in text.splitlines():
        if line.startswith("Approval ID:"):
            approval_id = line.split(":", 1)[1].strip()
        elif line.startswith("Command:"):
            command = line.split(":", 1)[1].strip()
        elif line.startswith("Reason:"):
            reason = line.split(":", 1)[1].strip()
    return {
        "approval_id": approval_id,
        "command": command,
        "reason": reason,
    }


def _record_codex_approval(
    *,
    conversation_id: int,
    approval_id: str,
    turn_id: int,
    chat_id: str,
    command: str,
    reason: str,
    language: str = "zh",
) -> None:
    patch = {
        "codex_approval_language": language,
        "codex_approvals": {
            approval_id: {
                "status": "pending",
                "turn_id": turn_id,
                "chat_id": chat_id,
                "command": command,
                "reason": reason,
                "language": language,
                "created_at": int(time.time()),
            }
        }
    }
    try:
        get_conversation_store().update_conversation_metadata(conversation_id, patch)
    except Exception:
        logger.exception("failed to record codex approval conversation_id=%s approval_id=%s", conversation_id, approval_id)


def _record_codex_approval_decision(
    *,
    conversation_id: int,
    approval_id: str,
    decision: str,
    decided_by: str,
) -> None:
    patch = {
        "codex_approvals": {
            approval_id: {
                "status": decision,
                "decided_by": decided_by,
                "decided_at": int(time.time()),
            }
        }
    }
    get_conversation_store().update_conversation_metadata(conversation_id, patch)


def _remember_codex_approval_prefix(conversation_id: int, command: str) -> None:
    prefix = approval_command_prefix(command)
    if not prefix:
        return
    prefixes = [item for item in _codex_approval_prefixes(conversation_id) if item != prefix]
    prefixes.append(prefix)
    patch = {"codex_approval_prefixes": prefixes[-50:]}
    try:
        get_conversation_store().update_conversation_metadata(conversation_id, patch)
    except Exception:
        logger.exception("failed to remember codex approval prefix conversation_id=%s prefix=%s", conversation_id, prefix)


def _codex_approval_prefixes(conversation_id: int) -> list[str]:
    try:
        conversation = get_conversation_store().get_conversation(conversation_id)
    except Exception:
        logger.exception("failed to load codex approval prefixes conversation_id=%s", conversation_id)
        return []
    if conversation is None:
        return []
    metadata = getattr(conversation, "metadata", None) or {}
    prefixes = metadata.get("codex_approval_prefixes")
    if not isinstance(prefixes, list):
        return []
    return [str(item) for item in prefixes if str(item).strip()]


def _codex_approval_status(conversation_id: int, approval_id: str) -> str | None:
    conversation = get_conversation_store().get_conversation(conversation_id)
    if conversation is None:
        return None
    metadata = getattr(conversation, "metadata", None) or {}
    approvals = metadata.get("codex_approvals")
    if not isinstance(approvals, dict):
        return None
    approval = approvals.get(approval_id)
    if not isinstance(approval, dict):
        return None
    status = approval.get("status")
    return str(status) if status else None


def _codex_approval_language(conversation_id: int) -> str:
    try:
        conversation = get_conversation_store().get_conversation(conversation_id)
    except Exception:
        logger.exception("failed to load codex approval language conversation_id=%s", conversation_id)
        return "zh"
    if conversation is None:
        return "zh"
    metadata = getattr(conversation, "metadata", None) or {}
    language = str(metadata.get("codex_approval_language") or "").strip().lower()
    return "en" if language == "en" else "zh"


def _detect_approval_language(text: str) -> str:
    value = str(text or "")
    if re.search(r"[\u4e00-\u9fff]", value):
        return "zh"
    return "en"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _card_action_toast(toast_type: str, content: str) -> P2CardActionTriggerResponse:
    return _card_action_response(toast_type, content)


def _card_action_response(
    toast_type: str,
    content: str,
    *,
    delivery: FeishuDelivery | None = None,
) -> P2CardActionTriggerResponse:
    payload: dict[str, Any] = {
        "toast": {
            "type": toast_type,
            "content": content,
        }
    }
    if delivery is not None and delivery.msg_type == "interactive":
        try:
            payload["card"] = {"type": "raw", "data": json.loads(delivery.content)}
        except json.JSONDecodeError:
            logger.exception("failed to attach synchronous feishu card action response")
    return P2CardActionTriggerResponse(
        payload
    )


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


def _extract_message_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id:
            return message_id
    return None
