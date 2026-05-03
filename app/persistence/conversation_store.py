from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import create_engine

from app.agent_react.session_state import (
    ConversationSessionState,
    dump_session_state,
    load_session_state,
    render_session_state,
)
from app.agent_react.turn_classifier import classify_turn, should_apply_session_mode_update
from app.api.schemas import (
    ConversationCreateRequest,
    ConversationMessageCreateRequest,
    MessageCreateRequest,
    MessageIngestResponse,
    SenderInput,
)
from app.config import get_settings
from app.persistence.models import (
    ConversationRecord,
    MessageRecord,
    ToolCallRecord,
    TurnRecord,
    UserRecord,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _chat_type_label(chat_type: str) -> str:
    if chat_type == "dm":
        return "私聊"
    if chat_type == "group":
        return "群聊"
    return chat_type


def _trigger_type(
    *,
    chat_type: str,
    content: str,
    mentions: list[str],
    reply_to_message_id: int | None,
    metadata: dict[str, Any],
) -> str | None:
    normalized_mentions = {mention.lower() for mention in mentions}
    text = content.strip()
    if text.startswith("/"):
        return "command"
    if chat_type in {"dm", "web", "cli"}:
        return "dm_message" if chat_type == "dm" else "manual"
    if metadata.get("always_listen"):
        return "manual"
    if "jarvis" in normalized_mentions or "@jarvis" in text.lower():
        return "mention"
    if reply_to_message_id is not None and metadata.get("reply_to_bot"):
        return "reply_to_bot"
    return None


def _turn_type(content: str) -> str:
    text = content.lower()
    if any(marker in text for marker in ("research", "调研", "研究")):
        return "research"
    if any(marker in text for marker in ("code", "代码", "重构", "bug")):
        return "coding"
    if any(marker in text for marker in ("总结", "summary", "summarize")):
        return "summary"
    if any(marker in text for marker in ("画图", "image", "图片")):
        return "image_generation"
    if text.strip().startswith("/"):
        return "command"
    return "chat"


class MySQLConversationStore:
    """MySQL-backed conversation store for V1 multi-turn dialogue."""

    def __init__(self) -> None:
        settings = get_settings()
        url = (
            f"mysql+pymysql://{settings.mysql_user}:{settings.mysql_password}"
            f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
            f"?charset=utf8mb4"
        )
        self._engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
        logger.info("mysql store initialized host=%s db=%s", settings.mysql_host, settings.mysql_database)
        self._reset_stale_turns()

    def _reset_stale_turns(self) -> None:
        """Mark any leftover 'running' turns as failed on startup."""
        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    sa.text(
                        "UPDATE turns SET status = 'failed', completed_at = NOW(), "
                        "error_message = 'Server restarted while turn was running.' "
                        "WHERE status = 'running'"
                    )
                )
                if result.rowcount:
                    logger.warning("reset %s stale running turn(s) to failed", result.rowcount)
        except Exception:
            logger.exception("failed to reset stale running turns")

    def create_conversation(self, request: ConversationCreateRequest) -> ConversationRecord:
        with self._engine.begin() as conn:
            created_by_user_id = self._ensure_user(conn, request.platform, request.created_by) if request.created_by else None
            return self._ensure_conversation(
                conn,
                platform=request.platform,
                external_chat_id=request.external_chat_id,
                chat_type=request.chat_type,
                title=request.title,
                created_by_user_id=created_by_user_id,
                metadata=request.metadata,
            )

    def get_conversation(self, conversation_id: int) -> ConversationRecord | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            ).mappings().one_or_none()
            return self._conv_from_row(row) if row else None

    def update_conversation_session(
        self,
        conversation_id: int,
        session_state: ConversationSessionState,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE conversations "
                    "SET metadata = JSON_MERGE_PATCH(COALESCE(metadata, '{}'), :patch), "
                    "updated_at = :now "
                    "WHERE id = :id"
                ),
                {
                    "patch": json.dumps(dump_session_state(session_state)),
                    "now": _now(),
                    "id": conversation_id,
                },
            )

    def list_messages(self, conversation_id: int) -> list[MessageRecord]:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM messages WHERE conversation_id = :cid ORDER BY created_at ASC"
                ),
                {"cid": conversation_id},
            ).mappings().all()
            return [self._msg_from_row(r) for r in rows]

    def list_turns(self, conversation_id: int) -> list[TurnRecord]:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM turns WHERE conversation_id = :cid ORDER BY created_at ASC"
                ),
                {"cid": conversation_id},
            ).mappings().all()
            return [self._turn_from_row(r) for r in rows]

    def get_turn(self, turn_id: int) -> TurnRecord | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM turns WHERE id = :id"),
                {"id": turn_id},
            ).mappings().one_or_none()
            return self._turn_from_row(row) if row else None

    def cancel_turn(self, turn_id: int) -> TurnRecord | None:
        with self._engine.begin() as conn:
            turn = self.get_turn(turn_id)
            if turn is None:
                return None
            if turn.status not in {"completed", "failed", "cancelled"}:
                conn.execute(
                    sa.text(
                        "UPDATE turns SET status = :status, completed_at = :now WHERE id = :id"
                    ),
                    {"status": "cancelled", "now": _now(), "id": turn_id},
                )
                turn.status = "cancelled"
                turn.completed_at = _now()
            return turn

    def append_assistant_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "markdown",
        external_message_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> MessageRecord:
        with self._engine.begin() as conn:
            return self._append_message(
                conn,
                conversation_id=conversation_id,
                turn_id=turn_id,
                sender_type="assistant",
                user_id=None,
                role="assistant",
                content=content,
                content_type=content_type,
                external_message_id=external_message_id,
                reply_to_message_id=None,
                raw_payload=raw_payload or {},
            )

    def append_tool_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "text",
        external_message_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> MessageRecord:
        with self._engine.begin() as conn:
            return self._append_message(
                conn,
                conversation_id=conversation_id,
                turn_id=turn_id,
                sender_type="tool",
                user_id=None,
                role="tool",
                content=content,
                content_type=content_type,
                external_message_id=external_message_id,
                reply_to_message_id=None,
                raw_payload=raw_payload or {},
            )

    def mark_turn_running(self, turn_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE turns SET status = :status WHERE id = :id AND status = :queued"
                ),
                {"status": "running", "id": turn_id, "queued": "queued"},
            )

    def complete_turn(
        self,
        turn_id: int,
        *,
        status: str,
        summary: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _now()
        metadata_update: dict[str, Any] = {}
        if summary is not None:
            metadata_update["summary"] = summary
        with self._engine.begin() as conn:
            if metadata_update:
                # merge JSON: existing metadata updated with summary
                conn.execute(
                    sa.text(
                        "UPDATE turns SET status = :status, completed_at = :now, "
                        "error_message = :error, metadata = JSON_MERGE_PATCH(COALESCE(metadata, '{}'), :patch) "
                        "WHERE id = :id"
                    ),
                    {
                        "status": status,
                        "now": now,
                        "error": error_message,
                        "patch": json.dumps(metadata_update),
                        "id": turn_id,
                    },
                )
            else:
                conn.execute(
                    sa.text(
                        "UPDATE turns SET status = :status, completed_at = :now, "
                        "error_message = :error WHERE id = :id"
                    ),
                    {
                        "status": status,
                        "now": now,
                        "error": error_message,
                        "id": turn_id,
                    },
                    )

    def finalize_turn_success(
        self,
        *,
        turn_id: int,
        conversation_id: int,
        content: str,
        content_type: str = "markdown",
        raw_payload: dict[str, Any] | None = None,
    ) -> MessageRecord:
        now = _now()
        with self._engine.begin() as conn:
            message = self._append_message(
                conn,
                conversation_id=conversation_id,
                turn_id=turn_id,
                sender_type="assistant",
                user_id=None,
                role="assistant",
                content=content,
                content_type=content_type,
                external_message_id=None,
                reply_to_message_id=None,
                raw_payload=raw_payload or {},
            )
            patch = json.dumps({"summary": content})
            conn.execute(
                sa.text(
                    "UPDATE turns SET status = :status, completed_at = :now, "
                    "error_message = NULL, metadata = JSON_MERGE_PATCH(COALESCE(metadata, '{}'), :patch) "
                    "WHERE id = :id"
                ),
                {
                    "status": "completed",
                    "now": now,
                    "patch": patch,
                    "id": turn_id,
                },
            )
            return message

    def finalize_turn_failure(
        self,
        turn_id: int,
        *,
        status: str = "failed",
        error_message: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE turns SET status = :status, completed_at = :now, "
                    "error_message = :error WHERE id = :id"
                ),
                {
                    "status": status,
                    "now": _now(),
                    "error": error_message,
                    "id": turn_id,
                },
            )

    # ------------------------------------------------------------------
    # tool_calls
    # ------------------------------------------------------------------

    def create_tool_call(
        self,
        *,
        turn_id: int,
        tool_name: str,
        input: dict[str, Any],
        assistant_message_id: int | None = None,
        provider_tool_call_id: str | None = None,
        step_index: int = 0,
    ) -> ToolCallRecord:
        now = _now()
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT INTO tool_calls "
                    "(turn_id, tool_name, assistant_message_id, provider_tool_call_id, step_index, status, input, started_at, created_at) "
                    "VALUES (:turn_id, :tool_name, :assistant_message_id, :provider_tool_call_id, :step_index, 'requested', :input, :now, :now)"
                ),
                {
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "assistant_message_id": assistant_message_id,
                    "provider_tool_call_id": provider_tool_call_id,
                    "step_index": step_index,
                    "input": json.dumps(input),
                    "now": now,
                },
            )
            return ToolCallRecord(
                id=result.lastrowid,  # type: ignore[arg-type]
                turn_id=turn_id,
                tool_name=tool_name,
                assistant_message_id=assistant_message_id,
                provider_tool_call_id=provider_tool_call_id,
                step_index=step_index,
                status="requested",
                input=dict(input),
                output=None,
                error_message=None,
                started_at=now,
                finished_at=None,
                created_at=now,
            )

    def update_tool_call(
        self,
        tool_call_id: int,
        *,
        status: str | None = None,
        output: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> ToolCallRecord | None:
        now = _now()
        with self._engine.begin() as conn:
            existing = conn.execute(
                sa.text("SELECT * FROM tool_calls WHERE id = :id"),
                {"id": tool_call_id},
            ).mappings().one_or_none()
            if existing is None:
                return None

            updates: list[str] = []
            params: dict[str, Any] = {"id": tool_call_id, "now": now}
            if status is not None:
                updates.append("status = :status")
                params["status"] = status
            if output is not None:
                updates.append("output = :output")
                params["output"] = json.dumps(output)
            if error_message is not None:
                updates.append("error_message = :error")
                params["error"] = error_message
            if status in {"completed", "failed", "cancelled", "rejected"}:
                updates.append("finished_at = :now")

            if updates:
                sql = "UPDATE tool_calls SET " + ", ".join(updates) + " WHERE id = :id"
                conn.execute(sa.text(sql), params)

            row = conn.execute(
                sa.text("SELECT * FROM tool_calls WHERE id = :id"),
                {"id": tool_call_id},
            ).mappings().one()
            return self._tool_call_from_row(row)

    def list_tool_calls_by_turn(self, turn_id: int) -> list[ToolCallRecord]:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM tool_calls WHERE turn_id = :tid ORDER BY created_at ASC"
                ),
                {"tid": turn_id},
            ).mappings().all()
            return [self._tool_call_from_row(r) for r in rows]

    def ingest_message(self, request: MessageCreateRequest) -> MessageIngestResponse:
        with self._engine.begin() as conn:
            user_id = self._ensure_user(conn, request.platform, request.sender)
            conversation = self._ensure_conversation(
                conn,
                platform=request.platform,
                external_chat_id=request.external_chat_id,
                chat_type=request.chat_type,
                title=None,
                created_by_user_id=user_id,
                metadata={},
            )

            # Intercept DM commands before creating a turn.
            if conversation.chat_type == "dm" and request.content.strip().startswith("/"):
                return self._handle_command(
                    conn,
                    conversation=conversation,
                    user_id=user_id,
                    request=request,
                )

            return self._create_message_and_maybe_turn(
                conn,
                conversation=conversation,
                user_id=user_id,
                content=request.content,
                content_type=request.content_type,
                external_message_id=request.external_message_id,
                reply_to_message_id=self._resolve_reply_message_id(
                    conn,
                    conversation.id,
                    request.reply_to_message_id,
                    request.reply_to_external_message_id,
                ),
                mentions=request.mentions,
                raw_payload=request.raw_payload,
                metadata=request.metadata,
            )

    def ingest_conversation_message(
        self,
        conversation_id: int,
        request: ConversationMessageCreateRequest,
    ) -> MessageIngestResponse:
        with self._engine.begin() as conn:
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                raise KeyError(conversation_id)
            user_id = self._ensure_user(conn, conversation.platform, request.sender)
            return self._create_message_and_maybe_turn(
                conn,
                conversation=conversation,
                user_id=user_id,
                content=request.content,
                content_type=request.content_type,
                external_message_id=request.external_message_id,
                reply_to_message_id=self._resolve_reply_message_id(
                    conn,
                    conversation.id,
                    request.reply_to_message_id,
                    request.reply_to_external_message_id,
                ),
                mentions=request.mentions,
                raw_payload=request.raw_payload,
                metadata=request.metadata,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_user(self, conn: sa.Connection, platform: str, sender: SenderInput) -> int:
        key = (platform, sender.platform_user_id)
        row = conn.execute(
            sa.text(
                "SELECT id, display_name, metadata FROM users WHERE platform = :platform AND external_user_id = :eid"
            ),
            {"platform": platform, "eid": sender.platform_user_id},
        ).mappings().one_or_none()
        now = _now()
        if row is not None:
            user_id = row["id"]
            new_meta = {**json.loads(row["metadata"] or "{}"), **sender.metadata}
            new_display = sender.display_name or row["display_name"]
            conn.execute(
                sa.text(
                    "UPDATE users SET display_name = :dn, metadata = :meta, updated_at = :now WHERE id = :id"
                ),
                {"dn": new_display, "meta": json.dumps(new_meta), "now": now, "id": user_id},
            )
            return user_id

        result = conn.execute(
            sa.text(
                "INSERT INTO users (platform, external_user_id, display_name, metadata, created_at, updated_at) "
                "VALUES (:platform, :eid, :dn, :meta, :now, :now)"
            ),
            {
                "platform": platform,
                "eid": sender.platform_user_id,
                "dn": sender.display_name,
                "meta": json.dumps(sender.metadata),
                "now": now,
            },
        )
        return result.lastrowid  # type: ignore[return-value]

    def _ensure_conversation(
        self,
        conn: sa.Connection,
        *,
        platform: str,
        external_chat_id: str,
        chat_type: str,
        title: str | None,
        created_by_user_id: int | None,
        metadata: dict[str, Any],
    ) -> ConversationRecord:
        row = conn.execute(
            sa.text(
                "SELECT * FROM conversations WHERE platform = :platform AND external_chat_id = :eid AND status = 'active' "
                "ORDER BY clear_generation DESC LIMIT 1"
            ),
            {"platform": platform, "eid": external_chat_id},
        ).mappings().one_or_none()
        now = _now()
        if row is not None:
            conv_id = row["id"]
            new_meta = {**json.loads(row["metadata"] or "{}"), **metadata}
            new_title = title or row["title"]
            conn.execute(
                sa.text(
                    "UPDATE conversations SET title = :title, metadata = :meta, updated_at = :now WHERE id = :id"
                ),
                {"title": new_title, "meta": json.dumps(new_meta), "now": now, "id": conv_id},
            )
            # re-fetch to get updated row
            row = conn.execute(
                sa.text("SELECT * FROM conversations WHERE id = :id"),
                {"id": conv_id},
            ).mappings().one()
            return self._conv_from_row(row)

        result = conn.execute(
            sa.text(
                "INSERT INTO conversations (platform, external_chat_id, chat_type, title, owner_user_id, created_by_user_id, "
                "status, clear_generation, metadata, created_at, updated_at) "
                "VALUES (:platform, :eid, :ctype, :title, :owner, :created_by, 'active', 0, :meta, :now, :now)"
            ),
            {
                "platform": platform,
                "eid": external_chat_id,
                "ctype": chat_type,
                "title": title,
                "owner": created_by_user_id if chat_type == "dm" else None,
                "created_by": created_by_user_id,
                "meta": json.dumps(metadata),
                "now": now,
            },
        )
        new_id = result.lastrowid
        row = conn.execute(
            sa.text("SELECT * FROM conversations WHERE id = :id"),
            {"id": new_id},
        ).mappings().one()
        return self._conv_from_row(row)

    def _create_message_and_maybe_turn(
        self,
        conn: sa.Connection,
        *,
        conversation: ConversationRecord,
        user_id: int,
        content: str,
        content_type: str,
        external_message_id: str | None,
        reply_to_message_id: int | None,
        mentions: list[str],
        raw_payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> MessageIngestResponse:
        # Duplicate guard by external_message_id
        if external_message_id:
            dup = conn.execute(
                sa.text(
                    "SELECT id, turn_id FROM messages WHERE conversation_id = :cid AND external_message_id = :eid"
                ),
                {"cid": conversation.id, "eid": external_message_id},
            ).mappings().one_or_none()
            if dup is not None:
                turn_id = dup["turn_id"]
                trigger_type = None
                if turn_id:
                    trow = conn.execute(
                        sa.text("SELECT trigger_type FROM turns WHERE id = :id"),
                        {"id": turn_id},
                    ).mappings().one_or_none()
                    trigger_type = trow["trigger_type"] if trow else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=dup["id"],
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        trigger_type = _trigger_type(
            chat_type=conversation.chat_type,
            content=content,
            mentions=mentions,
            reply_to_message_id=reply_to_message_id,
            metadata=metadata,
        )
        should_respond = trigger_type is not None

        message = self._append_message(
            conn,
            conversation_id=conversation.id,
            turn_id=None,
            sender_type="user",
            user_id=user_id,
            role="user",
            content=content,
            content_type=content_type,
            external_message_id=external_message_id,
            reply_to_message_id=reply_to_message_id,
            raw_payload=dict(raw_payload),
        )

        turn_id: int | None = None
        if should_respond:
            classification = classify_turn(
                content=content,
                session_state=load_session_state(conversation.metadata),
            )
            if should_apply_session_mode_update(classification) and classification.session_mode_update is not None:
                session_state = replace(
                    load_session_state(conversation.metadata),
                    session_mode=classification.session_mode_update,
                )
                session_patch = dump_session_state(session_state)
                conversation.metadata = {**conversation.metadata, **session_patch}
                conn.execute(
                    sa.text(
                        "UPDATE conversations "
                        "SET metadata = JSON_MERGE_PATCH(COALESCE(metadata, '{}'), :patch), "
                        "updated_at = :now "
                        "WHERE id = :id"
                    ),
                    {
                        "patch": json.dumps(session_patch),
                        "now": _now(),
                        "id": conversation.id,
                    },
                )
            turn_id = self._create_turn(
                conn,
                conversation_id=conversation.id,
                trigger_message_id=message.id,
                trigger_type=trigger_type,
                turn_type=classification.turn_type,
                started_by_user_id=user_id,
                mentions=mentions,
                classification={
                    "source": classification.source,
                    "confidence": classification.confidence,
                    "reason": classification.reason,
                    "session_mode_update": classification.session_mode_update,
                },
            )
            conn.execute(
                sa.text("UPDATE messages SET turn_id = :turn_id WHERE id = :msg_id"),
                {"turn_id": turn_id, "msg_id": message.id},
            )
            message.turn_id = turn_id

        conn.execute(
            sa.text("UPDATE conversations SET updated_at = :now, last_message_at = :now WHERE id = :id"),
            {"now": _now(), "id": conversation.id},
        )

        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=turn_id,
            should_respond=should_respond,
            trigger_type=trigger_type,
            status="queued" if should_respond else "stored",
        )

    def _handle_command(
        self,
        conn: sa.Connection,
        *,
        conversation: ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        """Dispatch DM commands. Unknown commands fall through to normal turn creation."""
        cmd = request.content.strip().lower().split(maxsplit=1)[0]
        if cmd == "/clear":
            return self._handle_clear_command(
                conn, conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/cancel":
            return self._handle_cancel_command(
                conn, conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/status":
            return self._handle_status_command(
                conn, conversation=conversation, user_id=user_id, request=request
            )
        # Unknown command: fall through to normal turn creation.
        return self._create_message_and_maybe_turn(
            conn,
            conversation=conversation,
            user_id=user_id,
            content=request.content,
            content_type=request.content_type,
            external_message_id=request.external_message_id,
            reply_to_message_id=self._resolve_reply_message_id(
                conn,
                conversation.id,
                request.reply_to_message_id,
                request.reply_to_external_message_id,
            ),
            mentions=request.mentions,
            raw_payload=request.raw_payload,
            metadata=request.metadata,
        )

    def _handle_clear_command(
        self,
        conn: sa.Connection,
        *,
        conversation: ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        """Archive the current conversation and create a new generation.

        Idempotent across conversations for the same external_message_id.
        Rejects if a turn is currently running.
        """
        # Idempotency: same external_message_id should not trigger multiple clears.
        if request.external_message_id:
            existing = conn.execute(
                sa.text(
                    "SELECT 1 FROM messages m "
                    "JOIN conversations c ON m.conversation_id = c.id "
                    "WHERE c.platform = :platform AND c.external_chat_id = :eid AND m.external_message_id = :mid "
                    "LIMIT 1"
                ),
                {
                    "platform": request.platform,
                    "eid": request.external_chat_id,
                    "mid": request.external_message_id,
                },
            ).mappings().one_or_none()
            if existing is not None:
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=0,
                    turn_id=None,
                    should_respond=False,
                    trigger_type="command",
                    status="duplicate",
                )

        # Reject if any turn is currently running.
        running = conn.execute(
            sa.text(
                "SELECT id FROM turns WHERE conversation_id = :cid AND status = 'running' LIMIT 1"
            ),
            {"cid": conversation.id},
        ).mappings().one_or_none()
        if running is not None:
            return MessageIngestResponse(
                conversation_id=conversation.id,
                message_id=0,
                turn_id=None,
                should_respond=False,
                trigger_type="command",
                status="reset",
                reset_message="当前对话正在生成中，请稍后再试。",
            )

        now = _now()

        # Record the /clear command in the old conversation for audit.
        clear_msg = self._append_message(
            conn,
            conversation_id=conversation.id,
            turn_id=None,
            sender_type="user",
            user_id=user_id,
            role="user",
            content=request.content,
            content_type=request.content_type,
            external_message_id=request.external_message_id,
            reply_to_message_id=None,
            raw_payload=dict(request.raw_payload),
        )

        # Archive old conversation.
        conn.execute(
            sa.text(
                "UPDATE conversations SET status = 'archived', updated_at = :now WHERE id = :id"
            ),
            {"now": now, "id": conversation.id},
        )

        # Create new conversation with incremented generation.
        new_gen = conversation.clear_generation + 1
        result = conn.execute(
            sa.text(
                "INSERT INTO conversations (platform, external_chat_id, chat_type, title, owner_user_id, created_by_user_id, "
                "status, clear_generation, metadata, created_at, updated_at) "
                "VALUES (:platform, :eid, :ctype, :title, :owner, :created_by, 'active', :gen, :meta, :now, :now)"
            ),
            {
                "platform": conversation.platform,
                "eid": conversation.external_chat_id,
                "ctype": conversation.chat_type,
                "title": conversation.title,
                "owner": conversation.owner_user_id,
                "created_by": conversation.created_by_user_id,
                "gen": new_gen,
                "meta": json.dumps({"cleared_from_conversation_id": conversation.id}),
                "now": now,
            },
        )
        new_conv_id = result.lastrowid

        # Audit system message in the new conversation.
        audit_content = f"Conversation cleared from {conversation.id} by user {user_id} at {now}"
        self._append_message(
            conn,
            conversation_id=new_conv_id,
            turn_id=None,
            sender_type="system",
            user_id=None,
            role="system",
            content=audit_content,
            content_type="text",
            external_message_id=None,
            reply_to_message_id=None,
            raw_payload={"source": "clear_command", "previous_conversation_id": conversation.id},
        )

        return MessageIngestResponse(
            conversation_id=new_conv_id,
            message_id=clear_msg.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="reset",
            reset_message="已开始新对话。",
        )

    def _handle_cancel_command(
        self,
        conn: sa.Connection,
        *,
        conversation: ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        # Duplicate guard within the current conversation.
        if request.external_message_id:
            dup = conn.execute(
                sa.text(
                    "SELECT id, turn_id FROM messages WHERE conversation_id = :cid AND external_message_id = :eid"
                ),
                {"cid": conversation.id, "eid": request.external_message_id},
            ).mappings().one_or_none()
            if dup is not None:
                turn_id = dup["turn_id"]
                trigger_type = None
                if turn_id:
                    trow = conn.execute(
                        sa.text("SELECT trigger_type FROM turns WHERE id = :id"),
                        {"id": turn_id},
                    ).mappings().one_or_none()
                    trigger_type = trow["trigger_type"] if trow else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=dup["id"],
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        now = _now()

        # Record the /cancel command.
        message = self._append_message(
            conn,
            conversation_id=conversation.id,
            turn_id=None,
            sender_type="user",
            user_id=user_id,
            role="user",
            content=request.content,
            content_type=request.content_type,
            external_message_id=request.external_message_id,
            reply_to_message_id=None,
            raw_payload=dict(request.raw_payload),
        )

        running_rows = conn.execute(
            sa.text(
                "SELECT id FROM turns WHERE conversation_id = :cid AND status = 'running'"
            ),
            {"cid": conversation.id},
        ).mappings().all()

        if running_rows:
            for row in running_rows:
                conn.execute(
                    sa.text(
                        "UPDATE turns SET status = 'cancelled', completed_at = :now WHERE id = :id"
                    ),
                    {"now": now, "id": row["id"]},
                )
            reply = "已取消当前生成。"
        else:
            reply = "没有正在进行的对话。"

        conn.execute(
            sa.text("UPDATE conversations SET updated_at = :now WHERE id = :id"),
            {"now": now, "id": conversation.id},
        )

        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="cancelled",
            reset_message=reply,
        )

    def _handle_status_command(
        self,
        conn: sa.Connection,
        *,
        conversation: ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        # Duplicate guard within the current conversation.
        if request.external_message_id:
            dup = conn.execute(
                sa.text(
                    "SELECT id, turn_id FROM messages WHERE conversation_id = :cid AND external_message_id = :eid"
                ),
                {"cid": conversation.id, "eid": request.external_message_id},
            ).mappings().one_or_none()
            if dup is not None:
                turn_id = dup["turn_id"]
                trigger_type = None
                if turn_id:
                    trow = conn.execute(
                        sa.text("SELECT trigger_type FROM turns WHERE id = :id"),
                        {"id": turn_id},
                    ).mappings().one_or_none()
                    trigger_type = trow["trigger_type"] if trow else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=dup["id"],
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        now = _now()

        # Record the /status command.
        message = self._append_message(
            conn,
            conversation_id=conversation.id,
            turn_id=None,
            sender_type="user",
            user_id=user_id,
            role="user",
            content=request.content,
            content_type=request.content_type,
            external_message_id=request.external_message_id,
            reply_to_message_id=None,
            raw_payload=dict(request.raw_payload),
        )

        msg_count = conn.execute(
            sa.text("SELECT COUNT(*) AS cnt FROM messages WHERE conversation_id = :cid"),
            {"cid": conversation.id},
        ).mappings().one()["cnt"]

        turn_stats = conn.execute(
            sa.text(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running, "
                "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled "
                "FROM turns WHERE conversation_id = :cid"
            ),
            {"cid": conversation.id},
        ).mappings().one()

        settings = get_settings()
        provider = settings.llm_provider
        model = getattr(settings, f"{provider}_model", "unknown")
        session_report = render_session_state(load_session_state(conversation.metadata))

        running_count = turn_stats["running"] or 0
        status_label = "执行中" if running_count > 0 else "空闲"

        reply = (
            f"{session_report}\n"
            f"---\n"
            f"当前会话\n"
            f"类型: {_chat_type_label(conversation.chat_type)}\n"
            f"状态: {status_label}\n"
            f"消息数: {msg_count}\n"
            f"会话代数: {conversation.clear_generation}\n"
            f"最近活跃: {_fmt_time(conversation.updated_at)}\n"
            f"---\n"
            f"系统参数\n"
            f"App: {settings.app_name} ({settings.environment})\n"
            f"LLM: {provider} / {model}\n"
            f"超时: {settings.llm_timeout_seconds}s\n"
            f"Bot: {settings.feishu_bot_name}"
        )

        conn.execute(
            sa.text("UPDATE conversations SET updated_at = :now WHERE id = :id"),
            {"now": now, "id": conversation.id},
        )

        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="status_report",
            reset_message=reply,
        )

    def _create_turn(
        self,
        conn: sa.Connection,
        *,
        conversation_id: int,
        trigger_message_id: int,
        trigger_type: str,
        turn_type: str,
        started_by_user_id: int,
        mentions: list[str],
        classification: dict[str, Any] | None = None,
    ) -> int:
        now = _now()
        metadata = {"mentions": mentions}
        if classification is not None:
            metadata["classification"] = classification
        result = conn.execute(
            sa.text(
                "INSERT INTO turns (conversation_id, trigger_message_id, trigger_type, status, turn_type, "
                "started_by_user_id, started_at, metadata, created_at, updated_at) "
                "VALUES (:cid, :mid, :ttype, 'queued', :turn_type, :user_id, :now, :meta, :now, :now)"
            ),
            {
                "cid": conversation_id,
                "mid": trigger_message_id,
                "ttype": trigger_type,
                "turn_type": turn_type,
                "user_id": started_by_user_id,
                "now": now,
                "meta": json.dumps(metadata),
            },
        )
        return result.lastrowid  # type: ignore[return-value]

    def _append_message(
        self,
        conn: sa.Connection,
        *,
        conversation_id: int,
        turn_id: int | None,
        sender_type: str,
        user_id: int | None,
        role: str,
        content: str,
        content_type: str,
        external_message_id: str | None,
        reply_to_message_id: int | None,
        raw_payload: dict[str, Any],
    ) -> MessageRecord:
        now = _now()
        result = conn.execute(
            sa.text(
                "INSERT INTO messages (conversation_id, turn_id, sender_type, user_id, role, content, "
                "content_type, external_message_id, reply_to_message_id, raw_payload, created_at) "
                "VALUES (:cid, :tid, :stype, :uid, :role, :content, :ctype, :eid, :reply, :raw, :now)"
            ),
            {
                "cid": conversation_id,
                "tid": turn_id,
                "stype": sender_type,
                "uid": user_id,
                "role": role,
                "content": content,
                "ctype": content_type,
                "eid": external_message_id,
                "reply": reply_to_message_id,
                "raw": json.dumps(raw_payload),
                "now": now,
            },
        )
        msg_id = result.lastrowid
        return MessageRecord(
            id=msg_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            sender_type=sender_type,
            user_id=user_id,
            role=role,
            content=content,
            content_type=content_type,
            external_message_id=external_message_id,
            reply_to_message_id=reply_to_message_id,
            raw_payload=dict(raw_payload),
            created_at=now,
        )

    def _resolve_reply_message_id(
        self,
        conn: sa.Connection,
        conversation_id: int,
        reply_to_message_id: int | None,
        reply_to_external_message_id: str | None,
    ) -> int | None:
        if reply_to_message_id is not None:
            return reply_to_message_id
        if reply_to_external_message_id:
            row = conn.execute(
                sa.text(
                    "SELECT id FROM messages WHERE conversation_id = :cid AND external_message_id = :eid"
                ),
                {"cid": conversation_id, "eid": reply_to_external_message_id},
            ).mappings().one_or_none()
            return row["id"] if row else None
        return None

    # ------------------------------------------------------------------
    # Row mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _conv_from_row(row: sa.RowMapping) -> ConversationRecord:
        return ConversationRecord(
            id=row["id"],
            platform=row["platform"],
            external_chat_id=row["external_chat_id"],
            chat_type=row["chat_type"],
            title=row["title"],
            status=row["status"],
            clear_generation=row["clear_generation"],
            owner_user_id=row["owner_user_id"],
            created_by_user_id=row["created_by_user_id"],
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    @staticmethod
    def _msg_from_row(row: sa.RowMapping) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            sender_type=row["sender_type"],
            user_id=row["user_id"],
            role=row["role"],
            content=row["content"] or "",
            content_type=row["content_type"],
            external_message_id=row["external_message_id"],
            reply_to_message_id=row["reply_to_message_id"],
            raw_payload=json.loads(row["raw_payload"] or "{}"),
            created_at=_iso(row["created_at"]),
        )

    @staticmethod
    def _tool_call_from_row(row: sa.RowMapping) -> ToolCallRecord:
        return ToolCallRecord(
            id=row["id"],
            turn_id=row["turn_id"],
            tool_name=row["tool_name"],
            assistant_message_id=row.get("assistant_message_id"),
            provider_tool_call_id=row.get("provider_tool_call_id"),
            step_index=row.get("step_index") or 0,
            status=row["status"],
            input=json.loads(row["input"] or "{}"),
            output=json.loads(row["output"]) if row["output"] else None,
            error_message=row["error_message"],
            started_at=_iso(row["started_at"]) if row["started_at"] else None,
            finished_at=_iso(row["finished_at"]) if row["finished_at"] else None,
            created_at=_iso(row["created_at"]),
        )

    @staticmethod
    def _turn_from_row(row: sa.RowMapping) -> TurnRecord:
        return TurnRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            trigger_message_id=row["trigger_message_id"],
            trigger_type=row["trigger_type"],
            status=row["status"],
            turn_type=row["turn_type"],
            started_by_user_id=row["started_by_user_id"],
            started_at=_iso(row["started_at"]),
            completed_at=_iso(row["completed_at"]) if row["completed_at"] else None,
            error_message=row["error_message"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
