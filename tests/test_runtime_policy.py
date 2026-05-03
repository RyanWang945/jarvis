from app.agent_react.runtime_policy import resolve_runtime_policy


def test_research_policy_exposes_research_tools_and_budget() -> None:
    policy = resolve_runtime_policy(session_mode="research", turn_type="research")

    assert policy.mode == "research"
    assert "tavily_search" in policy.allowed_tools
    assert "business_knowledge_search" in policy.allowed_tools
    assert "delegate_to_codex" not in policy.allowed_tools
    assert policy.max_steps == 10
    assert policy.search_budget == 4


def test_chat_policy_does_not_expose_coder_tools() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="chat")

    assert policy.mode == "chat"
    assert "tavily_search" in policy.allowed_tools
    assert "obsidian_wiki_draft" in policy.allowed_tools
    assert "obsidian_wiki_apply" in policy.allowed_tools
    assert "delegate_to_codex" not in policy.allowed_tools
    assert "shell_run_command" not in policy.allowed_tools


def test_coding_policy_exposes_coder_tools() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="coding")

    assert policy.mode == "coding"
    assert "delegate_to_codex" in policy.allowed_tools
    assert "shell_inspect" not in policy.allowed_tools
    assert "shell_run_command" not in policy.allowed_tools
    assert "tavily_search" not in policy.allowed_tools
