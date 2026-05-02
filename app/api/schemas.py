from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

Platform = Literal["api", "cli", "feishu", "wechat", "web", "system"]
ChatType = Literal["dm", "group", "web", "cli", "system"]
MessageRole = Literal["user", "assistant", "system", "tool"]
SenderType = Literal["user", "assistant", "system", "tool"]
ConversationStatus = Literal["active", "archived"]
TurnStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class SenderInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    platform_user_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("platform_user_id", "channel_user_id", "external_user_id"),
    )
    display_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationCreateRequest(BaseModel):
    platform: Platform = "api"
    external_chat_id: str = Field(min_length=1)
    chat_type: ChatType
    title: str | None = None
    created_by: SenderInput | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationResponse(BaseModel):
    conversation_id: int
    platform: str
    external_chat_id: str
    chat_type: str
    title: str | None = None
    status: str
    clear_generation: int
    created_by_user_id: int | None = None
    owner_user_id: int | None = None
    created_at: str
    updated_at: str


class MessageCreateRequest(BaseModel):
    platform: Platform = "api"
    external_chat_id: str = Field(min_length=1)
    chat_type: ChatType
    sender: SenderInput
    content: str = Field(min_length=1)
    content_type: str = "text"
    external_message_id: str | None = None
    reply_to_message_id: int | None = None
    reply_to_external_message_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationMessageCreateRequest(BaseModel):
    sender: SenderInput
    content: str = Field(min_length=1)
    content_type: str = "text"
    external_message_id: str | None = None
    reply_to_message_id: int | None = None
    reply_to_external_message_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageResponse(BaseModel):
    message_id: int
    conversation_id: int
    turn_id: int | None
    sender_type: str
    user_id: int | None
    role: str
    content: str
    content_type: str
    external_message_id: str | None = None
    reply_to_message_id: int | None = None
    created_at: str


class MessageIngestResponse(BaseModel):
    conversation_id: int
    message_id: int
    turn_id: int | None
    should_respond: bool
    trigger_type: str | None
    status: str
    reset_message: str | None = None


class TurnResponse(BaseModel):
    turn_id: int
    conversation_id: int
    trigger_message_id: int | None
    trigger_type: str
    status: str
    turn_type: str
    started_by_user_id: int | None = None
    started_at: str
    completed_at: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunTurnResponse(BaseModel):
    turn_id: int
    conversation_id: int
    status: str
    reply: str
    content_type: str = "text"
