from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import get_settings
from app.tools.common import ToolArtifact, ToolExecutionRequest, ToolExecutionResult


def run_write_file(request: ToolExecutionRequest) -> ToolExecutionResult:
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()

    relative_path = str(request.args.get("relative_path") or "").strip()
    content = str(request.args.get("content") or "")

    if not relative_path:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="Missing required argument: relative_path",
            summary="File path is required.",
        )
    if not content.strip():
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="Missing required argument: content",
            summary="File content is required.",
        )

    target = Path(relative_path)
    if target.is_absolute():
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="relative_path must stay inside the Jarvis workspace.",
            summary="Absolute paths are not allowed for write_file.",
        )

    full_path = (workspace_root / target).resolve()
    if not _is_within_workspace(full_path, workspace_root):
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="Target path escapes the Jarvis workspace.",
            summary="write_file path must stay inside the Jarvis workspace.",
        )

    if full_path.suffix.lower() != ".md":
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="write_file currently only supports .md files.",
            summary="write_file path must end with .md.",
        )

    parent = full_path.parent
    if not parent.exists():
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=f"Target directory does not exist: {parent}",
            summary="Target directory does not exist. Ask the user to confirm or create it first.",
        )

    full_path.write_text(content, encoding="utf-8")
    size_bytes = full_path.stat().st_size
    artifact_id = f"file:{hashlib.sha256(str(full_path).encode('utf-8')).hexdigest()[:16]}"
    return ToolExecutionResult(
        ok=True,
        exit_code=0,
        stdout=str(full_path),
        artifacts=[str(full_path)],
        tool_artifacts=[
            ToolArtifact(
                artifact_id=artifact_id,
                kind="file",
                path=str(full_path),
                mime_type="text/markdown",
                filename=full_path.name,
                size_bytes=size_bytes,
                source_tool="write_file",
                metadata={"relative_path": relative_path},
            )
        ],
        summary=f"File written to {full_path}",
    )


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.relative_to(workspace_root)
        return True
    except ValueError:
        return False
