import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from lark_oapi import EventDispatcherHandler, ws
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
import lark_oapi.ws.client as lark_ws_client

from app.agent.events import build_user_event
from app.agent.runner import AgentRunResult, ThreadManager
from app.config import get_settings

logger = logging.getLogger(__name__)


class FeishuChannel:
    """飞书 WebSocket 长连接通道，接收用户消息并投递给 Agent，执行完成后回传结果。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        thread_manager: ThreadManager,
        *,
        bot_name: str = "Jarvis",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._thread_manager = thread_manager
        self._bot_name = bot_name
        self._client: ws.Client | None = None
        self._event_handler: EventDispatcherHandler | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu_")
        self._running = False
        self._lock = threading.Lock()

        # HTTP client for sending messages
        self._http = httpx.Client(timeout=30.0)
        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                logger.warning("feishu channel already running")
                return
            self._running = True

        logger.info("feishu channel starting app_id=%s", self._app_id)
        try:
            # Long-connection mode does not require encrypt_key / verification_token.
            event_handler = _install_event_diagnostics(
                EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message)
                .build()
            )
            self._event_handler = event_handler
            self._stopping = False
            # lark-oapi keeps a module-level asyncio loop and Client.start()
            # calls run_until_complete() on that loop. Create the client inside
            # the worker thread after rebinding that module-level loop.
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
        """Run lark-oapi's blocking websocket client in an isolated loop."""
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

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """WebSocket 收到用户消息时的回调。"""
        try:
            event = data.event
            if event is None or event.sender is None or event.message is None:
                logger.warning("feishu message event missing sender or message data=%s", data)
                return
            sender = event.sender.sender_id.open_id
            chat_id = event.message.chat_id
            chat_type = event.message.chat_type  # "p2p" or "group"
            msg_type = event.message.message_type
            content_raw = event.message.content

            logger.info(
                "feishu message received sender=%s chat=%s chat_type=%s type=%s",
                sender,
                chat_id,
                chat_type,
                msg_type,
            )

            # Skip non-text messages for now
            if msg_type != "text":
                logger.debug("skipping non-text message type=%s", msg_type)
                return

            content = json.loads(content_raw)
            text = content.get("text", "").strip()
            logger.info(
                "feishu text parsed chat=%s chat_type=%s text_len=%s text_preview=%s",
                chat_id,
                chat_type,
                len(text),
                _safe_preview(text),
            )

            # Strip @bot mention in group chats
            if chat_type == "group":
                text = self._strip_at_bot(text)
                logger.info(
                    "feishu group text after mention strip chat=%s text_len=%s text_preview=%s",
                    chat_id,
                    len(text),
                    _safe_preview(text),
                )
                if not text:
                    logger.debug("message was only an @mention, ignoring")
                    return

            # Build AgentEvent and run in background so we do not block the
            # WebSocket heartbeat thread.
            self._executor.submit(
                self._handle_agent_run, sender, chat_id, chat_type, text
            )
        except Exception:
            logger.exception("error handling feishu message")

    @staticmethod
    def _strip_at_bot(text: str) -> str:
        """Remove @_user_xxx Feishu at-mention tags from group chat messages."""
        import re

        cleaned = re.sub(r"@_user_\d+", "", text)
        return cleaned.strip()

    def _handle_agent_run(
        self, sender_open_id: str, chat_id: str, chat_type: str, text: str
    ) -> None:
        """把用户消息转成 AgentEvent，执行 Agent，然后把结果发回飞书。"""
        # Use chat_id as thread_id so the same conversation stays in one thread
        event = build_user_event(
            instruction=text,
            thread_id=chat_id,
            user_id=sender_open_id,
        )
        # Override source so downstream knows it came from Feishu
        event.source = "feishu"  # type: ignore[assignment]

        try:
            logger.info(
                "feishu agent run starting chat=%s chat_type=%s sender=%s text_preview=%s",
                chat_id,
                chat_type,
                sender_open_id,
                _safe_preview(text),
            )
            result = self._thread_manager.run_event(event)
        except Exception:
            logger.exception("agent run failed for feishu message")
            self._send_text_message(chat_id, chat_type, "抱歉，处理消息时出错了，请稍后再试。")
            return

        logger.info(
            "feishu agent run finished chat=%s status=%s summary_len=%s",
            chat_id,
            result.status,
            len(result.summary or ""),
        )
        reply = self._format_result(result)
        self._send_text_message(chat_id, chat_type, reply)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_result(result: AgentRunResult) -> str:
        """把 AgentRunResult 格式化成飞书文本消息。"""
        if result.summary:
            return result.summary
        if result.status == "waiting_approval":
            return "任务需要审批，请在管理端处理。"
        if result.status == "blocked":
            return "任务被阻塞，无法继续执行。"
        return "任务已完成，无输出内容。"

    # ------------------------------------------------------------------
    # HTTP API: send message
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        """获取或刷新 tenant_access_token（企业内部应用）。"""
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

    def _send_text_message(
        self, receive_id: str, chat_type: str, text: str
    ) -> None:
        """通过飞书 OpenAPI 发送纯文本消息。

        Args:
            receive_id: chat_id for the conversation.
            chat_type: "p2p" or "group".
            text: Message content.
        """
        try:
            token = self._ensure_token()
            content = json.dumps({"text": text}, ensure_ascii=False)
            id_type = "chat_id"
            logger.info(
                "feishu sending message receive_id=%s receive_id_type=%s chat_type=%s text_len=%s",
                receive_id,
                id_type,
                chat_type,
                len(text),
            )
            resp = self._http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": content,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 0:
                logger.error("feishu send message failed: %s", payload)
            else:
                logger.info("feishu message sent to %s", receive_id)
        except Exception:
            logger.exception("failed to send feishu message to %s", receive_id)

    # ------------------------------------------------------------------
    # Public API used by FeishuMessageSkill
    # ------------------------------------------------------------------

    def send_message(self, receive_id: str, text: str) -> bool:
        """供 Skill 主动推送消息时调用。"""
        try:
            self._send_text_message(receive_id, "group", text)
            return True
        except Exception:
            return False


def build_feishu_channel(thread_manager: ThreadManager) -> FeishuChannel | None:
    """根据配置创建飞书通道；若缺少配置则返回 None。"""
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.info("feishu credentials not configured, skipping channel")
        return None
    return FeishuChannel(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        thread_manager=thread_manager,
        bot_name=settings.feishu_bot_name or "Jarvis",
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
