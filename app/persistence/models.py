from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserRecord:
    id: int
    platform: str
    external_user_id: str
    display_name: str | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationRecord:
    id: int
    platform: str
    external_chat_id: str
    chat_type: str
    title: str | None
    status: str
    clear_generation: int
    owner_user_id: int | None
    created_by_user_id: int | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageRecord:
    id: int
    conversation_id: int
    turn_id: int | None
    sender_type: str
    user_id: int | None
    role: str
    content: str
    content_type: str
    external_message_id: str | None
    reply_to_message_id: int | None
    raw_payload: dict[str, Any]
    created_at: str


@dataclass
class TurnRecord:
    id: int
    conversation_id: int
    trigger_message_id: int | None
    trigger_type: str
    status: str
    turn_type: str
    started_by_user_id: int | None
    started_at: str
    completed_at: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRecord:
    id: int
    turn_id: int
    tool_name: str
    assistant_message_id: int | None
    provider_tool_call_id: str | None
    step_index: int
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error_message: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str


@dataclass
class ArtifactRecord:
    id: int
    artifact_id: str
    conversation_id: int
    turn_id: int | None
    tool_call_id: str | None
    source_tool: str
    kind: str
    path: str | None
    mime_type: str | None
    filename: str | None
    size_bytes: int | None
    status: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass
class DeliveryRecord:
    id: int
    delivery_id: str
    artifact_id: str
    conversation_id: int | None
    turn_id: int | None
    channel: str
    external_chat_id: str
    purpose: str
    status: str
    upload_key: str | None
    external_message_id: str | None
    error_message: str | None
    attempt_count: int
    created_at: str
    updated_at: str
