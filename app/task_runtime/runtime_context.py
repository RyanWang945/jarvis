from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TurnRuntimeContext:
    turn_id: int | None = None
    conversation_id: int | None = None

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> TurnRuntimeContext:
        return cls(
            turn_id=_optional_int(_hint(hints, "turn_id")),
            conversation_id=_optional_int(_hint(hints, "conversation_id")),
        )


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
        return cls(repos_dir=_optional_path(_hint(hints, "node_repo_dir") or _hint(hints, "node_repos_dir")))


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


@dataclass(frozen=True)
class RuntimeContext:
    legacy_hints: dict[str, Any]
    turn: TurnRuntimeContext
    temporal: TemporalRuntimeContext
    branch: BranchRuntimeContext
    node_workspace: NodeWorkspaceRuntimeContext
    workspace: WorkspaceRuntimeContext
    repo: RepoRuntimeContext

    @classmethod
    def from_hints(cls, hints: Mapping[str, Any] | None) -> RuntimeContext:
        legacy_hints = dict(hints or {})
        return cls(
            legacy_hints=legacy_hints,
            turn=TurnRuntimeContext.from_hints(legacy_hints),
            temporal=TemporalRuntimeContext.from_hints(legacy_hints),
            branch=BranchRuntimeContext.from_hints(legacy_hints),
            node_workspace=NodeWorkspaceRuntimeContext.from_hints(legacy_hints),
            workspace=WorkspaceRuntimeContext.from_hints(legacy_hints),
            repo=RepoRuntimeContext.from_hints(legacy_hints),
        )

    def to_legacy_hints(self) -> dict[str, Any]:
        return dict(self.legacy_hints)

    def with_hints(self, updates: Mapping[str, Any]) -> RuntimeContext:
        return RuntimeContext.from_hints({**self.legacy_hints, **dict(updates)})


def _hint(hints: Mapping[str, Any] | None, key: str) -> Any:
    return hints.get(key) if hints is not None else None


def _text(value: Any) -> str:
    return str(value or "").strip() if value is not None else ""


def _optional_path(value: Any) -> Path | None:
    text = _text(value)
    return Path(text) if text else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def truncate(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
