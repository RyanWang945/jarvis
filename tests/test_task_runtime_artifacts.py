from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.task_runtime.artifacts.publisher import ArtifactPublisher
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeResult
from app.task_runtime.session_workspace import NodeWorkspaceRef, SessionWorkspaceRef
from app.tools.common import ToolArtifact


def _session_workspace() -> SessionWorkspaceRef:
    root = Path("/tmp/jarvis-test-session")
    node = _node_workspace(root, "review")
    return SessionWorkspaceRef(
        session_id="test-session",
        root_path=root,
        session_path=root / "session.json",
        dag_path=root / "dag.json",
        summary_path=root / "summary.md",
        artifacts_dir=root / "artifacts",
        approvals_dir=root / "approvals",
        nodes_dir=root / "nodes",
        nodes={"review": node},
    )


def _node_workspace(root: Path, node_id: str) -> NodeWorkspaceRef:
    return NodeWorkspaceRef(**_node_workspace_kwargs(root, node_id))


def _node_workspace_kwargs(root: Path, node_id: str) -> dict[str, Any]:
    node_root = root / "nodes" / node_id
    return {
        "node_id": node_id,
        "safe_node_id": node_id,
        "root_path": node_root,
        "task_path": node_root / "TASK.md",
        "progress_path": node_root / "PROGRESS.md",
        "result_markdown_path": node_root / "RESULT.md",
        "state_path": node_root / "state.json",
        "artifacts_dir": node_root / "artifacts",
        "input_snapshot_path": node_root / "input_snapshot.md",
        "output_path": node_root / "RESULT.md",
        "result_path": node_root / "result.json",
        "manifest_path": node_root / "node_manifest.json",
        "provider_run_dir": node_root / "provider_run",
        "repo_path": node_root / "repo",
    }


class _FakeStore:
    def __init__(self) -> None:
        self.upserted: list[tuple[Any, int]] = []

    def upsert_artifact(self, artifact: Any, *, conversation_id: int) -> None:
        self.upserted.append((artifact, conversation_id))


class _NoUpsertStore:
    """Store without upsert_artifact — should be handled gracefully."""


# ── collect ─────────────────────────────────────────────────────────


def test_collect_node_artifact_without_path_produces_git_ref() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="completed",
                summary="done",
                artifacts=[
                    NodeArtifact(
                        ref="commit_abc",
                        kind="git_ref",
                        name="abc123",
                        publish=True,
                    )
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    assert len(records) == 1
    assert records[0].kind == "git_ref"
    assert records[0].artifact_id is not None


def test_collect_skips_unpublished_node_artifact() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="completed",
                summary="done",
                artifacts=[
                    NodeArtifact(ref="hidden", kind="git_ref", name="hidden", publish=False),
                    NodeArtifact(ref="visible", kind="git_ref", name="visible", publish=True),
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    assert len(records) == 1
    assert records[0].artifact_id is not None


def test_collect_skips_file_artifact_with_invalid_session_relative_path() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="review",
                runtime="coder",
                status="completed",
                summary="done",
                artifacts=[
                    NodeArtifact(
                        ref="missing_file",
                        kind="file",
                        name="report.md",
                        path="nonexistent/path.md",
                        session_relative_path="nonexistent/path.md",
                        publish=True,
                    ),
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    assert len(records) == 0


def test_collect_tool_artifacts_from_node_result() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="research",
                runtime="react",
                status="completed",
                summary="done",
                tool_artifacts=[
                    {
                        "artifact_id": "art_001",
                        "kind": "git_ref",
                        "turn_id": 1,
                        "tool_call_id": "tc_1",
                        "path": None,
                        "session_relative_path": None,
                        "mime_type": None,
                        "filename": None,
                        "size_bytes": None,
                        "source_tool": "search",
                        "node_id": "research",
                        "publish": True,
                        "metadata": {},
                    }
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    assert len(records) == 1
    assert records[0].artifact_id == "art_001"


def test_collect_dedup_by_artifact_id() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    shared_artifact = NodeArtifact(ref="dup", kind="git_ref", name="dup", publish=True)
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="same_node",
                runtime="coder",
                status="completed",
                summary="done",
                artifacts=[
                    shared_artifact,
                    shared_artifact,  # same artifact repeated in the same node
                ],
            ),
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    # Same node_id + same ref => same stable id => deduplicated
    assert len(records) == 1


def test_collect_nested_tool_call_artifacts() -> None:
    """Artifacts nested inside tool_calls — not lifted to result.tool_artifacts."""
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="generate",
                runtime="react",
                status="completed",
                summary="generated image",
                # Not in result.tool_artifacts — only inside tool_calls
                tool_calls=[
                    {
                        "id": "call_1",
                        "tool_name": "write_image",
                        "status": "completed",
                        "tool_artifacts": [
                            {
                                "artifact_id": "nested_art_001",
                                "kind": "image",
                                "path": None,
                                "mime_type": "image/png",
                                "filename": "output.png",
                                "size_bytes": 1234,
                                "source_tool": "write_image",
                            }
                        ],
                    }
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=1)

    assert len(records) == 1
    assert records[0].artifact_id == "nested_art_001"


def test_collect_normalizes_tool_artifact_turn_and_node() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())
    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="react_node",
                runtime="react",
                status="completed",
                summary="done",
                tool_artifacts=[
                    {
                        "artifact_id": "ta_no_turn",
                        "kind": "git_ref",
                        "turn_id": None,
                        "tool_call_id": "",
                        "path": None,
                        "session_relative_path": None,
                        "mime_type": None,
                        "filename": None,
                        "size_bytes": None,
                        "source_tool": "",
                        "node_id": None,
                        "publish": True,
                        "metadata": {},
                    }
                ],
            )
        ],
    )

    records = publisher._collect_from_report(report, turn_id=42)

    assert len(records) == 1
    assert records[0].turn_id == 42
    assert records[0].tool_call_id == "node:react_node"
    assert records[0].node_id == "react_node"
    assert records[0].source_tool == "react"


# ── promote ─────────────────────────────────────────────────────────


def test_promote_skips_non_publishable_artifact() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())

    artifact = ToolArtifact(
        artifact_id="a1",
        kind="git_ref",
        publish=False,
    )
    result = publisher._promote_one(artifact)

    assert result is artifact


def test_promote_copies_file_to_session_dir(tmp_path: Path) -> None:
    # Setup a session workspace rooted at tmp_path
    root = tmp_path / "session"
    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True)

    node = NodeWorkspaceRef(
        **_node_workspace_kwargs(root, "coder"),
    )
    workspace = SessionWorkspaceRef(
        session_id="promote-test",
        root_path=root,
        session_path=root / "session.json",
        dag_path=root / "dag.json",
        summary_path=root / "summary.md",
        artifacts_dir=artifacts_dir,
        approvals_dir=root / "approvals",
        nodes_dir=root / "nodes",
        nodes={"coder": node},
    )

    # Create a file outside the session
    source_file = tmp_path / "outside.txt"
    source_file.write_text("hello promote", encoding="utf-8")

    artifact = ToolArtifact(
        artifact_id="promo_1",
        kind="file",
        path=str(source_file),
        filename="outside.txt",
        publish=True,
    )

    publisher = ArtifactPublisher(_NoUpsertStore(), workspace)
    result = publisher._promote_one(artifact)

    assert result.path != str(source_file)  # was copied
    assert result.session_relative_path is not None
    assert result.size_bytes == len("hello promote")
    assert result.metadata.get("promoted_to_session_artifacts") is True
    # Verify the file exists at the new path
    assert Path(result.path).is_file()
    assert Path(result.path).read_text(encoding="utf-8") == "hello promote"


# ── persist ─────────────────────────────────────────────────────────


def test_persist_calls_upsert_artifact() -> None:
    store = _FakeStore()
    publisher = ArtifactPublisher(store, _session_workspace())

    artifacts = [
        ToolArtifact(artifact_id="a1", kind="git_ref"),
        ToolArtifact(artifact_id="a2", kind="git_ref"),
    ]
    publisher._persist_to_store(artifacts, conversation_id=99)

    assert len(store.upserted) == 2
    assert store.upserted[0] == (artifacts[0], 99)
    assert store.upserted[1] == (artifacts[1], 99)


def test_persist_graceful_when_store_lacks_upsert() -> None:
    publisher = ArtifactPublisher(_NoUpsertStore(), _session_workspace())

    # Should not raise
    publisher._persist_to_store(
        [ToolArtifact(artifact_id="a1", kind="git_ref")],
        conversation_id=1,
    )


# ── publish (integration) ───────────────────────────────────────────


def test_publish_returns_promoted_records_and_persists(tmp_path: Path) -> None:
    root = tmp_path / "session"
    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True)

    node = NodeWorkspaceRef(
        **_node_workspace_kwargs(root, "n1"),
    )
    workspace = SessionWorkspaceRef(
        session_id="integ",
        root_path=root,
        session_path=root / "session.json",
        dag_path=root / "dag.json",
        summary_path=root / "summary.md",
        artifacts_dir=artifacts_dir,
        approvals_dir=root / "approvals",
        nodes_dir=root / "nodes",
        nodes={"n1": node},
    )

    store = _FakeStore()
    publisher = ArtifactPublisher(store, workspace)

    report = ExecutionReport(
        status="completed",
        node_results=[
            NodeResult(
                node_id="n1",
                runtime="coder",
                status="completed",
                summary="done",
                artifacts=[
                    NodeArtifact(
                        ref="result_ref",
                        kind="git_ref",
                        name="result",
                        publish=True,
                    )
                ],
            )
        ],
    )

    records = publisher.publish(report, turn_id=1, conversation_id=10)

    assert len(records) >= 1
    assert len(store.upserted) >= 1


def test_publish_empty_report() -> None:
    store = _FakeStore()
    publisher = ArtifactPublisher(store, _session_workspace())

    report = ExecutionReport(status="completed", node_results=[])

    records = publisher.publish(report, turn_id=1, conversation_id=5)

    assert records == []
    assert store.upserted == []
