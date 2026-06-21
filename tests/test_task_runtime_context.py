from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from app.task_runtime.node_execute_runtime import NodeExecutionContext, _llm_messages, _react_messages
from app.task_runtime.planner import PlanNode
from app.task_runtime.runtime_context import (
    BranchRuntimeContext,
    NodeWorkspaceRuntimeContext,
    RepoRuntimeContext,
    TemporalRuntimeContext,
    TurnRuntimeContext,
    UsageRuntimeContext,
    WorkspaceRuntimeContext,
)


def test_temporal_runtime_context_trims_and_drops_empty_values() -> None:
    context = TemporalRuntimeContext.from_hints(
        {
            "current_date": " 2026-06-21 ",
            "current_time": "",
            "timezone": " Asia/Shanghai ",
        }
    )

    assert context.current_date == "2026-06-21"
    assert context.as_payload() == {"current_date": "2026-06-21", "timezone": "Asia/Shanghai"}


def test_turn_runtime_context_coerces_ids() -> None:
    context = TurnRuntimeContext.from_hints({"turn_id": "42", "conversation_id": 7})

    assert context.turn_id == 42
    assert context.conversation_id == 7
    assert TurnRuntimeContext.from_hints({"turn_id": "bad"}).turn_id is None


def test_branch_runtime_context_uses_active_branch_as_target_fallback() -> None:
    context = BranchRuntimeContext.from_hints(
        {
            "source_branch": " main ",
            "active_branch": " feat/refactor ",
            "node_branch": " jarvis-nodes/session/node ",
            "worktree_mode": " node_branch_worktree ",
        }
    )

    assert context.source_branch == "main"
    assert context.target_branch == "feat/refactor"
    assert context.node_branch == "jarvis-nodes/session/node"
    assert context.worktree_mode == "node_branch_worktree"


def test_branch_runtime_context_uses_git_branch_as_last_target_fallback() -> None:
    context = BranchRuntimeContext.from_hints({"git_branch": " feat/git-branch "})

    assert context.target_branch == "feat/git-branch"


def test_node_workspace_runtime_context_normalizes_repo_dir() -> None:
    context = NodeWorkspaceRuntimeContext.from_hints({"node_repos_dir": "runs/session/nodes/a/repo"})

    assert context.repos_dir == Path("runs/session/nodes/a/repo")


def test_workspace_runtime_context_normalizes_paths_and_manifest_default() -> None:
    context = WorkspaceRuntimeContext.from_hints(
        {
            "session_id": " session-1 ",
            "session_workspace_dir": "runs/session-1",
            "node_workspace_dir": "runs/session-1/nodes/a",
            "node_manifest_path": "runs/session-1/nodes/a/node_manifest.json",
        }
    )

    assert context.session_id == "session-1"
    assert context.session_root == Path("runs/session-1")
    assert context.node_workspace == Path("runs/session-1/nodes/a")
    assert context.manifest_path == Path("runs/session-1/nodes/a/node_manifest.json")
    assert context.manifest_name() == "runs/session-1/nodes/a/node_manifest.json"


def test_repo_runtime_context_normalizes_repo_and_provider_run_dir() -> None:
    context = RepoRuntimeContext.from_hints({"active_repo": " jarvis ", "provider_run_dir": "runs/provider"})

    assert context.active_repo == "jarvis"
    assert context.provider_run_dir == Path("runs/provider")


def test_usage_runtime_context_keeps_only_structured_git_context_usage() -> None:
    usage = {"provider": "planner", "stage": "git_context"}

    assert UsageRuntimeContext.from_hints({"git_context_usage": usage}).git_context_usage == usage
    assert UsageRuntimeContext.from_hints({"git_context_usage": "bad"}).git_context_usage is None


def test_node_execution_context_builds_runtime_context_from_legacy_hints() -> None:
    context = NodeExecutionContext(
        user_objective="review repo",
        node=PlanNode(id="review", runtime="coder", objective="Review"),
        legacy_hints={"active_repo": " jarvis ", "target_branch": " feat/runtime-context "},
    )

    assert context.runtime_context.repo.active_repo == "jarvis"
    assert context.runtime_context.branch.target_branch == "feat/runtime-context"
    assert context.runtime_context.to_legacy_hints()["active_repo"] == " jarvis "

    updated = replace(context, legacy_hints=context.runtime_context.with_hints({"active_repo": "smoke-test"}).to_legacy_hints())

    assert updated.runtime_context.repo.active_repo == "smoke-test"
    assert updated.legacy_hints["target_branch"] == " feat/runtime-context "
    assert not hasattr(updated, "runtime_hints")


def test_node_execute_prompt_payload_uses_runtime_context() -> None:
    context = NodeExecutionContext(
        user_objective="explain",
        node=PlanNode(id="answer", runtime="llm", objective="Explain"),
        legacy_hints={"active_repo": "jarvis"},
    )

    llm_payload = json.loads(_llm_messages(context)[1].content)
    react_payload = json.loads(_react_messages(replace(context, node=PlanNode(id="research", runtime="react", objective="Research")))[1].content)

    assert llm_payload["runtime_context"]["active_repo"] == "jarvis"
    assert react_payload["runtime_context"]["active_repo"] == "jarvis"
    assert "runtime_hints" not in llm_payload
    assert "runtime_hints" not in react_payload
