from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from threading import RLock
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.agent_react.session_state import (
    ConversationSessionState,
    dump_session_state,
    load_session_state,
    render_session_state,
)
from app.api.schemas import (
    ConversationCreateRequest,
    ConversationMessageCreateRequest,
    ConversationResponse,
    MessageCreateRequest,
    MessageIngestResponse,
    MessageResponse,
    RunTurnResponse,
    SenderInput,
    TurnResponse,
)
from app.config import get_settings
from app.llm.model_profiles import model_command_response, render_model_status, runtime_preferences_metadata
from app.persistence.models import (
    ArtifactRecord as _ArtifactRecord,
    ConversationRecord as _ConversationRecord,
    DeliveryRecord as _DeliveryRecord,
    MessageRecord as _MessageRecord,
    ToolCallRecord as _ToolCallRecord,
    TurnRecord as _TurnRecord,
    UserRecord as _UserRecord,
)
from app.persistence.conversation_store import MySQLConversationStore, _task_runtime_ingest_classification
from app.repositories import render_repository_report
from app.task_runtime import TaskAgentRuntime

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation-runtime"])
from app.persistence.models import (
    ConversationRecord as _ConversationRecord,
    MessageRecord as _MessageRecord,
    TurnRecord as _TurnRecord,
    UserRecord as _UserRecord,
)
from app.persistence.conversation_store import MySQLConversationStore

router = APIRouter(tags=["conversation-runtime"])


class InMemoryConversationStore:
    """Temporary in-process store for the V1 conversation API contract.

    The next implementation step is replacing this store with the MySQL
    repository described in the v3 design.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_user_id = 1
        self._next_conversation_id = 1
        self._next_message_id = 1
        self._next_turn_id = 1
        self._next_tool_call_id = 1
        self._next_artifact_id = 1
        self._next_delivery_id = 1
        self._users: dict[int, _UserRecord] = {}
        self._users_by_external: dict[tuple[str, str], int] = {}
        self._conversations: dict[int, _ConversationRecord] = {}
        self._active_conversations: dict[tuple[str, str], int] = {}
        self._messages: dict[int, _MessageRecord] = {}
        self._messages_by_external: dict[tuple[int, str], int] = {}
        self._turns: dict[int, _TurnRecord] = {}
        self._tool_calls: dict[int, _ToolCallRecord] = {}
        self._artifacts: dict[str, _ArtifactRecord] = {}
        self._deliveries: dict[str, _DeliveryRecord] = {}

    def create_conversation(self, request: ConversationCreateRequest) -> _ConversationRecord:
        with self._lock:
            created_by_user_id = self._ensure_user_locked(request.platform, request.created_by) if request.created_by else None
            return self._ensure_conversation_locked(
                platform=request.platform,
                external_chat_id=request.external_chat_id,
                chat_type=request.chat_type,
                title=request.title,
                created_by_user_id=created_by_user_id,
                metadata=request.metadata,
            )

    def get_conversation(self, conversation_id: int) -> _ConversationRecord | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def update_conversation_session(
        self,
        conversation_id: int,
        session_state: ConversationSessionState,
    ) -> None:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return
            conversation.metadata = {
                **conversation.metadata,
                **dump_session_state(session_state),
            }
            conversation.updated_at = _now()

    def update_conversation_metadata(self, conversation_id: int, patch: dict[str, Any]) -> None:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                raise KeyError(conversation_id)
            conversation.metadata = _merge_metadata_patch(conversation.metadata, patch)
            conversation.updated_at = _now()

    def list_messages(self, conversation_id: int) -> list[_MessageRecord]:
        with self._lock:
            if conversation_id not in self._conversations:
                raise KeyError(conversation_id)
            return [
                message
                for message in sorted(self._messages.values(), key=lambda item: item.created_at)
                if message.conversation_id == conversation_id
            ]

    def list_turns(self, conversation_id: int) -> list[_TurnRecord]:
        with self._lock:
            if conversation_id not in self._conversations:
                raise KeyError(conversation_id)
            return [
                turn
                for turn in sorted(self._turns.values(), key=lambda item: item.started_at)
                if turn.conversation_id == conversation_id
            ]

    def claim_next_queued_turn(self, conversation_id: int) -> _TurnRecord | None:
        with self._lock:
            queued = [
                turn
                for turn in self._turns.values()
                if turn.conversation_id == conversation_id and turn.status == "queued"
            ]
            if not queued:
                return None
            turn = sorted(queued, key=lambda item: (item.started_at, item.id))[0]
            turn.status = "running"
            return turn

    def get_turn(self, turn_id: int) -> _TurnRecord | None:
        with self._lock:
            return self._turns.get(turn_id)

    def cancel_turn(self, turn_id: int) -> _TurnRecord | None:
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return None
            if turn.status not in {"completed", "failed", "cancelled"}:
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
    ) -> _MessageRecord:
        with self._lock:
            if conversation_id not in self._conversations:
                raise KeyError(conversation_id)
            if turn_id is not None and turn_id not in self._turns:
                raise KeyError(turn_id)
            return self._append_message_locked(
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
    ) -> _MessageRecord:
        with self._lock:
            if conversation_id not in self._conversations:
                raise KeyError(conversation_id)
            if turn_id is not None and turn_id not in self._turns:
                raise KeyError(turn_id)
            return self._append_message_locked(
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
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is not None and turn.status == "queued":
                turn.status = "running"

    def complete_turn(
        self,
        turn_id: int,
        *,
        status: str,
        summary: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return
            turn.status = status
            turn.completed_at = _now()
            turn.error_message = error_message
            if summary is not None:
                turn.metadata = {**turn.metadata, "summary": summary}

    def finalize_turn_success(
        self,
        *,
        turn_id: int,
        conversation_id: int,
        content: str,
        content_type: str = "markdown",
        raw_payload: dict[str, Any] | None = None,
    ) -> _MessageRecord:
        with self._lock:
            message = self._append_message_locked(
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
            turn = self._turns.get(turn_id)
            if turn is not None:
                turn.status = "completed"
                turn.completed_at = _now()
                turn.error_message = None
                turn.metadata = {**turn.metadata, "summary": content}
            return message

    def finalize_turn_failure(
        self,
        turn_id: int,
        *,
        status: str = "failed",
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return
            turn.status = status
            turn.completed_at = _now()
            turn.error_message = error_message

    def create_tool_call(
        self,
        *,
        turn_id: int,
        tool_name: str,
        input: dict[str, Any],
        assistant_message_id: int | None = None,
        provider_tool_call_id: str | None = None,
        step_index: int = 0,
    ) -> _ToolCallRecord:
        with self._lock:
            record_id = self._next_tool_call_id
            self._next_tool_call_id += 1
            record = _ToolCallRecord(
                id=record_id,
                turn_id=turn_id,
                tool_name=tool_name,
                assistant_message_id=assistant_message_id,
                provider_tool_call_id=provider_tool_call_id,
                step_index=step_index,
                status="requested",
                input=dict(input),
                output=None,
                error_message=None,
                started_at=_now(),
                finished_at=None,
                created_at=_now(),
            )
            self._tool_calls[record_id] = record
            return record

    def update_tool_call(
        self,
        tool_call_id: int,
        *,
        status: str | None = None,
        output: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> _ToolCallRecord | None:
        with self._lock:
            record = self._tool_calls.get(tool_call_id)
            if record is None:
                return None
            if status is not None:
                record.status = status
            if output is not None:
                record.output = dict(output)
            if error_message is not None:
                record.error_message = error_message
            if status in {"completed", "failed", "cancelled", "rejected"}:
                record.finished_at = _now()
            return record

    def list_tool_calls_by_turn(self, turn_id: int) -> list[_ToolCallRecord]:
        with self._lock:
            return [
                record
                for record in sorted(self._tool_calls.values(), key=lambda item: item.created_at)
                if record.turn_id == turn_id
            ]

    def upsert_artifact(self, artifact, *, conversation_id: int) -> _ArtifactRecord:
        with self._lock:
            now = _now()
            existing = self._artifacts.get(artifact.artifact_id)
            record_id = existing.id if existing is not None else self._next_artifact_id
            created_at = existing.created_at if existing is not None else now
            if existing is None:
                self._next_artifact_id += 1
            record = _ArtifactRecord(
                id=record_id,
                artifact_id=artifact.artifact_id,
                conversation_id=conversation_id,
                turn_id=artifact.turn_id,
                tool_call_id=artifact.tool_call_id,
                source_tool=artifact.source_tool,
                kind=artifact.kind,
                path=artifact.path,
                mime_type=artifact.mime_type,
                filename=artifact.filename,
                size_bytes=artifact.size_bytes,
                status="available",
                metadata=dict(artifact.metadata),
                created_at=created_at,
                updated_at=now,
            )
            self._artifacts[artifact.artifact_id] = record
            return record

    def get_artifact(self, artifact_id: str) -> _ArtifactRecord | None:
        with self._lock:
            return self._artifacts.get(artifact_id)

    def list_artifacts_by_turn(self, turn_id: int) -> list[_ArtifactRecord]:
        with self._lock:
            return [
                record
                for record in sorted(self._artifacts.values(), key=lambda item: item.created_at)
                if record.turn_id == turn_id
            ]

    def list_recent_artifacts_by_conversation(self, conversation_id: int, *, limit: int = 5) -> list[_ArtifactRecord]:
        safe_limit = max(1, min(int(limit or 5), 20))
        with self._lock:
            return [
                record
                for record in sorted(self._artifacts.values(), key=lambda item: item.updated_at, reverse=True)
                if record.conversation_id == conversation_id and record.status == "available"
            ][:safe_limit]

    def update_artifact_status(self, artifact_id: str, *, status: str, metadata_patch: dict[str, Any] | None = None) -> None:
        with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None:
                return
            record.status = status
            if metadata_patch:
                record.metadata = _merge_metadata_patch(record.metadata, metadata_patch)
            record.updated_at = _now()

    def find_sent_delivery(
        self,
        *,
        channel: str,
        external_chat_id: str,
        artifact_id: str,
        purposes: tuple[str, ...],
    ) -> _DeliveryRecord | None:
        with self._lock:
            matches = [
                record
                for record in self._deliveries.values()
                if record.channel == channel
                and record.external_chat_id == external_chat_id
                and record.artifact_id == artifact_id
                and record.purpose in purposes
                and record.status == "sent"
            ]
            if not matches:
                return None
            return sorted(matches, key=lambda item: item.updated_at)[-1]

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
    ) -> _DeliveryRecord:
        with self._lock:
            now = _now()
            record = _DeliveryRecord(
                id=self._next_delivery_id,
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
                created_at=now,
                updated_at=now,
            )
            self._next_delivery_id += 1
            self._deliveries[delivery_id] = record
            return record

    def mark_delivery_uploaded(self, delivery_id: str, *, upload_key: str) -> None:
        self._update_delivery(delivery_id, status="uploaded", upload_key=upload_key)

    def mark_delivery_sent(
        self,
        delivery_id: str,
        *,
        upload_key: str | None = None,
        external_message_id: str | None = None,
    ) -> None:
        self._update_delivery(delivery_id, status="sent", upload_key=upload_key, external_message_id=external_message_id)

    def mark_delivery_failed(self, delivery_id: str, *, error_message: str) -> None:
        self._update_delivery(delivery_id, status="failed", error_message=error_message)

    def _update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        upload_key: str | None = None,
        external_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            record = self._deliveries.get(delivery_id)
            if record is None:
                return
            record.status = status
            if upload_key is not None:
                record.upload_key = upload_key
            if external_message_id is not None:
                record.external_message_id = external_message_id
            if error_message is not None:
                record.error_message = error_message
            record.updated_at = _now()

    def ingest_message(self, request: MessageCreateRequest) -> MessageIngestResponse:
        with self._lock:
            user_id = self._ensure_user_locked(request.platform, request.sender)
            conversation = self._ensure_conversation_locked(
                platform=request.platform,
                external_chat_id=request.external_chat_id,
                chat_type=request.chat_type,
                title=None,
                created_by_user_id=user_id,
                metadata={},
            )

            if conversation.chat_type == "dm" and request.content.strip().startswith("/"):
                return self._handle_command_locked(
                    conversation=conversation,
                    user_id=user_id,
                    request=request,
                )

            return self._create_message_and_maybe_turn_locked(
                conversation=conversation,
                user_id=user_id,
                content=request.content,
                content_type=request.content_type,
                external_message_id=request.external_message_id,
                reply_to_message_id=self._resolve_reply_message_id_locked(
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
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                raise KeyError(conversation_id)
            user_id = self._ensure_user_locked(conversation.platform, request.sender)
            return self._create_message_and_maybe_turn_locked(
                conversation=conversation,
                user_id=user_id,
                content=request.content,
                content_type=request.content_type,
                external_message_id=request.external_message_id,
                reply_to_message_id=self._resolve_reply_message_id_locked(
                    conversation.id,
                    request.reply_to_message_id,
                    request.reply_to_external_message_id,
                ),
                mentions=request.mentions,
                raw_payload=request.raw_payload,
                metadata=request.metadata,
            )

    def _ensure_user_locked(self, platform: str, sender: SenderInput) -> int:
        key = (platform, sender.platform_user_id)
        existing_id = self._users_by_external.get(key)
        now = _now()
        if existing_id is not None:
            user = self._users[existing_id]
            user.display_name = sender.display_name or user.display_name
            user.updated_at = now
            user.metadata = {**user.metadata, **sender.metadata}
            return existing_id

        user_id = self._next_user_id
        self._next_user_id += 1
        self._users[user_id] = _UserRecord(
            id=user_id,
            platform=platform,
            external_user_id=sender.platform_user_id,
            display_name=sender.display_name,
            created_at=now,
            updated_at=now,
            metadata=dict(sender.metadata),
        )
        self._users_by_external[key] = user_id
        return user_id

    def _ensure_conversation_locked(
        self,
        *,
        platform: str,
        external_chat_id: str,
        chat_type: str,
        title: str | None,
        created_by_user_id: int | None,
        metadata: dict[str, Any],
    ) -> _ConversationRecord:
        key = (platform, external_chat_id)
        existing_id = self._active_conversations.get(key)
        now = _now()
        if existing_id is not None:
            conversation = self._conversations[existing_id]
            conversation.title = title or conversation.title
            conversation.updated_at = now
            conversation.metadata = {**conversation.metadata, **metadata}
            return conversation

        conversation_id = self._next_conversation_id
        self._next_conversation_id += 1
        conversation = _ConversationRecord(
            id=conversation_id,
            platform=platform,
            external_chat_id=external_chat_id,
            chat_type=chat_type,
            title=title,
            status="active",
            clear_generation=0,
            owner_user_id=created_by_user_id if chat_type == "dm" else None,
            created_by_user_id=created_by_user_id,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata),
        )
        self._conversations[conversation_id] = conversation
        self._active_conversations[key] = conversation_id
        return conversation

    def _create_message_and_maybe_turn_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        content: str,
        content_type: str,
        external_message_id: str | None,
        reply_to_message_id: int | None,
        mentions: list[str],
        raw_payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> MessageIngestResponse:
        if external_message_id:
            existing_message_id = self._find_message_by_external_chat_locked(conversation, external_message_id)
            if existing_message_id is not None:
                message = self._messages[existing_message_id]
                return MessageIngestResponse(
                    conversation_id=message.conversation_id,
                    message_id=message.id,
                    turn_id=message.turn_id,
                    should_respond=False,
                    trigger_type=self._turns[message.turn_id].trigger_type if message.turn_id else None,
                    status="duplicate",
                )

        trigger_type = _trigger_type(
            chat_type=conversation.chat_type,
            content=content,
            mentions=mentions,
            reply_to_message_id=reply_to_message_id,
            metadata=metadata,
        )
        suppress_turn = bool(metadata.get("suppress_turn"))
        should_respond = trigger_type is not None and not suppress_turn
        active_turn_exists = should_respond and self._has_active_turn_locked(conversation.id)

        message = self._append_message_locked(
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
            original_session_state = load_session_state(conversation.metadata)
            turn_type, classification_metadata, session_state = _task_runtime_ingest_classification(
                content,
                original_session_state,
            )
            logger.info(
                "turn lightweight-classified conversation_id=%s turn_type=%s classification=%s",
                conversation.id,
                turn_type,
                json.dumps(classification_metadata, ensure_ascii=False),
            )
            if session_state != original_session_state:
                conversation.metadata = {
                    **conversation.metadata,
                    **dump_session_state(session_state),
                }
            turn_id = self._next_turn_id
            self._next_turn_id += 1
            turn = _TurnRecord(
                id=turn_id,
                conversation_id=conversation.id,
                trigger_message_id=message.id,
                trigger_type=trigger_type,
                status="queued",
                turn_type=turn_type,
                started_by_user_id=user_id,
                started_at=_now(),
                metadata={
                    "mentions": mentions,
                    "classification": classification_metadata,
                },
            )
            self._turns[turn_id] = turn
            message.turn_id = turn_id

        conversation.updated_at = _now()
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=turn_id,
            should_respond=should_respond and not active_turn_exists,
            trigger_type=trigger_type,
            status="queued" if should_respond else "stored",
            reset_message=self._queued_message_locked(conversation.id) if active_turn_exists else None,
        )

    def _handle_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        """Dispatch DM commands. Unknown commands fall through to normal turn creation."""
        cmd = request.content.strip().lower().split(maxsplit=1)[0]
        if cmd == "/clear":
            return self._handle_clear_command_locked(
                conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/cancel":
            return self._handle_cancel_command_locked(
                conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/status":
            return self._handle_status_command_locked(
                conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/model":
            return self._handle_model_command_locked(
                conversation=conversation, user_id=user_id, request=request
            )
        if cmd == "/repos":
            return self._handle_repos_command_locked(
                conversation=conversation, user_id=user_id, request=request
            )
        # Unknown command: fall through to normal turn creation.
        return self._create_message_and_maybe_turn_locked(
            conversation=conversation,
            user_id=user_id,
            content=request.content,
            content_type=request.content_type,
            external_message_id=request.external_message_id,
            reply_to_message_id=self._resolve_reply_message_id_locked(
                conversation.id,
                request.reply_to_message_id,
                request.reply_to_external_message_id,
            ),
            mentions=request.mentions,
            raw_payload=request.raw_payload,
            metadata=request.metadata,
        )

    def _handle_clear_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        # Idempotency across any conversation for this chat.
        if request.external_message_id:
            for msg in self._messages.values():
                conv = self._conversations.get(msg.conversation_id)
                if (
                    conv
                    and conv.platform == conversation.platform
                    and conv.external_chat_id == conversation.external_chat_id
                    and msg.external_message_id == request.external_message_id
                ):
                    return MessageIngestResponse(
                        conversation_id=conversation.id,
                        message_id=msg.id,
                        turn_id=msg.turn_id,
                        should_respond=msg.turn_id is not None,
                        trigger_type=self._turns[msg.turn_id].trigger_type if msg.turn_id else None,
                        status="duplicate",
                    )

        # Reject if any turn is currently running.
        running = any(
            turn.conversation_id == conversation.id and turn.status == "running"
            for turn in self._turns.values()
        )
        if running:
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

        # Record /clear in old conversation.
        clear_msg = self._append_message_locked(
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
        conversation.status = "archived"
        conversation.updated_at = now
        key = (conversation.platform, conversation.external_chat_id)
        if self._active_conversations.get(key) == conversation.id:
            del self._active_conversations[key]

        # Create new conversation.
        new_conv_id = self._next_conversation_id
        self._next_conversation_id += 1
        new_metadata = {
            **runtime_preferences_metadata(conversation.metadata),
            "cleared_from_conversation_id": conversation.id,
        }
        new_conv = _ConversationRecord(
            id=new_conv_id,
            platform=conversation.platform,
            external_chat_id=conversation.external_chat_id,
            chat_type=conversation.chat_type,
            title=conversation.title,
            status="active",
            clear_generation=conversation.clear_generation + 1,
            owner_user_id=conversation.owner_user_id,
            created_by_user_id=conversation.created_by_user_id,
            created_at=now,
            updated_at=now,
            metadata=new_metadata,
        )
        self._conversations[new_conv_id] = new_conv
        self._active_conversations[key] = new_conv_id

        # Audit system message.
        audit_content = f"Conversation cleared from {conversation.id} by user {user_id} at {now}"
        self._append_message_locked(
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

    def _has_active_turn_locked(self, conversation_id: int) -> bool:
        return any(
            turn.conversation_id == conversation_id and turn.status in {"running", "queued"}
            for turn in self._turns.values()
        )

    def _queued_message_locked(self, conversation_id: int) -> str:
        queued_count = sum(
            1
            for turn in self._turns.values()
            if turn.conversation_id == conversation_id and turn.status == "queued"
        )
        if queued_count > 1:
            return f"已排队，前面还有 {queued_count - 1} 个任务，当前任务完成后继续处理。"
        return "已排队，当前任务完成后继续处理。"

    def _handle_cancel_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        # Duplicate guard within the current conversation.
        if request.external_message_id:
            existing_id = self._messages_by_external.get((conversation.id, request.external_message_id))
            if existing_id is not None:
                msg = self._messages[existing_id]
                turn_id = msg.turn_id
                trigger_type = self._turns[turn_id].trigger_type if turn_id else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=msg.id,
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        now = _now()
        message = self._append_message_locked(
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

        running = [
            turn for turn in self._turns.values()
            if turn.conversation_id == conversation.id and turn.status == "running"
        ]
        if running:
            for turn in running:
                turn.status = "cancelled"
                turn.completed_at = now
            reply = "已取消当前生成。"
        else:
            reply = "没有正在进行的对话。"

        conversation.updated_at = now
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="cancelled",
            reset_message=reply,
        )

    def _handle_status_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        # Duplicate guard within the current conversation.
        if request.external_message_id:
            existing_id = self._messages_by_external.get((conversation.id, request.external_message_id))
            if existing_id is not None:
                msg = self._messages[existing_id]
                turn_id = msg.turn_id
                trigger_type = self._turns[turn_id].trigger_type if turn_id else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=msg.id,
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        now = _now()
        message = self._append_message_locked(
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

        msg_count = len([
            m for m in self._messages.values() if m.conversation_id == conversation.id
        ])
        turns = [t for t in self._turns.values() if t.conversation_id == conversation.id]
        total = len(turns)
        running = sum(1 for t in turns if t.status == "running")
        completed = sum(1 for t in turns if t.status == "completed")
        failed = sum(1 for t in turns if t.status == "failed")
        cancelled = sum(1 for t in turns if t.status == "cancelled")

        settings = get_settings()
        model_status = render_model_status(conversation.metadata, settings)
        session_report = render_session_state(load_session_state(conversation.metadata))
        status_label = "执行中" if running > 0 else "空闲"

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
            f"{model_status}\n"
            f"超时: {settings.llm_timeout_seconds}s\n"
            f"Bot: {settings.feishu_bot_name}"
        )

        conversation.updated_at = now
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="status_report",
            reset_message=reply,
        )

    def _handle_model_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        if request.external_message_id:
            existing_id = self._messages_by_external.get((conversation.id, request.external_message_id))
            if existing_id is not None:
                msg = self._messages[existing_id]
                turn_id = msg.turn_id
                trigger_type = self._turns[turn_id].trigger_type if turn_id else None
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=msg.id,
                    turn_id=turn_id,
                    should_respond=turn_id is not None,
                    trigger_type=trigger_type,
                    status="duplicate",
                )

        now = _now()
        message = self._append_message_locked(
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
        status, reply, patch = model_command_response(request.content, conversation.metadata, get_settings())
        if patch:
            conversation.metadata = _merge_metadata_patch(conversation.metadata, patch)
        conversation.updated_at = now
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status=status,
            reset_message=reply,
        )

    def _handle_repos_command_locked(
        self,
        *,
        conversation: _ConversationRecord,
        user_id: int,
        request: MessageCreateRequest,
    ) -> MessageIngestResponse:
        message = self._append_message_locked(
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
        conversation.updated_at = _now()
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=None,
            should_respond=False,
            trigger_type="command",
            status="repos_report",
            reset_message=render_repository_report(),
        )

    def _append_message_locked(
        self,
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
    ) -> _MessageRecord:
        message_id = self._next_message_id
        self._next_message_id += 1
        message = _MessageRecord(
            id=message_id,
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
            created_at=_now(),
        )
        self._messages[message_id] = message
        if external_message_id:
            self._messages_by_external[(conversation_id, external_message_id)] = message_id
        conversation = self._conversations.get(conversation_id)
        if conversation is not None:
            conversation.updated_at = message.created_at
        return message

    def _resolve_reply_message_id_locked(
        self,
        conversation_id: int,
        reply_to_message_id: int | None,
        reply_to_external_message_id: str | None,
    ) -> int | None:
        if reply_to_message_id is not None:
            return reply_to_message_id
        if reply_to_external_message_id:
            return self._messages_by_external.get((conversation_id, reply_to_external_message_id))
        return None

    def _find_message_by_external_chat_locked(
        self,
        conversation: _ConversationRecord,
        external_message_id: str,
    ) -> int | None:
        for message in sorted(self._messages.values(), key=lambda item: item.id):
            if message.external_message_id != external_message_id:
                continue
            existing_conversation = self._conversations.get(message.conversation_id)
            if existing_conversation is None:
                continue
            if (
                existing_conversation.platform == conversation.platform
                and existing_conversation.external_chat_id == conversation.external_chat_id
            ):
                return message.id
        return None


@router.post("/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(request: ConversationCreateRequest) -> ConversationResponse:
    return _conversation_response(get_conversation_store().create_conversation(request))


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(conversation_id: int) -> ConversationResponse:
    conversation = get_conversation_store().get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return _conversation_response(conversation)


@router.post("/messages", response_model=MessageIngestResponse, status_code=status.HTTP_202_ACCEPTED)
def ingest_message(request: MessageCreateRequest) -> MessageIngestResponse:
    return get_conversation_store().ingest_message(request)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_conversation_message(
    conversation_id: int,
    request: ConversationMessageCreateRequest,
) -> MessageIngestResponse:
    try:
        return get_conversation_store().ingest_conversation_message(conversation_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
def list_conversation_messages(conversation_id: int) -> list[MessageResponse]:
    try:
        messages = get_conversation_store().list_messages(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc
    return [_message_response(message) for message in messages]


@router.get("/conversations/{conversation_id}/turns", response_model=list[TurnResponse])
def list_conversation_turns(conversation_id: int) -> list[TurnResponse]:
    try:
        turns = get_conversation_store().list_turns(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc
    return [_turn_response(turn) for turn in turns]


@router.get("/turns/{turn_id}", response_model=TurnResponse)
def get_turn(turn_id: int) -> TurnResponse:
    turn = get_conversation_store().get_turn(turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn not found.")
    return _turn_response(turn)


@router.post("/turns/{turn_id}/cancel", response_model=TurnResponse)
def cancel_turn(turn_id: int) -> TurnResponse:
    turn = get_conversation_store().cancel_turn(turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn not found.")
    return _turn_response(turn)


@router.post("/turns/{turn_id}/run", response_model=RunTurnResponse)
def run_turn(turn_id: int) -> RunTurnResponse:
    try:
        result = get_agent_runtime().run_turn(turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RunTurnResponse(
        turn_id=result.turn_id,
        conversation_id=result.conversation_id,
        status=result.status,
        reply=result.reply,
        content_type=result.message.content_type,
        attachments=[attachment.__dict__ for attachment in result.message.attachments],
    )


@lru_cache
def get_conversation_store() -> MySQLConversationStore:
    return MySQLConversationStore()


def get_agent_runtime() -> TaskAgentRuntime:
    return TaskAgentRuntime(get_conversation_store())


def _conversation_response(conversation: _ConversationRecord) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=conversation.id,
        platform=conversation.platform,
        external_chat_id=conversation.external_chat_id,
        chat_type=conversation.chat_type,
        title=conversation.title,
        status=conversation.status,
        clear_generation=conversation.clear_generation,
        owner_user_id=conversation.owner_user_id,
        created_by_user_id=conversation.created_by_user_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_response(message: _MessageRecord) -> MessageResponse:
    return MessageResponse(
        message_id=message.id,
        conversation_id=message.conversation_id,
        turn_id=message.turn_id,
        sender_type=message.sender_type,
        user_id=message.user_id,
        role=message.role,
        content=message.content,
        content_type=message.content_type,
        external_message_id=message.external_message_id,
        reply_to_message_id=message.reply_to_message_id,
        created_at=message.created_at,
    )


def _turn_response(turn: _TurnRecord) -> TurnResponse:
    return TurnResponse(
        turn_id=turn.id,
        conversation_id=turn.conversation_id,
        trigger_message_id=turn.trigger_message_id,
        trigger_type=turn.trigger_type,
        status=turn.status,
        turn_type=turn.turn_type,
        started_by_user_id=turn.started_by_user_id,
        started_at=turn.started_at,
        completed_at=turn.completed_at,
        error_message=turn.error_message,
        metadata=turn.metadata,
    )


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


def _merge_metadata_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_metadata_patch(merged[key], value)
        else:
            merged[key] = value
    return merged


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
