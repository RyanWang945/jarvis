"""Model-facing context assembly for the turn runtime."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent_react.model_usage import strip_token_usage_footer
from app.agent_react.session_state import ConversationSessionState
from app.llm.client import LLMMessage
from app.prompting import PromptRegistry
from app.skills.bootstrap import get_skill_registry
from utils.token_counter import count_text_tokens

_MAX_SKILL_LISTING_TOKENS = 900
_MAX_SKILL_LISTING_ITEM_CHARS = 250
_MAX_HISTORY_MESSAGE_TOKENS = 180
_MAX_FAST_HISTORY_ROUNDS = 6
_MAX_FAST_MESSAGE_CHARS = 1200
_FAST_MESSAGE_HEAD_CHARS = 700
_FAST_MESSAGE_TAIL_CHARS = 300
_MAX_PLANNER_HISTORY_MESSAGES = 12
_MAX_PLANNER_HISTORY_TOKENS = 1800
_MAX_WORKING_SUMMARY_CHARS = 2400
_CONTEXT_REFERENCE_MARKERS = (
    "刚才",
    "上面",
    "上一轮",
    "前面",
    "这个",
    "那个",
    "继续",
    "按你说的",
    "按刚刚",
    "按之前",
    "刚刚",
    "前一个",
    "上一版",
    "这版",
    "不够细",
    "不够详细",
    "详细点",
    "展开",
    "再详细",
    "it",
    "that",
    "this",
    "previous",
    "above",
    "continue",
)


@dataclass(frozen=True)
class ContextMessage:
    """A single message in the conversation context, with optional compression metadata.

    AI-facing format is always plain ``{"role": role, "content": content}``.
    Metadata fields are for internal tracking only.
    """

    role: str
    content: str
    original_index: int | None = None  # index in the original (pre-compression) message list
    is_compressed: bool = False        # this message is a compressed version of something
    compression_level: str = "none"    # "none" | "single" | "batch"
    compressed_from_indices: tuple[int, ...] = ()  # indices of original messages this replaces
    original_token_count: int = 0      # estimated tokens before compression


# Alias kept for backward compatibility during migration.
ConversationContextMessage = ContextMessage  # noqa: F811


# Number of most recent user+assistant rounds to always keep in full.
_PRESERVE_RECENT_ROUNDS = 2


@dataclass(frozen=True)
class ConversationContext:
    """Model-facing conversation history.

    Contains a flat message array with optional compression baked in.
    The ``summary`` field and ``summary_node`` in planner_payload are removed —
    old messages are compressed directly into the messages array as ``role: system`` entries.
    """

    messages: tuple[ContextMessage, ...]
    context_reference_detected: bool = False
    older_summary: str = ""
    fast_messages: tuple[ContextMessage, ...] = ()

    @property
    def has_history(self) -> bool:
        return bool(self.messages)

    def fast_payload(self) -> dict[str, Any]:
        recent_messages = [
            {
                "role": message.role,
                "content": message.content,
            }
            for message in self.fast_messages
        ]
        return {
            "has_history": self.has_history,
            "context_reference_detected": self.context_reference_detected,
            "older_summary": self.older_summary,
            "recent_round_limit": _MAX_FAST_HISTORY_ROUNDS,
            "recent_messages": recent_messages,
        }

    def planner_payload(self) -> dict[str, Any]:
        return {
            "has_history": self.has_history,
            "context_reference_detected": self.context_reference_detected,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in self.messages
            ],
        }


def slice_records_through_trigger(records: list, trigger_message_id: int | None) -> list:
    if trigger_message_id is None:
        return records

    bounded: list = []
    for record in records:
        bounded.append(record)
        if getattr(record, "id", None) == trigger_message_id:
            break
    return bounded


def select_records_for_turn(
    records: list,
    turn_records: list,
    *,
    current_turn_id: int | None,
    trigger_message_id: int | None,
) -> list:
    if current_turn_id is None:
        return slice_records_through_trigger(records, trigger_message_id)

    turns_by_id = {getattr(turn, "id", None): turn for turn in turn_records}
    current_turn = turns_by_id.get(current_turn_id)
    if current_turn is None:
        return slice_records_through_trigger(records, trigger_message_id)

    ordered_turns = sorted(
        [turn for turn in turn_records if getattr(turn, "id", None) is not None],
        key=lambda turn: (str(getattr(turn, "started_at", "")), int(getattr(turn, "id", 0) or 0)),
    )
    current_index = next(
        (idx for idx, turn in enumerate(ordered_turns) if getattr(turn, "id", None) == current_turn_id),
        None,
    )
    if current_index is None:
        return slice_records_through_trigger(records, trigger_message_id)

    prior_completed_turn_ids = {
        getattr(turn, "id", None)
        for turn in ordered_turns[:current_index]
        if getattr(turn, "status", None) == "completed"
    }
    trigger_limit = trigger_message_id if trigger_message_id is not None else float("inf")

    background = [
        record
        for record in records
        if getattr(record, "turn_id", None) is None
        and int(getattr(record, "id", 0) or 0) <= trigger_limit
    ]
    prior_turn_messages: list = []
    for turn in ordered_turns[:current_index]:
        turn_id = getattr(turn, "id", None)
        if turn_id not in prior_completed_turn_ids:
            continue
        prior_turn_messages.extend(record for record in records if getattr(record, "turn_id", None) == turn_id)

    current_messages = [
        record
        for record in records
        if getattr(record, "turn_id", None) == current_turn_id
    ]

    selected: list = []
    seen: set[int] = set()
    for record in [*background, *prior_turn_messages, *current_messages]:
        record_id = int(getattr(record, "id", 0) or 0)
        if record_id in seen:
            continue
        seen.add(record_id)
        selected.append(record)
    return selected


def _looks_like_raw_tool_markup(content: str) -> bool:
    normalized = content.lstrip()
    return normalized.startswith("<｜｜DSML｜｜tool_calls>") or normalized.startswith("<|tool_calls|>")


def _has_context_reference(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    lower = text.lower()
    for marker in _CONTEXT_REFERENCE_MARKERS:
        if marker.isascii():
            if re.search(rf"\b{re.escape(marker)}\b", lower):
                return True
            continue
        if marker in text:
            return True
    return False


class ContextManager:
    """Owns model-visible message preparation inside the turn runtime."""

    def __init__(
        self,
        *,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()

    def build_conversation_context(
        self,
        records: list,
        trigger_message_id: int | None,
        *,
        turn_records: list | None = None,
        current_turn_id: int | None = None,
        session_state: ConversationSessionState | None = None,
        current_user_input: str = "",
    ) -> ConversationContext:
        selected = select_records_for_turn(
            records,
            turn_records or [],
            current_turn_id=current_turn_id,
            trigger_message_id=trigger_message_id,
        )
        history_records = [
            record
            for record in selected
            if getattr(record, "id", None) != trigger_message_id
            and (current_turn_id is None or getattr(record, "turn_id", None) != current_turn_id)
        ]
        # Step 1: records → ContextMessage with per-message token truncation
        context_messages = self._records_to_context_messages(history_records)
        fast_messages = self._records_to_fast_context_messages(history_records)

        # Step 2: two-layer compression — single-big then batch-old
        compressed = self._compress_context_messages(context_messages)

        # Step 3: enforce message count cap
        compressed = compressed[-_MAX_PLANNER_HISTORY_MESSAGES:]

        return ConversationContext(
            messages=tuple(compressed),
            context_reference_detected=_has_context_reference(current_user_input),
            older_summary=str(getattr(session_state, "working_summary", "") or "").strip(),
            fast_messages=tuple(_recent_round_messages(fast_messages, _MAX_FAST_HISTORY_ROUNDS)),
        )

    def update_working_summary(
        self,
        session_state: ConversationSessionState | None,
        *,
        current_user_input: str,
        assistant_reply: str | None,
    ) -> str | None:
        current = session_state.working_summary if session_state is not None else None
        exchange = self._summarize_exchange(
            current_user_input=current_user_input,
            assistant_reply=assistant_reply,
        )
        if not exchange:
            return current
        combined = f"{current.strip()}\n{exchange}" if current else exchange
        if len(combined) <= _MAX_WORKING_SUMMARY_CHARS:
            return combined
        tail = combined[-_MAX_WORKING_SUMMARY_CHARS:].lstrip()
        first_line_break = tail.find("\n")
        if first_line_break > 0:
            tail = tail[first_line_break + 1 :].lstrip()
        return "Earlier conversation was compressed. Recent working summary:\n" + tail

    def build_skill_listing_message(self) -> HumanMessage | None:
        rendered = self._render_skill_listing()
        if rendered is None:
            return None
        return HumanMessage(content=f"<system-reminder>\n{rendered}\n</system-reminder>")

    def _render_skill_listing(self) -> str | None:
        try:
            skills = get_skill_registry().list()
        except Exception:
            return None

        skill_lines: list[str] = []
        total_tokens = self.estimate_text_tokens(
            self._prompt_registry.load("skill_listing").render_text({"skill_lines": ""})
        )
        item_count = 0
        for skill in skills:
            if skill.manifest.disable_model_invocation:
                continue
            if skill.manifest.is_planner_skill or not skill.manifest.user_invocable:
                continue
            description = (skill.effective_description or "").strip()
            if not description:
                continue
            detail = description
            if skill.manifest.when_to_use:
                detail = f"{detail} Use when: {skill.manifest.when_to_use.strip()}"
            detail = _truncate_chars(detail, _MAX_SKILL_LISTING_ITEM_CHARS)
            line = f"- {skill.skill_id}: {detail}"
            line_tokens = self.estimate_text_tokens(line)
            if item_count and total_tokens + line_tokens > _MAX_SKILL_LISTING_TOKENS:
                break
            if line_tokens > _MAX_SKILL_LISTING_TOKENS:
                line = f"- {skill.skill_id}"
                line_tokens = self.estimate_text_tokens(line)
            skill_lines.append(line)
            total_tokens += line_tokens
            item_count += 1

        if item_count == 0:
            return None
        return self._prompt_registry.load("skill_listing").render_text(
            {"skill_lines": "\n".join(skill_lines)}
        )

    def _truncate_text_by_tokens(self, text: str, max_tokens: int) -> str:
        if self.estimate_text_tokens(text) <= max_tokens:
            return text
        max_chars = max(0, max_tokens * 4)
        return text[:max_chars].rstrip() + "\n\n[Content truncated by Jarvis token budget.]"

    def _records_to_context_messages(self, records: list) -> list[ContextMessage]:
        """Convert raw records to ContextMessage, applying per-message token truncation."""
        messages: list[ContextMessage] = []
        for idx, record in enumerate(records):
            role = getattr(record, "role", None)
            if role not in {"user", "assistant"}:
                continue
            content = str(getattr(record, "content", "") or "").strip()
            if not content or _looks_like_raw_tool_markup(content):
                continue
            if role == "assistant":
                content = strip_token_usage_footer(content)
            original_tokens = self.estimate_text_tokens(content)
            content = self._truncate_text_by_tokens(content, _MAX_HISTORY_MESSAGE_TOKENS)
            messages.append(
                ContextMessage(
                    role=role,
                    content=content,
                    original_index=idx,
                    original_token_count=original_tokens,
                )
            )
        return messages

    def _records_to_fast_context_messages(self, records: list) -> list[ContextMessage]:
        messages: list[ContextMessage] = []
        for idx, record in enumerate(records):
            role = getattr(record, "role", None)
            if role not in {"user", "assistant"}:
                continue
            content = str(getattr(record, "content", "") or "").strip()
            if not content or _looks_like_raw_tool_markup(content):
                continue
            if role == "assistant":
                content = strip_token_usage_footer(content)
            content = _truncate_fast_context_message(content)
            messages.append(
                ContextMessage(
                    role=role,
                    content=content,
                    original_index=idx,
                    original_token_count=self.estimate_text_tokens(content),
                )
            )
        return messages

    def _compress_context_messages(
        self,
        messages: list[ContextMessage],
    ) -> list[ContextMessage]:
        """Two-layer compression for context messages.

        Layer 1 (single): truncate each message to ``_MAX_HISTORY_MESSAGE_TOKENS``
        — already done in ``_records_to_context_messages``.

        Layer 2 (batch): when total tokens exceed the budget, compress the oldest
        messages into one ``role: system`` summary, preserving the most recent
        ``_PRESERVE_RECENT_ROUNDS`` user+assistant pairs in full.
        """
        total_tokens = sum(self.estimate_text_tokens(m.content) + 4 for m in messages)
        if total_tokens <= _MAX_PLANNER_HISTORY_TOKENS:
            return messages

        if len(messages) <= _PRESERVE_RECENT_ROUNDS * 2:
            return messages

        # Identify the cut point: keep last N rounds, compress the rest.
        # A "round" = one user + one assistant message (may not always be paired).
        recent_count = _PRESERVE_RECENT_ROUNDS * 2
        to_keep = messages[-recent_count:]
        to_compress = messages[:-recent_count]

        if not to_compress:
            return messages

        # Build a condensed summary of the older messages.
        # Sum of user content is the most signal-dense representation.
        user_texts: list[str] = []
        for msg in to_compress:
            if msg.role == "user":
                text = msg.content.strip()
                if text and len(text) > 2:
                    user_texts.append(text)

        # Clear error messages or empty summaries don't help.
        if not user_texts:
            return to_keep

        # Build batch compression summary
        batch_summary = _build_batch_summary(user_texts, _MAX_PLANNER_HISTORY_TOKENS)
        compressed_indices = tuple(
            i for m in to_compress if m.original_index is not None
            for i in (m.original_index,)
        )
        batch_msg = ContextMessage(
            role="system",
            content=batch_summary,
            original_index=None,
            is_compressed=True,
            compression_level="batch",
            compressed_from_indices=compressed_indices,
            original_token_count=sum(self.estimate_text_tokens(m.content) for m in to_compress),
        )
        return [batch_msg, *to_keep]

    def _summarize_exchange(
        self,
        *,
        current_user_input: str,
        assistant_reply: str | None,
    ) -> str:
        lines: list[str] = []
        user = " ".join(str(current_user_input or "").split())
        assistant = " ".join(strip_token_usage_footer(str(assistant_reply or "")).split())
        if user:
            lines.append(f"User: {user[:500]}")
        if assistant:
            lines.append(f"Assistant: {assistant[:800]}")
        return "\n".join(lines)

    def estimate_text_tokens(self, text: str) -> int:
        return count_text_tokens(text)

    def estimate_message_tokens(self, message: BaseMessage) -> int:
        content = str(message.content or "")
        tokens = self.estimate_text_tokens(content) + 4
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_json = json.dumps(message.tool_calls, ensure_ascii=False)
            tokens += self.estimate_text_tokens(tool_json)
        if isinstance(message, ToolMessage):
            tokens += self.estimate_text_tokens(str(message.tool_call_id or ""))
        return tokens

    def _group_messages_for_budget(self, messages: list[BaseMessage]) -> tuple[SystemMessage | None, list[list[BaseMessage]]]:
        first = messages[0] if messages and isinstance(messages[0], SystemMessage) else None
        tail = messages[1:] if first is not None else list(messages)
        blocks: list[list[BaseMessage]] = []
        idx = 0

        while idx < len(tail):
            message = tail[idx]
            if isinstance(message, AIMessage) and message.tool_calls:
                block = [message]
                idx += 1
                tool_call_ids = {tool_call.get("id") for tool_call in message.tool_calls}
                while idx < len(tail):
                    next_message = tail[idx]
                    if isinstance(next_message, ToolMessage) and next_message.tool_call_id in tool_call_ids:
                        block.append(next_message)
                        idx += 1
                        continue
                    break
                blocks.append(block)
                continue

            blocks.append([message])
            idx += 1

        return first, blocks

    def fit_messages_to_token_budget(self, messages: list[BaseMessage], token_budget: int | None) -> list[BaseMessage]:
        if token_budget is None or token_budget <= 0:
            return messages

        total = sum(self.estimate_message_tokens(message) for message in messages)
        if total <= token_budget:
            return messages

        first, blocks = self._group_messages_for_budget(messages)
        kept_reversed: list[list[BaseMessage]] = []
        used = self.estimate_message_tokens(first) if first is not None else 0

        for block in reversed(blocks):
            block_tokens = sum(self.estimate_message_tokens(message) for message in block)
            if kept_reversed and used + block_tokens > token_budget:
                continue
            if not kept_reversed and used + block_tokens > token_budget:
                kept_reversed.append(block)
                used += block_tokens
                continue
            if used + block_tokens > token_budget:
                break
            kept_reversed.append(block)
            used += block_tokens

        kept_blocks = list(reversed(kept_reversed))
        kept = [message for block in kept_blocks for message in block]
        return [first, *kept] if first is not None else kept

    def render_for_model(self, messages: list[BaseMessage], token_budget: int | None = None) -> list[LLMMessage]:
        fitted_messages = self.fit_messages_to_token_budget(messages, token_budget)
        result: list[LLMMessage] = []
        for msg in fitted_messages:
            if isinstance(msg, SystemMessage):
                result.append(LLMMessage(role="system", content=str(msg.content)))
            elif isinstance(msg, HumanMessage):
                result.append(LLMMessage(role="user", content=str(msg.content)))
            elif isinstance(msg, AIMessage):
                tool_calls: list[dict[str, Any]] | None = None
                if msg.tool_calls:
                    tool_calls = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"]),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                reasoning = None
                if msg.response_metadata:
                    reasoning = msg.response_metadata.get("reasoning_content")
                result.append(
                    LLMMessage(
                        role="assistant",
                        content=strip_token_usage_footer(str(msg.content or "")),
                        tool_calls=tool_calls,
                        reasoning_content=reasoning,
                    )
                )
            elif isinstance(msg, ToolMessage):
                result.append(
                    LLMMessage(
                        role="tool",
                        content=str(msg.content),
                        tool_call_id=msg.tool_call_id,
                    )
                )
        return result


def _truncate_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _recent_round_messages(
    messages: tuple[ContextMessage, ...],
    max_rounds: int,
) -> list[ContextMessage]:
    if max_rounds <= 0:
        return []

    selected_reversed: list[ContextMessage] = []
    user_count = 0
    for message in reversed(messages):
        if message.role == "system":
            continue
        selected_reversed.append(message)
        if message.role == "user":
            user_count += 1
            if user_count >= max_rounds:
                break
    return list(reversed(selected_reversed))


def _truncate_fast_context_message(content: str) -> str:
    text = str(content or "")
    if len(text) <= _MAX_FAST_MESSAGE_CHARS:
        return text
    head = text[:_FAST_MESSAGE_HEAD_CHARS].rstrip()
    tail = text[-_FAST_MESSAGE_TAIL_CHARS:].lstrip()
    return f"{head}\n\n[Content truncated by Jarvis fast context budget.]\n\n{tail}"


def _build_batch_summary(user_texts: list[str], max_tokens: int) -> str:
    """Build a compressed summary of older conversation turns.

    Each entry is a user message (the intent signal). The output is a single
    ``role: system`` message placed in the messages array, replacing the
    compressed entries.

    Token budget: aim for ~15% of ``_MAX_PLANNER_HISTORY_TOKENS`` so the
    summary doesn't compete with the recent full-text messages.
    """
    budget = max(120, int(max_tokens * 0.15))
    lines: list[str] = []
    used = 0
    for text in user_texts:
        if used >= budget:
            break
        # use first sentence-ish chunk (up to 120 chars)
        chunk = _first_sentence(text, max_chars=120)
        chunk_tokens = count_text_tokens(chunk) + 4
        if lines and used + chunk_tokens > budget:
            break
        lines.append(chunk)
        used += chunk_tokens

    if not lines:
        return ""

    # Join: use arrows between turns for readability
    if len(lines) == 1:
        body = lines[0]
    else:
        body = " → ".join(lines)

    return f"[对话历史] {body}"


def _first_sentence(text: str, *, max_chars: int = 120) -> str:
    """Extract the first sentence-ish chunk of text, up to max_chars."""
    if len(text) <= max_chars:
        return text.strip()
    # Try to break at first sentence-ending punctuation
    for i, ch in enumerate(text):
        if i >= max_chars:
            break
        if ch in "。！？\n" and i > 4:
            return text[: i + 1].strip()
    # Fallback: hard truncate at max_chars
    return text[:max_chars].rstrip() + "…"
