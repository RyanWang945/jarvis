from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agent_react.session_state import SessionMode
from app.agent_react.turn_classifier import TurnType

WritebackStrategy = Literal["basic", "research", "coding", "none"]


@dataclass(frozen=True)
class RuntimePolicy:
    mode: str
    allowed_tools: tuple[str, ...]
    forced_skills: tuple[str, ...] = ()
    context_sections: tuple[str, ...] = ()
    max_steps: int = 8
    search_budget: int | None = None
    writeback_strategy: WritebackStrategy = "basic"


_CHAT_TOOLS = (
    "tavily_search",
    "obsidian_wiki_query",
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
    "business_knowledge_search",
)
_RESEARCH_TOOLS = (
    "tavily_search",
    "business_knowledge_search",
    "obsidian_wiki_query",
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
    "write_file",
)
_CODING_TOOLS = (
    "delegate_to_codex",
)


def resolve_runtime_policy(
    *,
    session_mode: SessionMode,
    turn_type: TurnType | str,
) -> RuntimePolicy:
    if turn_type == "research":
        return RuntimePolicy(
            mode="research",
            allowed_tools=_RESEARCH_TOOLS,
            context_sections=("research_protocol", "session_state"),
            max_steps=10,
            search_budget=4,
            writeback_strategy="research",
        )
    if turn_type == "coding":
        return RuntimePolicy(
            mode="coding",
            allowed_tools=_CODING_TOOLS,
            context_sections=("coding_protocol", "session_state"),
            max_steps=8,
            search_budget=0,
            writeback_strategy="coding",
        )
    if turn_type == "summary":
        return RuntimePolicy(
            mode="summary",
            allowed_tools=("obsidian_wiki_query", "business_knowledge_search"),
            context_sections=("summary_protocol", "session_state"),
            max_steps=4,
            search_budget=0,
            writeback_strategy="basic",
        )
    if turn_type == "command":
        return RuntimePolicy(
            mode="command",
            allowed_tools=(),
            context_sections=("session_state",),
            max_steps=1,
            search_budget=0,
            writeback_strategy="none",
        )
    if turn_type == "image_generation":
        return RuntimePolicy(
            mode="image_generation",
            allowed_tools=(),
            context_sections=("session_state",),
            max_steps=4,
            search_budget=0,
            writeback_strategy="basic",
        )

    if session_mode == "research":
        context_sections = ("session_state", "research_background")
    elif session_mode == "coding":
        context_sections = ("session_state", "coding_background")
    else:
        context_sections = ("session_state",)
    return RuntimePolicy(
        mode="chat",
        allowed_tools=_CHAT_TOOLS,
        context_sections=context_sections,
        max_steps=6,
        search_budget=2,
        writeback_strategy="basic",
    )


def render_runtime_policy_for_model(policy: RuntimePolicy) -> str:
    lines = [
        "Runtime policy:",
        f"Mode: {policy.mode}",
        "Allowed tools: " + (", ".join(policy.allowed_tools) if policy.allowed_tools else "-"),
    ]
    if "research_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Research protocol:",
                "- Treat research as evidence-driven work, not casual summarization.",
                "- Prefer reliable sources and record uncertainty explicitly.",
                "- Separate facts, interpretations, and recommendations.",
                "- Stop searching when gathered evidence is sufficient for the current turn.",
            ]
        )
    if "coding_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Coding protocol:",
                "- Inspect before changing repository state.",
                "- Use high-privilege coder delegation only for explicit repository tasks.",
                "- Prefer delegate_to_codex with repo_id; do not guess unregistered workdirs.",
                "- Report changed files, verification, and permission limits.",
            ]
        )
    if "summary_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Summary protocol:",
                "- Preserve confirmed facts and decisions.",
                "- Do not invent missing context.",
                "- Call out unresolved questions separately.",
            ]
        )
    return "\n".join(lines)
