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
    "read_file",
    "search_files",
    "ask_user",
)
_BASE_DISCOVERY_TOOLS = ("tool_search",)
_WORKFLOW_GUIDANCE_TOOLS = ("load_skill_guidance",)
_ACTION_TOOLS = ("scheduled_task",)
_WEB_TOOLS = ("tavily_search",)
_KB_WRITE_TOOLS = (
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
)
_ARTIFACT_DELIVERY_TOOLS = ("deliver_file",)
_ARTIFACT_DELIVERY_CAPABILITIES = {
    "artifact.deliver",
}
_ARTIFACT_GENERATION_CAPABILITIES = {
    "artifact.generate",
    "artifact.revise",
}
_WORKSPACE_FILE_TOOLS = ("read_file", "search_files")
_WORKSPACE_FILE_CAPABILITIES = {
    "workspace.read_file",
    "workspace.search_files",
}
_WORKSPACE_CODE_TOOLS = ("delegate_to_codex",)
_WORKSPACE_CODE_CAPABILITIES = {
    "workspace.inspect",
    "workspace.edit",
    "workspace.test",
    "workspace.report",
}
_WORKSPACE_CAPABILITIES = _WORKSPACE_FILE_CAPABILITIES | _WORKSPACE_CODE_CAPABILITIES
_ARTIFACT_CAPABILITIES = _ARTIFACT_DELIVERY_CAPABILITIES | _ARTIFACT_GENERATION_CAPABILITIES
_CODE_CAPABILITY_ALIASES = {
    "code.inspect": "workspace.inspect",
    "code.edit": "workspace.edit",
    "code.test": "workspace.test",
    "image.generate": "artifact.generate",
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
            allowed_tools=_BASE_DISCOVERY_TOOLS,
            context_sections=("session_state",),
            max_steps=3,
            search_budget=0,
            writeback_strategy="none",
        )
    if mode == "image_generation":
        return RuntimePolicy(
            mode="image_generation",
            allowed_tools=(*_BASE_DISCOVERY_TOOLS, *_WORKFLOW_GUIDANCE_TOOLS, *_WORKSPACE_CODE_TOOLS),
            context_sections=("session_state", "workspace_protocol"),
            max_steps=4,
            search_budget=0,
            writeback_strategy="basic",
        )

    allowed_tools: list[str] = [*_BASE_READ_TOOLS, *_BASE_DISCOVERY_TOOLS, *_WORKFLOW_GUIDANCE_TOOLS]
    context_sections: list[str] = ["session_state"]

    if "reminder.manage" in capabilities:
        allowed_tools.extend(_ACTION_TOOLS)
    if "web.search" in capabilities:
        allowed_tools.extend(_WEB_TOOLS)
    if "kb.write" in capabilities:
        allowed_tools.extend(_KB_WRITE_TOOLS)
    if capabilities & _ARTIFACT_DELIVERY_CAPABILITIES:
        allowed_tools.extend(_ARTIFACT_DELIVERY_TOOLS)
        context_sections.append("artifact_delivery_protocol")
    if capabilities & _WORKSPACE_FILE_CAPABILITIES:
        allowed_tools.extend(_WORKSPACE_FILE_TOOLS)
        context_sections.append("workspace_file_protocol")
    if capabilities & (_WORKSPACE_CODE_CAPABILITIES | _ARTIFACT_GENERATION_CAPABILITIES):
        allowed_tools.extend(_WORKSPACE_CODE_TOOLS)
        context_sections.append("workspace_protocol")

    if mode == "research" or "research.deep" in capabilities:
        context_sections.append("research_protocol")
    elif mode == "summary":
        context_sections.append("summary_protocol")
    elif session_mode == "research":
        context_sections.append("research_background")
    elif session_mode == "coding" and not capabilities & (_WORKSPACE_CAPABILITIES | _ARTIFACT_CAPABILITIES):
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
    return 10


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
    ]
    if "tool_search" in policy.allowed_tools:
        lines.append(
            "tool_search is an internal fallback for discovering a missing capability; never mention tool_search, hidden tools, visible tools, or tool discovery to the user."
        )
    if "load_skill_guidance" in policy.allowed_tools:
        lines.append(
            "Task-specific procedural guidance may be loaded with load_skill_guidance when no relevant <system-reminder> skill is already present."
        )
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
                "- Keep tool discovery entirely internal: do not tell the user you are searching for tools, checking available tools, or looking for a capability.",
                "- If tool_search returns no_capable_tool, answer from context, state the limitation plainly, or ask a concise clarification.",
                "- If a capability is found, continue with the appropriate tool; do not narrate the discovery step.",
                "- Do not use tool_search to add intent the user did not express.",
                "- Use ask_user when a required slot is missing and a concise clarification is necessary.",
            ]
        )
    if any(tool in policy.allowed_tools for tool in _WEB_TOOLS):
        lines.extend(
            [
                "",
                "Web search protocol:",
                "- For current, latest, today, or real-time facts, verify that source dates are fresh enough for the user's request.",
                "- If search results are stale or disagree materially, search again with a more precise query or a more authoritative source.",
                "- Keep simple lookup answers concise after verification: state the value, timestamp/date, source, and uncertainty if any.",
            ]
        )
    if "load_skill_guidance" in policy.allowed_tools:
        lines.extend(
            [
                "",
                "Skill guidance protocol:",
                "- If a task involves a specialized workflow and no relevant <system-reminder> skill is already present, call load_skill_guidance before delegating to Codex or using external tools.",
                "- Use load_skill_guidance for reusable procedures such as artifact generation, channel delivery, repository workflows, wiki workflows, or other multi-step tool orchestration.",
                "- load_skill_guidance only loads turn-scoped instructions; it does not execute the task or grant additional permissions.",
                "- After skill guidance is loaded, follow it as procedural guidance and continue the task with the appropriate allowed tools.",
            ]
        )
    if "workspace_file_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Workspace file protocol:",
                "- Use read_file only for bounded reading of text content or metadata from a known workspace file path.",
                "- Use search_files only for workspace path lookup, existence checks, or bounded text search.",
                "- Do not use read_file as the final delivery mechanism for binary files, images, documents, or archives.",
                "- If the user wants a local file or artifact sent back and deliver_file is visible, use deliver_file after any necessary existence check.",
                "- If deliver_file is not visible but file delivery is clearly required, use tool_search to discover the delivery capability.",
                "- These tools are read-only and do not perform code review, architecture analysis, edits, tests, or git workflows.",
                "- Use delegate_to_codex only when the task requires repository reasoning, code review, reports, tests, edits, or git workflows.",
            ]
        )
    if "artifact_delivery_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Artifact delivery protocol:",
                "- Use deliver_file when the final deliverable is an existing local file or prior artifact that should be sent to the remote conversation.",
                "- Use read_file/search_files only to verify or locate the target file when needed; they do not send attachments.",
                "- For binary files such as images, PDFs, archives, and spreadsheets, deliver the file instead of trying to embed file bytes in text.",
                "- If the requested file is missing or ambiguous, ask one concise clarification question or explain the missing target.",
            ]
        )
    if "workspace_protocol" in policy.context_sections or "coding_protocol" in policy.context_sections:
        lines.extend(
            [
                "",
                "Workspace protocol:",
                "- Use delegate_to_codex for multi-file repository reasoning, code review, reports, tests, edits, artifact generation, and git workflows.",
                "- Do not use delegate_to_codex for listing files, checking file existence, reading a known file, or bounded text search; use read_file/search_files for those.",
                "- Trust Codex to handle routine repository work inside the registered workspace.",
                "- Preserve the user's full repository outcome when delegating. If the user asks to update/edit/create, delegate an execution task, not a read-only preview.",
                "- If the user asks to commit, set allow_commit=true. If the user asks to push, set allow_commit=true and allow_push=true.",
                "- Delegate compact outcome-oriented tasks to Codex; do not prescribe shell commands, "
                "recovery steps, or old stderr unless the user explicitly asks.",
                "- Codex owns repository reasoning, planning, retries, and approval requests.",
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
