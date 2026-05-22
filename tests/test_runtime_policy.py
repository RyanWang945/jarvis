from app.agent_react.runtime_policy import render_runtime_policy_for_model, resolve_runtime_policy
from app.agent_react.runtime import _runtime_policy_with_intent_budget


def test_research_policy_exposes_research_tools_and_budget() -> None:
    policy = resolve_runtime_policy(
        session_mode="research",
        turn_type="chat",
        scene="research",
        access="read",
    )

    assert policy.mode == "research"
    assert "tavily_search" in policy.allowed_tools
    assert "business_knowledge_search" in policy.allowed_tools
    assert "delegate_to_codex" not in policy.allowed_tools
    assert policy.max_steps == 10
    assert policy.search_budget == 10


def test_chat_policy_exposes_low_risk_retrieval_but_not_coder_tools() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="chat")

    assert policy.mode == "chat"
    assert "tavily_search" in policy.allowed_tools
    assert "x_search" not in policy.allowed_tools
    assert "obsidian_wiki_draft" not in policy.allowed_tools
    assert "obsidian_wiki_apply" not in policy.allowed_tools
    assert "obsidian_wiki_query" in policy.allowed_tools
    assert "business_knowledge_search" in policy.allowed_tools
    assert "read_file" not in policy.allowed_tools
    assert "search_files" not in policy.allowed_tools
    assert "ask_user" in policy.allowed_tools
    assert "tool_search" in policy.allowed_tools
    assert "load_skill_guidance" in policy.allowed_tools
    assert "scheduled_task" not in policy.allowed_tools
    assert "delegate_to_codex" not in policy.allowed_tools
    assert "shell_run_command" not in policy.allowed_tools
    assert policy.search_budget == 3


def test_project_read_scene_exposes_file_and_codex_read_tools() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
        deliver=True,
    )

    assert policy.mode == "coding"
    assert "read_file" in policy.allowed_tools
    assert "search_files" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "deliver_file" in policy.allowed_tools
    assert "workspace_file_protocol" in policy.context_sections
    assert "workspace_protocol" in policy.context_sections
    assert "artifact_delivery_protocol" in policy.context_sections


def test_reminder_scene_exposes_reminder_tool() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="reminder",
        access="none",
    )

    assert "tool_search" in policy.allowed_tools
    assert "scheduled_task" in policy.allowed_tools


def test_reminder_capability_overrides_command_no_tools_policy() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="command",
        legacy_capabilities=("reminder.manage",),
    )

    assert policy.mode == "chat"
    assert "scheduled_task" in policy.allowed_tools
    assert policy.max_steps > 1


def test_artifact_delivery_capability_overrides_command_no_tools_policy() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="command",
        legacy_capabilities=("artifact.deliver",),
    )

    assert policy.mode == "chat"
    assert "deliver_file" in policy.allowed_tools
    assert "artifact_delivery_protocol" in policy.context_sections
    assert policy.max_steps > 1


def test_command_policy_keeps_tool_search_as_internal_fallback() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="command")

    assert policy.mode == "command"
    assert policy.allowed_tools == ("tool_search",)
    assert policy.max_steps > 1

    rendered = render_runtime_policy_for_model(policy)
    assert "Tool discovery protocol:" in rendered
    assert "never mention tool_search" in rendered
    assert "do not tell the user you are searching for tools" in rendered


def test_image_generation_policy_can_delegate_to_codex_with_guidance() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="write",
    )

    assert policy.mode == "coding"
    assert "tool_search" in policy.allowed_tools
    assert "load_skill_guidance" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "workspace_protocol" in policy.context_sections
    assert "tavily_search" in policy.allowed_tools
    assert policy.search_budget == 3

    rendered = render_runtime_policy_for_model(policy)
    assert "call load_skill_guidance before delegating to Codex" in rendered
    assert "Use delegate_to_codex" in rendered


def test_coding_policy_exposes_coder_tools() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
    )

    assert policy.mode == "coding"
    assert "delegate_to_codex" in policy.allowed_tools
    assert "shell_inspect" not in policy.allowed_tools
    assert "shell_run_command" not in policy.allowed_tools
    assert "tavily_search" in policy.allowed_tools
    assert "workspace_read_only_protocol" in policy.context_sections

    rendered = render_runtime_policy_for_model(policy)
    assert "compact outcome-oriented tasks" in rendered
    assert "do not prescribe shell commands" in rendered
    assert "call load_skill_guidance before delegating to Codex" in rendered
    assert "Workspace read-only ceiling:" in rendered
    assert "prefer delegate_to_codex with a read-only task contract" in rendered


def test_workspace_read_file_policy_exposes_file_tools_without_codex() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
    )

    assert policy.mode == "coding"
    assert "read_file" in policy.allowed_tools
    assert "search_files" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "shell_inspect" not in policy.allowed_tools
    assert "workspace_file_protocol" in policy.context_sections
    assert "workspace_protocol" in policy.context_sections

    rendered = render_runtime_policy_for_model(policy)
    assert "Workspace file protocol:" in rendered
    assert "locate, verify, read, or deliver a specific known or guessable file/path/artifact" in rendered
    assert "repository-level judgment" in rendered
    assert "explanation of how something should fit into the project" in rendered


def test_artifact_delivery_policy_exposes_deliver_file_without_codex() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
        deliver=True,
    )

    assert "deliver_file" in policy.allowed_tools
    assert "search_files" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "artifact_delivery_protocol" in policy.context_sections

    rendered = render_runtime_policy_for_model(policy)
    assert "Artifact delivery protocol:" in rendered
    assert "read_file/search_files only to verify or locate" in rendered
    assert "deliver the file instead of trying to embed file bytes" in rendered
    assert "locating a specific artifact for delivery" in rendered


def test_artifact_revision_policy_can_delegate_to_codex() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="write",
    )

    assert "delegate_to_codex" in policy.allowed_tools
    assert "workspace_protocol" in policy.context_sections


def test_workspace_file_and_inspect_policy_can_expose_both_paths() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
    )

    assert "read_file" in policy.allowed_tools
    assert "search_files" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "workspace_file_protocol" in policy.context_sections
    assert "workspace_protocol" in policy.context_sections


def test_coding_policy_exposes_search_when_web_capability_requested() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
        legacy_capabilities=("web.search",),
    )

    assert policy.mode == "coding"
    assert "delegate_to_codex" in policy.allowed_tools
    assert "tavily_search" in policy.allowed_tools
    assert policy.search_budget == 10


def test_web_policy_prompts_simple_lookup_to_stay_concise() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="research",
        access="read",
    )

    rendered = render_runtime_policy_for_model(policy)

    assert "Web search protocol:" in rendered
    assert "verify that source dates are fresh enough" in rendered
    assert "search again with a more precise query" in rendered
    assert "Keep simple lookup answers concise" in rendered


def test_tavily_conversation_intent_gets_fresh_turn_budget() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="chat")

    assert policy.search_budget == 3

    adjusted = _runtime_policy_with_intent_budget(
        policy,
        ["obsidian_wiki_query", "tool_search", "tavily_search"],
    )

    assert adjusted.search_budget == 3


def test_research_policy_allows_workspace_and_web_capabilities_together() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="research",
        access="read",
        legacy_capabilities=("workspace.inspect",),
    )

    assert policy.mode == "research"
    assert "delegate_to_codex" in policy.allowed_tools
    assert "tavily_search" in policy.allowed_tools
    assert "workspace_protocol" in policy.context_sections
    assert "research_protocol" in policy.context_sections
    assert policy.search_budget == 10


def test_legacy_code_capabilities_map_to_workspace_tools() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="research",
        legacy_capabilities=("code.inspect", "web.search"),
    )

    assert "delegate_to_codex" in policy.allowed_tools
    assert "tavily_search" in policy.allowed_tools
