from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DraftResult:
    draft_id: str
    path: Path
    page_type: str
    title: str
    target_page: str
    source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApplyResult:
    status: str
    page_path: Path | None = None
    conflict_reason: str | None = None


@dataclass(frozen=True)
class QueryHit:
    path: Path
    title: str
    snippet: str
    layer: str


@dataclass(frozen=True)
class MaintainIssue:
    path: Path
    code: str
    message: str


@dataclass(frozen=True)
class MaintainResult:
    issues: list[MaintainIssue] = field(default_factory=list)
