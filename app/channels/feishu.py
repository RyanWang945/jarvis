from __future__ import annotations

import asyncio
import base64
import http
import inspect
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
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

from app.agent_react import ChannelAttachment, ChannelMessage, TurnResult
from app.agent_react.delivery import DeliveryManager, register_delivery_handler
from app.api.agent import get_agent_runtime, get_conversation_store
from app.channels.feishu_progress import FeishuCardKitProgressSink, FeishuProgressSink
from app.channels.feishu_renderer import FeishuDelivery, FeishuRenderer
from app.config import get_settings
from app.gateway import InboundEvent, get_gateway_service
from app.progress import NoopProgressReporter, ProgressEvent, ProgressReporter
from app.persistence.models import DeliveryRecord
from app.task_runtime.coder_provider import resume_coder_approval
from app.tools.codex_app_server import approval_command_prefix

logger = logging.getLogger(__name__)

_FEISHU_NO_PROXY_HOSTS = (
    "open.feishu.cn",
    "msg-frontier.feishu.cn",
    ".feishu.cn",
)


@dataclass
class _ArtifactDeliveryState:
    status: str
    upload_key: str | None = None
    external_message_id: str | None = None
    error_message: str | None = None


_PROGRESS_ACTIVATION_EVENTS = {
    "planning_started",
    "plan_created",
    "node_started",
    "node_completed",
    "node_failed",
    "aggregation_started",
    "aggregation_completed",
}


class _LazyFeishuProgressReporter(ProgressReporter):
    """Create the visible Feishu progress entry only after a planned path starts."""

    def __init__(self, channel: "FeishuChannel", receive_id: str, prompt: str) -> None:
        super().__init__([])
        self._channel = channel
        self._receive_id = receive_id
        self._prompt = prompt
        self._message_id: str | None = None
        self._sink: FeishuProgressSink | None = None
        self._closed = False

    @property
    def message_id(self) -> str | None:
        return self._message_id

    def emit(self, event_type: str, **payload: Any) -> None:
        if self._closed:
            return
        if self._message_id is None:
            if event_type not in _PROGRESS_ACTIVATION_EVENTS:
                return
            self._message_id = self._channel._send_progress_entry_card(self._receive_id, self._prompt)
            if self._message_id is None:
                return
            self._sink = self._channel._progress_sink_for(self._message_id)
        if self._sink is not None:
            self._sink.on_progress(ProgressEvent(event_type=event_type, **payload))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sink is not None:
            self._sink.close()


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
        self._artifact_deliveries: dict[tuple[str, str], _ArtifactDeliveryState] = {}
        self._cardkit_progress_message_ids: set[str] = set()
        self._progress_sinks: dict[str, FeishuProgressSink] = {}
        self.channel = "feishu"

    def start(self) -> None:
        with self._lock:
            if self._running:
                logger.warning("feishu channel already running")
                return
            self._running = True

        logger.info("feishu channel starting app_id=%s", self._app_id)
        try:
            register_delivery_handler(self)
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
        _ensure_feishu_no_proxy()
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

            gateway_result = get_gateway_service().handle_inbound_event(
                InboundEvent(
                    platform="feishu",
                    external_chat_id=chat_id,
                    external_message_id=message_id,
                    chat_type=_conversation_chat_type(chat_type),
                    sender_id=sender,
                    sender_name=None,
                    text=text or raw_text,
                    mentions=mentions,
                    reply_to_external_message_id=parent_id or root_id,
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
            if not gateway_result.should_run_agent:
                if gateway_result.immediate_reply:
                    self._send_text_message(chat_id, gateway_result.immediate_reply)
                logger.info(
                    "feishu message stored without response chat=%s conversation_id=%s message_id=%s status=%s",
                    chat_id,
                    gateway_result.conversation_id,
                    gateway_result.message_id,
                    gateway_result.status,
                )
                return

            self._executor.submit(
                self._handle_agent_run,
                sender,
                chat_id,
                chat_type,
                text or raw_text,
                gateway_result.conversation_id,
                gateway_result.turn_id,
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
        drain_after = True
        try:
            result = resume_coder_approval(
                approval_id,
                approved=approved,
                timeout_seconds=get_settings().coder_timeout_seconds,
                provider="codex",
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
                next_approval_id = _approval_value(approval, "approval_id") or _approval_value(approval, "id") or approval_id
                next_command = _approval_value(approval, "command")
                next_reason = _approval_value(approval, "reason")
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
                drain_after = False
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
        finally:
            if drain_after:
                self._submit_next_queued_turn(conversation_id, chat_id)

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
        progress: ProgressReporter | None = None
        drain_after = True
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
            runtime = get_agent_runtime()
            if _runtime_accepts_progress(runtime):
                progress = _LazyFeishuProgressReporter(self, chat_id, text)
                result = _run_turn_with_optional_progress(runtime, turn_id, progress)
                thinking_message_id = progress.message_id if isinstance(progress, _LazyFeishuProgressReporter) else None
            else:
                thinking_message_id = self._send_progress_entry_card(chat_id, text)
                progress = self._progress_reporter_for(thinking_message_id)
                result = _run_turn_with_optional_progress(runtime, turn_id, progress)
            progress.close()
        except Exception:
            if progress is not None:
                progress.close()
                thinking_message_id = thinking_message_id or getattr(progress, "message_id", None)
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
            if drain_after:
                self._submit_next_queued_turn(conversation_id, chat_id)
            return

        logger.info(
            "feishu agent run finished chat=%s status=%s summary_len=%s",
            chat_id,
            result.status,
            len(result.reply),
        )
        if result.status == "failed":
            error_text = result.reply.strip() or "抱歉，调用模型时出错了，请稍后再试。"
            if thinking_message_id:
                self._update_card_message(
                    thinking_message_id,
                    self._renderer.render_error_card(error_text),
                )
            else:
                self._send_text_message(chat_id, error_text)
            if drain_after:
                self._submit_next_queued_turn(conversation_id, chat_id)
            return

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
            self._send_message_attachments(chat_id, result.message)
            drain_after = False
            return

        message = self._format_result(result)
        if thinking_message_id:
            self._update_channel_message(thinking_message_id, message)
            self._send_message_attachments(chat_id, message)
        else:
            self._send_channel_message(chat_id, message)
        if drain_after:
            self._submit_next_queued_turn(conversation_id, chat_id)

    def _progress_reporter_for(self, thinking_message_id: str | None) -> ProgressReporter:
        sink = self._progress_sink_for(thinking_message_id)
        if sink is None:
            return NoopProgressReporter()
        return ProgressReporter([sink])

    def _progress_sink_for(self, thinking_message_id: str | None) -> FeishuProgressSink | None:
        settings = get_settings()
        if not thinking_message_id or not settings.feishu_progress_updates_enabled:
            return None
        mode = _progress_mode(settings.feishu_progress_mode)
        logger.info(
            "feishu progress enabled mode=%s message_id=%s min_interval_seconds=%s",
            mode,
            thinking_message_id,
            settings.feishu_progress_min_interval_seconds,
        )
        sink_class = FeishuCardKitProgressSink if mode == "cardkit" else FeishuProgressSink
        sink = sink_class(
            message_id=thinking_message_id,
            renderer=self._renderer,
            update_card=self._update_card_message,
            min_interval_seconds=settings.feishu_progress_min_interval_seconds,
            max_recent_events=settings.feishu_progress_max_recent_events,
            flush_on_close=False,
        )
        self._progress_sinks[thinking_message_id] = sink
        return sink

    def _submit_next_queued_turn(self, conversation_id: int, chat_id: str) -> None:
        try:
            store = get_conversation_store()
            claim_next = getattr(store, "claim_next_queued_turn", None)
            if claim_next is None:
                return
            turn = claim_next(conversation_id)
            if turn is None:
                return
            prompt = self._prompt_for_turn(store, conversation_id, turn)
            conversation = store.get_conversation(conversation_id)
            chat_type = getattr(conversation, "chat_type", "")
            logger.info(
                "feishu queued turn claimed conversation_id=%s turn_id=%s",
                conversation_id,
                turn.id,
            )
            self._executor.submit(
                self._handle_agent_run,
                "queued",
                chat_id,
                chat_type,
                prompt,
                conversation_id,
                turn.id,
            )
        except Exception:
            logger.exception("failed to submit next queued turn conversation_id=%s", conversation_id)

    @staticmethod
    def _prompt_for_turn(store: Any, conversation_id: int, turn: Any) -> str:
        trigger_message_id = getattr(turn, "trigger_message_id", None)
        if trigger_message_id is not None:
            for message in store.list_messages(conversation_id):
                if getattr(message, "id", None) == trigger_message_id:
                    return str(getattr(message, "content", "") or "")
        return "(queued turn)"

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
        sent_main = False
        try:
            self._send_delivery(receive_id, delivery)
            sent_main = True
        except Exception:
            logger.exception(
                "failed to send feishu delivery receive_id=%s msg_type=%s, retrying text fallback",
                receive_id,
                delivery.msg_type,
            )
            fallback = self._renderer.render_text_fallback(message.content)
            try:
                self._send_delivery(receive_id, fallback)
                sent_main = True
            except Exception:
                logger.exception("failed to send feishu fallback text to %s", receive_id)
        if sent_main:
            self._send_message_attachments(receive_id, message)

    def _update_channel_message(self, message_id: str, message: ChannelMessage) -> None:
        if message_id in self._cardkit_progress_message_ids:
            sink = self._progress_sinks.get(message_id)
            snapshot = sink.snapshot if sink is not None else _initial_progress_snapshot()
            delivery = self._renderer.render_cardkit_progress_card(snapshot, output_markdown=message.content)
        else:
            delivery = self._renderer.render(message)
        if delivery.msg_type != "interactive":
            raise RuntimeError("Only interactive card messages can be updated.")
        self._update_card_message(message_id, delivery)
        # Thinking-card updates do not carry chat_id. The caller sends attachments
        # after update when it has the receive_id available.

    def _send_thinking_card(self, receive_id: str, prompt: str) -> str | None:
        try:
            payload = self._send_delivery(receive_id, self._renderer.render_thinking_card(prompt))
        except Exception:
            logger.exception("failed to send thinking card to %s", receive_id)
            return None
        return _extract_message_id(payload)

    def _send_progress_entry_card(self, receive_id: str, prompt: str) -> str | None:
        settings = get_settings()
        if settings.feishu_progress_updates_enabled and _progress_mode(settings.feishu_progress_mode) == "cardkit":
            try:
                payload = self._send_delivery(receive_id, self._renderer.render_cardkit_progress_card(_initial_progress_snapshot()))
                message_id = _extract_message_id(payload)
                if message_id:
                    self._cardkit_progress_message_ids.add(message_id)
                return message_id
            except Exception:
                logger.exception("failed to send cardkit progress card to %s, falling back to thinking card", receive_id)
        return self._send_thinking_card(receive_id, prompt)

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

    def _send_message_attachments(self, receive_id: str, message: ChannelMessage) -> None:
        try:
            store = get_conversation_store()
        except Exception:
            logger.exception("delivery store unavailable; falling back to in-memory attachment send")
            store = self
        manager = DeliveryManager(store, self)
        metadata = message.metadata or {}
        if _coerce_int(metadata.get("conversation_id")) is None:
            store = self
            manager = DeliveryManager(store, self)
        try:
            manager.deliver_attachments(
                external_chat_id=receive_id,
                attachments=message.attachments,
                conversation_id=_coerce_int(metadata.get("conversation_id")),
                turn_id=_coerce_int(metadata.get("turn_id")),
                purpose=str(metadata.get("delivery_purpose") or "auto"),
            )
        except Exception:
            if store is self:
                raise
            logger.exception("persistent delivery manager failed; retrying with in-memory delivery state")
            DeliveryManager(self, self).deliver_attachments(
                external_chat_id=receive_id,
                attachments=message.attachments,
                conversation_id=_coerce_int(metadata.get("conversation_id")),
                turn_id=_coerce_int(metadata.get("turn_id")),
                purpose=str(metadata.get("delivery_purpose") or "auto"),
            )

    def upload_attachment(self, attachment: ChannelAttachment) -> str | None:
        if attachment.kind != "image":
            raise RuntimeError(f"unsupported attachment kind for feishu: {attachment.kind}")
        return self._upload_image(attachment)

    def send_attachment(self, external_chat_id: str, attachment: ChannelAttachment, upload_key: str | None) -> str | None:
        if attachment.kind != "image":
            raise RuntimeError(f"unsupported attachment kind for feishu: {attachment.kind}")
        if not upload_key:
            raise RuntimeError("image upload key is required")
        payload = self._send_delivery(
            external_chat_id,
            FeishuDelivery(
                msg_type="image",
                content=json.dumps({"image_key": upload_key}, ensure_ascii=False),
            ),
        )
        return _extract_message_id(payload)

    def send_failure_notice(self, external_chat_id: str, attachment: ChannelAttachment, error_message: str) -> None:
        self._send_text_message(
            external_chat_id,
            f"图片已生成到本地，但上传飞书失败：{attachment.filename}\n本地路径：{attachment.path}",
        )

    def find_sent_delivery(
        self,
        *,
        channel: str,
        external_chat_id: str,
        artifact_id: str,
        purposes: tuple[str, ...],
    ) -> DeliveryRecord | None:
        state = self._artifact_deliveries.get((external_chat_id, artifact_id))
        if state is None or state.status != "sent":
            return None
        return DeliveryRecord(
            id=0,
            delivery_id=f"memory:{external_chat_id}:{artifact_id}",
            artifact_id=artifact_id,
            conversation_id=None,
            turn_id=None,
            channel=channel,
            external_chat_id=external_chat_id,
            purpose=purposes[0] if purposes else "auto",
            status="sent",
            upload_key=state.upload_key,
            external_message_id=state.external_message_id,
            error_message=state.error_message,
            attempt_count=1,
            created_at="",
            updated_at="",
        )

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
    ) -> DeliveryRecord:
        self._artifact_deliveries[(external_chat_id, artifact_id)] = _ArtifactDeliveryState(status=status)
        return DeliveryRecord(
            id=0,
            delivery_id=delivery_id,
            artifact_id=artifact_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            channel=channel,
            external_chat_id=external_chat_id,
            purpose=purpose,
            status=status,
            upload_key=None,
            external_message_id=None,
            error_message=None,
            attempt_count=1,
            created_at="",
            updated_at="",
        )

    def mark_delivery_uploaded(self, delivery_id: str, *, upload_key: str) -> None:
        self._update_memory_delivery(delivery_id, status="uploaded", upload_key=upload_key)

    def mark_delivery_sent(
        self,
        delivery_id: str,
        *,
        upload_key: str | None = None,
        external_message_id: str | None = None,
    ) -> None:
        self._update_memory_delivery(delivery_id, status="sent", upload_key=upload_key, external_message_id=external_message_id)

    def mark_delivery_failed(self, delivery_id: str, *, error_message: str) -> None:
        self._update_memory_delivery(delivery_id, status="failed", error_message=error_message)

    def _update_memory_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        upload_key: str | None = None,
        external_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        prefix, _, suffix = delivery_id.partition(":")
        if prefix != "memory" or not suffix:
            # DeliveryManager-generated UUIDs are not reversible. Use the only
            # pending in-memory delivery when running without the persistent store.
            keys = [key for key, value in self._artifact_deliveries.items() if value.status in {"pending", "uploaded"}]
            if not keys:
                return
            key = keys[-1]
        else:
            chat_id, _, artifact_id = suffix.partition(":")
            key = (chat_id, artifact_id)
        previous = self._artifact_deliveries.get(key)
        self._artifact_deliveries[key] = _ArtifactDeliveryState(
            status=status,
            upload_key=upload_key or (previous.upload_key if previous else None),
            external_message_id=external_message_id or (previous.external_message_id if previous else None),
            error_message=error_message or (previous.error_message if previous else None),
        )

    def _send_image_attachment(self, receive_id: str, attachment: ChannelAttachment) -> None:
        delivery_key = (receive_id, attachment.artifact_id)
        state = self._artifact_deliveries.get(delivery_key)
        if state is not None and state.status == "sent":
            logger.info(
                "feishu attachment delivery skipped receive_id=%s artifact_id=%s reason=already_sent",
                receive_id,
                attachment.artifact_id,
            )
            return

        image_key = state.upload_key if state is not None and state.status == "uploaded" else None
        if not image_key:
            logger.info(
                "feishu attachment upload starting receive_id=%s artifact_id=%s path=%s",
                receive_id,
                attachment.artifact_id,
                attachment.path,
            )
            image_key = self._upload_image(attachment)
            self._artifact_deliveries[delivery_key] = _ArtifactDeliveryState(
                status="uploaded",
                upload_key=image_key,
            )
            logger.info(
                "feishu attachment upload completed receive_id=%s artifact_id=%s image_key=%s",
                receive_id,
                attachment.artifact_id,
                image_key,
            )

        payload = self._send_delivery(
            receive_id,
            FeishuDelivery(
                msg_type="image",
                content=json.dumps({"image_key": image_key}, ensure_ascii=False),
            ),
        )
        self._artifact_deliveries[delivery_key] = _ArtifactDeliveryState(
            status="sent",
            upload_key=image_key,
            external_message_id=_extract_message_id(payload),
        )

    def _upload_image(self, attachment: ChannelAttachment) -> str:
        token = self._ensure_token()
        path = Path(attachment.path)
        with path.open("rb") as fh:
            resp = self._http.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (attachment.filename, fh, attachment.mime_type)},
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"status_code": resp.status_code, "body": resp.text}
        if resp.status_code >= 400:
            log_id = resp.headers.get("x-tt-logid") or resp.headers.get("X-Tt-Logid")
            raise RuntimeError(f"feishu image upload http_error status={resp.status_code} log_id={log_id} payload={payload}")
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu image upload failed: {payload}")
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("image_key"):
            raise RuntimeError(f"feishu image upload missing image_key: {payload}")
        return str(data["image_key"])

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

    def send_reminder(self, *, platform: str, external_chat_id: str, text: str) -> str | None:
        if platform != "feishu":
            raise ValueError(f"unsupported reminder platform: {platform}")
        payload = self._send_delivery(external_chat_id, self._renderer.render_text_fallback(text))
        return _extract_message_id(payload)


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


def _run_turn_with_optional_progress(runtime: Any, turn_id: int, progress: ProgressReporter) -> TurnResult:
    if _runtime_accepts_progress(runtime):
        return runtime.run_turn(turn_id, progress=progress)
    return runtime.run_turn(turn_id)


def _runtime_accepts_progress(runtime: Any) -> bool:
    run_turn = runtime.run_turn
    try:
        signature = inspect.signature(run_turn)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    return "progress" in parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _progress_mode(value: str) -> str:
    return "cardkit" if str(value or "").strip().lower() in {"cardkit", "cardkit_v2", "json2"} else "patch"


def _initial_progress_snapshot() -> Any:
    return SimpleNamespace(
        current_stage="准备中",
        current_action="正在理解请求",
        completed_items=[],
        recent_events=["已收到请求"],
        node_total=None,
        node_completed=0,
        status="running",
    )


def _ensure_feishu_no_proxy() -> None:
    """Keep lark_oapi's long-lived Feishu WebSocket off generic HTTP proxies."""
    values: list[str] = []
    lowered: set[str] = set()
    for env_name in ("NO_PROXY", "no_proxy"):
        for item in os.environ.get(env_name, "").split(","):
            host = item.strip()
            if host and host.lower() not in lowered:
                values.append(host)
                lowered.add(host.lower())
    for host in _FEISHU_NO_PROXY_HOSTS:
        if host.lower() not in lowered:
            values.append(host)
            lowered.add(host.lower())
    merged = ",".join(values)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


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


def _approval_value(approval: Any, key: str) -> str:
    if isinstance(approval, dict):
        return str(approval.get(key) or "").strip()
    return str(getattr(approval, key, "") or "").strip()


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
