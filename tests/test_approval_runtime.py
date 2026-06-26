from __future__ import annotations

from types import SimpleNamespace

from app.task_runtime.approval_runtime import continue_approval


def test_continue_runtime_git_approval_merges_and_reports(monkeypatch) -> None:
    merges: list[tuple[str, str]] = []

    def fake_merge(workspace, *, node_commit):
        merges.append((workspace.target_branch, node_commit.short_hash))
        return SimpleNamespace(
            metadata=lambda: {"target_branch": workspace.target_branch, "short_hash": "abcdef123456"},
            node_branch=workspace.node_branch,
            target_branch=workspace.target_branch,
            merge_commit="abcdef1234567890",
        )

    monkeypatch.setattr("app.task_runtime.approval_runtime.merge_node_repo_to_target", fake_merge)
    payload = {
        "repo_workspace": {
            "repo_path": "E:/repo-node",
            "repo_id": "jarvis",
            "source_branch": "main",
            "target_branch": "main",
            "node_branch": "jarvis-nodes/jarvis/session/write",
            "base_commit": "base",
            "integration_path": "E:/repo",
        },
        "node_commit": {
            "commit_hash": "commit",
            "short_hash": "c0ffee",
            "subject": "Write",
            "files": ["node-output.txt"],
        },
    }

    result = continue_approval(
        source="runtime_git",
        approval_id="runtime_git_1",
        approved=True,
        timeout_seconds=30,
        payload=payload,
    )

    assert result.status == "completed"
    assert merges == [("main", "c0ffee")]
    assert "jarvis-nodes/jarvis/session/write -> main" in result.final_text
    assert result.metadata["source"] == "runtime_git"


def test_continue_approval_rejects_without_running_source() -> None:
    result = continue_approval(
        source="runtime_git",
        approval_id="runtime_git_1",
        approved=False,
        timeout_seconds=30,
        payload={},
    )

    assert result.status == "rejected"
    assert result.metadata["source"] == "runtime_git"


def test_continue_approval_rejects_unknown_source() -> None:
    result = continue_approval(
        source="unknown",
        approval_id="approval_1",
        approved=True,
        timeout_seconds=30,
        payload={},
    )

    assert result.status == "unsupported"
    assert "Unsupported approval source" in result.error
