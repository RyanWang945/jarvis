from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TemporalRuntimeContext:
    current_date: str = ""
    current_time: str = ""
    timezone: str = ""

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> TemporalRuntimeContext:
        return cls(
            current_date=_text(_hint(hints, "current_date")),
            current_time=_text(_hint(hints, "current_time")),
            timezone=_text(_hint(hints, "timezone")),
        )

    def as_payload(self) -> dict[str, str]:
        return {key: value for key, value in self.__dict__.items() if value}


@dataclass(frozen=True)
class BranchRuntimeContext:
    source_branch: str = ""
    target_branch: str = ""
    node_branch: str = ""
    worktree_mode: str = ""

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> BranchRuntimeContext:
        return cls(
            source_branch=_text(_hint(hints, "source_branch")),
            target_branch=(
                _text(_hint(hints, "target_branch"))
                or _text(_hint(hints, "active_branch"))
                or _text(_hint(hints, "git_branch"))
            ),
            node_branch=_text(_hint(hints, "node_branch")),
            worktree_mode=_text(_hint(hints, "worktree_mode")),
        )


@dataclass(frozen=True)
class NodeWorkspaceRuntimeContext:
    repos_dir: Path | None = None

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> NodeWorkspaceRuntimeContext:
        return cls(repos_dir=_optional_path(_hint(hints, "node_repos_dir")))


@dataclass(frozen=True)
class WorkspaceRuntimeContext:
    session_id: str = ""
    session_root: Path | None = None
    node_workspace: Path | None = None
    manifest_path: Path | None = None
    manifest_path_text: str = ""

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> WorkspaceRuntimeContext:
        manifest_path_text = _text(_hint(hints, "node_manifest_path"))
        return cls(
            session_id=_text(_hint(hints, "session_id")),
            session_root=_optional_path(_hint(hints, "session_workspace_dir")),
            node_workspace=_optional_path(_hint(hints, "node_workspace_dir")),
            manifest_path=_optional_path(manifest_path_text),
            manifest_path_text=manifest_path_text,
        )

    def manifest_name(self, default: str = "node_manifest.json") -> str:
        return self.manifest_path_text or default


@dataclass(frozen=True)
class RepoRuntimeContext:
    active_repo: str = ""
    provider_run_dir: Path | None = None

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> RepoRuntimeContext:
        return cls(
            active_repo=_text(_hint(hints, "active_repo")),
            provider_run_dir=_optional_path(_hint(hints, "provider_run_dir")),
        )


def _hint(hints: Mapping[str, Any] | None, key: str) -> Any:
    return hints.get(key) if hints is not None else None


def _text(value: Any) -> str:
    return str(value or "").strip() if value is not None else ""


def _optional_path(value: Any) -> Path | None:
    text = _text(value)
    return Path(text) if text else None
