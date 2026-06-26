from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: Literal["image", "file", "directory", "log", "git_ref"]
    turn_id: int | None = None
    tool_call_id: str | None = None
    path: str | None = None
    session_relative_path: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    source_tool: str = ""
    node_id: str | None = None
    publish: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


ToolArtifact = ArtifactRecord


@dataclass(frozen=True)
class ToolExecutionRequest:
    tool_name: str
    workdir: str | None
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30


@dataclass(frozen=True)
class ToolExecutionResult:
    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    tool_artifacts: list[ToolArtifact] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
