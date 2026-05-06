from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.agent_react.session_state import SessionMode
from app.agent_react.turn_classifier import Capability, TurnType

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


_BASE_READ_TOOLS = (
    "obsidian_wiki_query",
    "business_knowledge_search",
    "ask_user",
)
_DISCOVERY_TOOLS = ("tool_search",)
_ACTION_TOOLS = ("scheduled_task",)
_WEB_TOOLS = ("tavily_search", "x_search")
_KB_WRITE_TOOLS = (
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
)
_WORKSPACE_TOOLS = ("delegate_to_codex",)
_WORKSPACE_CAPABILITIES = {
    "workspace.inspect",
    "workspace.edit",
    "workspace.test",
    "workspace.report",
}
_CODE_CAPABILITY_ALIASES = {
    "code.inspect": "workspace.inspect",
    "code.edit": "workspace.edit",
    "code.test": "workspace.test",
}


def resolve_runtime_policy(
    *,
    session_mode: SessionMode,
    turn_type: TurnType | str,
    requested_capabilities: tuple[Capability | str, ...] = (),
) -> RuntimePolicy:
    capabilities = _normalize_capabilities(requested_capabilities)
    mode = _mode_for_turn(turn_type)

    if mode == "command" and "reminder.manage" in capabilities:
        mode = "chat"

    if mode == "command":
        return RuntimePolicy(
            mode="command",
            allowed_tools=(),
            context_sections=("session_state",),
            max_steps=1,
            search_budget=0,
            writeback_strategy="none",
        )
    if mode == "image_generation":
        return RuntimePolicy(
            mode="image_generation",
            allowed_tools=(),
            context_sections=("session_state",),
            max_steps=4,
            search_budget=0,
            writeback_strategy="basic",
        )

    allowed_tools: list[str] = [*_BASE_READ_TOOLS, *_DISCOVERY_TOOLS]
    context_sections: list[str] = ["session_state"]

    if "reminder.manage" in capabilities:
        allowed_tools.extend(_ACTION_TOOLS)
    if "web.search" in capabilities:
        allowed_tools.extend(_WEB_TOOLS)
    if "kb.write" in capabilities:
        allowed_tools.extend(_KB_WRITE_TOOLS)
    if capabilities & _WORKSPACE_CAPABILITIES:
        allowed_tools.extend(_WORKSPACE_TOOLS)
        context_sections.append("workspace_protocol")

    if mode == "research" or "research.deep" in capabilities:
        context_sections.append("research_protocol")
    elif mode == "summary":
        context_sections.append("summary_protocol")
    elif session_mode == "research":
        context_sections.append("research_background")
    elif session_mode == "coding" and not capabilities & _WORKSPACE_CAPABILITIES:
        context_sections.append("coding_background")

    return RuntimePolicy(
        mode=mode,
        allowed_tools=tuple(dict.fromkeys(allowed_tools)),
        context_sections=tuple(dict.fromkeys(context_sections)),
        max_steps=_max_steps(mode, capabilities),
        search_budget=_search_budget(mode, capabilities),
        writeback_strategy=_writeback_strategy(mode),
    )


def _normalize_capabilities(capabilities: tuple[Capability | str, ...]) -> set[str]:
    normalized: set[str] = set()
    for capability in capabilities:
        value = str(capability)
        normalized.add(value)
        alias = _CODE_CAPABILITY_ALIASES.get(value)
        if alias is not None:
            normalized.add(alias)
    return normalized


def _mode_for_turn(turn_type: TurnType | str) -> str:
    if turn_type in {"research", "coding", "summary", "command", "image_generation"}:
        return str(turn_type)
    return "chat"


def _max_steps(mode: str, capabilities: set[str]) -> int:
    if mode == "research" or "research.deep" in capabilities:
        return 10
    if capabilities & _WORKSPACE_CAPABILITIES:
        return 8
    if mode == "summary":
        return 4
    return 6


def _search_budget(mode: str, capabilities: set[str]) -> int:
    if "web.search" not in capabilities:
        return 0
    if mode == "research" or "research.deep" in capabilities:
        return 4
    return 2


def _writeback_strategy(mode: str) -> WritebackStrategy:
    if mode == "research":
        return "research"
    if mode == "coding":
        return "coding"
    return "basic"


def render_runtime_policy_for_model(policy: RuntimePolicy) -> str:
    lines = [
        "Runtime policy:",
        f"Mode: {policy.mode}",
        "Allowed tools: " + (", ".join(policy.allowed_tools) if policy.allowed_tools else "-"),
        "Hidden tools may be discovered with tool_search when the visible tools cannot satisfy an explicit user request.",
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
    if "tool_search" in policy.allowed_tools:
        lines.extend(
            [
                "",
                "Tool discovery protocol:",
                "- Use tool_search only when the current visible tools cannot satisfy an explicit user request.",
                "- tool_search only discovers candidates; it does not execute or authorize them.",
                "- If tool_search returns no_capable_tool, answer from context or ask a concise clarification.",
                "- Do not use tool_search to add intent the user did not express.",
                "- Use ask_user when a required slot is missing and a concise clarification is necessary.",
            ]
        )
    if "workspace_protocol" in policy.context_sections or "coding_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Workspace protocol:",
                "- Use delegate_to_codex for local repository inspection, reports, tests, and edits.",
                "- Trust Codex to handle routine repository work inside the registered workspace.",
                "- Preserve the user's full repository outcome when delegating. If the user asks to update/edit/create, delegate an execution task, not a read-only preview.",
                "- If the user asks to commit, set allow_commit=true. If the user asks to push, set allow_commit=true and allow_push=true.",
                "- Delegate compact outcome-oriented tasks to Codex; do not prescribe shell commands, "
                "recovery steps, or old stderr unless the user explicitly asks.",
                "- Codex owns repository inspection, planning, retries, and approval requests.",
                "- Do not ask Codex to confirm routine details such as commit messages; let Codex choose and proceed.",
                "- Let Codex request approval for elevated actions; do not pre-split ordinary repo work into micro approvals.",
                "- Prefer delegate_to_codex with repo_id; do not guess unregistered workdirs.",
                "- Surface only material approval requests to the user, then report changed files and verification.",
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
