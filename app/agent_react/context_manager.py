"""Model-facing context assembly for the turn runtime."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent_react.model_usage import strip_token_usage_footer
from app.agent_react.session_state import ConversationSessionState, render_session_state_for_model
from app.config import get_settings
from app.llm.client import LLMMessage
from app.prompting import PromptRegistry
from app.repositories import RepositoryRegistryError, get_repository_registry
from app.skills.bootstrap import get_skill_registry
from app.skills.rendering import render_loaded_skill_guidance
from utils.token_counter import count_text_tokens

def load_agent_system_prompt(
    prompt_registry: PromptRegistry | None = None,
    prompt_version: str | None = None,
) -> str:
    return (prompt_registry or PromptRegistry()).load("agent_system", prompt_version).render_text({})


SYSTEM_PROMPT = _SYSTEM_PROMPT = load_agent_system_prompt()

_MAX_SELECTED_SKILLS = 3
_MAX_SKILL_BODY_TOKENS = 800
_MAX_TOTAL_SKILL_TOKENS = 1800
_MAX_SKILL_LISTING_TOKENS = 900
_MAX_SKILL_LISTING_ITEM_CHARS = 250
_MAX_HISTORY_MESSAGE_TOKENS = 180
_MAX_FAST_HISTORY_MESSAGES = 2
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
    "it",
    "that",
    "this",
    "previous",
    "above",
    "continue",
)


@dataclass(frozen=True)
class ConversationContextMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ConversationContext:
    summary: str | None
    messages: tuple[ConversationContextMessage, ...]
    context_reference_detected: bool = False

    @property
    def has_history(self) -> bool:
        return bool(self.summary or self.messages)

    def fast_payload(self) -> dict[str, Any]:
        return {
            "has_history": self.has_history,
            "context_reference_detected": self.context_reference_detected,
            "summary": self.summary,
            "recent_messages": [
                {"role": message.role, "content": message.content}
                for message in self.messages[-_MAX_FAST_HISTORY_MESSAGES:]
            ],
        }

    def planner_payload(self) -> dict[str, Any]:
        summary_node = None
        if self.summary:
            summary_node = {
                "id": "conversation_summary",
                "kind": "compressed_history",
                "content": self.summary,
            }
        return {
            "has_history": self.has_history,
            "context_reference_detected": self.context_reference_detected,
            "summary_node": summary_node,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in self.messages
            ],
        }


def _render_runtime_temporal_context(
    now: datetime | None = None,
    *,
    prompt_registry: PromptRegistry | None = None,
) -> str:
    settings = get_settings()
    timezone_name = settings.default_timezone
    tz = _resolve_timezone(timezone_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return (prompt_registry or PromptRegistry()).load("runtime_temporal_context").render_text(
        {
            "current_date": current.date().isoformat(),
            "current_time": current.isoformat(timespec="seconds"),
            "timezone": timezone_name,
        }
    )


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name in {"Asia/Shanghai", "Asia/Chongqing"}:
            return timezone(timedelta(hours=8), name=timezone_name)
        return UTC


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


def latest_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


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
        agent_system_prompt_version: str | None = None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._agent_system_prompt_version = agent_system_prompt_version

    def system_prompt(self) -> str:
        return load_agent_system_prompt(
            prompt_registry=self._prompt_registry,
            prompt_version=self._agent_system_prompt_version,
        )

    def records_to_lc_messages(self, messages: list) -> list[BaseMessage]:
        lc: list[BaseMessage] = []
        for msg in messages:
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", "") or ""
            raw = getattr(msg, "raw_payload", {}) or {}
            if role == "system" and raw.get("source") == "clear_command":
                continue
            if role == "user":
                lc.append(HumanMessage(content=content))
            elif role == "assistant":
                response_metadata: dict[str, Any] = {}
                reasoning = raw.get("reasoning_content")
                if reasoning is not None:
                    response_metadata["reasoning_content"] = reasoning
                lc.append(
                    AIMessage(
                        content=content,
                        tool_calls=[
                            {
                                "id": tc.get("id", ""),
                                "name": tc.get("name", ""),
                                "args": tc.get("args", {}) or {},
                            }
                            for tc in raw.get("tool_calls", [])
                            if tc.get("id") and tc.get("name")
                        ],
                        response_metadata=response_metadata if response_metadata else {},
                    )
                )
            elif role == "tool":
                tool_call_id = raw.get("tool_call_id", "unknown")
                lc.append(ToolMessage(content=content, tool_call_id=tool_call_id))
            elif role == "system":
                lc.append(SystemMessage(content=content))
        return lc

    def strip_historical_tool_protocol(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Persisted tool protocol is turn-local; do not replay it into later turns."""
        stripped: list[BaseMessage] = []
        for message in messages:
            if isinstance(message, ToolMessage):
                continue
            if isinstance(message, AIMessage):
                content = str(message.content or "").strip()
                if not content or _looks_like_raw_tool_markup(content):
                    continue
                stripped.append(
                    AIMessage(
                        content=content,
                        response_metadata=message.response_metadata if message.response_metadata else {},
                    )
                )
                continue
            stripped.append(message)
        return stripped

    def inject_selected_skills(self, messages: list[BaseMessage], skill_names: list[str]) -> list[BaseMessage]:
        if not skill_names:
            return messages

        skill_message = self._build_skill_reminder_message(skill_names)
        if skill_message is None:
            return messages

        insert_at = 1 if messages and isinstance(messages[0], SystemMessage) else 0
        return [*messages[:insert_at], skill_message, *messages[insert_at:]]

    def inject_skill_listing(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        skill_message = self.build_skill_listing_message()
        if skill_message is None:
            return messages
        insert_at = 1 if messages and isinstance(messages[0], SystemMessage) else 0
        return [*messages[:insert_at], skill_message, *messages[insert_at:]]

    def build_context_header(
        self,
        *,
        session_state: ConversationSessionState | None,
        skill_names: list[str],
        task_plan: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
    ) -> SystemMessage:
        sections = [self.system_prompt().strip()]
        sections.append(_render_runtime_temporal_context(prompt_registry=self._prompt_registry))

        if session_state is not None:
            rendered_session = render_session_state_for_model(session_state)
            if rendered_session is not None:
                sections.append(rendered_session)

        rendered_repositories = self._render_repository_context(session_state)
        if rendered_repositories is not None:
            sections.append(rendered_repositories)

        rendered_task_plan = self._render_task_plan(task_plan)
        if rendered_task_plan is not None:
            sections.append(rendered_task_plan)

        rendered_artifacts = self._render_recent_artifacts(recent_artifacts)
        if rendered_artifacts is not None:
            sections.append(rendered_artifacts)

        return SystemMessage(content="\n\n".join(sections))

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
        messages = self._records_to_conversation_context_messages(history_records)
        messages = self._fit_context_messages(messages, max_tokens=_MAX_PLANNER_HISTORY_TOKENS)
        summary = session_state.working_summary if session_state is not None else None
        return ConversationContext(
            summary=summary,
            messages=tuple(messages[-_MAX_PLANNER_HISTORY_MESSAGES:]),
            context_reference_detected=_has_context_reference(current_user_input),
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

    def build_skill_reminder_message(self, skill_names: list[str]) -> HumanMessage | None:
        rendered = self._render_selected_skills(skill_names)
        if rendered is None:
            return None
        return HumanMessage(content=rendered)

    def build_skill_listing_message(self) -> HumanMessage | None:
        rendered = self._render_skill_listing()
        if rendered is None:
            return None
        return HumanMessage(content=f"<system-reminder>\n{rendered}\n</system-reminder>")

    def _build_skill_reminder_message(self, skill_names: list[str]) -> HumanMessage | None:
        return self.build_skill_reminder_message(skill_names)

    def _render_selected_skills(self, skill_names: list[str]) -> str | None:
        if not skill_names:
            return None

        registry = get_skill_registry()
        sections: list[str] = []
        total_tokens = 0
        for skill_name in skill_names[:_MAX_SELECTED_SKILLS]:
            try:
                skill = registry.get(skill_name)
            except ValueError:
                continue
            body = self._bounded_skill_body(render_loaded_skill_guidance(skill).strip())
            if not body:
                continue
            section = f"[Skill: {skill.skill_id}]\n{body}"
            section_tokens = self.estimate_text_tokens(section)
            if total_tokens and total_tokens + section_tokens > _MAX_TOTAL_SKILL_TOKENS:
                break
            if section_tokens > _MAX_TOTAL_SKILL_TOKENS:
                section = self._truncate_text_by_tokens(section, _MAX_TOTAL_SKILL_TOKENS)
                section_tokens = self.estimate_text_tokens(section)
            sections.append(section)
            total_tokens += section_tokens

        if not sections:
            return None

        return self._prompt_registry.load("loaded_skill_guidance").render_text(
            {"skill_sections": "\n\n".join(sections)}
        )

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

    def _bounded_skill_body(self, body: str) -> str:
        if not body:
            return ""
        return self._truncate_text_by_tokens(body, _MAX_SKILL_BODY_TOKENS)

    def _truncate_text_by_tokens(self, text: str, max_tokens: int) -> str:
        if self.estimate_text_tokens(text) <= max_tokens:
            return text
        max_chars = max(0, max_tokens * 4)
        return text[:max_chars].rstrip() + "\n\n[Skill content truncated by Jarvis token budget.]"

    def _render_repository_context(
        self,
        session_state: ConversationSessionState | None,
    ) -> str | None:
        try:
            repositories = get_repository_registry().list_repositories()
        except (RepositoryRegistryError, OSError):
            return None
        if not repositories:
            return None

        active_repo_id = session_state.active_repo_id if session_state is not None else None
        active_repo_line = ""
        if active_repo_id:
            active_repo_line = f"Active repository: {active_repo_id}"
        repository_lines: list[str] = []
        for repo in repositories:
            active_marker = " (active)" if repo.repo_id == active_repo_id else ""
            repository_lines.append(f"- {repo.repo_id}{active_marker}: {repo.canonical_root_path}")
        return self._prompt_registry.load("repository_context").render_text(
            {
                "active_repo_line": active_repo_line,
                "repository_lines": "\n".join(repository_lines),
            }
        )

    def _render_task_plan(self, task_plan: dict[str, Any] | None) -> str | None:
        if not isinstance(task_plan, dict) or not task_plan:
            return None
        rendered = json.dumps(task_plan, ensure_ascii=False, default=str, indent=2)
        objective = str(task_plan.get("objective") or task_plan.get("user_objective") or "").strip()
        objective_line = f"Current turn objective: {objective}" if objective else ""
        return self._prompt_registry.load("task_plan_context").render_text(
            {
                "objective_line": objective_line,
                "task_plan_json": rendered,
            }
        )

    def _render_recent_artifacts(self, recent_artifacts: list[dict[str, Any]] | None) -> str | None:
        if not recent_artifacts:
            return None
        lines: list[str] = []
        for artifact in recent_artifacts[:5]:
            if not isinstance(artifact, dict):
                continue
            artifact_id = str(artifact.get("artifact_id") or artifact.get("id") or "").strip()
            kind = str(artifact.get("kind") or "").strip()
            filename = str(artifact.get("filename") or "").strip()
            path = str(artifact.get("path") or "").strip()
            source_tool = str(artifact.get("source_tool") or "").strip()
            turn_id = artifact.get("turn_id")
            status = str(artifact.get("status") or "").strip()
            parts = []
            if artifact_id:
                parts.append(f"id={artifact_id}")
            if kind:
                parts.append(f"kind={kind}")
            if filename:
                parts.append(f"filename={filename}")
            if path:
                parts.append(f"path={path}")
            if source_tool:
                parts.append(f"source_tool={source_tool}")
            if turn_id is not None:
                parts.append(f"turn_id={turn_id}")
            if status:
                parts.append(f"status={status}")
            if parts:
                lines.append("- " + "; ".join(parts))
        if not lines:
            return None
        return self._prompt_registry.load("recent_artifacts_context").render_text(
            {"artifact_lines": "\n".join(lines)}
        )

    def _records_to_conversation_context_messages(self, records: list) -> list[ConversationContextMessage]:
        messages: list[ConversationContextMessage] = []
        for record in records:
            role = getattr(record, "role", None)
            if role not in {"user", "assistant"}:
                continue
            content = str(getattr(record, "content", "") or "").strip()
            if not content or _looks_like_raw_tool_markup(content):
                continue
            if role == "assistant":
                content = strip_token_usage_footer(content)
            content = self._truncate_text_by_tokens(content, _MAX_HISTORY_MESSAGE_TOKENS)
            messages.append(ConversationContextMessage(role=role, content=content))
        return messages

    def _fit_context_messages(
        self,
        messages: list[ConversationContextMessage],
        *,
        max_tokens: int,
    ) -> list[ConversationContextMessage]:
        kept_reversed: list[ConversationContextMessage] = []
        used = 0
        for message in reversed(messages):
            tokens = self.estimate_text_tokens(message.content) + 4
            if kept_reversed and used + tokens > max_tokens:
                break
            kept_reversed.append(message)
            used += tokens
        return list(reversed(kept_reversed))

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

    def ensure_system_prompt(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        system_prompt = self.system_prompt()
        if not messages:
            return [SystemMessage(content=system_prompt)]

        first = messages[0]
        if isinstance(first, SystemMessage):
            content = str(first.content or "")
            if system_prompt in content:
                return messages
            updated = SystemMessage(content=f"{content}\n\n{system_prompt}" if content else system_prompt)
            return [updated, *messages[1:]]

        return [SystemMessage(content=system_prompt), *messages]

    def inject_session_state(
        self,
        messages: list[BaseMessage],
        session_state: ConversationSessionState | None,
    ) -> list[BaseMessage]:
        if session_state is None:
            return messages
        rendered = render_session_state_for_model(session_state)
        if rendered is None:
            return messages

        insert_at = 1 if messages and isinstance(messages[0], SystemMessage) else 0
        return [
            *messages[:insert_at],
            SystemMessage(content=rendered),
            *messages[insert_at:],
        ]

    def build_initial_messages(
        self,
        records: list,
        trigger_message_id: int | None,
        *,
        turn_records: list | None = None,
        current_turn_id: int | None = None,
        session_state: ConversationSessionState | None = None,
        task_plan: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[list[BaseMessage], list[str]]:
        bounded_records = select_records_for_turn(
            records,
            turn_records or [],
            current_turn_id=current_turn_id,
            trigger_message_id=trigger_message_id,
        )
        lc_messages = self.records_to_lc_messages(bounded_records)
        lc_messages = self.strip_historical_tool_protocol(lc_messages)
        skill_names: list[str] = []
        header = self.build_context_header(
            session_state=session_state,
            skill_names=skill_names,
            task_plan=task_plan,
            recent_artifacts=recent_artifacts,
        )
        messages = self.inject_skill_listing([header, *lc_messages])
        messages = self.inject_selected_skills(messages, skill_names)
        return messages, skill_names

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
