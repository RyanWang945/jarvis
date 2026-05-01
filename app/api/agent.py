from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.agent_react import AgentRuntime
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
        self._lock = Lock()
        self._next_user_id = 1
        self._next_conversation_id = 1
        self._next_message_id = 1
        self._next_turn_id = 1
        self._users: dict[int, _UserRecord] = {}
        self._users_by_external: dict[tuple[str, str], int] = {}
        self._conversations: dict[int, _ConversationRecord] = {}
        self._active_conversations: dict[tuple[str, str], int] = {}
        self._messages: dict[int, _MessageRecord] = {}
        self._messages_by_external: dict[tuple[int, str], int] = {}
        self._turns: dict[int, _TurnRecord] = {}

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
            existing_message_id = self._messages_by_external.get((conversation.id, external_message_id))
            if existing_message_id is not None:
                message = self._messages[existing_message_id]
                return MessageIngestResponse(
                    conversation_id=conversation.id,
                    message_id=message.id,
                    turn_id=message.turn_id,
                    should_respond=message.turn_id is not None,
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
        should_respond = trigger_type is not None

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
            turn_id = self._next_turn_id
            self._next_turn_id += 1
            turn = _TurnRecord(
                id=turn_id,
                conversation_id=conversation.id,
                trigger_message_id=message.id,
                trigger_type=trigger_type,
                status="queued",
                turn_type=_turn_type(content),
                started_by_user_id=user_id,
                started_at=_now(),
                metadata={"mentions": mentions},
            )
            self._turns[turn_id] = turn
            message.turn_id = turn_id

        conversation.updated_at = _now()
        return MessageIngestResponse(
            conversation_id=conversation.id,
            message_id=message.id,
            turn_id=turn_id,
            should_respond=should_respond,
            trigger_type=trigger_type,
            status="queued" if should_respond else "stored",
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
    )


@lru_cache
def get_conversation_store() -> MySQLConversationStore:
    return MySQLConversationStore()


def get_agent_runtime() -> AgentRuntime:
    return AgentRuntime(get_conversation_store())


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
