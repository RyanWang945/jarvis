from __future__ import annotations

import pytest

from app.agent_react.runtime_policy import resolve_runtime_policy


@pytest.mark.parametrize(
    ("turn_type", "capabilities", "expected_tools", "forbidden_tools", "expected_sections"),
    [
        (
            "chat",
            ("web.search",),
            {"tavily_search"},
            {"delegate_to_codex", "scheduled_task"},
            set(),
        ),
        (
            "chat",
            ("reminder.manage",),
            {"scheduled_task"},
            {"delegate_to_codex"},
            set(),
        ),
        (
            "coding",
            ("workspace.read_file",),
            {"read_file", "search_files"},
            {"scheduled_task"},
            {"workspace_file_protocol", "workspace_protocol"},
        ),
        (
            "coding",
            ("workspace.search_files", "artifact.deliver"),
            {"search_files", "deliver_file"},
            {"scheduled_task"},
            {"artifact_delivery_protocol", "workspace_file_protocol", "workspace_protocol"},
        ),
        (
            "coding",
            ("workspace.inspect", "web.search"),
            {"delegate_to_codex", "tavily_search"},
            {"scheduled_task"},
            {"workspace_protocol"},
        ),
        (
            "image_generation",
            ("image.generate",),
            {"load_skill_guidance", "delegate_to_codex"},
            {"scheduled_task"},
            {"workspace_protocol"},
        ),
    ],
)
def test_runtime_policy_contract_maps_capabilities_to_tools_and_sections(
    turn_type: str,
    capabilities: tuple[str, ...],
    expected_tools: set[str],
    forbidden_tools: set[str],
    expected_sections: set[str],
) -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type=turn_type,
        requested_capabilities=capabilities,
    )

    assert expected_tools <= set(policy.allowed_tools)
    assert forbidden_tools.isdisjoint(policy.allowed_tools)
    assert expected_sections <= set(policy.context_sections)


def test_runtime_policy_contract_command_is_fail_closed_unless_capability_overrides() -> None:
    command = resolve_runtime_policy(session_mode="chat", turn_type="command")
    reminder = resolve_runtime_policy(
        session_mode="chat",
        turn_type="command",
        requested_capabilities=("reminder.manage",),
    )
    artifact = resolve_runtime_policy(
        session_mode="chat",
        turn_type="command",
        requested_capabilities=("artifact.deliver",),
    )

    assert command.mode == "command"
    assert command.allowed_tools == ("tool_search",)
    assert reminder.mode == "chat"
    assert "scheduled_task" in reminder.allowed_tools
    assert artifact.mode == "chat"
    assert "deliver_file" in artifact.allowed_tools


def test_runtime_policy_contract_scene_project_read_is_broad_but_bounded() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="project",
        access="read",
        deliver=False,
    )

    assert policy.mode == "coding"
    assert {"read_file", "search_files", "delegate_to_codex"} <= set(policy.allowed_tools)
    assert "tavily_search" in policy.allowed_tools
    assert "scheduled_task" not in policy.allowed_tools
    assert "workspace_file_protocol" in policy.context_sections
    assert "workspace_protocol" in policy.context_sections
    assert "workspace_read_only_protocol" in policy.context_sections


def test_runtime_policy_contract_scene_project_write_commit_push_have_same_tool_surface() -> None:
    for access in ("write", "commit", "push"):
        policy = resolve_runtime_policy(
            session_mode="chat",
            turn_type="chat",
            scene="project",
            access=access,
            deliver=False,
        )

        assert policy.mode == "coding"
        assert "delegate_to_codex" in policy.allowed_tools
        assert "read_file" not in policy.allowed_tools
        assert "tavily_search" in policy.allowed_tools
        assert "workspace_protocol" in policy.context_sections
        assert policy.max_steps == 8


def test_runtime_policy_contract_scene_chat_delivery_adds_file_location_tools() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="chat",
        scene="chat",
        access="none",
        deliver=True,
    )

    assert policy.mode == "chat"
    assert {"deliver_file", "read_file", "search_files"} <= set(policy.allowed_tools)
    assert "delegate_to_codex" not in policy.allowed_tools
    assert "artifact_delivery_protocol" in policy.context_sections
    assert "workspace_file_protocol" in policy.context_sections


def test_runtime_policy_contract_scene_research_with_workspace_capability_allows_coder_and_search() -> None:
    policy = resolve_runtime_policy(
        session_mode="chat",
        turn_type="research",
        requested_capabilities=("workspace.inspect",),
        scene="research",
        access="read",
        deliver=False,
    )

    assert policy.mode == "research"
    assert "tavily_search" in policy.allowed_tools
    assert "delegate_to_codex" in policy.allowed_tools
    assert "research_protocol" in policy.context_sections
    assert "workspace_protocol" in policy.context_sections
    assert policy.search_budget == 10
