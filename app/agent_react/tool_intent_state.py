from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

ACTIVE_TOOL_INTENTS_KEY = "active_tool_intents"

PERSISTABLE_TOOL_INTENTS = {
    "scheduled_task",
    "delegate_to_codex",
    "tavily_search",
    "x_search",
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
}


class ConversationMetadataStore(Protocol):
    def get_conversation(self, conversation_id: int) -> Any | None: ...

    def update_conversation_metadata(self, conversation_id: int, patch: dict[str, Any]) -> None: ...


def persistable_tool_intents(tool_names: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for tool_name in tool_names:
        name = str(tool_name or "").strip()
        if name in PERSISTABLE_TOOL_INTENTS and name not in result:
            result.append(name)
    return result


def tool_intents_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get(ACTIVE_TOOL_INTENTS_KEY)
    if not isinstance(raw, list):
        return []

    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("tool_name") or "").strip()
        else:
            name = str(item or "").strip()
        if name in PERSISTABLE_TOOL_INTENTS and name not in result:
            result.append(name)
    return result


def merge_tool_intents(*tool_groups: list[str] | tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    for group in tool_groups:
        for tool_name in group:
            name = str(tool_name or "").strip()
            if name and name not in merged:
                merged.append(name)
    return merged


def append_conversation_tool_intents(
    store: ConversationMetadataStore,
    conversation_id: int,
    tool_names: list[str] | tuple[str, ...],
) -> list[str]:
    new_tools = persistable_tool_intents(tool_names)
    if not new_tools:
        return []

    conversation = store.get_conversation(conversation_id)
    metadata = getattr(conversation, "metadata", None) if conversation is not None else None
    existing = tool_intents_from_metadata(metadata)
    merged = merge_tool_intents(existing, new_tools)
    added = [tool_name for tool_name in merged if tool_name not in existing]
    if not added:
        return []

    try:
        store.update_conversation_metadata(conversation_id, {ACTIVE_TOOL_INTENTS_KEY: merged})
    except Exception:
        logger.exception(
            "conversation tool intent append failed conversation_id=%s added_tools=%s",
            conversation_id,
            added,
        )
        return []
    return added
