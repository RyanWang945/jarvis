from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException, status

from app.api.schemas import (
    ConversationCreateRequest,
    ConversationMessageCreateRequest,
    ConversationResponse,
    MessageCreateRequest,
    MessageIngestResponse,
    MessageResponse,
    RunTurnResponse,
    TurnResponse,
)
from app.persistence.models import (
    ConversationRecord as _ConversationRecord,
    MessageRecord as _MessageRecord,
    TurnRecord as _TurnRecord,
)
from app.persistence.conversation_store import MySQLConversationStore
from app.task_runtime import TaskAgentRuntime

router = APIRouter(tags=["conversation-runtime"])


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
