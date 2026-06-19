from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.agent_react.artifacts import resolve_channel_attachments
from app.tools.common import ToolArtifact


def test_session_artifacts_are_deliverable_but_node_repo_files_are_not(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path
    session_root = workspace / "sessions" / "s1"
    artifact_path = session_root / "artifacts" / "diagram.png"
    node_repo_path = session_root / "nodes" / "n1" / "repo" / "jarvis" / "diagram.png"
    repo_path = workspace / "projects" / "jarvis" / "diagram.png"
    artifact_path.parent.mkdir(parents=True)
    node_repo_path.parent.mkdir(parents=True)
    repo_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"\x89PNG\r\n\x1a\nartifact")
    node_repo_path.write_bytes(b"\x89PNG\r\n\x1a\nnode-repo")
    repo_path.write_bytes(b"\x89PNG\r\n\x1a\nrepo")

    monkeypatch.setattr(
        "app.agent_react.artifacts.get_settings",
        lambda: SimpleNamespace(workspace_root=workspace, data_dir=workspace / "data"),
    )

    resolution = resolve_channel_attachments(
        [
            _image_artifact("session-artifact", artifact_path),
            _image_artifact("node-repo-artifact", node_repo_path),
            _image_artifact("repo-artifact", repo_path),
        ]
    )

    assert [item.artifact_id for item in resolution.attachments] == ["session-artifact"]
    assert [(item.artifact_id, item.reason) for item in resolution.rejected] == [
        ("node-repo-artifact", "path_outside_allowed_roots"),
        ("repo-artifact", "path_outside_allowed_roots"),
    ]


def _image_artifact(artifact_id: str, path: Path) -> ToolArtifact:
    return ToolArtifact(
        artifact_id=artifact_id,
        kind="image",
        path=str(path),
        mime_type="image/png",
        filename=path.name,
        size_bytes=path.stat().st_size,
        source_tool="test",
    )
