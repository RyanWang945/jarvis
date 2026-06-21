from __future__ import annotations

from pathlib import Path

from app.task_runtime.runtime_context import (
    BranchRuntimeContext,
    NodeWorkspaceRuntimeContext,
    RepoRuntimeContext,
    TemporalRuntimeContext,
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
